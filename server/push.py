"""Push notifications to the owner's phones with nothing in between.

The phone app registers its Expo push token with this server; when an
agent says something while the owner is away from every app window,
the server posts straight to Expo's push API. Per-thread cooldown so a
chatty agent produces one buzz, not ten. Tokens Expo reports dead are
dropped.
"""
import json
import logging
import time

from apphome import ROOT

logger = logging.getLogger("push")
DEVICES = ROOT / "devices.json"
EXPO = "https://exp.host/--/api/v2/push/send"
COOLDOWN = 90
_last: dict[str, float] = {}


def devices() -> list[dict]:
    try:
        return json.loads(DEVICES.read_text())
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]):
    DEVICES.write_text(json.dumps(rows, indent=1))


def register(token: str, name: str = "phone") -> list[dict]:
    token = token.strip()
    rows = [d for d in devices() if d.get("token") != token]
    rows.append({"token": token, "name": name.strip() or "phone", "added": time.time()})
    _save(rows)
    return rows


def remove(token: str) -> list[dict]:
    rows = [d for d in devices() if d.get("token") != token]
    _save(rows)
    return rows


def due(thread: str, now: float | None = None) -> bool:
    """One notification per thread per cooldown window."""
    now = now or time.time()
    if now - _last.get(thread, 0.0) < COOLDOWN:
        return False
    _last[thread] = now
    return True


async def notify(thread: str, title: str, body: str, data: dict | None = None,
                 force: bool = False) -> bool:
    rows = devices()
    if not rows or (not force and not due(thread)):
        return False
    import httpx
    messages = [{"to": d["token"], "title": title, "body": body[:300], "sound": "default",
                 "data": {"thread": thread, **(data or {})}} for d in rows]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(EXPO, json=messages, headers={"Accept": "application/json"})
            results = r.json().get("data") or []
    except Exception:
        logger.exception("push failed")
        return False
    ok = False
    for d, item in zip(rows, results):
        if item.get("status") == "ok":
            ok = True
        elif (item.get("details") or {}).get("error") == "DeviceNotRegistered":
            remove(d["token"])
        else:
            logger.warning("push to %s: %s", d.get("name"), item.get("message"))
    return ok
