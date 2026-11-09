"""What a turn does when it starts going wrong, and what it leaves behind.

Each of these was a pattern in the execution log: the same failing command run
three and four times in a row, a forty-round browser turn re-sending its first
screenshot on every round, the oldest standing rule falling out of view when
the ninth was saved, and a turn's quality going unrecorded however it ended.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import db
import evolve
import harness
import orchestrator
import skills


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "turns.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skills, "USES_PATH", tmp_path / "skills" / ".uses.json")
    evolve._prompt_cache.clear()
    yield
    if db._conn:
        db._conn.close()


async def _emit(event):
    return None


def test_the_third_identical_failure_is_refused_before_it_runs(isolated):
    h = harness.Harness({"devops": {}}, orchestrator.TOOL_DEFS, _emit)
    h.start_turn("devops")
    ran = []

    async def dispatch():
        ran.append(1)
        return "exit 1\ncd: /x/Documents/acmeshop: No such file or directory"

    args = {"command": "cd ~/Documents/acmeshop && git status"}

    async def run():
        out = []
        for _ in range(3):
            out.append(await h.execute("devops", "terminal", args, dispatch))
        return out
    first, second, third = asyncio.run(run())
    assert len(ran) == 2
    assert third.startswith("error: refused: this exact terminal call already failed 2 times")
    assert "No such file or directory" in third
    assert h.refused["devops"] == 1
    # A different command is a different attempt, and runs.
    asyncio.run(h.execute("devops", "terminal", {"command": "ls ~/Sites"}, dispatch))
    assert len(ran) == 3


def test_a_success_clears_the_count_and_a_new_turn_starts_fresh(isolated):
    h = harness.Harness({"devops": {}}, orchestrator.TOOL_DEFS, _emit)
    h.start_turn("devops")
    results = iter(["exit 1\nnot yet", "exit 0\nok", "exit 1\nnot yet", "exit 1\nnot yet"])

    async def dispatch():
        return next(results)

    args = {"command": "gh run view 1 --json status"}

    async def run():
        return [await h.execute("devops", "terminal", args, dispatch) for _ in range(4)]
    out = asyncio.run(run())
    assert not any(r.startswith("error: refused") for r in out)
    h.start_turn("devops")
    assert h.failures["devops"] == {}


def _tool_result(i, text=None, image=False):
    content = [{"type": "text", "text": text or ("x" * 20000)}]
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}})
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": content}]}


def test_a_long_turn_keeps_its_newest_results_whole():
    messages = [{"role": "user", "content": "context"}]
    for i in range(10):
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "browser",
                                                           "input": {"action": "read"}}]})
        messages.append(_tool_result(i, image=True))
    saved = orchestrator.trim_tool_context(messages)
    assert saved > 0
    results = [m["content"][0] for m in messages if isinstance(m["content"], list)
               and m["content"][0].get("type") == "tool_result"]
    assert len(results) == 10                         # nothing removed, pairing intact
    newest = results[-orchestrator.CONTEXT_KEEP:]
    assert all(isinstance(r["content"], list) and len(r["content"][0]["text"]) == 20000 for r in newest)
    images = sum(1 for r in results if isinstance(r["content"], list)
                 for p in r["content"] if p.get("type") == "image")
    assert images == orchestrator.CONTEXT_KEEP_IMAGES
    assert isinstance(results[0]["content"], str) and "cut from this older result" in results[0]["content"]
    # Trimming twice changes nothing more.
    assert orchestrator.trim_tool_context(messages) == 0


def test_a_short_turn_is_left_alone():
    messages = [_tool_result(i, text="short") for i in range(3)]
    before = json.dumps(messages)
    assert orchestrator.trim_tool_context(messages) == 0
    assert json.dumps(messages) == before


def test_every_standing_rule_stays_in_view(isolated):
    o = orchestrator.Orchestrator()
    for i in range(15):
        db.add_memory("cto", f"standing rule {i}: never do thing number {i} without asking", "rule")
    lines = o._rules_block(db.list_memories(400))
    assert lines[0].endswith("(15):")
    assert any("rule 0:" in l for l in lines) and any("rule 14:" in l for l in lines)


def test_rules_past_the_budget_are_counted_not_dropped(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    monkeypatch.setattr(o, "RULES_CHARS", 200)
    for i in range(15):
        db.add_memory("cto", f"standing rule {i}: never do thing number {i} without asking", "rule")
    lines = o._rules_block(db.list_memories(400))
    assert "rule 14:" in "\n".join(lines)            # the newest fit
    assert "more standing rules did not fit" in lines[-1]


def test_lessons_reach_the_system_prompt(isolated):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    evolve.add_lesson(aid, "The acmeshop repo is at ~/Sites/acmeshop, not ~/Documents/acmeshop.", "failure")
    assert "OPERATING LESSONS" in o._system_prompt(aid)
    assert "~/Sites/acmeshop" in o._system_prompt(aid)


def test_an_owner_correction_is_recorded_when_it_arrives(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    tid = evolve.turn_start(aid, f"dm:{aid}", "user")
    evolve.turn_finish(tid, "completed", 1, 2, 0)

    async def run():
        await o.user_message(f"dm:{aid}", "you typed the wrong domain, its site.test not site.com")
    asyncio.run(run())
    assert evolve.feedback(5, "new")[0]["agent"] == aid
    assert evolve.last_turn(aid)["corrected"] == 1










def test_the_learning_engine_is_out_of_an_agents_reach():
    import selfimprove
    assert selfimprove.protected_hits(["server/evolve.py", "server/harness.py"]) == [
        "server/evolve.py", "server/harness.py"]


@pytest.mark.parametrize("text", ["NO_REPLY", "NO_REPLY.", "no reply", " No_Reply! ", ""])
def test_silence_is_silence_however_it_is_spelled(text):
    assert orchestrator.is_silence(text)










def test_openai_style_length_stops_count_as_cut_off():
    import llm
    resp = llm._response_from_openai({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]})
    assert llm.truncated(resp)
    assert not llm.truncated(llm._response_from_openai({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}))


def _worker(o, monkeypatch, responses, tool_output, aid=None):
    import browser
    import budget
    import kernel_store
    import llm
    aid = aid or next(iter(o.agents))
    monkeypatch.setattr(budget, "check", lambda **kwargs: ("ok", None))
    monkeypatch.setattr(browser, "current_observation", AsyncMock(return_value=""))
    monkeypatch.setattr(o, "_dispatch_tool", AsyncMock(side_effect=tool_output) if isinstance(tool_output, list)
                        else AsyncMock(return_value=tool_output))
    seen = []

    async def complete(system, messages, *args, **kwargs):
        seen.append(json.loads(json.dumps(messages, default=str)))
        llm._record_usage(kwargs.get("agent"), {"provider": "x", "model": "m"},
                          {"input_tokens": 100, "output_tokens": 10}, "anthropic")
        return responses.pop(0)
    monkeypatch.setattr(llm, "complete", complete)
    # A run that is the whole task, so its plain last words are its result.
    row = kernel_store.session_create("worker", aid, title="t", status="working", meta={"one_shot": True})
    kernel_store.inbox_put(row["id"], "brief", "go")
    return o.workers.worker_for(row), seen, row["id"]


def _use(i):
    return {"content": [{"type": "tool_use", "id": f"u{i}", "name": "terminal", "input": {"command": f"try {i}"}}]}


def test_a_flailing_turn_is_told_once_to_stop_and_think(isolated, monkeypatch):
    import kernel_store
    o = orchestrator.Orchestrator()
    responses = [_use(i) for i in range(5)] + [{"content": [{"type": "text", "text": "Blocked on the path."}]}]
    worker, seen, sid = _worker(o, monkeypatch, responses, harness.ToolResult("exit 1\nnope", "failed"))
    assert asyncio.run(worker.run()) == "completed"
    assert kernel_store.reports_of(sid)[0]["summary"] == "Blocked on the path."
    nudges = [b for m in seen[-1] if m["role"] == "user" and isinstance(m["content"], list)
              for b in m["content"] if b.get("type") == "text" and "stop and work out why" in b["text"]]
    assert len(nudges) == 1
    rec = worker.rec
    assert rec["tools"] == 5 and rec["errors"] == 5 and "flailing" in rec["flags"]
    assert rec["tokens"] == {"in": 600, "out": 60} and rec["rounds"] == 6


def test_every_run_leaves_a_record(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    responses = [_use(0), _use(1), _use(2), {"content": [{"type": "text", "text": "done"}]}]
    outcomes = [harness.ToolResult("exit 1\nnope", "failed"), harness.ToolResult("ok"), harness.ToolResult("ok")]
    worker, _, _ = _worker(o, monkeypatch, responses, outcomes, aid)
    asyncio.run(worker.run("github"))
    t = evolve.last_turn(aid)
    assert t["outcome"] == "completed" and t["tools"] == 3 and t["tool_errors"] == 1
    assert t["rounds"] == 4 and t["trigger"] == "github" and evolve.clean(t)


def test_a_turn_the_owner_stopped_is_not_a_clean_one(isolated, monkeypatch):
    import browser
    import budget
    import llm
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    started = asyncio.Event()

    async def complete(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(budget, "check", lambda **kwargs: ("ok", None))
    monkeypatch.setattr(browser, "current_observation", AsyncMock(return_value=""))
    monkeypatch.setattr(o, "_current_state_block", AsyncMock(return_value=""))

    async def run():
        await o.workers.spawn(aid, "t", "", [])
        await asyncio.wait_for(started.wait(), 2)
        assert await o.stop_agent(aid)
        await asyncio.wait_for(o.close(), 2)
    asyncio.run(run())
    t = evolve.last_turn(aid)
    assert t["outcome"] == "stopped" and not evolve.clean(t)


def test_history_is_never_trimmed_where_the_provider_binds_it(isolated, monkeypatch):
    """Anthropic ties each thinking block to the exact history before it; an
    edited earlier tool result is a 400 there, so the turn must not trim."""
    import llm
    import session
    monkeypatch.setattr(llm, "config", lambda: {"provider": "anthropic", "model": "claude-opus-5-5",
                                                "base_url": "", "max_tokens": 3000})
    assert llm.history_is_bound()
    assert llm._resolved()["max_tokens"] == llm.THINKING_MAX_TOKENS
    monkeypatch.setattr(llm, "config", lambda: {"provider": "minimax", "model": "MiniMax-M3",
                                                "base_url": "", "max_tokens": 3000})
    assert not llm.history_is_bound()
    assert llm._resolved()["max_tokens"] == 3000
    o = orchestrator.Orchestrator()
    monkeypatch.setattr(llm, "history_is_bound", lambda conf=None: True)
    called = []
    monkeypatch.setattr(session, "trim_tool_context", lambda m: called.append(1) or 0)
    worker, _, _ = _worker(o, monkeypatch, [{"content": [{"type": "text", "text": "done"}]}], harness.ToolResult("ok"))
    assert asyncio.run(worker.run()) == "completed"
    assert called == []


def test_a_reply_cut_off_at_the_output_limit_does_not_end_the_turn(isolated, monkeypatch):
    """A reply that hits max_tokens mid-way through a big tool call used to be taken
    as the agent's answer: the turn ended on "Let me build the full list:" and the
    agent sat idle with the job half done."""
    import kernel_store
    o = orchestrator.Orchestrator()
    cut = {"stop_reason": "max_tokens", "content": [
        {"type": "text", "text": "Let me build the full transaction list:"},
        {"type": "tool_use", "id": "half", "name": "terminal", "input": "{\"command\": \"echo 1"}]}
    use = {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "u1", "name": "terminal",
                                                   "input": {"command": "echo first 15"}}]}
    done = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Recorded all 40 rows."}]}
    worker, seen, sid = _worker(o, monkeypatch, [cut, use, done], "ok")
    assert asyncio.run(worker.run()) == "completed"
    assert kernel_store.reports_of(sid)[0]["summary"] == "Recorded all 40 rows."
    assert o._dispatch_tool.await_count == 1, "the half-written call never ran"
    told = [m for m in seen[1] if m["role"] == "user" and isinstance(m["content"], str)
            and "cut off" in m["content"]]
    assert told and "smaller steps" in told[0]["content"]
    assert "cut_off" in worker.rec["flags"]


def test_a_reply_that_keeps_getting_cut_off_says_so(isolated, monkeypatch):
    import kernel_store
    o = orchestrator.Orchestrator()
    cut = lambda: {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Building the list:"}]}
    worker, _, sid = _worker(o, monkeypatch, [cut(), cut(), cut()], "ok")
    assert asyncio.run(worker.run()) == "waiting"
    summaries = " ".join(r["summary"] for r in kernel_store.reports_of(sid))
    assert "cut off at the output limit" in summaries and "Say continue" in summaries


# ---------- memory that is found, said once, and sized to the task ----------

def test_what_was_decided_outranks_a_log_line_that_matches_as_well(isolated):
    db.add_memory("github", "acmeshop PR #41 merged: php tests moved to the herd binary in CI", "github-event")
    db.add_memory("github", "acmeshop PR #42 opened: php tests herd binary path", "pr")
    rule = db.add_memory("cto", "Always run php tests with the Herd binary, not the Homebrew one.", "rule")
    assert db.search_memories("which php binary runs the tests", 3)[0]["id"] == rule


def test_one_fact_in_two_wordings_is_found_once_and_numbers_keep_notes_apart(isolated):
    assert db.same_note("Always use Herd's php bin for PHP work and tests.",
                        "always use Herd's PHP bin for tests and for PHP work")
    # One word is a different note. A review of a looser version of this found it would
    # have replaced the first of each pair with the second.
    for a, b in (("acmeshop PR #522 merged and deployed", "acmeshop PR #523 merged and deployed"),
                 ("Always ask the owner before merging any PR into master on public repos",
                  "Never ask the owner before merging any PR into master on public repos"),
                 ("Send the weekly report to Alice every Monday morning", "Send the weekly report to Bob every Monday morning"),
                 ("Merge dependabot PRs on the public repos without asking",
                  "Never merge dependabot PRs on the public repos without asking"),
                 ("Never deploy on Fridays.", "Never deploy on Mondays.")):
        assert not db.same_note(a, b), (a, b)
    db.add_memory("cto", "Always use Herd's php bin for PHP work and tests.", "rule")
    db.add_memory("webdev", "always use Herd's PHP bin for tests and for PHP work", "rule")
    assert len(db.search_memories("herd php bin tests", 5)) == 1
    assert len(db.search_memories("herd php bin tests", 5, repeats=True)) == 2


def test_the_recent_notes_are_what_people_wrote_down_not_the_event_log(isolated):
    db.add_memory("cto", "The owner prefers one PR per fix.", "fact")
    for n in range(12):
        db.add_memory("github", f"acmeshop PR #{n} merged", "github-event")
    assert [m["kind"] for m in db.list_recent_notes(6)] == ["fact"]


def test_a_rule_saved_twice_is_shown_once_and_the_task_decides_what_fits(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    first = db.add_memory("cto", "Always use Herd's php bin for PHP work and tests.", "rule")
    second = db.add_memory("webdev", "always use Herd's PHP bin for tests and for PHP work", "rule")
    lines = o._rules_block(db.list_memories_by_kind("rule"))
    assert "standing rules (1)" in lines[0] and f"[{second}] (also [{first}])" in lines[1]
    for words in ("invoices keep their original currency", "translations get a native reader first",
                  "store screenshots come from demo data", "weekly numbers start on a Sunday",
                  "holiday cover is agreed a month ahead"):
        db.add_memory("cto", f"{words}. " + " ".join(f"{w}{n}" for n in range(12) for w in words.split()[:3]), "rule")
    monkeypatch.setattr(orchestrator.Orchestrator, "RULES_CHARS", 700)
    by_age = "\n".join(o._rules_block(db.list_memories_by_kind("rule")))
    by_task = "\n".join(o._rules_block(db.list_memories_by_kind("rule"), "fix the failing php tests"))
    assert "Herd" not in by_age and "Herd" in by_task        # the oldest rule, and the one this task needs
    assert "more standing rules did not fit" in by_task


def test_saving_what_is_already_known_updates_it_instead_of_adding_to_it(isolated, monkeypatch):
    import asyncio

    async def main():
        o = orchestrator.Orchestrator()
        first = await o._dispatch_tool(o.lead, ".", "remember", {"text": "Staging deploys run from the release branch only.", "kind": "fact"})
        again = await o._dispatch_tool(o.lead, ".", "remember", {"text": "staging deploys: only run from the release branch", "kind": "fact"})
        other = await o._dispatch_tool(o.lead, ".", "remember", {"text": "Production deploys run from the release branch only.", "kind": "fact"})
        number = lambda said: int(said.split("[")[1].split("]")[0])
        assert "already said this" in again and number(first) == number(again)
        assert "already" not in other
        notes = db.list_recent_notes(10)
        assert len(notes) == 2 and any("staging deploys: only" in m["text"] for m in notes)
        # Someone else's note is not reworded by a session the owner is not behind.
        theirs = db.add_memory("owner", "Invoices keep their original currency.", "decision")
        said = await o._dispatch_tool(o.lead, ".", "remember", {"text": "invoices: keep their ORIGINAL currency", "kind": "decision"})
        assert "already" not in said
        assert [m["text"] for m in db.list_memories(50) if m["id"] == theirs] == ["Invoices keep their original currency."]
    asyncio.run(main())


def test_a_session_is_told_of_the_skills_its_task_touches_and_only_names_the_rest(isolated, monkeypatch):
    import skills
    o = orchestrator.Orchestrator()
    skills.save_skill("cto", "Watch an Envoyer deploy to completion", "Use when a production deploy was started through Envoyer and someone must know how it ended.", "1. list deployments\n2. poll the newest\n3. report the result", "starting a deploy")
    skills.save_skill("cto", "Capture store screenshots on the simulator", "Use to get app screenshots from demo data on the iOS simulator.", "1. boot\n2. seed\n3. capture", "uploading them")
    context = o._work_context(o.lead, "merge the fix and watch the envoyer deploy")
    described = context.split("TEAM SKILLS that match this task")[1].split("Other team skills")[0]
    assert "watch-an-envoyer-deploy-to-completion" in described and "simulator" not in described
    assert "capture-store-screenshots-on-the-simulator" in context.split("Other team skills")[1]
    assert "TEAM SKILLS (skill read to inspect one" in o._work_context(o.lead, "write a poem about the sea")


def test_a_skill_name_cut_at_a_hyphen_is_still_one_skill(isolated):
    import skills
    name = "replace google play phone screenshots via android publisher api again"
    assert not skills._slug(name).endswith("-") and skills._slug(skills._slug(name)) == skills._slug(name)


def test_a_procedure_learned_a_second_time_revises_the_first(isolated, monkeypatch):
    import asyncio, json, skills

    async def main():
        o = orchestrator.Orchestrator()
        skills.save_skill("cto", "Watch an Envoyer deploy to completion", "Use when a production deploy was started through Envoyer and someone must know how it ended.", "1. list deployments\n2. poll the newest\n3. report the result")
        other = json.dumps({"action": "new", "name": "Watch an Envoyer deploy to staging",
                            "description": "Use when a staging deploy was started through Envoyer and someone must know how it ended.",
                            "content": "1. list staging deployments\n2. poll the newest one until it ends\n3. report the result to the owner"})
        assert (await o._apply_learning_inner(o.lead, other)).startswith("learned:")       # a different skill stays one
        skills.delete_skill("watch-an-envoyer-deploy-to-staging")
        raw = json.dumps({"action": "new", "name": "Watch the Envoyer deploy to completion",
                          "description": "Use when a production deploy was started through Envoyer and someone must know how it ended.",
                          "content": "1. poll the newest deployment every 20 seconds\n2. stop at finished or failed\n3. say which server it ended on"})
        assert (await o._apply_learning_inner(o.lead, raw)).startswith("revised:watch-an-envoyer-deploy-to-completion")
        assert len(skills.list_skills()) == 1 and "every 20 seconds" in skills.read_skill("watch-an-envoyer-deploy-to-completion")
    asyncio.run(main())


def test_dependency_bot_prs_are_a_count_and_the_rest_lose_their_addresses(isolated, monkeypatch):
    import asyncio, processes, settings as cfg
    prs = [{"number": 7, "title": "Fix the null crash", "headRefName": "fix/null", "baseRefName": "main",
            "url": "https://github.com/acme/shop/pull/7", "isDraft": False, "state": "OPEN"}] + [
           {"number": n, "title": f"deps: bump thing-{n}", "headRefName": f"dependabot/npm_and_yarn/thing-{n}",
            "baseRefName": "main", "url": f"https://github.com/acme/shop/pull/{n}", "isDraft": False, "state": "OPEN"}
           for n in (11, 12, 13)]

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps(prs).encode(), b""

    async def fake(*args, **kwargs):
        return Proc()
    monkeypatch.setattr(processes, "create_subprocess_exec", fake)
    monkeypatch.setattr(cfg, "load", lambda: {"github": {"repos": ["acme/shop"]}})

    async def main():
        text = await orchestrator.Orchestrator()._current_state_block()
        assert "#7 open `fix/null` -> `main`: Fix the null crash" in text and "https://" not in text
        assert "acme/shop: 3 dependency updates (#11, #12, #13)" in text and "bump thing" not in text
        assert "standing rules" not in text       # those go with each session's own context now
    asyncio.run(main())


def test_only_those_with_a_browser_are_told_how_to_read_one(isolated):
    o = orchestrator.Orchestrator()
    aid = o.lead
    o.agents[aid]["tools"] = ["terminal"]
    assert "BROWSER EVIDENCE" not in o._system_prompt(aid)
    o.agents[aid]["tools"] = ["terminal", "browser"]
    assert "BROWSER EVIDENCE" in o._system_prompt(aid)
    o.agents[aid]["tools"] = None
    assert "BROWSER EVIDENCE" in o._system_prompt(aid)


def test_learning_is_told_when_a_kind_of_task_keeps_coming_back(isolated, monkeypatch):
    import asyncio
    import kernel_store as store

    async def main():
        o = orchestrator.Orchestrator()
        aid = o.lead
        for n in (501, 502, 503):
            store.session_create("worker", aid, title=f"Review shop PR {n}", status="ended")
        store.session_create("worker", aid, title="Write the release notes", status="ended")
        earlier = o._done_before(aid, "Review shop PR 504")
        assert len(earlier) == 3 and all("Review" in t for t in earlier)
        assert o._done_before(aid, "Deploy") == []
        asked = {}

        async def quick(system, user, **kwargs):
            asked["system"], asked["user"] = system, user
            return '{"action": "none"}'
        monkeypatch.setattr(orchestrator.llm, "quick", quick)
        monkeypatch.setattr(o, "_autolearn_on", lambda: True)
        monkeypatch.setattr(o, "_learn_gate", AsyncMock(return_value=(True, "")))
        monkeypatch.setattr(orchestrator.runtime_store, "decision_outcome", lambda *a: None)
        await o._maybe_learn(aid, ["terminal: gh pr view 504", "terminal: gh pr diff 504", "terminal: gh pr review 504"],
                             [], "Review shop PR 504")
        assert "KEEPS COMING BACK" in asked["user"] and "4th time" in asked["user"]
        assert "never for this one instance" in asked["system"]
        assert o._learn_gate.call_args.args[-1] == earlier
    asyncio.run(main())


def test_the_trail_says_which_calls_failed(isolated, monkeypatch):
    import asyncio

    async def main():
        o = orchestrator.Orchestrator()
        aid = o.lead
        ctx = o._ctx(aid)
        ctx.trail.clear()

        async def execute(aid, name, args, run, call_id=None):
            await run()
            return harness.ToolResult("exit 1\nfatal: not a git repository", "failed") if "status" in args["command"] else harness.ToolResult("exit 0", "completed")
        monkeypatch.setattr(o.harness, "execute", execute)
        monkeypatch.setattr(o, "_dispatch_tool", AsyncMock(side_effect=lambda aid, cwd, name, args: ctx.trail.append(f"{name}: {args['command']}")))
        await o._exec_tool(aid, ".", "terminal", {"command": "git status"})
        await o._exec_tool(aid, ".", "terminal", {"command": "ls"})
        assert ctx.trail == ["terminal: git status  -> FAILED: exit 1 fatal: not a git repository", "terminal: ls"]
    asyncio.run(main())


def test_a_skill_folder_named_before_names_were_trimmed_can_still_be_read_and_removed(isolated):
    import skills
    skills.save_skill("cto", "Replace store screenshots", "Use to replace store screenshots.", "1. a\n2. b\n3. c")
    old = skills.SKILLS_DIR / "replace-store-screenshots-"
    (skills.SKILLS_DIR / "replace-store-screenshots").rename(old)
    assert skills.slug_for("replace-store-screenshots-") == "replace-store-screenshots-"
    assert skills.read_skill("replace-store-screenshots-")
    assert skills.delete_skill("replace-store-screenshots-") and not old.exists()


def test_a_job_that_runs_on_a_timetable_is_not_the_same_work_being_done_again(isolated):
    import kernel_store as store
    o = orchestrator.Orchestrator()
    for _ in range(4):
        row = store.session_create("worker", o.lead, title="Feature announcements check", status="ended")
        store.session_set(row["id"], meta={"source": "cron", "one_shot": True})
    assert o._done_before(o.lead, "Feature announcements check") == []
