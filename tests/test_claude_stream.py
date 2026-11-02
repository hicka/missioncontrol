"""Claude Code runs report their steps as they happen."""
import asyncio
import os
import stat


def test_events_become_readable_lines():
    import tools
    ev = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me look at the   server first.\n"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "server/main.py"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "def ready", "path": "server"}},
    ]}}
    lines = tools.describe_event(ev)
    assert lines[0] == "claude: Let me look at the server first."
    assert lines[1] == "claude › Read server/main.py"
    assert lines[2] == "claude › Bash pytest -q"
    assert lines[3] == "claude › Grep def ready in server"
    done = tools.describe_event({"type": "result", "duration_ms": 61234, "total_cost_usd": 0.42,
                                 "num_turns": 7, "result": "all good"})
    assert done == ["claude finished (61s, $0.42, 7 turns)"]
    assert tools.describe_event({"type": "system", "subtype": "init"}) == []
    assert tools.describe_event({"type": "result", "is_error": True}) == ["claude failed"]


def test_fake_claude_streams_progress_and_returns_the_result(tmp_path, monkeypatch):
    import tools
    fake = tmp_path / "claude"
    fake.write_text('''#!/bin/sh
echo '{"type":"system","subtype":"init"}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Reading the code."}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}'
echo '{"type":"user","message":{"content":[{"type":"tool_result","content":"..."}]}}'
echo 'not json at all'
echo '{"type":"result","subtype":"success","duration_ms":1500,"num_turns":2,"result":"Found 2 bugs."}'
''')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(tools, "_env", lambda: {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    seen = []

    async def progress(note):
        seen.append(note)

    out = asyncio.run(tools.claude_code("review", None, str(tmp_path), progress=progress))
    assert out.startswith("claude exit 0") and "Found 2 bugs." in out
    assert seen == ["claude: Reading the code.", "claude › Read a.py",
                    "claude finished (2s, 2 turns)"]


def test_runs_in_the_same_repo_take_turns(tmp_path, monkeypatch):
    import time
    import tools
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nsleep 0.3\necho '{\"type\":\"result\",\"result\":\"done\"}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(tools, "_env", lambda: {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    repo = tmp_path / "repo"
    repo.mkdir()

    async def both():
        t0 = time.monotonic()
        await asyncio.gather(tools.claude_code("a", None, str(repo)),
                             tools.claude_code("b", None, str(repo)))
        return time.monotonic() - t0

    assert asyncio.run(both()) >= 0.55        # serialised, not parallel


def test_a_stream_line_bigger_than_the_64kb_limit_does_not_crash(tmp_path, monkeypatch):
    import json as _json
    import tools
    big = "x" * 200000                       # one JSON line well over asyncio's 64KB limit
    fake = tmp_path / "claude"
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "big.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": big}]}},
        {"type": "result", "result": "ok", "duration_ms": 1200, "num_turns": 1},
    ]
    fake.write_text("#!/bin/sh\n" + "".join(
        "printf '%s\\n' '" + _json.dumps(x) + "'\n" for x in lines))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(tools, "_env", lambda: {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    out = asyncio.run(tools.claude_code("t", None, str(tmp_path)))
    assert out.startswith("claude exit 0") and "ok" in out
