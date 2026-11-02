"""Apple's own apps as a tool: Notes, Calendar, Reminders, Messages, Mail.

These are the owner's real, personal apps on their real machine, so the
surface is deliberately lopsided: agents can read widely and write
narrowly.

- Notes, Calendar, Reminders: read, and create. A new note, event or
  reminder is easy for the owner to see and delete, and ticking a
  reminder off is trivially undone.
- Mail: read, and draft. Never send. A sent mail cannot be recalled, and
  the team already has its own address through the email channel for
  correspondence it owns. The one move is the owner's mail sweep
  (mailsweep.py), which archives bulk mail by rules the owner set; agents
  cannot move or delete mail themselves.
- Messages: read only. Texting already goes through the iMessage channel,
  which routes to one agent the owner chose and mirrors back to them;
  a second, unmirrored way to text people is not an improvement.

Everything here needs macOS to have granted this process access:
Automation for each app (System Settings > Privacy & Security >
Automation) and Full Disk Access for Messages history, which lives in a
SQLite file macOS protects. Running under launchd, those grants attach to
the launchd job rather than a terminal, so `status()` reports what is
actually working instead of guessing.
"""
import asyncio
import processes
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import settings as cfg

MAC = sys.platform == "darwin"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
TIMEOUT = 45
MAX_ROWS = 50

# Apple's epoch for Messages is 2001-01-01, in nanoseconds since Sierra.
APPLE_EPOCH = 978307200

# Each app is its own decision. Someone may happily let the team read their
# calendar and never their mail, so these are five switches rather than one,
# and every one of them starts off: this is the owner's personal life, not the
# app's data.
APPS = ("notes", "calendar", "reminders", "mail", "messages")


class AppleError(RuntimeError):
    pass


def enabled(app: str) -> bool:
    return bool((cfg.load().get("apple") or {}).get(app, False))


def require(app: str):
    if app not in APPS:
        raise AppleError(f"unknown app '{app}'; one of {', '.join(APPS)}")
    if not enabled(app):
        raise AppleError(f"{app} is not connected. The owner turns it on in "
                         f"Settings > Apple apps, one app at a time.")


async def osa(script: str, lang: str = "AppleScript", timeout: int = TIMEOUT) -> str:
    """Run a script through osascript, passed on stdin so nothing has to be
    escaped through a shell."""
    if not MAC:
        raise AppleError("the Apple apps are macOS only")
    proc = await processes.create_subprocess_exec(
        "osascript", "-l", lang, "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AppleError(f"{lang} call timed out after {timeout}s; Mail and Notes "
                         f"can be slow on a big library, try a narrower query")
    text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        msg = err.decode("utf-8", errors="replace").strip()
        if "-1743" in msg or "Not authorized" in msg:
            raise AppleError(
                "macOS has not granted Mission Control permission to control that app. "
                "Open System Settings > Privacy & Security > Automation and allow it, "
                "then try again.")
        raise AppleError(msg[:400] or f"osascript exited {proc.returncode}")
    return text


def _rows(text: str) -> list[dict]:
    """Scripts return one JSON object per line, so a single unparseable record
    cannot lose the whole result."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------- Notes ----------

async def notes_search(query: str, limit: int = 20) -> list[dict]:
    """Same bulk-property trick as Reminders. Bodies are only pulled for the
    notes whose title already matched, because plaintext() on a whole library
    is the slow part."""
    script = f'''
    const app = Application("Notes");
    const q = {json.dumps((query or "").lower())};
    const cap = {min(limit, MAX_ROWS)};
    const names = app.notes.name();
    const mods = app.notes.modificationDate();
    const out = [];
    const byTitle = [];
    for (let i = 0; i < names.length; i++) {{
      if (!q || (names[i] || "").toLowerCase().includes(q)) byTitle.push(i);
    }}
    const idx = byTitle.length ? byTitle : (q ? [] : names.map((_, i) => i));
    for (let k = 0; k < idx.length && out.length < cap; k++) {{
      const i = idx[k];
      let body = "";
      try {{ body = app.notes[i].plaintext() || ""; }} catch (e) {{}}
      out.push(JSON.stringify({{
        name: names[i] || "(untitled)",
        modified: String(mods[i]).slice(0, 24),
        preview: body.replace(/\\s+/g, " ").slice(0, 300)
      }}));
    }}
    out.join("\\n");
    '''
    rows = _rows(await osa(script, "JavaScript", timeout=60))
    if rows or not query:
        return rows
    # nothing matched a title, so fall back to a bounded body scan
    return await _notes_body_search(query, limit)


async def _notes_body_search(query: str, limit: int) -> list[dict]:
    script = f'''
    const app = Application("Notes");
    const q = {json.dumps(query.lower())};
    const cap = {min(limit, MAX_ROWS)};
    const names = app.notes.name();
    const out = [];
    for (let i = 0; i < names.length && out.length < cap && i < 400; i++) {{
      let body = "";
      try {{ body = app.notes[i].plaintext() || ""; }} catch (e) {{ continue; }}
      if (!body.toLowerCase().includes(q)) continue;
      out.push(JSON.stringify({{
        name: names[i] || "(untitled)", modified: "",
        preview: body.replace(/\\s+/g, " ").slice(0, 300)
      }}));
    }}
    out.join("\\n");
    '''
    return _rows(await osa(script, "JavaScript", timeout=90))


async def note_read(name: str) -> str:
    script = f'''
    const app = Application("Notes");
    const want = {json.dumps(name)}.toLowerCase();
    const notes = app.notes();
    for (let i = 0; i < notes.length; i++) {{
      try {{
        if (notes[i].name().toLowerCase() === want) return notes[i].plaintext();
      }} catch (e) {{}}
    }}
    for (let i = 0; i < notes.length; i++) {{
      try {{
        if (notes[i].name().toLowerCase().includes(want)) return notes[i].plaintext();
      }} catch (e) {{}}
    }}
    "";
    '''
    return await osa(script, "JavaScript", timeout=90)


async def note_create(title: str, body: str) -> str:
    html = (body or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
    script = f'''
    const app = Application("Notes");
    const n = app.make({{new: "note", withProperties: {{
      name: {json.dumps(title)},
      body: {json.dumps("<div><b>" + title.replace("<", "&lt;") + "</b></div>" + html)}
    }}}});
    n.name();
    '''
    return await osa(script, "JavaScript")


# ---------- Calendar ----------

async def calendar_agenda(days: int = 7) -> list[dict]:
    days = max(1, min(days, 60))
    script = f'''
    const app = Application("Calendar");
    const now = new Date();
    const until = new Date(now.getTime() + {days} * 86400000);
    const out = [];
    const cals = app.calendars();
    for (let c = 0; c < cals.length; c++) {{
      let evs = [];
      try {{
        evs = cals[c].events.whose({{_and: [
          {{startDate: {{_greaterThan: now}}}}, {{startDate: {{_lessThan: until}}}}]}})();
      }} catch (e) {{ continue; }}
      for (let i = 0; i < evs.length; i++) {{
        try {{
          out.push(JSON.stringify({{
            calendar: cals[c].name(),
            title: evs[i].summary(),
            start: String(evs[i].startDate()),
            end: String(evs[i].endDate()),
            location: evs[i].location() || ""
          }}));
        }} catch (e) {{}}
      }}
    }}
    out.join("\\n");
    '''
    rows = _rows(await osa(script, "JavaScript", timeout=90))
    return sorted(rows, key=lambda r: r.get("start", ""))[:MAX_ROWS]


async def calendar_create(title: str, start_iso: str, end_iso: str,
                          calendar: str = "", location: str = "") -> str:
    script = f'''
    const app = Application("Calendar");
    const want = {json.dumps(calendar)};
    let cal = null;
    const cals = app.calendars();
    for (let i = 0; i < cals.length; i++) {{
      if (!want || cals[i].name() === want) {{ cal = cals[i]; break; }}
    }}
    if (!cal) throw new Error("no calendar named " + want);
    const ev = cal.events.push(app.Event({{
      summary: {json.dumps(title)},
      startDate: new Date({json.dumps(start_iso)}),
      endDate: new Date({json.dumps(end_iso)}),
      location: {json.dumps(location)}
    }}));
    "created in " + cal.name();
    '''
    return await osa(script, "JavaScript")


MAX_UPDATED = 50


async def calendar_update(query: str, days: int = 365, calendar: str = "",
                          title: str = "", location: str = "",
                          alert_minutes_before: int | None = None,
                          silent_alert: bool = True) -> str:
    """Change events that already exist. Narrow on purpose: no deleting, no
    moving in time, and nothing happens without a query that matches something.

    `alert_minutes_before` sets a display alarm, which is a banner, and clears
    the sound alarms on the same event when `silent_alert` is on. That pairing
    is the whole point: "alarm" and "notification" are two different alarm kinds
    on the same event, not a setting somewhere else, and an event carrying both
    still makes a noise.
    """
    if not (query or "").strip():
        return "a query is required: it selects which existing events to change"
    if alert_minutes_before is None and not title and not location:
        return "nothing to change: give alert_minutes_before, title or location"
    days = max(1, min(int(days or 365), 3650))
    script = f'''
    const app = Application("Calendar");
    const now = new Date();
    const until = new Date(now.getTime() + {days} * 86400000);
    const needle = {json.dumps(query)}.toLowerCase();
    const wantCal = {json.dumps(calendar)};
    const newTitle = {json.dumps(title)};
    const newLoc = {json.dumps(location)};
    const alertMins = {json.dumps(alert_minutes_before)};
    const silent = {json.dumps(bool(silent_alert))};
    const out = [];
    const cals = app.calendars();
    for (let c = 0; c < cals.length && out.length < {MAX_UPDATED}; c++) {{
      if (wantCal && cals[c].name() !== wantCal) continue;
      let evs = [];
      try {{
        evs = cals[c].events.whose({{_and: [
          {{startDate: {{_greaterThan: now}}}}, {{startDate: {{_lessThan: until}}}}]}})();
      }} catch (e) {{ continue; }}
      for (let i = 0; i < evs.length && out.length < {MAX_UPDATED}; i++) {{
        let was = "";
        try {{ was = evs[i].summary(); }} catch (e) {{ continue; }}
        if (was.toLowerCase().indexOf(needle) < 0) continue;
        const changed = [];
        try {{
          if (newTitle) {{ evs[i].summary = newTitle; changed.push("title"); }}
          if (newLoc) {{ evs[i].location = newLoc; changed.push("location"); }}
          if (alertMins !== null) {{
            // A display alarm is the banner; a sound alarm is the noise. Both
            // can sit on one event, so replacing one without clearing the other
            // changes nothing the owner would notice.
            if (silent) {{
              const sounds = evs[i].soundAlarms();
              for (let s = sounds.length - 1; s >= 0; s--) {{
                try {{ app.delete(sounds[s]); }} catch (e) {{}}
              }}
            }}
            const shown = evs[i].displayAlarms();
            for (let d = shown.length - 1; d >= 0; d--) {{
              try {{ app.delete(shown[d]); }} catch (e) {{}}
            }}
            evs[i].displayAlarms.push(app.DisplayAlarm({{triggerInterval: -Math.abs(alertMins)}}));
            changed.push("alert");
          }}
          out.push(JSON.stringify({{calendar: cals[c].name(), title: was,
                                    changed: changed.join("+")}}));
        }} catch (e) {{
          out.push(JSON.stringify({{calendar: cals[c].name(), title: was,
                                    changed: "FAILED: " + e.message}}));
        }}
      }}
    }}
    out.join("\\n");
    '''
    rows = _rows(await osa(script, "JavaScript", timeout=120))
    if not rows:
        return f"no upcoming events matching '{query}' in the next {days} days; nothing changed"
    ok = [r for r in rows if not str(r.get("changed", "")).startswith("FAILED")]
    lines = [f"- {r['title']} ({r['calendar']}): {r['changed']}" for r in rows]
    return f"updated {len(ok)} of {len(rows)} matching events:\n" + "\n".join(lines)


async def calendar_preview(query: str, days: int = 365, calendar: str = "") -> list[dict]:
    """Exactly what calendar_update would touch, changing nothing.

    This runs its own search rather than filtering the agenda, because the
    agenda stops at 60 days and update reaches much further. A preview that
    sees less than the change it is previewing is worse than no preview: it
    would show five events and quietly alter a sixth.
    """
    days = max(1, min(int(days or 365), 3650))
    script = f'''
    const app = Application("Calendar");
    const now = new Date();
    const until = new Date(now.getTime() + {days} * 86400000);
    const needle = {json.dumps(query)}.toLowerCase();
    const wantCal = {json.dumps(calendar)};
    const out = [];
    const cals = app.calendars();
    for (let c = 0; c < cals.length && out.length < {MAX_UPDATED}; c++) {{
      if (wantCal && cals[c].name() !== wantCal) continue;
      let evs = [];
      try {{
        evs = cals[c].events.whose({{_and: [
          {{startDate: {{_greaterThan: now}}}}, {{startDate: {{_lessThan: until}}}}]}})();
      }} catch (e) {{ continue; }}
      for (let i = 0; i < evs.length && out.length < {MAX_UPDATED}; i++) {{
        try {{
          const title = evs[i].summary();
          if (title.toLowerCase().indexOf(needle) < 0) continue;
          out.push(JSON.stringify({{
            calendar: cals[c].name(), title: title,
            start: String(evs[i].startDate()),
            alarms: evs[i].displayAlarms().length + "d/" + evs[i].soundAlarms().length + "s"}}));
        }} catch (e) {{}}
      }}
    }}
    out.join("\\n");
    '''
    rows = _rows(await osa(script, "JavaScript", timeout=120))
    return sorted(rows, key=lambda r: r.get("start", ""))


# ---------- Reminders ----------

def _as_str(s: str) -> str:
    """A quoted AppleScript string literal."""
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def remindctl() -> str | None:
    """Reminders through EventKit instead of the AppleScript bridge.

    Walking eight lists over AppleScript takes ~44s on a real library; the same
    read through remindctl, which sits on EventKit, takes 0.25s. Hermes reaches
    for the same tool rather than scripting Reminders.app, and having tried both
    the reason is obvious. Optional: without it the AppleScript path below still
    works, just slowly.
    """
    for p in ("/opt/homebrew/bin/remindctl", "/usr/local/bin/remindctl"):
        if Path(p).exists():
            return p
    return shutil.which("remindctl")


async def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    proc = await processes.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AppleError(f"{args[0]} timed out after {timeout}s")
    return proc.returncode, out.decode("utf-8", errors="replace")


async def reminders_list(list_name: str = "", include_done: bool = False,
                         limit: int = 30) -> list[dict]:
    cap = min(limit, MAX_ROWS)
    cli = remindctl()
    if cli:
        args = [cli, "export", "--export-format", "json"]
        if list_name:
            args += ["-l", list_name]
        code, out = await _run(args, timeout=60)
        if code != 0:
            raise AppleError(f"remindctl: {out.strip()[:300]}")
        try:
            rows = json.loads(out)
        except ValueError:
            raise AppleError("remindctl returned output that is not JSON")
        picked = []
        for r in rows:
            if r.get("isCompleted") and not include_done:
                continue
            picked.append({"list": r.get("listName", ""), "name": r.get("title", ""),
                           "due": (r.get("dueDate") or "")[:16].replace("T", " "),
                           "completed": bool(r.get("isCompleted")),
                           "id": r.get("id", "")})
            if len(picked) >= cap:
                break
        return picked
    return await _reminders_applescript(list_name, include_done, cap)


async def _reminders_applescript(list_name: str, include_done: bool,
                                 cap: int) -> list[dict]:
    """Fallback when remindctl is not installed. Properties are fetched a column
    at a time; asking per reminder is a round trip each and never finishes."""
    where = "" if include_done else " whose completed is false"
    script = f'''
    set want to {_as_str(list_name)}
    set out to ""
    tell application "Reminders"
      repeat with l in lists
        set lname to name of l
        if want is "" or lname is want then
          set ns to name of (reminders of l{where})
          set ds to due date of (reminders of l{where})
          repeat with i from 1 to (count of ns)
            set d to item i of ds
            set dtxt to ""
            if d is not missing value then set dtxt to (d as string)
            set out to out & lname & tab & (item i of ns) & tab & dtxt & linefeed
          end repeat
        end if
      end repeat
    end tell
    return out
    '''
    text = await osa(script, "AppleScript", timeout=120)
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({"list": parts[0], "name": parts[1], "due": parts[2],
                     "completed": False, "id": ""})
        if len(rows) >= cap:
            break
    return rows


async def reminder_create(title: str, due: str = "", list_name: str = "",
                          notes: str = "") -> str:
    cli = remindctl()
    if not cli:
        raise AppleError("adding reminders needs remindctl: brew install "
                         "steipete/tap/remindctl")
    args = [cli, "add", title]
    if list_name:
        args += ["--list", list_name]
    if due:
        args += ["--due", due]
    if notes:
        args += ["--notes", notes]
    code, out = await _run(args, timeout=30)
    if code != 0:
        raise AppleError(f"remindctl add: {out.strip()[:300]}")
    return out.strip()[:300] or f"added: {title}"


async def reminder_complete(title: str) -> str:
    """Find by title, then complete by id — a write, but a visible one the owner
    can untick."""
    cli = remindctl()
    if not cli:
        raise AppleError("completing reminders needs remindctl: brew install "
                         "steipete/tap/remindctl")
    open_now = await reminders_list(limit=MAX_ROWS)
    want = (title or "").lower()
    hit = next((r for r in open_now if want and want in r["name"].lower()), None)
    if not hit:
        return "no open reminder matching that"
    code, out = await _run([cli, "complete", hit["id"]], timeout=30)
    if code != 0:
        raise AppleError(f"remindctl complete: {out.strip()[:300]}")
    return f"completed: {hit['name']}"


# ---------- Mail (read and draft; never send) ----------

async def mail_search(query: str, limit: int = 15) -> list[dict]:
    """Newest inbox mail, filtered in the script so a big mailbox does not have
    to cross the bridge."""
    script = f'''
    const app = Application("Mail");
    const q = {json.dumps((query or "").lower())};
    const out = [];
    const boxes = app.inbox();
    const msgs = boxes.messages();
    for (let i = 0; i < msgs.length && out.length < {min(limit, MAX_ROWS)} && i < 400; i++) {{
      const m = msgs[i];
      try {{
        const subj = m.subject() || "";
        const from = m.sender() || "";
        if (q && !(subj + " " + from).toLowerCase().includes(q)) continue;
        out.push(JSON.stringify({{
          id: m.id(), subject: subj, from: from,
          date: String(m.dateReceived()), read: m.readStatus(),
          preview: (m.content() || "").replace(/\\s+/g, " ").slice(0, 240)
        }}));
      }} catch (e) {{}}
    }}
    out.join("\\n");
    '''
    return _rows(await osa(script, "JavaScript", timeout=120))


async def mail_draft(to: str, subject: str, body: str) -> str:
    """Compose a draft and leave it open. Sending stays the owner's to press."""
    script = f'''
    const app = Application("Mail");
    const msg = app.OutgoingMessage({{
      subject: {json.dumps(subject)},
      content: {json.dumps(body)},
      visible: true
    }});
    app.outgoingMessages.push(msg);
    msg.toRecipients.push(app.Recipient({{address: {json.dumps(to)}}}));
    "draft opened";
    '''
    return await osa(script, "JavaScript")


# ---------- Messages (read only) ----------

def messages_recent(contact: str = "", limit: int = 30) -> list[dict]:
    """Recent iMessage/SMS history, straight from the local database. Read only:
    sending goes through the iMessage channel, which the owner sees."""
    if not CHAT_DB.exists():
        raise AppleError("no Messages database on this machine")
    limit = max(1, min(limit, MAX_ROWS))
    sql = """
        SELECT m.is_from_me, h.id,
               datetime(m.date/1000000000 + ?, 'unixepoch', 'localtime'),
               COALESCE(m.text, '')
        FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE COALESCE(m.text, '') != ''
    """
    args = [APPLE_EPOCH]
    if contact:
        sql += " AND h.id LIKE ?"
        args.append(f"%{contact}%")
    sql += " ORDER BY m.date DESC LIMIT ?"
    args.append(limit)
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
    except sqlite3.OperationalError as exc:
        raise AppleError(
            "cannot read the Messages database. macOS keeps it behind Full Disk "
            f"Access; grant it to Mission Control and try again ({exc})")
    return [{"from": "you" if r[0] else (r[1] or "unknown"), "when": r[2],
             "text": (r[3] or "")[:400]} for r in rows]


# ---------- what actually works right now ----------

async def status() -> dict:
    """Probe each app rather than assume. macOS grants these per process, so
    what worked in a terminal says nothing about what works under launchd."""
    if not MAC:
        return {"platform": "not macOS", **{a: False for a in APPS}}
    out: dict = {"platform": "macOS"}

    async def probe(key: str, script: str):
        try:
            await osa(script, "JavaScript", timeout=25)
            out[key] = True
        except Exception as exc:
            out[key] = False
            out[f"{key}_error"] = str(exc)[:200]

    await probe("notes", 'Application("Notes").notes.length')
    await probe("calendar", 'Application("Calendar").calendars.length')
    await probe("reminders", 'Application("Reminders").lists.length')
    await probe("mail", 'Application("Mail").accounts.length')
    try:
        messages_recent(limit=1)
        out["messages"] = True
    except Exception as exc:
        out["messages"] = False
        out["messages_error"] = str(exc)[:200]
    # reachable and connected are different questions: macOS decides the first,
    # the owner decides the second, and a confused answer helps nobody
    out["connected"] = {a: enabled(a) for a in APPS}
    return out
