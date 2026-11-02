"""Deep research: search, read pages in parallel, synthesize with citations.

Search backends, best available first: Brave (BRAVE_API_KEY) or Tavily
(TAVILY_API_KEY) when a key is in .env, otherwise DuckDuckGo's HTML
endpoint with no key at all. Pages are fetched with httpx and stripped
to readable text; the active model writes a short cited brief. The whole
run is bounded: 6 results, 5 pages, 3000 chars per page.
"""
import asyncio
import html as html_mod
import logging
import re
from urllib.parse import unquote, urlparse, parse_qs

import httpx

import settings as cfg

logger = logging.getLogger("missioncontrol.research")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) MissionControl/1.0"}
MAX_RESULTS = 6
MAX_PAGES = 5
PAGE_CHARS = 3000


def strip_html(html: str) -> str:
    """Readable text from a page without extra dependencies."""
    html = re.sub(r"(?is)<(script|style|nav|header|footer|svg|form|aside)[^>]*>.*?</\1>",
                  " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


async def _search_brave(query: str, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": query, "count": MAX_RESULTS},
                             headers={"X-Subscription-Token": key})
        r.raise_for_status()
        return [{"title": w.get("title", ""), "url": w.get("url", ""),
                 "snippet": strip_html(w.get("description", ""))}
                for w in (r.json().get("web", {}).get("results") or [])[:MAX_RESULTS]]


async def _search_tavily(query: str, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post("https://api.tavily.com/search",
                              json={"api_key": key, "query": query,
                                    "max_results": MAX_RESULTS})
        r.raise_for_status()
        return [{"title": w.get("title", ""), "url": w.get("url", ""),
                 "snippet": (w.get("content") or "")[:300]}
                for w in (r.json().get("results") or [])[:MAX_RESULTS]]


def parse_ddg_html(page: str) -> list[dict]:
    """Result links out of DuckDuckGo's no-JS HTML page."""
    out = []
    for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S):
        href, title = m.group(1), strip_html(m.group(2))
        if "uddg=" in href:  # DDG redirect wrapper
            q = parse_qs(urlparse(href).query)
            href = unquote((q.get("uddg") or [""])[0])
        if href.startswith("http") and title:
            out.append({"title": title, "url": href, "snippet": ""})
        if len(out) >= MAX_RESULTS:
            break
    return out


async def _search_ddg(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.post("https://html.duckduckgo.com/html/",
                              data={"q": query}, headers=UA)
        return parse_ddg_html(r.text)


async def search(query: str) -> tuple[str, list[dict]]:
    """(backend_name, results)"""
    key = cfg.env_get("BRAVE_API_KEY")
    if key:
        try:
            return "brave", await _search_brave(query, key)
        except Exception as exc:
            logger.warning("brave search failed: %s", exc)
    key = cfg.env_get("TAVILY_API_KEY")
    if key:
        try:
            return "tavily", await _search_tavily(query, key)
        except Exception as exc:
            logger.warning("tavily search failed: %s", exc)
    return "duckduckgo", await _search_ddg(query)


async def _fetch_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url, headers=UA)
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return ""
            return strip_html(r.text)[:PAGE_CHARS]
    except Exception:
        return ""


async def run(query: str, agent: str | None = None) -> str:
    """Full research pass; returns a cited brief for the calling agent."""
    import llm
    backend, results = await search(query)
    if not results:
        return f"search ({backend}) returned no results for: {query}"
    pages = await asyncio.gather(*[_fetch_page(r["url"]) for r in results[:MAX_PAGES]])
    sources = []
    for i, (res, page) in enumerate(zip(results, list(pages) + [""] * MAX_PAGES), 1):
        body = page or res.get("snippet") or "(page unreachable)"
        sources.append(f"[{i}] {res['title']}\n{res['url']}\n{body}")
    brief = await llm.quick(
        "You are a research analyst. Using ONLY the numbered sources, write a "
        "decision-ready brief for the question: facts first, disagreements "
        "noted, unknowns admitted. Cite claims with [n]. Under 350 words. End "
        "with a Sources list of the cited urls.",
        f"QUESTION: {query}\n\nSOURCES:\n\n" + "\n\n---\n\n".join(sources),
        max_tokens=900, agent=agent)
    return brief or "synthesis failed; raw sources:\n" + "\n".join(
        f"[{i}] {r['url']}" for i, r in enumerate(results, 1))
