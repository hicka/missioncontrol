"""Ephemeral watches: generic poll-until-resolved, no approval, self-stopping."""
import asyncio


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


class _FakeOrch:
    def __init__(self, aid):
        self.agents = {aid: {"name": aid}}
        self.fired = []

    async def external_event(self, aid, source, text, **kwargs):
        self.fired.append((aid, source, text))


async def _noop(_msg):
    return None


def test_guard_rejects_mutating_polls():
    import watch
    assert watch.guard("gh run view 5 --json status") is None
    assert watch.guard("https://envoyer.io/api/projects/1/deployments/2") is None
    assert watch.guard("git push origin main") is not None
    assert watch.guard("curl -X POST https://x/y") is not None
    assert watch.guard("eas submit --platform ios") is not None
    assert watch.guard("rm -rf build") is not None


def test_outcome_matches_regex():
    import watch
    assert watch._by_pattern("status: completed success", r"completed", None) == "done"
    assert watch._by_pattern("status: failed", None, r"failed") == "failed"
    assert watch._by_pattern("status: in_progress", r"completed", r"failed") is None
    # fail takes precedence when both could match
    assert watch._by_pattern("completed failed", r"completed", r"failed") == "failed"


def test_start_creates_an_active_watch_without_approval():
    import db
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "ci run", "poll": "gh run view 9 --json status",
        "done_when": "completed", "interval": 25, "max_checks": 8}))
    assert "watching 'ci run'" in out
    ws = db.list_watches("active")
    w = [x for x in ws if x["name"] == "ci run"][0]
    assert w["status"] == "active" and w["interval"] == 25 and w["max_checks"] == 8
    assert w["done_re"] == "completed"


def test_start_requires_a_stop_condition_and_a_safe_poll():
    o = _orch()
    aid = list(o.agents)[0]
    assert "done_when" in asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "x", "poll": "gh run view 1"}))
    assert "read-only" in asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "x", "poll": "git push", "done_when": "ok"}))
    assert "bad regex" in asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "x", "poll": "echo hi", "done_when": "([unclosed"}))


def test_interval_and_checks_are_clamped():
    import db
    import watch
    o = _orch()
    aid = list(o.agents)[0]
    asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "clamp", "poll": "echo x", "done_when": "x",
        "interval": 1, "max_checks": 99999}))
    w = [x for x in db.list_watches("active") if x["name"] == "clamp"][0]
    assert w["interval"] == watch.MIN_INTERVAL
    assert w["max_checks"] == watch.MAX_CHECKS


def test_tick_resolves_and_wakes_the_agent_once():
    import db
    import watch
    aid = "atlas"
    wid = db.add_watch(aid, "echo watch", "echo all done here", "done", None, 20, 5, aid)
    orch = _FakeOrch(aid)

    async def run():
        await watch._tick(orch, _noop, 1_000_000.0)  # far future so it's due
    asyncio.run(run())

    w = db.get_watch(wid)
    assert w["status"] == "done" and w["checks"] == 1
    assert len(orch.fired) == 1
    assert orch.fired[0][1] == "watch" and "resolved" in orch.fired[0][2]


def test_tick_expires_after_max_checks():
    import db
    import watch
    aid = "atlas"
    wid = db.add_watch(aid, "never", "echo still going", "NEVERMATCH", None, 20, 1, aid)
    orch = _FakeOrch(aid)
    asyncio.run(watch._tick(orch, _noop, 1_000_000.0))
    w = db.get_watch(wid)
    assert w["status"] == "expired" and w["checks"] == 1
    assert "did not resolve" in orch.fired[0][2]


def test_tick_respects_the_interval():
    import db
    import watch
    import time
    aid = "atlas"
    wid = db.add_watch(aid, "waiting", "echo done", "done", None, 300, 5, aid)
    db.update_watch(wid, last_poll=time.time())   # just polled
    orch = _FakeOrch(aid)
    asyncio.run(watch._tick(orch, _noop, time.time() + 5))   # only 5s later
    assert db.get_watch(wid)["checks"] == 0        # not yet due, untouched
    assert not orch.fired


# ---------- resolving by a stated goal, when no regex could be written ----------

def _decider(monkeypatch, picked, probability):
    import decide
    asked = []

    async def fake_choice(state, instructions, criteria):
        asked.append(state)
        assert set(criteria) == {"done", "failed", "pending"}
        return (picked, probability)

    monkeypatch.setattr(decide, "choice", fake_choice)
    return asked


def _watch(**over):
    base = {"id": 1, "agent": "atlas", "poll": "gh run view 9", "done_re": None,
            "fail_re": None, "goal": "the CI run finishes", "last_out": ""}
    return {**base, **over}


def test_a_goal_resolves_a_watch_that_has_no_regex(monkeypatch):
    """The point: the agent has to write a regex before the thing has run, and
    often cannot. A goal is stated once and read against whatever turns up."""
    import watch
    asked = _decider(monkeypatch, "done", 0.95)
    got = asyncio.run(watch._outcome(_watch(), "conclusion: victory (weird wording)"))
    assert got == "done"
    assert asked[0]["goal"] == "the CI run finishes"


def test_a_failed_goal_resolves_as_failed(monkeypatch):
    import watch
    _decider(monkeypatch, "failed", 0.91)
    assert asyncio.run(watch._outcome(_watch(), "the build was rejected")) == "failed"


def test_an_uncertain_reading_keeps_polling(monkeypatch):
    """Resolving wakes the agent and stops the watch; one more poll is cheaper."""
    import watch
    _decider(monkeypatch, "done", 0.62)
    assert asyncio.run(watch._outcome(_watch(), "probably finished?")) is None


def test_pending_never_resolves_however_certain(monkeypatch):
    import watch
    _decider(monkeypatch, "pending", 0.99)
    assert asyncio.run(watch._outcome(_watch(), "status: queued")) is None


def test_a_regex_match_is_never_second_guessed(monkeypatch):
    """The pattern is exact and free; it must not cost a network hop."""
    import watch
    asked = _decider(monkeypatch, "pending", 0.99)
    got = asyncio.run(watch._outcome(
        _watch(done_re="completed"), "status: completed"))
    assert got == "done" and not asked


def test_the_same_output_is_not_asked_about_twice(monkeypatch):
    """A watch sitting on 'queued' for twenty checks asks once."""
    import watch
    asked = _decider(monkeypatch, "done", 0.99)
    got = asyncio.run(watch._outcome(
        _watch(last_out="status: queued"), "status: queued"))
    assert got is None and not asked


def test_an_unreachable_decider_leaves_the_watch_polling(monkeypatch):
    """Never a false resolve: unreachable means keep going, then expire."""
    import decide
    import watch

    async def unreachable(state, instructions, criteria):
        return None

    monkeypatch.setattr(decide, "choice", unreachable)
    assert asyncio.run(watch._outcome(_watch(), "anything at all")) is None


def test_a_watch_with_no_regex_and_no_goal_is_unchanged(monkeypatch):
    import watch
    asked = _decider(monkeypatch, "done", 0.99)
    assert asyncio.run(watch._outcome(_watch(goal=None), "whatever")) is None
    assert not asked


def test_resolve_when_is_stored_as_the_goal(monkeypatch):
    import db
    import decide
    monkeypatch.setattr(decide, "configured", lambda: True)
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "goal watch", "poll": "gh run view 9",
        "resolve_when": "the deploy finishes, either way"}))
    assert "watching 'goal watch'" in out
    w = [x for x in db.list_watches("active") if x["name"] == "goal watch"][0]
    assert w["goal"] == "the deploy finishes, either way"
    assert not w["done_re"]


def test_a_goal_only_watch_is_refused_when_there_is_no_decider():
    """It would poll to expiry and wake nobody; say so now, not in forty checks."""
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_tool(aid, "~", "watch", {
        "action": "start", "name": "x", "poll": "gh run view 1",
        "resolve_when": "it finishes"}))
    assert "not configured" in out and "done_when" in out


def test_patterns_see_every_line_of_a_multiline_poll():
    import watch
    """gh pr checks prints one line per check; ^ must mean a line, and a
    counted group must be able to span lines (a CI watch on three PRs
    sat unresolved for this, all green)."""
    out = "frontend-tests\tpass\nlaravel-tests\tpass\nfrontend-tests\tpass\nlaravel-tests\tpass\nfrontend-tests\tpass\nlaravel-tests\tpass\n"
    assert watch._by_pattern(out, r"^(laravel-tests\s+(pass|fail).*){3}", None) == "done"
    assert watch._by_pattern(out, r"^laravel-tests\s+pass$", None) == "done"
    assert watch._by_pattern(out, r"^laravel-tests\s+fail", r"^\S+\s+fail$") is None


def test_unmatched_pattern_falls_back_to_the_name_as_goal(monkeypatch):
    import watch
    asked = []

    async def fake_choice(state, instructions, criteria):
        asked.append(state["goal"])
        return ("done", 0.9)

    monkeypatch.setattr(watch.decide, "choice", fake_choice)
    monkeypatch.setattr(watch.runtime_store, "decision_record", lambda *a, **k: None)
    w = {"id": 1, "agent": "devops", "name": "acmeshop CI 476/477/479", "poll": "gh pr checks",
         "done_re": "NEVER-MATCHES", "fail_re": None, "goal": "", "last_out": ""}
    assert asyncio.run(watch._outcome(w, "laravel-tests\tpass\n")) == "done"
    assert asked == ["acmeshop CI 476/477/479"]
    # no name and no goal: nothing to judge by, the pattern alone decides
    asked.clear()
    assert asyncio.run(watch._outcome({**w, "name": ""}, "laravel-tests\tpass\n")) is None
    assert asked == []
