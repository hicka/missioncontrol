"""A refused command becomes a request the owner can answer.

The guard stops a command the pattern list never named. Without somewhere for
that to go, the work simply moves to the owner's own hands and the agent learns
to route around the guard instead of using it.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import db
import decide
import main
import orchestrator
import runtime_store as store
import skills
import tools


@pytest.fixture(autouse=True)
def _clear():
    tools._verdicts.clear()
    yield
    tools._verdicts.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'approve.db')
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(skills, 'USES_PATH', tmp_path / 'skills' / '.uses.json')
    monkeypatch.setattr(main, 'orch', orchestrator.Orchestrator())
    monkeypatch.setattr(main, '_api_token', lambda: 'approve-test-token')
    yield TestClient(main.app, headers={'Authorization': 'Bearer approve-test-token'})
    if db._conn:
        db._conn.close()


def _held(monkeypatch, command="crontab -r", agent="atlas"):
    async def fake_noul(state, instructions, criteria=None):
        return 0.93
    monkeypatch.setattr(decide, "configured", lambda: True)
    monkeypatch.setattr(decide, "noul", fake_noul)
    monkeypatch.setattr(tools, "_guard_on", lambda: True)
    out = asyncio.run(tools.run_terminal(command, "~", "~", agent=agent))
    assert "held for approval" in out
    return store.approvals(status="pending")[0]


def test_the_refusal_records_who_wanted_what(monkeypatch):
    row = _held(monkeypatch)
    assert row["agent"] == "atlas"
    assert row["command"] == "crontab -r"
    assert row["status"] == "pending"


def test_approving_runs_exactly_what_was_judged(monkeypatch, client):
    row = _held(monkeypatch, command="echo approved-and-run")
    r = client.post(f"/api/approvals/{row['id']}/approve")
    assert r.json()["ok"]
    done = store.approval_get(row["id"])
    assert done["status"] == "ran"
    assert "approved-and-run" in done["result"]


def test_denying_runs_nothing(monkeypatch, client):
    row = _held(monkeypatch, command="echo should-not-run")
    assert client.post(f"/api/approvals/{row['id']}/deny").json()["ok"]
    denied = store.approval_get(row["id"])
    assert denied["status"] == "denied" and denied["result"] == ""


def test_a_second_click_does_not_run_it_twice(monkeypatch, client):
    row = _held(monkeypatch, command="echo once")
    assert client.post(f"/api/approvals/{row['id']}/approve").json()["ok"]
    again = client.post(f"/api/approvals/{row['id']}/approve").json()
    assert not again["ok"] and again["error"] == "already decided"


def test_the_owner_cannot_approve_past_the_pattern_list():
    """Approval lifts the judge's refusal, never policy. Otherwise the guard
    becomes a way to launder `sudo` through a click."""
    out = asyncio.run(tools.run_approved(
        {"command": "sudo rm -rf /", "cwd": str(tools.Path.home())}))
    assert "blocked by policy" in out


def test_the_owner_cannot_approve_past_the_self_checkout_guard():
    out = asyncio.run(tools.run_approved(
        {"command": "echo x > server/tools.py", "cwd": tools.SELF_REPO}))
    assert "improve tool" in out


def test_one_request_reads_as_it_stands_now(monkeypatch, client):
    """The chat's card for a held command reads this, so it follows the answer."""
    row = _held(monkeypatch, command="echo card-output")
    waiting = client.get(f"/api/approvals/{row['id']}").json()["approval"]
    assert waiting["status"] == "pending" and waiting["command"] == "echo card-output"
    client.post(f"/api/approvals/{row['id']}/approve")
    ran = client.get(f"/api/approvals/{row['id']}").json()["approval"]
    assert ran["status"] == "ran" and "card-output" in ran["result"]
    assert not client.get("/api/approvals/nope").json()["ok"]


def test_a_secret_in_the_output_never_reaches_the_card(monkeypatch, client):
    import secret_mask
    secret_mask.protect("tok-5f2a9c81e7")
    row = _held(monkeypatch, command="echo tok-5f2a9c81e7")
    client.post(f"/api/approvals/{row['id']}/approve")
    ran = client.get(f"/api/approvals/{row['id']}").json()["approval"]
    assert "tok-5f2a9c81e7" not in ran["result"]


def test_an_unknown_request_is_refused(client):
    assert not client.post("/api/approvals/nope/approve").json()["ok"]


def test_an_unknown_action_is_refused(monkeypatch, client):
    row = _held(monkeypatch)
    assert not client.post(f"/api/approvals/{row['id']}/burn").json()["ok"]
    assert store.approval_get(row["id"])["status"] == "pending"


# ---------- telling three near-identical requests apart ----------

def _ctx(pending_cmd, prior_cmd, prior_status="ran", agent="atlas", cwd="~"):
    pending = {"id": "p", "agent": agent, "command": pending_cmd, "cwd": cwd,
               "status": "pending", "ts": 200.0}
    prior = {"id": "q", "agent": agent, "command": prior_cmd, "cwd": cwd,
             "status": prior_status, "ts": 100.0}
    return main._approval_context(pending, [pending, prior])


def test_an_identical_repeat_says_so():
    got = _ctx("crontab -r", "crontab -r")
    assert got["identical"] is True and got["diff"] == []


def test_a_near_miss_shows_only_what_moved():
    got = _ctx("swift run --names A,B,C", "swift run --names A,B")
    assert got["identical"] is False
    assert any(line.startswith("+") for line in got["diff"])


def test_an_unrelated_command_is_not_offered_as_context():
    assert _ctx("crontab -r", "gh pr list --state open --json number,title") is None


def test_another_agent_is_never_the_context():
    """Whose earlier yes it was matters: one agent's approval is not another's."""
    pending = {"id": "p", "agent": "atlas", "command": "crontab -r", "cwd": "~",
               "status": "pending", "ts": 200.0}
    other = {"id": "q", "agent": "cto", "command": "crontab -r", "cwd": "~",
             "status": "ran", "ts": 100.0}
    assert main._approval_context(pending, [pending, other]) is None


def test_a_still_pending_request_is_not_context():
    """Only a request the owner actually answered can explain this one."""
    assert _ctx("crontab -r", "crontab -r", prior_status="pending") is None


def test_a_prior_denial_is_context_too():
    got = _ctx("crontab -r", "crontab -r", prior_status="denied")
    assert got["status"] == "denied" and got["identical"] is True


def test_context_never_decides_anything(monkeypatch, client):
    """A prior yes must not carry over: the new request still has to be held."""
    first = _held(monkeypatch, command="echo one")
    client.post(f"/api/approvals/{first['id']}/approve")
    second = _held(monkeypatch, command="echo one two")
    assert second["status"] == "pending"
    listed = client.get("/api/approvals").json()["approvals"]
    row = [a for a in listed if a["id"] == second["id"]][0]
    assert row["prior"] and row["prior"]["identical"] is False
