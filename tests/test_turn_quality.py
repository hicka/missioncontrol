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
