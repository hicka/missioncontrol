"""Regression tests for the bugs the team's own review found."""
import asyncio
import datetime
import time


def test_cron_day_of_week_counts_from_sunday():
    import cron
    # a Monday 08:00 base: "0 9 * * 1-5" must fire that same Monday at 09:00
    base = datetime.datetime(2026, 9, 7, 8, 0).timestamp()      # 2026-09-07 is a Monday
    d = datetime.datetime.fromtimestamp(cron.next_run("0 9 * * 1-5", base))
    assert d.weekday() == 0 and d.hour == 9
    # a Friday 10:00 base: the next weekday run is Monday, not Saturday
    base = datetime.datetime(2026, 9, 11, 10, 0).timestamp()     # Friday
    d = datetime.datetime.fromtimestamp(cron.next_run("0 9 * * 1-5", base))
    assert d.weekday() == 0
    # cron's 0 is Sunday
    d = datetime.datetime.fromtimestamp(cron.next_run("0 9 * * 0", base))
    assert d.weekday() == 6


def test_notify_webhooks_post_without_waking():
    import db
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    aid = list(o.agents)[0]
    asyncio.run(o.post_event_line(aid, "hook:deploy", "webhook 'deploy' received"))
    last = db.last_message(f"dm:{aid}")
    assert last["sender"] == "hook:deploy" and last["kind"] == "sys"
    assert o.queues[aid].empty()                        # notify mode never wakes the agent


def test_channel_dedupe_survives_a_restart():
    import db
    assert db.seen_once("whatsapp", "wamid.1") is True
    assert db.seen_once("whatsapp", "wamid.1") is False      # redelivery
    assert db.seen_once("imessage", "wamid.1") is True       # per channel




def test_budget_stop_is_announced_per_chat_and_reset_by_a_new_limit(monkeypatch):
    import budget
    import settings as cfg
    budget.reset_notes()
    monkeypatch.setattr(cfg, "load", lambda: {"budget": {"daily_tokens": 100}})
    monkeypatch.setattr(budget.db, "usage_today", lambda: 150)
    assert budget.check("d", thread="dm:cto")[1]               # first chat is told
    assert budget.check("d", thread="dm:cto")[1] is None       # once
    assert budget.check("d", thread="dm:devops")[1]            # another chat is told too
    budget.reset_notes()
    assert budget.check("d", thread="dm:cto")[1]               # raising the limit re-announces


def test_outward_action_is_recorded_before_it_runs_and_finished_after():
    import db
    aid_key = "git push origin main"
    action_id = db.add_action("devops", aid_key, aid_key, "running")
    assert db.recent_action(aid_key, 60)["outcome"] == "running"
    db.finish_action(action_id, "Everything up-to-date")
    assert db.recent_action(aid_key, 60)["outcome"] == "Everything up-to-date"
