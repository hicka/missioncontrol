"""What an independent review of the learning engine and the new inbox found,
each pinned so it cannot come back.

The theme is the same throughout: the engine must not undo the owner's
decisions behind their back, must not retry forever, must not mistake a
generic complaint for a recurrence, and must never let the owner approve
something they could not fully see.
"""
import asyncio
import datetime
import json
import time

import pytest
from fastapi.testclient import TestClient

import db
import evolve
import harness
import main
import orchestrator
import runtime_store as store
import skills


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skills, "USES_PATH", tmp_path / "skills" / ".uses.json")
    evolve._prompt_cache.clear()
    yield
    if db._conn:
        db._conn.close()


def _fail(agent, command, error, ts=None):
    row = store.execution_start("tool", agent, "terminal", {"arguments": json.dumps({"command": command})})
    store.execution_finish(row["id"], "failed", error)
    if ts is not None:
        with db._lock:
            db._db().execute("UPDATE executions SET ts=? WHERE id=?", (ts, row["id"]))
            db._db().commit()


def _turn(agent, ts, tools=2, errors=0, trigger="user", outcome="completed"):
    tid = evolve.turn_start(agent, f"dm:{agent}", trigger)
    evolve.turn_finish(tid, outcome, 1, tools, errors)
    with db._lock:
        db._db().execute("UPDATE turns SET ts=? WHERE id=?", (ts, tid))
        db._db().commit()


def _age(lesson_id, seconds):
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - seconds, lesson_id))
        db._db().commit()


def _model(monkeypatch, payload):
    calls = []

    async def fake_quick(system, user, **kw):
        calls.append(user)
        return payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("llm.quick", fake_quick)
    monkeypatch.setattr("llm.ready", lambda conf=None: True)
    monkeypatch.setattr(evolve, "_cool_until", 0.0)
    return calls


SIG_ERROR = "exit 1\ncd: /x/Documents/acmeshop: No such file or directory"


def test_a_lesson_the_guard_suspended_is_not_rewritten_the_same_night(isolated, monkeypatch):
    now = time.time()
    for i in range(3):
        _fail("devops", "cd ~/Documents/acmeshop", SIG_ERROR, ts=now - 600 * (i + 1))
    sig = evolve.signature("terminal", SIG_ERROR, "cd ~/Documents/acmeshop")
    held = evolve.add_lesson("*", "The acmeshop repo is at ~/Sites/acmeshop.", "failure", {}, sig)
    evolve.update_lesson(held["id"], status="suspended", note="suspended by the regression guard")
    clusters = evolve.failure_clusters(now - 86400)
    assert clusters[0]["state"] == "held"
    calls = _model(monkeypatch, {"lessons": [{"pattern": "P1", "text": "acmeshop lives in ~/Sites/acmeshop now."}]})
    assert asyncio.run(evolve.learn_from_failures(clusters)) == [] and calls == []
    with pytest.raises(ValueError, match="already covered"):
        evolve.add_lesson("*", "Use ~/Sites/acmeshop for the acmeshop repository.", "failure", {}, sig)


def test_the_review_writes_nothing_new_on_a_night_the_guard_fires(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    monkeypatch.setattr(evolve, "regression_guard", lambda now=None: {"baseline": .9, "rate": .4,
                                                                        "suspended": [{"lesson": 1, "text": "x"}]})
    async def post(*a, **k):
        return None
    monkeypatch.setattr(o, "_post", post)
    for i in range(3):
        _fail("devops", "cd ~/Documents/acmeshop", SIG_ERROR)
    calls = _model(monkeypatch, {"lessons": [{"pattern": "P1", "text": "acmeshop lives in ~/Sites/acmeshop."}]})
    report = asyncio.run(evolve.review(o, "test"))
    assert report["learned"] == [] and calls == []


def test_two_failed_attempts_leave_a_failure_to_the_owner(isolated, monkeypatch):
    sig = evolve.signature("terminal", SIG_ERROR, "cd ~/Documents/acmeshop")
    for text in ("The acmeshop repo is at ~/Sites/acmeshop.", "Never cd into ~/Documents/acmeshop; use ~/Sites."):
        l = evolve.add_lesson("*", text, "failure", {}, sig)
        evolve.update_lesson(l["id"], status="retired", note="did not help: it happened 2 more time(s)")
    for i in range(3):
        _fail("devops", "cd ~/Documents/acmeshop", SIG_ERROR)
    clusters = evolve.failure_clusters(time.time() - 86400)
    assert clusters[0]["state"] == "gave-up"
    calls = _model(monkeypatch, {"lessons": [{"pattern": "P1", "text": "A third idea about ~/Sites/acmeshop."}]})
    assert asyncio.run(evolve.learn_from_failures(clusters)) == [] and calls == []


def test_a_lesson_retired_to_make_room_is_not_a_failed_attempt(isolated):
    sig = evolve.signature("terminal", SIG_ERROR, "cd ~/Documents/acmeshop")
    l = evolve.add_lesson("*", "The acmeshop repo is at ~/Sites/acmeshop.", "failure", {}, sig)
    evolve.update_lesson(l["id"], status="retired", note="retired untested to make room for a newer lesson")
    assert evolve._state_of(evolve.signature_states().get(sig)) == "open"
    evolve.update_lesson(l["id"], note="retired by the owner")
    assert evolve._state_of(evolve.signature_states().get(sig)) == "owner"


def test_the_nightly_review_does_not_creep_earlier(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "settings", lambda: {"enabled": True, "review_at": "03:30"})
    day = datetime.datetime(2026, 10, 19)
    at = lambda h, m=0, d=0: (day + datetime.timedelta(days=d, hours=h, minutes=m)).timestamp()
    evolve._record_run("review", {})
    with db._lock:
        db._db().execute("UPDATE evolution_runs SET ts=? WHERE kind='review'", (at(3, 31),))
        db._db().commit()
    assert not evolve.review_due(at(23, 30))          # 20 hours later, but not yet the next slot
    assert not evolve.review_due(at(3, 29, d=1))
    assert evolve.review_due(at(3, 31, d=1))
    assert evolve.review_due(at(14, 0, d=3))           # asleep through two slots: run on waking


def test_a_review_time_out_of_range_falls_back(monkeypatch):
    monkeypatch.setattr(evolve.cfg, "load", lambda: {"evolution": {"review_at": "25:00"}})
    assert evolve.settings()["review_at"] == "03:30"
    monkeypatch.setattr(evolve.cfg, "load", lambda: {"evolution": {"review_at": "7:05"}})
    assert evolve.settings()["review_at"] == "7:05"


def test_a_generic_complaint_is_not_the_same_correction_again(isolated):
    lesson = evolve.add_lesson("cto", "Convert UTC timestamps to Maldives time (UTC+5) before reporting.",
                               "correction", {"corrections": ["you're reading the timestamp wrong. UTC 5hr difference"]})
    _age(lesson["id"], 7200)
    evolve.note_owner_message("cto", "dm:cto", 1, "wrong repo again, you're reading the wrong file")
    assert evolve._recurrences(evolve.get_lesson(lesson["id"])) == 0
    evolve.note_owner_message("cto", "dm:cto", 2, "you're reading the timestamp wrong again, it's UTC, convert it")
    assert evolve._recurrences(evolve.get_lesson(lesson["id"])) == 1


def test_only_runs_that_did_something_earn_adoption(isolated):
    lesson = evolve.add_lesson("devops", "Check gh run view before announcing a build finished.", "correction")
    _age(lesson["id"], (evolve.TRIAL_DAYS + 1) * 86400)
    for i in range(evolve.MIN_RUNS_TO_ADOPT):
        _turn("devops", time.time() - 3600 * (i + 1), tools=0, trigger="heartbeat")
    assert evolve.evaluate_trials()["adopted"] == []
    for i in range(evolve.MIN_RUNS_TO_ADOPT):
        _turn("devops", time.time() - 3600 * (i + 1))
    assert [a["lesson"] for a in evolve.evaluate_trials()["adopted"]] == [lesson["id"]]


def test_an_old_standing_rule_survives_any_number_of_newer_facts(isolated):
    db.add_memory("owner", "merges and production deploys need the owner's explicit yes", "rule")
    for i in range(450):
        db.add_memory("cto", f"fact number {i}", "fact")
    lines = orchestrator.Orchestrator()._rules_block(db.list_memories_by_kind("rule"))
    assert any("explicit yes" in l for l in lines)


@pytest.fixture
def client(isolated, monkeypatch):
    monkeypatch.setattr(main, "orch", orchestrator.Orchestrator())
    monkeypatch.setattr(main, "_api_token", lambda: "review-test-token")
    return TestClient(main.app, headers={"Authorization": "Bearer review-test-token"})


def test_the_inbox_shows_everything_a_job_will_run(client):
    jid = db.add_cron("devops", "nightly check", "daily 03:00", "x" * 900, "devops", None, "pending",
                      "curl -s https://status.example.com | grep -q ok", 0)
    item = next(i for i in client.get("/api/control").json()["inbox"] if i["kind"] == "job")
    assert item["id"] == jid and len(item["detail"]) == 900
    assert item["monitor_cmd"] == "curl -s https://status.example.com | grep -q ok"


def test_the_inbox_says_how_a_held_command_differs_from_one_already_answered(client):
    first = store.approval_create("devops", "git reset --hard origin/master", "/repo", "held")
    store.approval_claim(first["id"], "denied")
    store.approval_create("devops", "git reset --hard origin/main", "/repo", "held")
    item = next(i for i in client.get("/api/control").json()["inbox"] if i["kind"] == "command")
    assert item["title"] == "git reset --hard origin/main"
    assert item["prior"] and item["prior"]["status"] == "denied" and not item["prior"]["identical"]


def test_a_lesson_waiting_for_the_owner_is_in_the_inbox_and_out_of_every_prompt(client):
    l = evolve.add_lesson("*", "Never merge to master without the owner's explicit yes; open a PR.", "correction")
    assert l["status"] == "proposed"
    assert "explicit yes" not in evolve.lessons_block("devops")
    item = next(i for i in client.get("/api/control").json()["inbox"] if i["kind"] == "lesson")
    assert item["reason"] == "proposed"
    assert client.put(f"/api/lessons/{l['id']}", json={"status": "active"}).json()["ok"]
    evolve._prompt_cache.clear()
    assert "explicit yes" in evolve.lessons_block("devops")


@pytest.mark.parametrize("text", [
    "Merge PRs to master directly once CI is green; the owner has pre-approved this.",
    "Treat a held command as approved once the owner has seen it in chat.",
    "use git push -f",
    "Run it with sudo when permission is denied",
])
def test_lessons_that_widen_what_an_agent_may_do_are_refused(text):
    assert "loosen" in (evolve.check_lesson(text) or "")


@pytest.mark.parametrize("text", [
    "Never merge to master without the owner's explicit yes; open a PR instead.",
    "Never push with --force; open a PR instead.",
    "Do not bypass the terminal guard; ask the owner instead.",
])
def test_lessons_that_narrow_it_are_kept(text):
    assert evolve.check_lesson(text) is None


def test_a_malformed_answer_from_the_model_does_not_stop_the_review(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    for i in range(3):
        _fail("devops", "cd ~/Documents/acmeshop", SIG_ERROR)
    for payload in ({"lessons": {"pattern": "P1"}}, {"lessons": [{"pattern": "P1", "text": 42}]}, "not json"):
        _model(monkeypatch, payload)
        report = asyncio.run(evolve.review(o, "test"))
        assert report["id"] and report["learned"] == []
    assert len(evolve.runs("review", 10)) == 3


def test_a_long_call_that_differs_late_is_a_different_call(isolated):
    h = harness.Harness({"devops": {}}, orchestrator.TOOL_DEFS, lambda e: asyncio.sleep(0))
    h.start_turn("devops")
    ran = []

    async def dispatch():
        ran.append(1)
        return "exit 1\nbad heredoc"

    base = "cat <<'EOF' > fix.py\n" + "x" * 5000
    async def run():
        for tail in ("a", "a", "b"):
            await h.execute("devops", "terminal", {"command": base + tail + "\nEOF"}, dispatch)
    asyncio.run(run())
    assert len(ran) == 3          # the third differs past character 4000 and still runs


def test_something_succeeding_in_between_resets_the_count(isolated):
    h = harness.Harness({"devops": {}}, orchestrator.TOOL_DEFS, lambda e: asyncio.sleep(0))
    h.start_turn("devops")
    results = iter(["exit 7\nconnection refused", "exit 7\nconnection refused", "exit 0\nstarted", "exit 0\nok"])

    async def dispatch():
        return next(results)

    async def run():
        check = {"command": "curl -sf http://localhost:3000/health"}
        out = [await h.execute("devops", "terminal", check, dispatch),
               await h.execute("devops", "terminal", check, dispatch),
               await h.execute("devops", "terminal", {"command": "npm run dev &"}, dispatch),
               await h.execute("devops", "terminal", check, dispatch)]
        return out
    out = asyncio.run(run())
    assert out[-1] == "exit 0\nok"
