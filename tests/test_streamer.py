"""The orchestrator's streamer: the first delta goes out at once, the rest
coalesce into a few events a second, thinking and text kept apart."""
import asyncio

import orchestrator


def _collect(monkeypatch):
    events = []
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 100.0)   # nothing ages

    async def emit(ev):
        events.append(ev)

    return events, orchestrator.Orchestrator._Streamer(emit, "cto", "dm:cto")


def test_streamer_coalesces_and_separates_thinking_from_text(monkeypatch):
    events, s = _collect(monkeypatch)

    async def run():
        await s("step one, ", "thinking")      # first delta: shown at once
        await s("step two", "thinking")        # buffered
        await s.flush()
        await s("Hel")
        await s("lo")
        await s.flush()
        await s(None)                          # a fresh attempt drops what was shown
        await s("again")
        await s.flush()

    asyncio.run(run())
    assert events == [
        {"type": "stream", "agent": "cto", "thread": "dm:cto", "thinking": "step one, "},
        {"type": "stream", "agent": "cto", "thread": "dm:cto", "thinking": "step two"},
        {"type": "stream", "agent": "cto", "thread": "dm:cto", "text": "Hello"},
        {"type": "stream", "agent": "cto", "thread": "dm:cto", "reset": True},
        {"type": "stream", "agent": "cto", "thread": "dm:cto", "text": "again"},
    ]


def test_streamer_reset_is_silent_when_nothing_was_shown(monkeypatch):
    events, s = _collect(monkeypatch)

    async def run():
        await s(None)
        await s(None)

    asyncio.run(run())
    assert events == []
