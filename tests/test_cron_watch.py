"""Watch jobs stop themselves: a poll cap (max_runs) and an early resolve."""
import asyncio


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


class _FakeOrch:
    """Just enough surface for cron._fire_job."""
    def __init__(self, aid):
        self.agents = {aid: {"name": aid}}
        self.fired = []

    async def external_event(self, aid, source, text, **kwargs):
        self.fired.append((aid, source, text))


async def _noop(_msg):
    return None


def test_max_runs_stops_the_job_after_the_cap():
    import cron
    import db
    import time
    aid = "atlas"
    jid = db.add_cron(aid, "watch deploy", "every 1 minute", "poll the deploy",
                      "owner", time.time(), status="active", max_runs=2)
    orch = _FakeOrch(aid)

    async def run():
        # fire 1: runs -> 1, still scheduled, not the final check
        await cron._fire_job(orch, _noop, db.get_cron(jid), time.time())
        j = db.get_cron(jid)
        assert j["runs"] == 1 and j["status"] == "active" and j["next_run"] is not None
        assert "FINAL scheduled check" not in orch.fired[-1][2]

        # fire 2: hits the cap -> runs 2, done, no next_run, final-check note
        await cron._fire_job(orch, _noop, db.get_cron(jid), time.time())
        j = db.get_cron(jid)
        assert j["runs"] == 2 and j["status"] == "done" and j["next_run"] is None
        assert "FINAL scheduled check" in orch.fired[-1][2]

    asyncio.run(run())
    # a done job is never handed back to the scheduler
    assert all(x["id"] != jid for x in db.due_cron(time.time() + 999))


def test_resolve_action_stops_a_running_watch_job():
    import db
    import time
    o = _orch()
    aid = list(o.agents)[0]
    jid = db.add_cron(aid, "watch ci", "every 1 minute", "poll ci",
                      "owner", time.time(), status="active", max_runs=10)

    out = asyncio.run(o._exec_tool(aid, "~", "cron",
                                   {"action": "resolve", "job_id": jid}))
    assert "resolved" in out
    j = db.get_cron(jid)
    assert j["status"] == "done" and j["next_run"] is None
    assert asyncio.run(o._exec_tool(aid, "~", "cron",
                                    {"action": "resolve", "job_id": 999999})) == "no such job id"


def test_bounded_jobs_start_active_without_approval():
    import db
    o = _orch()
    aid = list(o.agents)[0]

    out = asyncio.run(o._exec_tool(aid, "~", "cron", {
        "action": "create", "name": "temp cleanup pass",
        "schedule": "every 10 minutes", "task": "delete temp files older than a day", "max_runs": 12}))
    assert "started" in out and "no approval" in out
    j = [x for x in db.list_cron() if x["name"] == "temp cleanup pass"][-1]
    assert j["status"] == "active" and j["max_runs"] == 12

    out2 = asyncio.run(o._exec_tool(aid, "~", "cron", {
        "action": "create", "name": "one shot", "schedule": "once 2099-01-01 09:00",
        "task": "do it once"}))
    assert "started" in out2
    j2 = [x for x in db.list_cron() if x["name"] == "one shot"][-1]
    assert j2["status"] == "active"


def _intent(schedule, task):
    import cron
    return asyncio.run(cron.looks_like_watch(schedule, task))


def test_looks_like_watch_detects_transient_watches():
    assert _intent("every 5 minutes", "watch the acmeshop deploy to completion")
    assert _intent("every 1 minute", "poll master CI run 123, then deploy when it turns green")
    assert _intent("every 5 minutes", "wait for mobiledev, merge, then deploy")
    # recurring calendar work and standing monitors are NOT transient watches
    assert not _intent("daily 08:00", "send the morning brief")
    assert not _intent("every 5 minutes", "check the error logs and alert on new errors")
    assert not _intent("every 10 minutes", "monitor site uptime and ping me if down")


def test_a_wording_the_patterns_miss_is_still_caught(monkeypatch):
    """'follow run 88123 and ping me with the conclusion' matches nothing in the
    list, and there is no end to how a bounded task can be phrased."""
    import cron
    import decide
    task = "follow run 88123 and ping me with the conclusion"
    assert not cron._WATCH_INTENT.search(task)

    async def judged(state, instructions, criteria=None):
        return 0.78

    monkeypatch.setattr(decide, "noul", judged)
    assert _intent("every 2 minutes", task)


def test_a_recurring_job_is_not_refused_by_the_judge(monkeypatch):
    import decide

    async def judged(state, instructions, criteria=None):
        return 0.07

    monkeypatch.setattr(decide, "noul", judged)
    assert not _intent("every 5 minutes", "sync the inventory spreadsheet")


def test_a_long_interval_never_leaves_the_machine(monkeypatch):
    """A daily job is not a watch whatever it says, so nothing is asked."""
    import decide
    asked = []

    async def judged(state, instructions, criteria=None):
        asked.append(state)
        return 0.99

    monkeypatch.setattr(decide, "noul", judged)
    assert not _intent("daily 08:00", "follow the deploy until it finishes")
    assert not asked


def test_an_unreachable_judge_leaves_the_patterns_in_charge(monkeypatch):
    import decide

    async def unreachable(state, instructions, criteria=None):
        return None

    monkeypatch.setattr(decide, "noul", unreachable)
    assert not _intent("every 2 minutes", "follow run 88123 and ping me")
    assert _intent("every 2 minutes", "watch the deploy to completion")


def test_cron_redirects_watch_shaped_jobs_to_the_watch_tool():
    import db
    o = _orch()
    aid = list(o.agents)[0]
    before = len(db.list_cron())
    out = asyncio.run(o._exec_tool(aid, "~", "cron", {
        "action": "create", "name": "watch acmeshop deploy",
        "schedule": "every 5 minutes",
        "task": "watch acmeshop prod deploy 21099999 to completion and report", "max_runs": 10}))
    assert "WATCH tool" in out and "done_when" in out
    assert len(db.list_cron()) == before          # no job was created


def test_open_ended_recurring_still_needs_approval():
    import db
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_tool(aid, "~", "cron", {
        "action": "create", "name": "morning report",
        "schedule": "daily 08:00", "task": "send the brief"}))
    assert "PENDING" in out
    j = [x for x in db.list_cron() if x["name"] == "morning report"][-1]
    assert j["status"] == "pending" and j["max_runs"] == 0


def test_fired_prompt_offers_early_resolve():
    import cron
    import db
    import time
    aid = "atlas"
    jid = db.add_cron(aid, "watch", "every 1 minute", "poll", "owner",
                      time.time(), status="active", max_runs=5)
    orch = _FakeOrch(aid)
    asyncio.run(cron._fire_job(orch, _noop, db.get_cron(jid), time.time()))
    text = orch.fired[-1][2]
    assert f"action='resolve', job_id={jid}" in text
    assert "check 1 of 5" in text
