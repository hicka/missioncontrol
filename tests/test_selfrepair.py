"""Self-repair: Mission Control noticing its own recurring problems and fixing
them through the same worktree, review and tests as any proposal.

The case it was built for: webdev's replies were cut off at the output limit
turn after turn, and no lesson in a prompt could have raised max_tokens.
"""
import asyncio
from types import SimpleNamespace
import subprocess
import time
from pathlib import Path

import pytest

import db
import evolve
import selfimprove as si
import settings


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "repair.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.yaml")
    monkeypatch.setattr(si, "STARTED", time.time() - 3600)
    yield
    if db._conn:
        db._conn.close()


def _turn(agent="webdev", outcome="waiting", flags=("cut_off",), error="", ago=60):
    tid = evolve.turn_start(agent, f"dm:{agent}", "user")
    evolve.turn_finish(tid, outcome, rounds=3, flags=flags, error=error)
    with db._lock:
        db._db().execute("UPDATE turns SET ts=? WHERE id=?", (time.time() - ago, tid))
        db._db().commit()
    return tid


def _on(monkeypatch, **over):
    s = {"enabled": True, "autofix": True, "auto_apply": False, "push": False, **over}
    monkeypatch.setattr(si, "settings", lambda: s)
    monkeypatch.setattr(si, "readiness", lambda: None)
    return s


class _Orch:
    def __init__(self, busy=()):
        self.agents = {"cto": {}}
        self.posts = []
        self.workers = SimpleNamespace(running=lambda: list(busy))

    def agent_llm(self, aid):
        return None

    async def _post(self, thread, sender, text, kind="chat", meta=None):
        self.posts.append(text)


# ---------- seeing the problem ----------

def test_replies_cut_off_again_and_again_are_a_problem_to_fix(isolated):
    for _ in range(3):
        _turn()
    found = si.detect()
    assert [i["signature"] for i in found] == ["cut_off"]
    issue = found[0]
    assert issue["count"] == 3 and issue["agents"] == {"webdev": 3}
    # the evidence says what each agent actually sent, so the cause can be found
    assert "max_tokens" in issue["evidence"] and "webdev:" in issue["evidence"]


def test_once_or_twice_is_bad_luck(isolated):
    _turn()
    _turn()
    assert si.detect() == []


def test_only_what_happened_on_the_code_running_now_counts(isolated, monkeypatch):
    """A problem from before the last restart may be what that restart fixed:
    today's max_tokens fix would otherwise have been fixed all over again."""
    for _ in range(3):
        _turn(ago=600)
    monkeypatch.setattr(si, "STARTED", time.time() - 300)
    assert si.detect() == []


def test_turns_crashing_the_same_way_are_one_problem(isolated):
    for n in range(3):
        _turn(outcome="failed", flags=(), error=f"KeyError: 'thread' at step {n}")
    found = si.detect()
    assert len(found) == 1 and found[0]["signature"].startswith("failed:KeyError")
    assert "KeyError" in found[0]["evidence"]


def test_a_provider_outage_is_not_a_bug_in_the_code(isolated):
    """reposteward failed fifteen turns on a used-up MiniMax plan; no change to
    this code would have stopped that."""
    for _ in range(4):
        _turn(outcome="failed", flags=(), error='LLM HTTP 429: {"message":"Token Plan usage limit reached"}')
    for _ in range(4):
        _turn(agent="cto", outcome="failed", flags=(), error="LLM HTTP 503: overloaded")
    assert si.detect() == []


def test_after_an_attempt_only_what_happened_since_counts(isolated):
    for _ in range(3):
        _turn(ago=120)
    db.add_improvement(si.AUTO, "fix", "e", "improve/x", "", {"issue": "cut_off"})
    assert si.detect() == []
    for _ in range(3):
        _turn(ago=0)
    assert si.detect()[0]["count"] == 3


def test_two_fixes_that_did_not_stop_it_make_it_the_owners(isolated):
    for _ in range(2):
        iid = db.add_improvement(si.AUTO, "fix", "e", "improve/x", "", {"issue": "cut_off"})
        db.update_improvement(iid, status="pushed")
    for _ in range(3):
        _turn(ago=-5)
    assert si.issues()[0]["state"] == "gave_up"


# ---------- acting on it ----------

def test_off_unless_the_owner_turns_it_on(isolated, monkeypatch):
    for _ in range(3):
        _turn()
    monkeypatch.setattr(si, "fix", lambda *a: (_ for _ in ()).throw(AssertionError("must not fix")))
    assert si.settings()["autofix"] is False and si.settings()["auto_apply"] is False
    assert si.settings()["push"] is False
    assert asyncio.run(si.tick(_Orch())) == "off"


def test_a_tested_fix_waits_for_the_team_to_go_idle(isolated, monkeypatch):
    """Landing restarts the app, which would kill an agent mid-turn."""
    _on(monkeypatch, auto_apply=True)
    db.add_improvement(si.AUTO, "fix", "e", "improve/x", "/tmp/x", {"issue": "cut_off"})
    landed = []

    async def land(iid, *a):
        landed.append(iid)
        return {"ok": True}

    monkeypatch.setattr(si, "land", land)

    async def busy():
        return await si.tick(_Orch(busy=("webdev",)))

    assert "webdev" in asyncio.run(busy())
    assert landed == []
    assert asyncio.run(si.tick(_Orch())) == "landed, restarting"
    assert len(landed) == 1


def test_an_agents_own_proposal_never_lands_on_its_own(isolated, monkeypatch):
    """An agent can be talked into things by what it reads; only fixes the app
    found itself skip the owner."""
    _on(monkeypatch, auto_apply=True)
    db.add_improvement("webdev", "new feature", "r", "improve/y", "/tmp/y", {"files": ["a"]})
    monkeypatch.setattr(si, "land", lambda *a: (_ for _ in ()).throw(AssertionError("must not land")))
    assert asyncio.run(si.tick(_Orch())) == "nothing to fix"


def test_a_fix_is_written_reviewed_and_tested_before_anyone_sees_it(isolated, monkeypatch, tmp_path):
    import tools
    _on(monkeypatch)
    for _ in range(3):
        _turn()
    briefs = []

    async def claude(task, repo, cwd, progress=None):
        briefs.append(task)
        return "The thinking used up max_tokens. Raised the floor for thinking models."

    monkeypatch.setattr(tools, "claude_code", claude)
    monkeypatch.setattr(si, "make_worktree", lambda t: ("improve/fix-1", tmp_path))
    monkeypatch.setattr(si, "review", lambda b, p, m: {"files": ["server/llm.py"], "added": 9,
                                                      "removed": 2, "branch": b, "problems": []})

    async def green(path):
        return True, "605 passed"

    monkeypatch.setattr(si, "run_tests", green)
    o = _Orch()
    assert "Replies cut off" in asyncio.run(si.tick(o))
    row = db.list_improvements()[0]
    assert row["agent"] == si.AUTO and row["status"] == "pending"
    assert row["stat"]["issue"] == "cut_off" and row["stat"]["files"] == ["server/llm.py"]
    assert "max_tokens" in briefs[0] and "server/tools.py" in briefs[0]
    assert o.posts and "waiting on you" in o.posts[0]


def test_a_red_fix_is_discarded_with_the_reason(isolated, monkeypatch, tmp_path):
    import tools
    _on(monkeypatch)
    for _ in range(3):
        _turn()

    async def claude(*a, **k):
        return "changed it"

    async def red(path):
        return False, "1 failed, 604 passed"

    monkeypatch.setattr(tools, "claude_code", claude)
    monkeypatch.setattr(si, "make_worktree", lambda t: ("improve/fix-2", tmp_path))
    monkeypatch.setattr(si, "drop_worktree", lambda *a: None)
    monkeypatch.setattr(si, "review", lambda b, p, m: {"files": ["x"], "added": 1, "removed": 0,
                                                      "branch": b, "problems": []})
    monkeypatch.setattr(si, "run_tests", red)
    asyncio.run(si.tick(_Orch()))
    row = db.list_improvements()[0]
    assert row["status"] == "discarded" and "1 failed" in row["note"]


# ---------- after the restart ----------

def test_a_restart_that_cut_a_fix_short_is_cleaned_up(isolated, monkeypatch):
    iid = db.add_improvement(si.AUTO, "fix", "e", "improve/x", "/tmp/x", {"issue": "cut_off"})
    db.update_improvement(iid, status="fixing")
    monkeypatch.setattr(si, "drop_worktree", lambda *a: None)
    asyncio.run(si.after_restart(_Orch()))
    assert db.get_improvement(iid)["status"] == "discarded"


def test_a_landed_change_is_pushed_once_the_app_is_back_on_it(isolated, monkeypatch):
    iid = db.add_improvement(si.AUTO, "fix", "e", "improve/x", "", {"issue": "cut_off"})
    db.update_improvement(iid, status="applied", stat={"issue": "cut_off", "from": "aaaa1111",
                                                       "to": "bbbb2222", "push": "pending"})
    monkeypatch.setattr(si, "_git", lambda *a, **k: (0, "bbbb2222cafe"))
    monkeypatch.setattr(si, "push_master", lambda: (True, "pushed to origin/master"))

    async def answers():
        return True

    monkeypatch.setattr(si, "_answers", answers)
    o = _Orch()
    asyncio.run(si.after_restart(o))
    row = db.get_improvement(iid)
    assert row["status"] == "pushed" and row["stat"]["push"] == "done"
    assert "pushed" in o.posts[0]


def test_a_rolled_back_change_is_never_pushed(isolated, monkeypatch):
    iid = db.add_improvement(si.AUTO, "fix", "e", "improve/x", "", {"issue": "cut_off"})
    db.update_improvement(iid, status="applied", stat={"issue": "cut_off", "from": "aaaa1111",
                                                       "to": "bbbb2222", "push": "pending"})
    monkeypatch.setattr(si, "_git", lambda *a, **k: (0, "aaaa1111cafe"))   # the watchdog put it back
    monkeypatch.setattr(si, "push_master", lambda: (_ for _ in ()).throw(AssertionError("no push")))
    asyncio.run(si.after_restart(_Orch()))
    assert db.get_improvement(iid)["status"] == "rolled_back"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_the_push_is_a_fast_forward_or_nothing(tmp_path, monkeypatch):
    """Today GitHub had a merged PR this Mac did not; a forced push would have
    thrown it away."""
    remote, here, other = tmp_path / "remote.git", tmp_path / "here", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True)
    for d in (here, other):
        subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
    (here / "a").write_text("1")
    _git(here, "add", "a")
    _git(here, "commit", "-qm", "first")
    _git(here, "push", "-q", "-u", "origin", "master")
    monkeypatch.setattr(si, "REPO", here)

    (here / "a").write_text("2")
    _git(here, "commit", "-qam", "landed fix")
    ok, out = si.push_master()
    assert ok, out

    _git(other, "pull", "-q", "origin", "master")
    (other / "b").write_text("pr")
    _git(other, "add", "b")
    _git(other, "commit", "-qm", "merged on GitHub")
    _git(other, "push", "-q", "origin", "master")
    (here / "a").write_text("3")
    _git(here, "commit", "-qam", "another fix")
    ok, out = si.push_master()
    assert not ok and "by hand" in out
