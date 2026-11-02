"""The second gate on the terminal: what the pattern list could not name.

The regex list is the floor. These cover the properties that make it safe to put
a probabilistic judgement behind it: it can only add a refusal, it never
overrides a pattern, and it fails open rather than blocking the team when it
cannot be reached.
"""
import asyncio

import pytest

import decide
import runtime_store as store
import tools


@pytest.fixture(autouse=True)
def _clear_cache():
    """Verdicts and held requests both carry between tests otherwise, and a
    leftover pending request would silently absorb the next one."""
    def wipe():
        tools._verdicts.clear()
        import db
        store._ensure()
        db._db().execute("DELETE FROM approvals")
        db._db().commit()
    wipe()
    yield
    wipe()


def _judge(monkeypatch, probability, on=True):
    asked = []

    async def fake_noul(state, instructions, criteria=None):
        asked.append(state)
        return probability

    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "noul", fake_noul)
    monkeypatch.setattr(tools, "_guard_on", lambda: on)
    return asked


def _run(cmd, cwd="~/project"):
    return asyncio.run(tools.run_terminal(cmd, cwd, cwd))


def test_a_command_the_patterns_missed_is_held_for_the_owner(monkeypatch):
    """`crontab -r` wipes every scheduled job and matches nothing in BLOCKED."""
    assert tools._blocked("crontab -r") is None
    _judge(monkeypatch, 0.93)
    out = _run("crontab -r")
    assert "held for approval" in out
    row = store.approvals(status="pending")[0]
    assert row["command"] == "crontab -r" and row["id"] in out


def test_routine_cleanup_is_not_refused(monkeypatch):
    _judge(monkeypatch, 0.61)
    out = _run("echo pretending to clean")
    assert "held for approval" not in out and "exit 0" in out


def test_an_unreachable_judge_does_not_block_the_team(monkeypatch):
    """A third-party outage must not stop an agent running its tests."""
    _judge(monkeypatch, None)
    assert "exit 0" in _run("echo hello")


def test_the_guard_can_be_switched_off(monkeypatch):
    asked = _judge(monkeypatch, 0.99, on=False)
    assert "exit 0" in _run("echo hello")
    assert not asked


def test_a_pattern_match_is_never_softened_by_the_judge(monkeypatch):
    """The judge can add a refusal; it can never lift one."""
    asked = _judge(monkeypatch, 0.01)
    out = _run("sudo rm -rf /")
    assert "blocked by policy" in out
    assert not asked          # never even consulted


def test_the_self_repo_guard_still_comes_first(monkeypatch):
    asked = _judge(monkeypatch, 0.01)
    out = asyncio.run(tools.run_terminal(
        "echo x > server/tools.py", tools.SELF_REPO, tools.SELF_REPO))
    assert "improve tool" in out
    assert not asked


def test_the_same_command_is_judged_once(monkeypatch):
    asked = _judge(monkeypatch, 0.05)
    _run("echo repeated")
    _run("echo repeated")
    _run("echo repeated")
    assert len(asked) == 1


def test_an_unreachable_judgement_is_not_cached(monkeypatch):
    """Otherwise one outage would disable the gate until restart."""
    asked = _judge(monkeypatch, None)
    _run("echo flaky")
    _run("echo flaky")
    assert len(asked) == 2


def test_the_same_command_elsewhere_is_judged_again(monkeypatch, tmp_path):
    """Where it runs changes what it touches."""
    asked = _judge(monkeypatch, 0.05)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    _run("echo x", cwd=str(one))
    _run("echo x", cwd=str(two))
    assert len(asked) == 2


def test_the_judgement_is_recorded_for_retuning(monkeypatch):
    _judge(monkeypatch, 0.88)
    _run("crontab -r")
    row = store.decisions(kind="terminal_guard")[0]
    assert row["acted"] == 1
    assert row["detail"]["probability"] == 0.88
    assert row["detail"]["threshold"] == tools.GUARD_P


# ---------- what the owner actually asked for ----------

def test_the_owners_own_words_reach_the_judge(monkeypatch):
    """A merge the owner asked for nine seconds ago is not an unsupervised act."""
    seen = {}

    async def fake_noul(state, instructions, criteria=None):
        seen.update(state)
        return 0.2

    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "noul", fake_noul)
    monkeypatch.setattr(tools, "_guard_on", lambda: True)
    asyncio.run(tools.run_terminal("echo x", "~", "~", agent="devops",
                                   intent="merge and deploy #397 on acmeshop"))
    assert seen["owner_instructions"] == "merge and deploy #397 on acmeshop"


def test_the_same_command_is_rejudged_once_the_owner_has_asked(monkeypatch):
    """Otherwise a refusal cached before the request outlives the request."""
    asked = _judge(monkeypatch, 0.9)
    asyncio.run(tools._judged("gh pr merge 397", "/tmp", "devops", ""))
    asyncio.run(tools._judged("gh pr merge 397", "/tmp", "devops", "merge 397 please"))
    assert len(asked) == 2


def test_a_second_request_for_the_same_intention_is_not_created(monkeypatch):
    """The agent edits the command and retries before the owner has answered."""
    _judge(monkeypatch, 0.9)
    first = _run("gh pr merge 397 -R o/r --squash --delete-branch")
    assert "held for approval" in first
    again = _run("gh pr merge 397 -R o/r --squash --delete-branch --yes")
    assert "still held for approval" in again
    assert len(store.approvals(status="pending")) == 1


def test_a_genuinely_different_command_still_gets_its_own_request(monkeypatch):
    _judge(monkeypatch, 0.9)
    _run("gh pr merge 397 -R o/r --squash")
    _run("crontab -r")
    assert len(store.approvals(status="pending")) == 2


def test_another_agents_pending_request_is_not_borrowed(monkeypatch):
    _judge(monkeypatch, 0.9)
    asyncio.run(tools.run_terminal("crontab -r", "~", "~", agent="devops"))
    out = asyncio.run(tools.run_terminal("crontab -r", "~", "~", agent="ops"))
    assert "held for approval" in out and "still held" not in out
    assert len(store.approvals(status="pending")) == 2
