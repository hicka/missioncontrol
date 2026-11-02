"""MCP servers: tools from other apps, through one gate.

Connecting a server hands agents real power over another app, often over money
or records. These tests are mostly about the gate: secrets stay out of
settings.yaml, an agent only sees the servers it was given, arguments are
checked before anything is sent, a destructive call waits for the owner and
approving it runs exactly what was shown, and a write is not repeated by
accident.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import db
import main
import mcp_client
import mcp_servers
import orchestrator
import runtime_store as store
import settings as cfg
import skills
import tools

import fake_mcp_server as fake


def _mock_transport(sse=False, sessions=False, expire_first=False):
    state = {"expired": not expire_first, "session": "s-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.headers.get("authorization") != "Bearer sekrit":
            return httpx.Response(401)
        message = json.loads(request.content)
        headers = {}
        if sessions:
            if message.get("method") == "initialize":
                headers["mcp-session-id"] = state["session"]
            elif request.headers.get("mcp-session-id") != state["session"]:
                return httpx.Response(400, text="missing session")
            elif not state["expired"] and message.get("method") == "tools/call":
                state["expired"], state["session"] = True, "s-2"
                return httpx.Response(404)
        reply = fake.handle(message)
        if reply is None:
            return httpx.Response(202, headers=headers)
        if sse:
            body = f"event: message\ndata: {json.dumps(reply)}\n\n"
            return httpx.Response(200, text=body, headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=reply, headers=headers)
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'mcp.db')
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(skills, 'USES_PATH', tmp_path / 'skills' / '.uses.json')
    cfg.update("mcp", {"servers": {}})
    mcp_servers._state.clear()
    mcp_servers._connections.clear()
    mcp_servers._locks.clear()
    monkeypatch.setattr(mcp_servers, "http_transport", _mock_transport())
    fake.CALLS.clear()
    yield
    for name in list(mcp_servers.servers()):
        mcp_servers.remove_server(name)
    mcp_servers._connections.clear()
    if db._conn:
        db._conn.close()


def _books(**conf):
    return mcp_servers.save_server("books", {"transport": "http", "url": "http://127.0.0.1:9/mcp", **conf},
                                   token="sekrit")


def _orch():
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


# ── configuration ───────────────────────────────────────────────────────

def test_an_imported_token_goes_to_env_never_to_settings():
    added, problems = mcp_servers.import_config(json.dumps({"mcpServers": {
        "Tracker": {"type": "http", "url": "http://127.0.0.1:9417/mcp",
                      "headers": {"Authorization": "Bearer tk_abc123", "X-Team": "ops"}},
        "files": {"command": "npx", "args": ["-y", "some-server"], "env": {"API_KEY": "k-999"}},
    }}))
    assert added == ["tracker", "files"] and not problems
    stored = json.dumps(cfg.load().get("mcp"))
    assert "tk_abc123" not in stored and "k-999" not in stored
    assert cfg.env_get(mcp_servers.secret_name("tracker")) == "tk_abc123"
    assert cfg.env_get(mcp_servers.secret_name("files", "ENV_API_KEY")) == "k-999"
    assert mcp_servers.servers()["tracker"]["headers"] == {"X-Team": "ops"}
    assert mcp_servers.servers()["files"]["env_keys"] == ["API_KEY"]


def test_removing_a_server_deletes_its_secrets():
    _books()
    assert mcp_servers.remove_server("books")
    assert not cfg.env_get(mcp_servers.secret_name("books"))
    assert "books" not in mcp_servers.servers()


def test_bad_entries_are_refused_with_a_reason():
    for name, conf in [("Bad Name!", {"url": "http://x"}), ("ok", {"transport": "ftp"}),
                       ("ok", {"transport": "http", "url": "file:///etc"}), ("ok", {"transport": "stdio"})]:
        with pytest.raises(mcp_servers.GateError):
            mcp_servers.save_server(name, conf)
    _, problems = mcp_servers.import_config('{"mcpServers": {"old": {"type": "sse", "url": "http://x/sse"}}}')
    assert "SSE" in problems[0]
    with pytest.raises(mcp_servers.GateError):
        mcp_servers.import_config("not json")


# ── the protocol ────────────────────────────────────────────────────────

@pytest.mark.parametrize("sse", [False, True])
def test_http_handshake_list_and_call(sse):
    async def go():
        conn = mcp_client.HTTPConnection("books", "http://test/mcp", {"Authorization": "Bearer sekrit"},
                                         transport=_mock_transport(sse=sse))
        tools_ = await conn.list_tools()
        result = await conn.call_tool("add", {"amount": "4.50"})
        await conn.close()
        return conn, tools_, result
    conn, tools_, result = asyncio.run(go())
    assert conn.server_info["title"] == "Fake Books" and conn.instructions == "Amounts are strings."
    assert [t["name"] for t in tools_] == ["lookup", "add", "wipe", "plain"]
    assert result["structuredContent"] == {"tool": "add", "arguments": {"amount": "4.50"}}


def test_a_server_that_forgets_the_session_gets_a_new_handshake():
    async def go():
        conn = mcp_client.HTTPConnection("books", "http://test/mcp", {"Authorization": "Bearer sekrit"},
                                         transport=_mock_transport(sessions=True, expire_first=True))
        result = await conn.call_tool("lookup", {"q": "x"})
        await conn.close()
        return conn, result
    conn, result = asyncio.run(go())
    assert conn.session_id == "s-2"
    assert "lookup ok" in result["content"][0]["text"]


def test_wrong_credentials_say_where_to_fix_them():
    async def go():
        conn = mcp_client.HTTPConnection("books", "http://test/mcp", {"Authorization": "Bearer nope"},
                                         transport=_mock_transport())
        try:
            await conn.list_tools()
        finally:
            await conn.close()
    with pytest.raises(mcp_client.MCPError, match="Settings > Integrations"):
        asyncio.run(go())


def test_a_stdio_server_answers_and_its_own_requests_are_answered():
    async def go():
        conn = mcp_client.StdioConnection("local", sys.executable,
                                          [str(Path(__file__).with_name("fake_mcp_server.py"))])
        await conn.start()
        try:
            tools_ = await conn.list_tools()
            result = await conn.call_tool("lookup", {"q": "coffee"})
            alive = conn.alive()
        finally:
            await conn.close()
        return tools_, result, alive, conn.alive()
    tools_, result, alive, after = asyncio.run(go())
    assert len(tools_) == 4
    assert "lookup ok" in result["content"][0]["text"]
    assert alive and not after


def test_a_missing_command_is_reported_plainly():
    async def go():
        await mcp_client.StdioConnection("gone", "/no/such/binary", []).start()
    with pytest.raises(mcp_client.MCPError, match="was not found"):
        asyncio.run(go())


# ── what agents see ─────────────────────────────────────────────────────

def test_an_agent_only_sees_servers_it_was_given():
    agents = list(orchestrator.Orchestrator().agents)
    _books(agents=[agents[0]])
    asyncio.run(mcp_servers.refresh("books"))
    assert "books" in mcp_servers.visible(agents[0])
    assert "books" not in mcp_servers.visible(agents[1])
    assert "lookup(q)" in mcp_servers.index_block(agents[0])
    assert mcp_servers.index_block(agents[1]) == ""
    with pytest.raises(mcp_servers.GateError, match="not an MCP server you can use"):
        asyncio.run(mcp_servers.prepare_call(agents[1], "books", "lookup", {"q": "x"}))


def test_arguments_are_checked_before_anything_is_sent():
    _books()
    with pytest.raises(mcp_servers.GateError, match="amount"):
        asyncio.run(mcp_servers.prepare_call("atlas", "books", "add", {"amount": [5]}))
    # A number for a string is fitted rather than refused: "5" is what was meant.
    assert asyncio.run(mcp_servers.prepare_call("atlas", "books", "add", {"amount": 5}))["arguments"] == {"amount": "5"}
    with pytest.raises(mcp_servers.GateError, match="no tool named"):
        asyncio.run(mcp_servers.prepare_call("atlas", "books", "nope", {}))
    assert not [c for c in fake.CALLS if c[0] == "add"]


def test_which_calls_wait_for_the_owner():
    _books()
    plan = lambda tool, args: asyncio.run(mcp_servers.prepare_call("atlas", "books", tool, args))
    assert not plan("lookup", {"q": "x"})["approval"]
    assert not plan("add", {"amount": "1"})["approval"]
    assert plan("wipe", {})["approval"]
    assert plan("plain", {})["approval"]           # unannotated reads as destructive
    _books(approval="writes")
    assert plan("add", {"amount": "1"})["approval"] and not plan("lookup", {"q": "x"})["approval"]
    _books(approval="none")
    assert not plan("wipe", {})["approval"]


def test_results_render_errors_and_images():
    error = mcp_servers.render({"content": [{"type": "text", "text": "nothing matches"}], "isError": True})
    assert error.startswith("error: ")
    picture = mcp_servers.render({"content": [{"type": "text", "text": "a receipt"},
                                              {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]})
    assert picture.content[1]["type"] == "image"
    assert picture.content[1]["source"]["media_type"] == "image/png"


# ── the tool, end to end ────────────────────────────────────────────────

def test_a_read_only_call_runs_and_returns_the_result():
    _books()
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_mcp(aid, {"action": "call", "server": "books", "tool": "lookup",
                                        "arguments": {"q": "coffee"}}))
    assert out.startswith("lookup ok")
    listing = asyncio.run(o._exec_mcp(aid, {"action": "list"}))
    assert "Fake Books" in listing and "wipe [needs owner approval]" in listing
    schema = asyncio.run(o._exec_mcp(aid, {"action": "describe", "server": "books", "tool": "add"}))
    assert '"required"' in schema


def test_the_same_write_twice_within_the_hour_needs_again():
    _books()
    o = _orch()
    aid = list(o.agents)[0]
    args = {"action": "call", "server": "books", "tool": "add", "arguments": {"amount": "9.99"}}
    assert asyncio.run(o._exec_mcp(aid, dict(args))).startswith("add ok")
    assert asyncio.run(o._exec_mcp(aid, dict(args))).startswith("NOT RUN")
    assert asyncio.run(o._exec_mcp(aid, {**args, "again": True})).startswith("add ok")
    assert len([c for c in fake.CALLS if c[0] == "add"]) == 2


def test_a_destructive_call_is_held_and_approving_runs_exactly_it(monkeypatch):
    _books()
    o = _orch()
    aid = list(o.agents)[0]
    out = asyncio.run(o._exec_mcp(aid, {"action": "call", "server": "books", "tool": "wipe",
                                        "arguments": {"confirm": True}}))
    assert out.startswith("held for approval")
    assert not [c for c in fake.CALLS if c[0] == "wipe"]
    row = store.approvals(status="pending")[0]
    assert row["kind"] == "mcp" and row["command"] == 'books.wipe {"confirm": true}'

    again = asyncio.run(o._exec_mcp(aid, {"action": "call", "server": "books", "tool": "wipe",
                                          "arguments": {"confirm": True}}))
    assert again.startswith("still held")

    monkeypatch.setattr(main, 'orch', o)
    monkeypatch.setattr(main, '_api_token', lambda: 'mcp-test-token')
    client = TestClient(main.app, headers={'Authorization': 'Bearer mcp-test-token'})
    inbox = asyncio.run(main._inbox())
    assert any(i["kind"] == "tool" and i["id"] == row["id"] for i in inbox)
    assert client.post(f"/api/approvals/{row['id']}/approve").json()["ok"]
    assert fake.CALLS[-1] == ("wipe", {"confirm": True})
    assert store.approval_get(row["id"])["status"] == "ran"


def test_a_tool_approval_can_never_reach_a_shell():
    out = asyncio.run(tools.run_approved({"kind": "mcp", "command": "echo pwned", "cwd": "/tmp"}))
    assert out.startswith("refused")


def test_settings_endpoints_are_for_the_local_owner_only(monkeypatch):
    monkeypatch.setattr(main, '_api_token', lambda: 'tok')
    client = TestClient(main.app, headers={'Authorization': 'Bearer tok', 'Origin': 'https://evil.example'})
    r = client.put("/api/mcp/evil", json={"transport": "stdio", "command": "/bin/sh", "args": ["-c", "true"]})
    assert r.status_code == 403
    assert "evil" not in mcp_servers.servers()


def test_disabled_server_is_not_a_problem(monkeypatch):
    import mcp_servers
    monkeypatch.setattr(mcp_servers, "_state", {
        "books": {"error": "books is not reachable"},
        "notes": {"error": "notes refused the credentials"}})
    monkeypatch.setattr(mcp_servers, "servers", lambda: {
        "books": {**mcp_servers.DEFAULTS, "enabled": False},
        "notes": {**mcp_servers.DEFAULTS, "enabled": True}})
    assert mcp_servers.problem() == "notes: notes refused the credentials"
