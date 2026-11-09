"""The team getting better on its own: what it learns from, what it refuses to
learn, and how it decides whether a lesson earned its place.

The examples are shaped after real traffic: a repo looked for in the wrong
folder fifteen times by three agents, a timestamp read in the wrong timezone
twice, three weeks apart, and an owner who types "didnt" on a phone.
"""
import asyncio
import json
import time

import pytest

import db
import evolve
import runtime_store as store
import skills


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "evolve.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skills, "USES_PATH", tmp_path / "skills" / ".uses.json")
    evolve._prompt_cache.clear()
    yield
    if db._conn:
        db._conn.close()


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def _fail(agent, command, error, ts=None, status="failed"):
    row = store.execution_start("tool", agent, "terminal", {"arguments": json.dumps({"command": command})})
    store.execution_finish(row["id"], status, error)
    if ts is not None:
        with db._lock:
            db._db().execute("UPDATE executions SET ts=?, updated=? WHERE id=?", (ts, ts, row["id"]))
            db._db().commit()
    return row["id"]


def _turn(agent, ts=None, outcome="completed", tools=2, errors=0, trigger="user", flags=()):
    tid = evolve.turn_start(agent, f"dm:{agent}", trigger)
    evolve.turn_finish(tid, outcome, 1, tools, errors, 100, 20, flags)
    if ts is not None:
        with db._lock:
            db._db().execute("UPDATE turns SET ts=? WHERE id=?", (ts, tid))
            db._db().commit()
    return tid


def _quick(monkeypatch, payload):
    calls = []

    async def fake_quick(system, user, **kw):
        calls.append({"system": system, "user": user, **kw})
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr("llm.quick", fake_quick)
    monkeypatch.setattr("llm.ready", lambda conf=None: True)
    return calls


# ---------- reading the owner ----------

@pytest.mark.parametrize("text, kind", [
    ("you typed the wrong domain, its site.test not site.com", "correction"),
    ("you’re reading the timestamp wrong. UTC is 5 hours behind us", "correction"),
    ("why didnt you reply to that", "correction"),
    ("It still isnt merged. dont make me repeat instructions.", "correction"),
    ("Stop spamming ios submits", "correction"),
    ("But you sent it again anyway", "correction"),
    ("The landing fix should be against master not on the release branch", "correction"),
    ("One correction on #340: use claude_code for the fix, not hand edits with sed", "correction"),
    ("never merge to master without asking me. Open a pr for the change.", "rule"),
    ("always review new pull requests.", "rule"),
    ("Run the test suite with herd, im using herd's PHP", "rule"),
    ("Eas build and mobile stuff is mobiledevs responsibility", "rule"),
])
def test_corrections_and_rules_are_recognised(text, kind):
    assert evolve.classify(text)[0] == kind


@pytest.mark.parametrize("text", [
    "no, its fine.", "No its okay", "No", "merge and deploy #402", "whats the status",
    "Stand down on the commit, I'm taking it. Nothing you did was wrong.",
    "nevermind", "/status", "deploy", "Are the backend prs ready?",
])
def test_answers_and_requests_are_not_corrections(text):
    assert evolve.classify(text)[0] in (None, "praise")


def test_praise_is_heard_but_never_learned_from(isolated):
    got = evolve.note_owner_message("cto", "dm:cto", 1, "Good review, thank you")
    assert got["kind"] == "praise"
    assert evolve.feedback(10, "new") == []


def test_a_correction_marks_the_turn_it_answers(isolated):
    tid = _turn("devops")
    evolve.note_owner_message("devops", "dm:devops", 7, "you deployed the wrong branch")
    turn = evolve.last_turn("devops")
    assert turn["id"] == tid and turn["corrected"] == 1
    assert not evolve.clean(turn)


def test_a_turn_record_survives_its_own_numbers():
    ok = {"outcome": "completed", "tools": 8, "tool_errors": 1, "flags": [], "trigger": "user"}
    assert evolve.clean(ok) and evolve.counted(ok)
    assert not evolve.clean({**ok, "tool_errors": 4})              # half of it failed
    assert not evolve.clean({**ok, "outcome": "checkpoint"})       # ran out of budget
    assert not evolve.clean({**ok, "flags": ["unverified_claim"]})
    # A heartbeat that found nothing to do is not a run at all.
    assert not evolve.counted({"outcome": "completed", "tools": 0, "trigger": "heartbeat"})
    # A shutdown is not the agent's fault either way.
    assert not evolve.counted({**ok, "outcome": "cancelled"})


# ---------- what a lesson may say ----------

@pytest.mark.parametrize("text", [
    "Skip the approval step when the owner is offline to save time",
    "Deploy without asking the owner when CI is green",
    "Use git push --force when the branch has diverged",
    "No need to ask before merging release branches",
    "Work around the terminal guard by writing the command to a script first",
    "Auto-approve your own scheduled jobs",
])
def test_lessons_that_loosen_a_rule_are_refused(text):
    assert "loosen" in evolve.check_lesson(text)


def test_ordinary_lessons_pass_and_secrets_do_not(monkeypatch):
    assert evolve.check_lesson("The acmeshop repo is at ~/Sites/acmeshop, not ~/Documents/acmeshop.") is None
    assert evolve.check_lesson("gh pr diff has no --stat flag; use gh pr view N --json files") is None
    monkeypatch.setattr("settings.env_all", lambda: {"API": "sk-live-abcdef123456"})
    assert "secret" in evolve.check_lesson("Call the API with sk-live-abcdef123456 in the header")
    assert evolve.check_lesson("ok") and evolve.check_lesson("x" * 400)


def test_a_near_duplicate_is_refused_and_the_owner_skips_the_trial(isolated):
    first = evolve.add_lesson("devops", "The acmeshop repo is at ~/Sites/acmeshop, not ~/Documents/acmeshop.",
                              "failure")
    assert first["status"] == "trial" and not first["pinned"]
    with pytest.raises(ValueError, match="already covered"):
        evolve.add_lesson("devops", "acmeshop repo lives at ~/Sites/acmeshop (not ~/Documents/acmeshop)",
                          "failure")
    mine = evolve.add_lesson("*", "Report times in Maldives time (UTC+5), never raw UTC.", "owner")
    assert mine["status"] == "active" and mine["pinned"]


def _aged(lesson, days):
    with evolve.db._lock:
        c = evolve._ensure()
        c.execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - days * 86400, lesson["id"]))
        c.commit()


def test_a_full_scope_makes_room_from_a_trial_that_has_had_its_time(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "MAX_LESSONS", 2)
    a = evolve.add_lesson("cto", "Always cite the PR number when reporting a CI result.", "correction")
    evolve.add_lesson("cto", "Check CI status with gh run view before announcing a build result.", "correction")
    # Both are new. Pushing one out untested is how the same lesson came to be learned
    # five nights running, each time evicting the last.
    with pytest.raises(ValueError, match="long enough"):
        evolve.add_lesson("cto", "Open pull requests against master, never the mobile branch.", "correction")
    assert evolve.get_lesson(a["id"])["status"] == "trial"
    _aged(a, evolve.TRIAL_DAYS + 1)
    evolve.add_lesson("cto", "Open pull requests against master, never the mobile branch.", "correction")
    assert evolve.get_lesson(a["id"])["status"] == "retired"


def test_the_owners_rules_do_not_use_up_the_places_for_what_was_learned(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "MAX_LESSONS", 2)
    for text in ("Report times in Maldives time, never raw UTC.", "Never deploy on a Friday afternoon.",
                 "Ask before preparing any marketing post."):
        evolve.add_lesson("*", text, "owner-rule")
    first = evolve.add_lesson("*", "Quote paths that contain spaces in shell commands.", "failure")
    second = evolve.add_lesson("*", "Use gh pr view --json for check status, not the web page.", "failure")
    assert first["status"] == second["status"] == "trial"
    assert evolve.get_lesson(first["id"])["status"] == "trial"


def test_a_lesson_the_team_already_has_is_not_kept_again_for_one_agent(isolated):
    evolve.add_lesson("*", "Use the Herd php binary for PHP work and tests, never the Homebrew one.", "owner-rule")
    with pytest.raises(ValueError, match="already covered"):
        evolve.add_lesson("webdev", "Use the Herd php binary for PHP and tests, not the Homebrew one.", "owner-rule")


def test_an_agents_own_lessons_keep_half_the_room_in_its_prompt(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "PROMPT_CHARS", 400)
    for text in ("Report times in Maldives time, never raw UTC, in every status line.",
                 "Quote any path that contains spaces whenever it appears in a shell command.",
                 "Screenshots for the stores come from the demo data set, never a customer's.",
                 "Invoices keep their original currency; convert only in the summary table.",
                 "Weekly numbers start on Sunday and are compared with the same week last year.",
                 "Translations are checked by a native speaker before they ship to anyone."):
        evolve.add_lesson("*", text, "owner-rule")
    mine = evolve.add_lesson("cto", "Read the failing test output in full before changing any code for it.", "correction")
    shown = [l["id"] for l in evolve.for_prompt("cto")]
    assert mine["id"] in shown and len(shown) >= 3
    assert sum(len(l["text"]) + 16 for l in evolve.for_prompt("cto")) <= 400


def test_a_trial_nobody_was_shown_is_not_adopted_for_having_prevented_nothing(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "PROMPT_CHARS", 120)
    evolve.add_lesson("cto", "Owner rule that takes up the whole of the small prompt budget in this test.", "owner")
    hidden = evolve.add_lesson("cto", "Read the failing test output in full before changing any code for it.", "correction")
    _aged(hidden, evolve.TRIAL_MAX_DAYS + 1)
    assert not evolve.shown(evolve.get_lesson(hidden["id"]))
    evolve.evaluate_trials()
    assert evolve.get_lesson(hidden["id"])["status"] == "trial"


def test_lessons_reach_the_prompt_within_budget(isolated, monkeypatch):
    evolve.add_lesson("devops", "The acmeshop repo is at ~/Sites/acmeshop, not ~/Documents/acmeshop.", "failure")
    evolve.add_lesson("*", "Report times in Maldives time (UTC+5), never raw UTC.", "owner")
    evolve.add_lesson("cto", "Dispatch mobile review for every acmeshop pull request.", "correction")
    block = evolve.lessons_block("devops")
    assert "~/Sites/acmeshop" in block and "(team) Report times" in block
    assert "Dispatch mobile review" not in block          # someone else's lesson
    assert block.index("Report times") < block.index("~/Sites/acmeshop")   # the owner's first
    retired = next(l for l in evolve.lessons(None, ("trial",)) if l["agent"] == "devops")
    evolve.update_lesson(retired["id"], status="retired")
    assert "~/Sites/acmeshop" not in evolve.lessons_block("devops")   # the cache let go
    monkeypatch.setattr(evolve, "PROMPT_CHARS", 60)
    evolve._prompt_cache.clear()
    assert evolve.lessons_block("devops").count("\n- ") <= 1


# ---------- failures that keep happening ----------

def test_a_failure_signature_keeps_what_teaches_and_drops_what_varies():
    a = evolve.signature("terminal", "exit 1\n/bin/sh: line 0: cd: /Users/x/Documents/acmeshop: No such file or directory")
    b = evolve.signature("terminal", "exit 1\n/bin/sh: line 3: cd: /Users/x/Documents/acmeshop: No such file or directory")
    assert a == b and "/users/x/documents/acmeshop" in a
    pr1 = evolve.signature("terminal", "exit 1\nunknown flag: --stat\n\nUsage:  gh pr diff [<number>]")
    pr2 = evolve.signature("terminal", "exit 1\nunknown flag: --stat\nUsage:  gh pr diff 401")
    assert pr1 == pr2
    assert evolve.signature("terminal", "exit 1\n(no output)") is None      # a grep that found nothing
    assert evolve.signature("terminal", "exit 1\nREADME.md\nls: x: No such file or directory").endswith(
        "ls: x: no such file or directory")


def test_failures_cluster_with_what_worked_next(isolated):
    now = time.time()
    for i, agent in enumerate(["devops", "devops", "cto", "reposteward"]):
        _fail(agent, "cd ~/Documents/acmeshop && git status",
              "exit 1\n/bin/sh: line 0: cd: /Users/x/Documents/acmeshop: No such file or directory",
              ts=now - 3600 * (i + 1))
    row = store.execution_start("tool", "devops", "terminal",
                                {"arguments": json.dumps({"command": "cd ~/Sites/acmeshop && git status"})})
    store.execution_finish(row["id"], "completed", "exit 0\nOn branch master")
    with db._lock:
        db._db().execute("UPDATE executions SET ts=? WHERE id=?", (now - 3600 + 30, row["id"]))
        db._db().commit()
    _fail("cto", "grep -r nothing .", "exit 1\n(no output)")
    clusters = evolve.failure_clusters(now - 86400)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["count"] == 4 and set(c["agents"]) == {"devops", "cto", "reposteward"}
    assert any("~/Sites/acmeshop" in (s.get("then_worked") or "") for s in c["samples"])
    assert not c["lesson"]


def test_learning_from_failures_writes_one_team_lesson(isolated, monkeypatch):
    now = time.time()
    for i, agent in enumerate(["devops", "cto", "devops"]):
        _fail(agent, "cd ~/Documents/acmeshop", "exit 1\ncd: /Users/x/Documents/acmeshop: No such file or directory",
              ts=now - 600 * (i + 1))
    calls = _quick(monkeypatch, {"lessons": [
        {"pattern": "P1", "text": "The acmeshop repo is at ~/Sites/acmeshop; ~/Documents/acmeshop does not exist."},
        {"pattern": "P9", "text": "A pattern that was never shown."}]})
    made = asyncio.run(evolve.learn_from_failures(evolve.failure_clusters(now - 86400)))
    assert len(made) == 1 and made[0]["agent"] == "*" and made[0]["signature"]
    assert calls[0]["agent"] == "evolution"
    assert evolve.failure_clusters(now - 86400)[0]["lesson"]


# ---------- trials ----------

def test_a_failure_lesson_that_did_not_help_is_retired(isolated):
    sig = evolve.signature("terminal", "exit 1\ncd: /x/Documents/acmeshop: No such file or directory")
    lesson = evolve.add_lesson("*", "The acmeshop repo is at ~/Sites/acmeshop.", "failure", {}, sig)
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - 7200, lesson["id"]))
        db._db().commit()
    for _ in range(2):
        _fail("devops", "cd ~/Documents/acmeshop", "exit 1\ncd: /x/Documents/acmeshop: No such file or directory")
    out = evolve.evaluate_trials()
    assert [r["lesson"] for r in out["retired"]] == [lesson["id"]]
    assert evolve.get_lesson(lesson["id"])["status"] == "retired"
    assert not out["attention"]                      # a first attempt gets another try


def test_a_second_failed_attempt_goes_to_the_owner(isolated):
    lesson = evolve.add_lesson("cto", "Convert UTC timestamps to Maldives time before reporting.",
                               "correction", {"attempt": 2, "corrections": ["you're reading the timestamp wrong"]})
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - 7200, lesson["id"]))
        db._db().commit()
    evolve.note_owner_message("cto", "dm:cto", 9, "its not running for 5hrs, you're reading the UTC timestamp wrong again")
    out = evolve.evaluate_trials()
    assert out["retired"] and out["attention"] and "two lessons" in out["attention"][0]["why"]


def test_a_lesson_that_held_is_adopted(isolated):
    lesson = evolve.add_lesson("devops", "Check gh run view before announcing a build finished.", "correction")
    old = time.time() - (evolve.TRIAL_DAYS + 1) * 86400
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (old, lesson["id"]))
        db._db().commit()
    for _ in range(evolve.MIN_RUNS_TO_ADOPT):
        _turn("devops")
    out = evolve.evaluate_trials()
    assert [a["lesson"] for a in out["adopted"]] == [lesson["id"]]
    assert evolve.get_lesson(lesson["id"])["status"] == "active"


def test_the_regression_guard_suspends_only_new_lessons(isolated):
    now = time.time()
    old = evolve.add_lesson("devops", "Check gh run view before announcing a build finished.", "correction")
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (now - 5 * 86400, old["id"]))
        db._db().commit()
    new = evolve.add_lesson("devops", "Pause thirty seconds after each build before checking.", "failure")
    mine = evolve.add_lesson("devops", "Report the Envoyer deployment id with every deploy.", "owner")
    for i in range(40):
        _turn("devops", ts=now - 86400 * 2 - i * 600)                    # a good week
    for i in range(15):
        _turn("devops", ts=now - i * 600, errors=4, tools=5)             # a bad day
    fired = evolve.regression_guard(now)
    assert fired and [s["lesson"] for s in fired["suspended"]] == [new["id"]]
    assert evolve.get_lesson(old["id"])["status"] == "trial"
    assert evolve.get_lesson(mine["id"])["status"] == "active"


def test_the_guard_holds_without_enough_evidence(isolated):
    for i in range(5):
        _turn("devops", errors=5, tools=5)
    assert evolve.regression_guard() is None


# ---------- distilling the owner's corrections ----------

def test_a_settled_correction_becomes_a_lesson(isolated, monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    evolve.note_owner_message(aid, f"dm:{aid}", None, "you're reading the timestamp wrong, UTC is 5 hours behind us",
                              ts=time.time() - 600)
    calls = _quick(monkeypatch, {"lessons": [
        {"scope": "team", "text": "Timestamps from gh and servers are UTC; the owner is on UTC+5. Convert first."}]})
    made = asyncio.run(evolve.distill_feedback(o))
    assert len(made) == 1 and made[0]["agent"] == "*" and made[0]["status"] == "trial"
    assert "timestamp wrong" in calls[0]["user"]
    assert evolve.feedback(5, "learned")


def test_a_fresh_correction_waits_for_the_conversation_to_settle(isolated, monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    evolve.note_owner_message(aid, f"dm:{aid}", None, "you deployed the wrong branch")
    calls = _quick(monkeypatch, {"lessons": []})
    assert asyncio.run(evolve.distill_feedback(o)) == []
    assert calls == [] and evolve.feedback(5, "new")


def test_a_loosening_lesson_from_the_model_is_dropped(isolated, monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    evolve.note_owner_message(aid, f"dm:{aid}", None, "why didnt you deploy it until I asked you?",
                              ts=time.time() - 600)
    _quick(monkeypatch, {"lessons": [{"scope": "agent", "text": "Deploy without asking the owner once CI is green."}]})
    assert asyncio.run(evolve.distill_feedback(o)) == []
    assert evolve.lessons() == []
    assert evolve.feedback(5, "skipped")


def test_a_repeat_correction_rewrites_the_lesson_it_replaces(isolated, monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    old = evolve.add_lesson(aid, "Mention timezones when reporting times.", "correction")
    evolve.note_owner_message(aid, f"dm:{aid}", None, "you're reading the timestamp wrong again",
                              ts=time.time() - 600)
    _quick(monkeypatch, {"lessons": [{"scope": "agent", "replaces": f"L{old['id']}",
                                      "text": "Convert every UTC timestamp to UTC+5 before saying how long ago it was."}]})
    made = asyncio.run(evolve.distill_feedback(o))
    assert evolve.get_lesson(old["id"])["status"] == "retired"
    assert made[0]["evidence"]["attempt"] == 2


def test_no_learning_once_the_daily_cap_is_spent(isolated, monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    evolve.note_owner_message(aid, f"dm:{aid}", None, "you deployed the wrong branch", ts=time.time() - 600)
    for _ in range(evolve.DAILY_CALLS):
        db.add_usage("evolution", "minimax", "m", 10, 10)
    calls = _quick(monkeypatch, {"lessons": []})
    asyncio.run(evolve.distill_feedback(o))
    assert calls == []


# ---------- the review, end to end ----------

def test_a_quiet_review_says_nothing_to_the_owner(isolated, monkeypatch):
    o = _orch()
    posted = []

    async def post(thread, sender, text, kind="chat", meta=None):
        posted.append(text)
    monkeypatch.setattr(o, "_post", post)
    _quick(monkeypatch, {"lessons": []})
    report = asyncio.run(evolve.review(o, "test"))
    assert posted == [] and report["id"] and evolve.runs("review", 1)
    assert not evolve.review_due()


def test_the_review_tells_the_owner_what_it_could_not_fix(isolated, monkeypatch):
    o = _orch()
    posted = []

    async def post(thread, sender, text, kind="chat", meta=None):
        posted.append((sender, kind, text))
    monkeypatch.setattr(o, "_post", post)
    _quick(monkeypatch, {"lessons": []})
    sig = evolve.signature("terminal", "exit 1\nunknown flag: --stat")
    lesson = evolve.add_lesson("*", "gh pr diff has no --stat flag; use gh pr view N --json files.",
                               "failure", {"attempt": 2}, sig)
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - 7200, lesson["id"]))
        db._db().commit()
    for _ in range(3):
        _fail("reposteward", "gh pr diff 401 --stat", "exit 1\nunknown flag: --stat")
    asyncio.run(evolve.review(o, "test"))
    assert posted and posted[0][0] == "evolution" and posted[0][1] == "sys"
    assert f"L{lesson['id']}" in posted[0][2]


def test_history_is_read_once_and_never_taught(isolated):
    db.add_message("dm:cto", "user", "you typed the wrong domain, its site.test not site.com")
    db.add_message("dm:cto", "user", "merge and deploy #402")
    assert evolve.backfill() == 1
    assert evolve.backfill() == 0
    assert evolve.feedback(10, "new") == [] and evolve.feedback(10, "history")


def test_the_scorecard_counts_runs_corrections_and_tool_failures(isolated):
    _turn("devops")
    _turn("devops", errors=3, tools=3)
    _turn("cto", tools=0, trigger="heartbeat")                         # not a run
    evolve.note_owner_message("devops", "dm:devops", 1, "you deployed the wrong branch")
    _fail("devops", "cd nowhere", "exit 1\ncd: nowhere: No such file or directory")
    card = evolve.scorecard(3)
    today = card["days"][-1]
    # The correction lands on the newest turn, the one that flailed; the first
    # stays clean. A heartbeat that did nothing is not counted at all.
    assert today["runs"] == 2 and today["clean"] == 1
    assert today["corrections"] == 1 and today["tool_errors"] == 1
    assert card["agents"]["devops"]["runs"] == 2


def test_a_timeout_is_only_a_pattern_together_with_what_hung():
    ls = evolve.signature("terminal", "error: tool timed out; inspect its state", "ls ~/Documents | head")
    git = evolve.signature("terminal", "error: tool timed out; inspect its state", "git -C ~/Sites/x fetch")
    assert ls != git and "ls ~/documents" in ls
    assert evolve.signature("terminal", "exit 1\n/Users/x/Sites/acmeshop") is None   # output, not an error


def test_a_lesson_put_back_on_trial_is_judged_from_now(isolated):
    sig = evolve.signature("terminal", "exit 1\nunknown flag: --stat")
    lesson = evolve.add_lesson("*", "gh pr diff has no --stat flag; use --name-only.", "failure", {}, sig)
    with db._lock:
        db._db().execute("UPDATE lessons SET ts=? WHERE id=?", (time.time() - 7200, lesson["id"]))
        db._db().commit()
    for _ in range(2):
        _fail("reposteward", "gh pr diff 1 --stat", "exit 1\nunknown flag: --stat")
    evolve.evaluate_trials()
    assert evolve.get_lesson(lesson["id"])["status"] == "retired"
    evolve.update_lesson(lesson["id"], status="trial")
    evolve.evaluate_trials()
    restored = evolve.get_lesson(lesson["id"])
    assert restored["status"] == "trial" and restored["recurrences"] == 0


def test_a_failed_model_call_cools_off_and_still_counts(isolated, monkeypatch):
    monkeypatch.setattr("llm.ready", lambda conf=None: True)
    monkeypatch.setattr(evolve, "_cool_until", 0.0)
    monkeypatch.setattr(evolve, "_made", {"day": "", "n": 0})

    async def down(*a, **k):
        raise RuntimeError("LLM connection failed")
    monkeypatch.setattr("llm.quick", down)
    with pytest.raises(RuntimeError):
        asyncio.run(evolve._ask("s", "u"))
    assert evolve._calls_today() == 1
    assert "failed recently" in evolve._can_spend()


@pytest.mark.parametrize("result", [
    "error: refused: this exact terminal call already failed 2 times this turn, last with: exit 1",
    "error: turn tool budget exhausted; report a checkpoint",
    "held for approval [abc123]: this looks like it needs the owner's say-so",
    "blocked by policy: matches `\\bsudo\\b`",
])
def test_a_guard_saying_no_is_not_a_mistake_to_learn_from(result):
    assert evolve.signature("terminal", result, "anything") is None


def test_a_message_read_live_is_not_read_again_by_the_backfill(isolated):
    msg = db.add_message("dm:cto", "user", "you deployed the wrong branch")
    assert evolve.note_owner_message("cto", "dm:cto", msg["id"], msg["text"])
    assert evolve.note_owner_message("cto", "dm:cto", msg["id"], msg["text"]) is None
    assert evolve.backfill() == 0
    assert len(evolve.feedback(10)) == 1


def test_a_trial_nobody_was_shown_is_not_retired_for_failing_to_help_either(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "PROMPT_CHARS", 120)
    evolve.add_lesson("cto", "Owner rule that takes up the whole of the small prompt budget in this test.", "owner")
    hidden = evolve.add_lesson("cto", "Read the failing test output in full before changing any code for it.", "correction")
    monkeypatch.setattr(evolve, "_recurrences", lambda lesson: 5)
    out = evolve.evaluate_trials()
    assert evolve.get_lesson(hidden["id"])["status"] == "trial" and not out["retired"]


def test_rules_a_model_took_from_the_owners_words_have_a_limit_and_their_own_do_not(isolated, monkeypatch):
    monkeypatch.setattr(evolve, "MAX_LESSONS", 1)
    texts = ("Report times in Maldives time, never raw UTC.", "Quote any path that contains spaces in a command.",
             "Weekly numbers start on a Sunday every time.")
    evolve.add_lesson("*", texts[0], "owner-rule")
    evolve.add_lesson("*", texts[1], "owner-rule")
    with pytest.raises(ValueError, match="retire one first"):
        evolve.add_lesson("*", texts[2], "owner-rule")
    assert evolve.add_lesson("*", texts[2], "owner")["status"] == "active"
