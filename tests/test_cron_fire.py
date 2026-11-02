import asyncio

import cron
import db


class DummyOrch:
    def __init__(self):
        self.agents = {"cto": {}}
        self.events = []

    async def external_event(self, aid, source, text, **kwargs):
        self.events.append((aid, source, text))


async def _noop_broadcast(_):
    pass


def test_monitor_job_skips_model_when_unchanged():
    orch = DummyOrch()
    jid = db.add_cron("cto", "watch", "every 30 minutes", "check the thing",
                      "cto", 1.0, status="active", monitor_cmd="echo stable-output")
    job = db.get_cron(jid)
    asyncio.run(cron._fire_job(orch, _noop_broadcast, job, 100.0))
    assert len(orch.events) == 1                     # first run: output is new
    assert "stable-output" in orch.events[0][2]

    job = db.get_cron(jid)                           # reload with monitor_last set
    asyncio.run(cron._fire_job(orch, _noop_broadcast, job, 200.0))
    assert len(orch.events) == 1                     # unchanged: model skipped
    assert db.get_cron(jid)["next_run"] > 200.0      # but it was rescheduled
    db.delete_cron(jid)


def test_note_carried_into_fire():
    orch = DummyOrch()
    jid = db.add_cron("cto", "report", "every 30 minutes", "daily report",
                      "cto", 1.0, status="active")
    db.update_cron(jid, note="already reported item A")
    job = db.get_cron(jid)
    asyncio.run(cron._fire_job(orch, _noop_broadcast, job, 100.0))
    assert "already reported item A" in orch.events[0][2]
    db.delete_cron(jid)


def test_once_job_finishes():
    import time
    orch = DummyOrch()
    jid = db.add_cron("cto", "one-shot", "once 2020-01-01 10:00", "do once",
                      "cto", 1.0, status="active")
    job = db.get_cron(jid)
    asyncio.run(cron._fire_job(orch, _noop_broadcast, job, time.time()))
    assert db.get_cron(jid)["status"] == "done"
    db.delete_cron(jid)
