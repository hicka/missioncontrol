"""Guardrails on agents changing Mission Control itself.

The whole feature rests on an agent being unable to edit what restrains it,
so these tests are mostly about what is refused.
"""
import asyncio
import subprocess
from pathlib import Path

import selfimprove as si


def test_the_leash_is_protected_from_the_thing_it_leashes():
    """An agent that could edit these could remove its own limits."""
    for p in ("server/selfimprove.py", "server/tools.py", "server/budget.py",
              "server/ledger.py", ".github/workflows/test.yml", "requirements.txt"):
        assert si.protected_hits([p]) == [p], p


def test_secrets_and_git_internals_are_protected_by_shape_not_by_name():
    for p in (".env", "server/.env", "secrets/asc.p8", "keys/private.pem",
              ".git/config", "certs/id.key"):
        assert si.protected_hits([p]) == [p], p


def test_ordinary_app_code_is_fair_game():
    for p in ("server/commands.py", "server/llm.py", "web/app.js",
              "server/skills.py", "tests/test_db.py", "README.md"):
        assert si.protected_hits([p]) == []


def test_every_file_holding_a_gate_is_off_limits():
    for p in ("server/orchestrator.py", "server/github.py", "server/watch.py", "server/cron.py", "server/session.py",
              "server/recall.py", "server/runtime_store.py", "server/router.py", "server/workers.py",
              "server/main.py", "server/secret_mask.py", "server/decide.py"):
        assert si.protected_hits([p]) == [p]


def test_off_by_default():
    import settings as cfg
    assert not (cfg.load().get("selfimprove") or {}).get("enabled", False)
    assert si.readiness() is not None


def test_readiness_explains_each_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "enabled", lambda: False)
    assert "off" in si.readiness()

    monkeypatch.setattr(si, "enabled", lambda: True)
    monkeypatch.setattr(si, "REPO", tmp_path)            # no .git
    assert "git checkout" in si.readiness()

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(si, "_git", lambda *a, **k: (0, " M server/main.py"))
    assert "uncommitted" in si.readiness()

    monkeypatch.setattr(si, "_git", lambda *a, **k: (0, ""))
    assert si.readiness() is None


def _review(monkeypatch, files, added=10, removed=2):
    monkeypatch.setattr(si, "_git", lambda *a, **k: (0, ""))
    monkeypatch.setattr(si, "_diff_stat",
                        lambda b, c: {"files": files, "added": added, "removed": removed})
    return si.review("improve/x", Path("/tmp/nope"))


def test_a_proposal_touching_the_blocklist_is_refused(monkeypatch):
    r = _review(monkeypatch, ["server/orchestrator.py", "server/tools.py"])
    assert any("protected" in p for p in r["problems"])
    assert "server/tools.py" in r["problems"][0]


def test_an_empty_change_is_refused(monkeypatch):
    r = _review(monkeypatch, [], 0, 0)
    assert any("empty" in p for p in r["problems"])


def test_a_sprawling_change_is_refused(monkeypatch):
    r = _review(monkeypatch, [f"web/f{i}.js" for i in range(si.MAX_FILES + 1)])
    assert any("spans" in p for p in r["problems"])


def test_a_change_too_big_to_read_is_refused(monkeypatch):
    r = _review(monkeypatch, ["server/commands.py"], si.MAX_LINES, 1)
    assert any("over the" in p and "line limit" in p for p in r["problems"])


def test_a_small_honest_change_passes_review(monkeypatch):
    r = _review(monkeypatch, ["server/commands.py", "tests/test_x.py"], 30, 4)
    assert r["problems"] == []
    assert r["branch"] == "improve/x"


# ---------- applying ----------

def test_apply_refuses_when_the_repo_is_not_ready(monkeypatch):
    monkeypatch.setattr(si, "readiness", lambda: "the working tree has uncommitted changes")
    out = asyncio.run(si.apply("improve/x", None))
    assert not out["ok"] and "uncommitted" in out["error"]


def test_apply_refuses_a_branch_whose_tests_now_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "readiness", lambda: None)

    async def red(_root):
        return False, "1 failed"

    monkeypatch.setattr(si, "run_tests", red)
    out = asyncio.run(si.apply("improve/x", str(tmp_path)))
    assert not out["ok"] and "tests fail" in out["error"]


def test_apply_never_merge_commits_past_a_conflict(monkeypatch, tmp_path):
    """--ff-only or nothing: a merge commit made by an agent's proposal is a
    change the owner never read."""
    monkeypatch.setattr(si, "readiness", lambda: None)

    async def green(_root):
        return True, "ok"

    monkeypatch.setattr(si, "run_tests", green)
    calls = []

    def fake_git(*args, **kw):
        calls.append(args)
        if args[0] == "rev-parse":
            return 0, "abc1234"
        if args[0] == "merge":
            return 1, "fatal: Not possible to fast-forward"
        return 0, ""

    monkeypatch.setattr(si, "_git", fake_git)
    out = asyncio.run(si.apply("improve/x", str(tmp_path)))
    assert not out["ok"] and "fast-forward" in out["error"]
    assert ("merge", "--ff-only", "improve/x") in calls


def test_a_successful_apply_arms_the_rollback_watchdog(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "readiness", lambda: None)

    async def green(_root):
        return True, "ok"

    monkeypatch.setattr(si, "run_tests", green)
    seq = iter(["oldsha1234", "newsha5678"])
    monkeypatch.setattr(si, "_git",
                        lambda *a, **k: (0, next(seq)) if a[0] == "rev-parse" else (0, ""))
    armed = {}
    monkeypatch.setattr(si, "_arm_watchdog",
                        lambda prev, port: armed.update(prev=prev, port=port) or Path("/tmp/w.log"))
    out = asyncio.run(si.apply("improve/x", str(tmp_path)))
    assert out["ok"] and out["from"] == "oldsha12" and out["to"] == "newsha56"
    assert armed["prev"] == "oldsha1234"


def test_the_watchdog_rolls_back_to_the_commit_before_the_merge():
    script = si.WATCHDOG.format(tries=10, port=8899, repo="/repo",
                                prev="deadbeef", log="/tmp/l")
    assert "git reset --hard deadbeef" in script
    assert "launchctl kickstart" in script
    assert "/api/state" in script          # health is "the app answers", not "the process exists"


# ---------- the agent-facing tool ----------

def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_the_tool_refuses_before_touching_git_when_disabled(monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    monkeypatch.setattr(si, "readiness", lambda: "self-improvement is off")

    def boom(*a, **k):
        raise AssertionError("must not create a worktree when refused")

    monkeypatch.setattr(si, "make_worktree", boom)
    out = asyncio.run(o._exec_improve(aid, {
        "action": "propose", "title": "t", "rationale": "r", "instructions": "i"}))
    assert "self-improvement is off" in out


def test_one_pending_proposal_per_agent(monkeypatch):
    import db
    o = _orch()
    aid = list(o.agents)[0]
    monkeypatch.setattr(si, "readiness", lambda: None)
    db.add_improvement(aid, "already open", "r", "improve/a", "/tmp/a", {})
    monkeypatch.setattr(si, "make_worktree",
                        lambda t: (_ for _ in ()).throw(AssertionError("should not run")))
    out = asyncio.run(o._exec_improve(aid, {
        "action": "propose", "title": "t", "rationale": "r", "instructions": "i"}))
    assert "one at a time" in out


def test_propose_needs_all_three_fields(monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_improve(aid, {"action": "propose", "title": "t"}))
    assert "required" in out


def test_the_brief_handed_to_claude_names_the_off_limits_files():
    """The model gets told, as well as being stopped — a refusal after the work
    is done wastes a run."""
    import inspect
    import orchestrator
    src = inspect.getsource(orchestrator.Orchestrator._exec_improve)
    for p in ("server/tools.py", "server/budget.py", "server/ledger.py",
              "server/selfimprove.py"):
        assert p in src


def test_agents_are_told_the_live_state_instead_of_guessing(monkeypatch):
    """The CTO once announced "self-improve is off" without checking anything.
    Whether it is on is a fact about the system, so it belongs in CURRENT STATE
    where the prompt already says the live answer lives."""
    o = _orch()

    monkeypatch.setattr(si, "readiness", lambda: None)
    on = asyncio.run(o._current_state_block())
    assert "self-improvement: ON" in on
    assert "improve tool" in on

    monkeypatch.setattr(si, "readiness", lambda: "the working tree has uncommitted changes")
    off = asyncio.run(o._current_state_block())
    assert "self-improvement: unavailable" in off
    assert "uncommitted changes" in off      # and why, so the agent can say what to do


def test_current_state_survives_selfimprove_blowing_up(monkeypatch):
    """A broken check must not cost every agent its whole state block."""
    o = _orch()

    def boom():
        raise RuntimeError("git exploded")

    monkeypatch.setattr(si, "readiness", boom)
    block = asyncio.run(o._current_state_block())
    assert block.startswith("CURRENT STATE")


def test_the_tool_lets_a_teammate_approve_but_not_the_author(monkeypatch):
    import db
    o = _orch()
    author, other = list(o.agents)[:2]
    s = {**si.settings(), "peer_approve": True}
    monkeypatch.setattr(si, "settings", lambda: s)
    iid = db.add_improvement(author, "clearer errors", "r", "improve/c", "/tmp/c", {"files": ["a"]})
    listed = asyncio.run(o._exec_improve(other, {"action": "list"}))
    assert "branch improve/c, worktree /tmp/c" in listed     # where a teammate tests or reads it
    assert "author" in asyncio.run(o._exec_improve(author, {"action": "approve", "id": iid}))
    assert asyncio.run(o._exec_improve(other, {"action": "approve", "id": iid})).startswith("approved")
    assert db.get_improvement(iid)["status"] == "approved"
    assert "id" in asyncio.run(o._exec_improve(other, {"action": "diff"}))
