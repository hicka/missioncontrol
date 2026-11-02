"""Reaching the owner and keeping spend honest: push, budget, editable memory."""
import asyncio
import time


def test_push_registers_and_cools_down(tmp_path, monkeypatch):
    import push
    monkeypatch.setattr(push, "DEVICES", tmp_path / "devices.json")
    push._last.clear()
    assert push.devices() == []
    rows = push.register("ExponentPushToken[abc]", "iphone")
    assert rows[0]["token"] == "ExponentPushToken[abc]" and rows[0]["name"] == "iphone"
    push.register("ExponentPushToken[abc]", "iphone again")      # replaces, never duplicates
    assert len(push.devices()) == 1
    assert push.due("dm:cto", now=1000.0)
    assert not push.due("dm:cto", now=1000.0 + 30)               # inside the cooldown
    assert push.due("dm:cto", now=1000.0 + 91)
    assert push.remove("ExponentPushToken[abc]") == []


def test_push_drops_tokens_expo_says_are_dead(tmp_path, monkeypatch):
    import push
    monkeypatch.setattr(push, "DEVICES", tmp_path / "devices.json")
    push.register("ExponentPushToken[dead]", "old phone")

    class FakeResp:
        def json(self):
            return {"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert asyncio.run(push.notify("dm:cto", "CTO", "hello", force=True)) is False
    assert push.devices() == []


def test_budget_warns_once_then_stops(monkeypatch):
    import budget
    import settings as cfg
    budget._noted.clear()
    monkeypatch.setattr(cfg, "load", lambda: {"budget": {"daily_tokens": 1000}})
    used = {"n": 0}
    monkeypatch.setattr(budget.db, "usage_today", lambda: used["n"])
    assert budget.check("d1") == ("ok", None)
    used["n"] = 850
    verdict, note = budget.check("d1")
    assert verdict == "warn" and "85%" in note
    assert budget.check("d1") == ("ok", None)                      # warned once today
    used["n"] = 1200
    verdict, note = budget.check("d1")
    assert verdict == "stop" and "1,200" in note
    assert budget.check("d1") == ("stop", None)                    # notice once today
    verdict, note = budget.check("d2")
    assert verdict == "stop" and note                             # new day, new notice
    monkeypatch.setattr(cfg, "load", lambda: {"budget": {"daily_tokens": 0}})
    assert budget.check("d2") == ("ok", None)                      # zero means no limit


def test_memory_can_be_corrected_and_still_found():
    import db
    mid = db.add_memory("cto", "prod deploys happen on Fridays", "fact")
    assert db.update_memory(mid, "prod deploys happen on Tuesdays", "lesson")
    rows = {m["id"]: m for m in db.list_memories(500)}
    assert rows[mid]["text"].endswith("Tuesdays") and rows[mid]["kind"] == "lesson"
    assert any(m["id"] == mid for m in db.search_memories("Tuesdays deploys"))
    assert not db.update_memory(999999, "nothing")


def test_playbook_delete_and_usage_today():
    import db
    db.save_playbook("cto", "example.com", "Log in", "1. open the site")
    assert any(p["title"] == "Log in" for p in db.list_playbooks())
    assert db.delete_playbook("example.com", "Log in")
    assert not any(p["title"] == "Log in" for p in db.list_playbooks())
    before = db.usage_today()
    db.add_usage("cto", "minimax", "MiniMax-M3", 120, 30)
    assert db.usage_today() == before + 150
