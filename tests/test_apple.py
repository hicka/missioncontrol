"""The owner's own Notes, Calendar, Reminders, Messages and Mail.

This is someone's personal life, so the tests are mostly about the gate and
about what agents are not allowed to do with it.
"""
import asyncio

import apple


def test_every_app_is_off_until_the_owner_says_otherwise():
    import settings as cfg
    conf = cfg.load().get("apple") or {}
    for a in apple.APPS:
        assert not conf.get(a, False), a
        assert not apple.enabled(a), a


def test_reminders_is_one_of_the_apps():
    assert apple.APPS == ("notes", "calendar", "reminders", "mail", "messages")


def test_require_refuses_a_disconnected_app_and_says_where_to_fix_it():
    for a in apple.APPS:
        try:
            apple.require(a)
            assert False, f"{a} should be gated"
        except apple.AppleError as exc:
            assert "not connected" in str(exc)
            assert "Settings" in str(exc)


def test_require_rejects_an_app_that_does_not_exist():
    try:
        apple.require("safari")
        assert False
    except apple.AppleError as exc:
        assert "unknown app" in str(exc)


def test_connecting_one_app_does_not_connect_the_others(monkeypatch):
    monkeypatch.setattr(apple.cfg, "load", lambda: {"apple": {"calendar": True}})
    apple.require("calendar")                      # no raise
    for a in ("notes", "reminders", "mail", "messages"):
        try:
            apple.require(a)
            assert False, f"{a} leaked"
        except apple.AppleError:
            pass


# ---------- what agents may not do ----------

def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def _call(monkeypatch, args, connected=("mail", "messages")):
    monkeypatch.setattr(apple.cfg, "load", lambda: {"apple": {a: True for a in connected}})
    o = _orch()
    return asyncio.run(o._exec_apple(list(o.agents)[0], args))


def test_mail_cannot_be_sent_only_drafted(monkeypatch):
    out = _call(monkeypatch, {"app": "mail", "action": "send", "to": "x@y.z"})
    assert "not available" in out and "draft" in out
    assert "cannot be recalled" in out


def test_messages_cannot_be_sent_from_here(monkeypatch):
    out = _call(monkeypatch, {"app": "messages", "action": "send", "to": "+1"})
    assert "not available" in out and "iMessage channel" in out


def test_a_disconnected_app_refuses_before_touching_the_mac(monkeypatch):
    called = []
    monkeypatch.setattr(apple, "notes_search", lambda *a, **k: called.append(1))
    out = _call(monkeypatch, {"app": "notes", "action": "search", "query": "x"},
                connected=("calendar",))
    assert "not connected" in out
    assert not called


def test_an_unknown_action_says_so_rather_than_guessing(monkeypatch):
    out = _call(monkeypatch, {"app": "mail", "action": "delete"})
    assert "not something mail does" in out


def test_a_draft_needs_a_recipient(monkeypatch):
    out = _call(monkeypatch, {"app": "mail", "action": "draft", "title": "hi"})
    assert "needs `to`" in out


def test_a_calendar_event_needs_a_start_and_an_end(monkeypatch):
    out = _call(monkeypatch, {"app": "calendar", "action": "create", "title": "x"},
                connected=("calendar",))
    assert "start and end" in out


def test_apple_errors_come_back_as_words_not_a_traceback(monkeypatch):
    async def boom(*a, **k):
        raise apple.AppleError("macOS has not granted Mission Control permission")

    monkeypatch.setattr(apple, "calendar_agenda", boom)
    out = _call(monkeypatch, {"app": "calendar", "action": "agenda"}, connected=("calendar",))
    assert "not granted" in out


# ---------- the fast path ----------

def test_reminders_prefers_eventkit_over_the_applescript_bridge():
    """remindctl is EventKit; the AppleScript route takes ~44s on a real library
    against ~0.2s here, so the CLI is the path and AppleScript is the fallback."""
    import inspect
    src = inspect.getsource(apple.reminders_list)
    assert "remindctl()" in src
    assert "_reminders_applescript" in src        # still works without the CLI


def test_messages_reads_the_database_and_never_writes_it():
    import inspect
    src = inspect.getsource(apple.messages_recent)
    assert "mode=ro" in src
    assert "INSERT" not in src.upper() and "UPDATE" not in src.upper()
