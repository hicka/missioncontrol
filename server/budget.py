"""A daily token ceiling for the whole team.

Zero means no limit. At 80% the lead's chat gets one warning for the
day; at 100% turns stop with one notice, and resume at local midnight
or when the owner raises the limit in Settings.
"""
import datetime

import db
import settings as cfg

WARN_AT = 0.8
_noted: dict[str, str] = {}   # "warn" / "stop" -> local date it was posted


def settings() -> dict:
    s = cfg.load().get("budget") or {}
    try:
        daily = max(0, int(s.get("daily_tokens") or 0))
    except (TypeError, ValueError):
        daily = 0
    return {"daily_tokens": daily}


def status() -> dict:
    limit = settings()["daily_tokens"]
    used = db.usage_today()
    pct = (used / limit) if limit else 0.0
    return {"daily_tokens": limit, "used_today": used, "pct": round(pct, 3),
            "exceeded": bool(limit) and used >= limit}


def reset_notes():
    """The limit changed: whatever was announced no longer applies."""
    _noted.clear()


def check(today: str | None = None, thread: str = "") -> tuple[str, str | None]:
    """('ok'|'warn'|'stop', note-or-None). The warning is announced once a
    day; the stop is announced once a day in each chat that runs into it."""
    st = status()
    limit = st["daily_tokens"]
    if not limit:
        return "ok", None
    today = today or datetime.date.today().isoformat()
    used = st["used_today"]
    if used >= limit:
        if _noted.get("stop:" + thread) == today:
            return "stop", None
        _noted["stop:" + thread] = today
        return "stop", (f"daily budget reached: {used:,} of {limit:,} tokens used today. "
                        "The team pauses until midnight, or raise the budget in Settings.")
    if used >= limit * WARN_AT:
        if _noted.get("warn") == today:
            return "ok", None
        _noted["warn"] = today
        return "warn", (f"budget note: {used:,} of {limit:,} tokens used today "
                        f"({round(used / limit * 100)}%).")
    return "ok", None
