"""Agent-driven browser: Playwright Chromium with a persistent profile.

Text-first control surface for non-vision models: every action returns a
fresh snapshot (url, title, indexed interactive elements, visible text).
Each agent gets its own tab; sessions/cookies persist in browser-profile/
so the owner can log into a site once and agents reuse it.
"""
import asyncio
import base64
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import db
import settings as cfg
import browser_auth
import credentials
import secret_mask
from harness import ToolResult

logger = logging.getLogger("missioncontrol.browser")

from apphome import ROOT

PROFILE_DIR = ROOT / "browser-profile"
ACTION_TIMEOUT = 30_000  # ms

_pw = None
_context = None
_pages: dict[str, object] = {}
_tabs: dict[object, dict] = {}
_action_locks: dict[str, asyncio.Lock] = {}
_captures: dict[object, dict] = {}
_lock = asyncio.Lock()

SENSITIVE_JS = r"""el => {
  const autocomplete = (el.autocomplete || '').toLowerCase().split(/\s+/);
  const identity = [el.name, el.id, el.placeholder, el.getAttribute('aria-label')]
    .filter(Boolean).join(' ').toLowerCase();
  return el.hasAttribute('data-mc-private') || el.type === 'password' || autocomplete.some(t =>
    ['current-password', 'new-password', 'one-time-code'].includes(t) || t.startsWith('cc-')) ||
    /password|passcode|\botp\b|\b2fa\b|\bmfa\b|card.?number|\bcvv\b|\bcvc\b|security.?code/.test(identity);
}"""

SNAPSHOT_JS = r"""
() => {
  const sensitive = SENSITIVE_CHECK;
  const sels = 'a,button,input,select,textarea,summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="combobox"],[onclick],[contenteditable="true"],[tabindex]:not([tabindex="-1"]),[aria-haspopup],[aria-expanded],[data-toggle],[data-bs-toggle],[data-kt-menu-trigger]';
  const state = window.__mcRefs ||= {next: 1, refs: new WeakMap()};
  // Reusing numbered positions after opening a menu could click an unrelated
  // link. IDs stay bound to their element even when the DOM order changes.
  document.querySelectorAll('[data-oref]').forEach(el => el.removeAttribute('data-oref'));
  const candidates = [...document.querySelectorAll(sels)];
  const known = new Set(candidates);
  for (const el of document.querySelectorAll('body *')) {
    if (known.has(el) || getComputedStyle(el).cursor !== 'pointer') continue;
    if (el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer') continue;
    if (el.closest(sels)) continue;
    candidates.push(el);
  }
  const rows = [];
  for (const el of candidates) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility !== 'visible' || style.display === 'none' || style.opacity === '0' || el.closest('[inert],[aria-hidden="true"]')) continue;
    const visible = r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
    rows.push({el, r, visible});
  }
  // Keep menus/header controls in the bounded result on long table pages.
  rows.sort((a,b) => Number(b.visible) - Number(a.visible) || a.r.top - b.r.top || a.r.left - b.r.left);
  const out = [];
  for (const {el, r, visible} of rows.slice(0, 150)) {
    if (!state.refs.has(el)) state.refs.set(el, state.next++);
    const ref = state.refs.get(el); el.setAttribute('data-oref', ref);
    const privateField = sensitive(el);
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
    const label = (privateField ? '[private field — use secure login/input]' : (el.getAttribute('aria-label') || labelled.trim() || el.innerText || el.value || el.placeholder ||
                   el.getAttribute('title') || el.querySelector('img[alt]')?.alt || '')
                  .trim().replace(/\s+/g, ' ').slice(0, 110));
    let entry = `[${ref}] <${el.tagName.toLowerCase()}${el.type ? ' ' + el.type : ''}> ${label}`;
    const x = Math.round(r.x + r.width / 2), y = Math.round(r.y + r.height / 2);
    const horizontal = x < innerWidth / 3 ? 'left' : x > innerWidth * 2 / 3 ? 'right' : 'center';
    const vertical = y < innerHeight / 3 ? 'top' : y > innerHeight * 2 / 3 ? 'bottom' : 'middle';
    entry += visible ? ` @ ${vertical}-${horizontal} (${x},${y})` : ' [outside viewport]';
    if (el.matches('[aria-haspopup],[aria-expanded],[data-toggle],[data-bs-toggle],[data-kt-menu-trigger]')) entry += ' [menu/toggle]';
    if (el.hasAttribute('aria-expanded')) entry += ` expanded=${el.getAttribute('aria-expanded')}`;
    if (el.disabled) entry += ' disabled';
    if (el.tagName === 'A' && el.href) {
      try { const url = new URL(el.href); entry += ` -> ${url.origin}${url.pathname}`.slice(0, 160); } catch (_) {}
    }
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].slice(0, 12).map(o => `${o.value}=${o.label}`).join('|');
      entry += ` options: ${opts.slice(0, 150)}`;
    }
    out.push(entry);
  }
  if (rows.length > 150) out.push(`(${rows.length - 150} more controls outside this bounded view; scroll to inspect them.)`);
  return out;
}
""".replace('SENSITIVE_CHECK', SENSITIVE_JS)


def headless() -> bool:
    s = cfg.load()
    return bool(s.get("browser", {}).get("headless", False))


def website_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Enter a website URL.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an http or https website URL without embedded credentials.")
    # Validate malformed ports before opening a page.
    parsed.port
    return value


def _display_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return "New tab"
    # Login callbacks can contain credentials in the query or fragment.
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"[:400]


def _remember_page(page, aid="owner"):
    if page not in _tabs:
        _tabs[page] = {"id": uuid.uuid4().hex[:12], "agent": aid}
        def closed(*_):
            browser_auth.cancel_page(page)
            _captures.pop(page, None)
            _tabs.pop(page, None)
            for agent, current in list(_pages.items()):
                if current is page:
                    _pages.pop(agent, None)
        def popup(child):
            agent = _tabs.get(page, {}).get("agent", "owner")
            _remember_page(child, agent)
            _pages[agent] = child
        page.on("close", closed)
        page.on("framenavigated", lambda frame: browser_auth.cancel_page(page, navigation=True) if frame == page.main_frame else None)
        page.on("popup", popup)
    else:
        _tabs[page]["agent"] = aid


async def release(aid: str):
    """A finished worker's tab goes with it; the owner's tabs are never touched."""
    page = _pages.pop(aid, None)
    if page is not None and not page.is_closed():
        await page.close()


async def snapshot() -> dict:
    import runtime
    service = runtime.registry.services.get("browser")
    rows = []
    for page, meta in list(_tabs.items()):
        if page.is_closed():
            continue
        try:
            title = await asyncio.wait_for(page.title(), 1)
        except Exception:
            title = "Loading…"
        rows.append({**meta, "url": secret_mask.redact(_display_url(page.url)), "title": secret_mask.redact(title[:180]) or "New tab"})
    return {"enabled": runtime.registry.enabled("browser"),
            "status": service.state if service else "running", "headless": headless(),
            "running": _context is not None, "sessions": rows}


async def _ensure_page(aid: str):
    import runtime
    service = runtime.registry.services.get("browser")
    if not runtime.registry.enabled("browser") or (service and service.state != "running"):
        raise RuntimeError("Agent browser is disabled in Runtime Manager.")
    global _pw, _context
    async with _lock:
        if _context is None:
            from playwright.async_api import async_playwright
            try:
                if _pw is None:
                    _pw = await async_playwright().start()
                _context = await _pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR), headless=headless(),
                    executable_path=os.environ.get("MISSIONCONTROL_BROWSER_EXECUTABLE") or None,
                    viewport={"width": 1440, "height": 900},
                    env={**os.environ, "MISSIONCONTROL_SERVICE": "browser"},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = _context
                def closed(*_):
                    global _context
                    if _context is context:
                        _context = None
                        _pages.clear()
                        _tabs.clear()
                context.on("close", closed)
                context.on("page", _remember_page)
                for existing in context.pages:
                    _remember_page(existing)
                if context.pages:
                    _pages["owner"] = context.pages[0]
            except BaseException:
                await _reset_locked()
                raise
            logger.info("browser context started (headless=%s)", headless())
        page = _pages.get(aid)
        if page is None or page.is_closed():
            owner = _pages.get('owner')
            if aid != 'owner' and owner is not None and not owner.is_closed():
                page = _pages.pop('owner')
            else:
                page = await _context.new_page()
            _remember_page(page, aid)
            _pages[aid] = page
        return page


async def _reset():
    async with _lock:
        await _reset_locked()


async def _reset_locked():
    global _pw, _context
    context, driver = _context, _pw
    browser_auth.cancel_page()
    _pw, _context = None, None
    _pages.clear()
    _tabs.clear()
    _captures.clear()
    try:
        if context:
            await context.close()
    except Exception:
        pass
    finally:
        if driver:
            await driver.stop()


async def open_owner(url: str = ""):
    target = website_url(url) if url.strip() else None
    if headless():
        raise ValueError("Turn on Show browser windows before signing in.")
    page = await _ensure_page("owner")
    if target:
        await page.goto(target, timeout=ACTION_TIMEOUT, wait_until="domcontentloaded")
    await page.bring_to_front()


async def session_action(session: str, action: str):
    page = next((page for page, meta in _tabs.items() if meta["id"] == session), None)
    if page is None or page.is_closed():
        raise ValueError("This tab has closed. Refresh the browser sessions.")
    if action == "focus":
        if headless():
            raise ValueError("Turn on Show browser windows to see this tab.")
        await page.bring_to_front()
    elif action == "close":
        await page.close()
    else:
        raise ValueError("Use focus or close.")


def _playbook_hint(url: str) -> str:
    domain = urlparse(url).hostname or ""
    books = db.find_playbooks(domain)
    if not books:
        return ""
    parts = [f"--- SAVED PLAYBOOK \"{b['title']}\" (by {b['author']}) ---\n{b['content']}"
             for b in books[:3]]
    return ("\n\nSAVED PROCEDURES (may be wrong or stale; the owner's corrections and live page evidence take precedence):\n"
            + "\n".join(parts)[:3500])


async def _snapshot(page, full_text: bool = False) -> str:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    try:
        elements = await page.evaluate(SNAPSHOT_JS)
    except Exception as exc:
        elements = [f"(element scan failed: {exc})"]
    try:
        text = await page.evaluate("""() => {
          const sensitive = SENSITIVE_CHECK;
          let text = document.body ? document.body.innerText : '';
          for (const el of document.querySelectorAll('input,textarea,[data-mc-private]')) {
            if (!sensitive(el)) continue;
            const value = el.value || el.innerText;
            if (value) text = text.split(value).join('[private input]');
          }
          return text;
        }""".replace('SENSITIVE_CHECK', SENSITIVE_JS))
    except Exception:
        text = ""
    cap = 12000 if full_text else 3500
    text = " ".join(text.split())[:cap]
    el_block = "\n".join(elements[:150])
    accounts = []
    try:
        accounts = await asyncio.to_thread(credentials.list_entries, page.url)
    except credentials.VaultError:
        pass
    logins = "\nSAVED LOGINS (use login; secret values are never returned):\n" + "\n".join(
        f"{row['id']}: {row['label']}" for row in accounts) if accounts else ""
    return secret_mask.redact(f"URL: {_display_url(page.url)}\nTITLE: {await page.title()}\n"
            f"INTERACTIVE ELEMENTS (click/type by [ref]):\n{el_block}\n"
            f"PAGE TEXT:\n{text}{logins}")


async def current_observation(aid: str) -> str:
    """Observe a session's own tab at the start of a turn without navigation/launch. Never
    the owner's: what they have open is not any Worker's to read unless it goes and looks."""
    page = _pages.get(aid)
    if page is None or page.is_closed():
        return ''
    async with _action_locks.setdefault(aid, asyncio.Lock()):
        return await asyncio.wait_for(_snapshot(page), 15) + _playbook_hint(page.url)


async def _screenshot(page) -> ToolResult:
    state = await _snapshot(page)
    viewport = await page.evaluate('() => ({width: innerWidth, height: innerHeight, scrollX, scrollY})')
    # Mask fields, private login data and text echoes of recently filled secrets.
    # Images stay ephemeral in model context; only a local owner-viewable PNG
    # artifact and its path persist, never base64 in chat/audit/memory.
    text_nodes = await page.evaluate('''() => {
      const out = []; let id = 0;
      for (const el of document.querySelectorAll('body *')) {
        if (['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) continue;
        const text = [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('');
        const r = el.getBoundingClientRect();
        if (!text.trim() || !r.width || !r.height || r.bottom < 0 || r.top > innerHeight) continue;
        el.setAttribute('data-mc-capture-text', ++id); out.push({id, text});
      }
      return out;
    }''')
    masks = ['input', 'textarea', 'select', '[contenteditable="true"]', '[data-mc-private]', 'iframe']
    masks += [f'[data-mc-capture-text="{row["id"]}"]' for row in text_nodes if secret_mask.redact(row['text']) != row['text']]
    try:
        png = await page.screenshot(type='png', full_page=False, scale='css',
                                   mask=[page.locator(','.join(masks))], timeout=15000)
    finally:
        await page.evaluate("() => document.querySelectorAll('[data-mc-capture-text]').forEach(el => el.removeAttribute('data-mc-capture-text'))")
    import uploads
    path = await asyncio.to_thread(uploads.save, png, 'browser.png')
    path.chmod(0o600)
    _captures[page] = {**viewport, 'url': page.url, 'ts': time.monotonic()}
    text = (f"BROWSER SCREENSHOT: current website viewport {viewport['width']}x{viewport['height']}. "
            f"Image attached for visual inspection. Form values/private inputs masked.\n"
            f"Local image: {path}\n{state}")
    return ToolResult(text, content=[{'type': 'text', 'text': text},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                    'data': base64.b64encode(png).decode()}}])


def page_desc(aid: str) -> str:
    page = _pages.get(aid)
    try:
        if page and not page.is_closed():
            return urlparse(page.url).hostname or "new tab"
    except Exception:
        pass
    return "new tab"


async def act(aid: str, action: str, args: dict) -> str:
    """Execute one browser action for an agent, return the resulting snapshot."""
    try:
        async with _action_locks.setdefault(aid, asyncio.Lock()):
            timeout = 360 if action in ('login', 'request_input') else 90
            return await asyncio.wait_for(_act(aid, action, args), timeout=timeout)
    except asyncio.TimeoutError:
        return "Error: browser action timed out; read the page before retrying."
    except (browser_auth.AuthError, credentials.VaultError) as exc:
        return f"Blocked: {exc}"
    except Exception as exc:
        msg = str(exc)
        if "Target closed" in msg or "has been closed" in msg or "browser has disconnected" in msg.lower():
            return "Error: browser closed; reopen it and read the page before retrying."
        # Playwright call logs can include raw fill values; never persist those.
        return f"Error: browser {type(exc).__name__}; inspect the page or reopen the browser."


async def _act(aid: str, action: str, args: dict) -> str:
    if action == 'tabs':
        return 'BROWSER TABS (use_tab with tab_id to continue an existing tab):\n' + '\n'.join(
            f"{row['id']} | {row['agent']} | {row['title']} | {row['url']}" for row in (await snapshot())['sessions'])
    if action == 'use_tab':
        target = next((p for p, m in _tabs.items() if m['id'] == args.get('tab_id') and not p.is_closed()), None)
        if target is None:
            return 'Error: tab not found; use tabs to see the current browser tabs.'
        owner = _tabs[target]['agent']
        if owner not in ('owner', aid):
            return f'Blocked: this tab is in use by {owner}; use your own tab.'
        _pages.pop(owner, None)
        _pages[aid] = target
        _remember_page(target, aid)
        return await _snapshot(target) + _playbook_hint(target.url)
    page = await _ensure_page(aid)
    if action in ('screenshot', 'screencapture'):
        return await _screenshot(page)
    capture = _captures.pop(page, None)
    if action == 'login':
        return await browser_auth.login(aid, page, args)
    if action == 'request_input':
        return await browser_auth.request_input(aid, page, args)
    ref = args.get("ref")
    sel = f'[data-oref="{ref}"]' if ref is not None else None
    if action in ('type', 'select') and sel is None:
        return 'Error: this action requires a current element ref; read the page first.'
    if sel and action in ('click', 'type', 'select') and await page.locator(sel).count() != 1:
        return 'Error: this element ref is stale or ambiguous; read the current page before acting.'
    if action in ("type", "select") and await page.locator(sel).evaluate(SENSITIVE_JS, timeout=ACTION_TIMEOUT):
        return "Blocked: use login for saved credentials or request_input for 2FA/private login details. Payment fields need owner interaction."

    if action == "goto":
        url = website_url(args.get("url", ""))
        await page.goto(url, timeout=ACTION_TIMEOUT, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)
        return await _snapshot(page) + _playbook_hint(page.url)
    if action == "click":
        if sel:
            await page.click(sel, timeout=ACTION_TIMEOUT)
        else:
            x, y = args.get('x'), args.get('y')
            if not capture or capture['url'] != page.url or time.monotonic() - capture['ts'] > 120:
                return 'Error: coordinate clicks require a fresh browser screenshot; prefer an element ref.'
            viewport = await page.evaluate('() => ({width: innerWidth, height: innerHeight, scrollX, scrollY})')
            if any(capture[key] != value for key, value in viewport.items()):
                return 'Error: the viewport changed; take a new browser screenshot.'
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not (0 <= x < viewport['width'] and 0 <= y < viewport['height']):
                return 'Error: click coordinates must be inside the captured viewport.'
            await page.mouse.click(x, y)
        await asyncio.sleep(1.2)
        page = _pages.get(aid, page)  # Follow a website's newly opened tab.
        return await _snapshot(page)
    if action == "type":
        await page.fill(sel, args.get("text", ""), timeout=ACTION_TIMEOUT)
        if args.get("press_enter"):
            await page.press(sel, "Enter")
            await asyncio.sleep(1.2)
        return await _snapshot(page)
    if action == "select":
        await page.select_option(sel, args.get("text", ""), timeout=ACTION_TIMEOUT)
        return await _snapshot(page)
    if action == "scroll":
        delta = -700 if args.get("text") == "up" else 700
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(0.5)
        return await _snapshot(page)
    if action == "back":
        await page.go_back(timeout=ACTION_TIMEOUT)
        return await _snapshot(page)
    if action == "wait":
        await asyncio.sleep(min(float(args.get("text") or 3), 10))
        return await _snapshot(page)
    if action == "read":
        return await _snapshot(page, full_text=True) + _playbook_hint(page.url)
    return f"unknown action '{action}'. Valid: goto, click, type, select, scroll, back, wait, read, screenshot, tabs, use_tab, login, request_input"
