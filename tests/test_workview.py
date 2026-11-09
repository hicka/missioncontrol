"""Work as a conversation can draw it: who has a task, what they are on, and each step."""
import asyncio
import json

from test_kernel import isolated, say, call, until, make, second, wind_down  # noqa: F401
from test_kernel_gaps import Hold, asked
import harness
import kernel_store as store
import runtime_store
import workview


def test_a_step_reads_as_the_agent_named_it_or_as_the_plain_command():
    label = workview.step_label
    assert label("terminal", {"arguments": json.dumps({"command": "cd ~/Sites/shop && php artisan test 2>&1 | tail -5", "note": "Run the test suite"})}) == "Run the test suite"
    assert label("terminal", {"arguments": json.dumps({"command": "cd ~/Sites/shop && php artisan test 2>&1 | tail -5"})}) == "php artisan test"
    assert label("terminal", {"arguments": {"command": "git status; git log -1"}}) == "git status"
    assert label("claude_code", {"arguments": json.dumps({"task": "Add a guard for the empty cart in CartTotals"})}).startswith("Writing code: Add a guard")
    assert label("report", {"arguments": json.dumps({"status": "ready", "summary": "done"})}) == "Reported it finished"
    assert label("browser", {"arguments": json.dumps({"action": "goto", "url": "https://www.example.com/orders/5"})}) == "Browser: goto example.com"
    # A note is only a note on the tools that take one. Elsewhere it is that tool's own field.
    assert "Run the test suite" not in label("cron", {"arguments": json.dumps({"action": "note", "note": "Run the test suite"})})
    # Arguments cut off by the audit's length limit still give their first words.
    assert label("terminal", {"arguments": '{"command": "pytest tests/ -q", "cwd": "/Users/x/very/long'}) == "pytest tests/ -q"


def test_the_live_event_carries_the_label_and_says_which_tools_are_quiet():
    shown = workview.labelled({"kind": "tool", "name": "terminal", "detail": {"arguments": {"command": "ls", "note": "List the files"}}})
    assert shown["label"] == "List the files" and not shown["quiet"]
    assert workview.labelled({"kind": "tool", "name": "recall", "detail": {"arguments": {"query": "x"}}})["quiet"]
    assert "label" not in workview.labelled({"kind": "skill", "name": "x"})


def test_the_same_failing_command_under_a_new_note_is_the_same_command():
    one = harness._call_key("terminal", {"command": "make build", "note": "Build it"})
    assert one == harness._call_key("terminal", {"command": "make build", "note": "Try the build again"})
    assert one != harness._call_key("terminal", {"command": "make test", "note": "Build it"})
    assert harness._call_key("cron", {"action": "note", "note": "a"}) != harness._call_key("cron", {"action": "note", "note": "b"})


def test_a_conversation_shows_its_task_with_steps_and_what_it_waits_on(isolated, monkeypatch):
    import tools
    from unittest.mock import AsyncMock
    monkeypatch.setattr(tools, "run_terminal", AsyncMock(return_value="exit 0"))
    model = Hold(monkeypatch)
    model.worker = [call("terminal", command="ls", note="Look at the files"),
                    call("report", status="blocked", summary="Two ways to do this.", question="Which one?")]

    async def main():
        o = make(monkeypatch)
        aid = second(o)
        mission = runtime_store.mission_create(aid, f"dm:{o.lead}", "Fix the checkout crash")
        row = await o.workers.spawn(aid, "Fix the checkout crash", "", [asked(o)], mission=mission["id"],
                                    conversation=f"dm:{o.lead}")
        await until(lambda: store.session_get(row["id"])["status"] == "blocked")
        tasks = workview.view(o, f"dm:{o.lead}")
        assert len(tasks) == 1
        task = tasks[0]
        assert task["title"] == "Fix the checkout crash" and task["agent"] == aid and task["thread"] == f"dm:{o.lead}"
        assert [s["label"] for s in task["steps"]] == ["Look at the files", "Asked a question"]
        assert task["steps"][0]["status"] == "completed"
        assert task["phase"] == "needs_you" and task["waiting_on"] == {"kind": "you", "text": "Which one?"}
        assert task["live"]
        assert workview.view(o, "dm:someone-else") == []
        await wind_down(o)
    asyncio.run(main())


def test_trying_again_after_a_failure_reads_as_fixing_and_a_finished_task_as_done():
    working = {"id": "w", "status": "working"}
    steps = [{"tool": "terminal", "status": "failed"}, {"tool": "terminal", "status": "running"}]
    assert workview._phase(working, steps, True, None) == "fixing"
    assert workview._phase(working, [{"tool": "terminal", "status": "completed"}, steps[1]], True, None) == "working"
    assert workview._phase(working, [{"tool": "terminal", "status": "completed"}], True, None) == "thinking"
    assert workview._phase({"id": "w", "status": "waiting"}, [], False, {"kind": "watch", "text": "the deploy"}) == "waiting"
    assert workview._phase({"id": "w", "status": "idle"}, [], False, None) == "done"
