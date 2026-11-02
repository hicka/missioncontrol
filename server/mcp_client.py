"""Speaking the Model Context Protocol to one server, over HTTP or stdio.

MCP is JSON-RPC 2.0. Over HTTP ("Streamable HTTP") every message is a POST, and
the reply comes back either as plain JSON or as a short server-sent-event
stream; a server may hand out a session id that has to be echoed on every
request after initialize, and forgets it with a 404 when it restarts. Over stdio
the server is a child process, messages are single lines on its stdin and
stdout, and the server may send requests of its own (ping, roots) that need an
answer or it waits forever.

This module knows nothing about agents, approvals or settings. mcp_servers.py
decides who may call what; this only carries the messages.
"""
import asyncio
import collections
import itertools
import json
import logging

import httpx

import processes

logger = logging.getLogger("missioncontrol.mcp")

# Newest first. The client asks for a version it fully implements and accepts
# whatever the server settles on from this list.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
REQUESTED_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mission-control", "title": "Mission Control", "version": "1"}
CALL_TIMEOUT = 300
CONNECT_TIMEOUT = 20
MAX_MESSAGE = 16_000_000


class MCPError(Exception):
    """A server that could not be reached or refused a request. The text is
    written for an agent to read."""


class _SessionExpired(Exception):
    pass


class Connection:
    """What every transport shares: the handshake and the three calls agents need."""

    def __init__(self, name: str):
        self.name = name
        self.server_info: dict = {}
        self.instructions = ""
        self.protocol: str | None = None
        self.tools_changed = False
        self._ids = itertools.count(1)
        self._ready = False
        self._handshake = asyncio.Lock()

    async def ensure(self):
        if self._ready:
            return
        async with self._handshake:
            if self._ready:
                return
            result = await self._request("initialize", {
                "protocolVersion": REQUESTED_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, timeout=CONNECT_TIMEOUT)
            version = result.get("protocolVersion")
            if version not in PROTOCOL_VERSIONS:
                logger.warning("mcp %s speaks protocol %s; continuing", self.name, version)
            self.protocol = version or REQUESTED_VERSION
            self.server_info = result.get("serverInfo") or {}
            self.instructions = (result.get("instructions") or "").strip()
            await self._notify("notifications/initialized")
            self._ready = True

    async def list_tools(self) -> list[dict]:
        await self.ensure()
        tools, cursor = [], None
        for _ in range(50):          # a server that pages forever is not a reason to hang
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        await self.ensure()
        return await self._request("tools/call", {"name": tool, "arguments": arguments})

    async def ping(self) -> bool:
        try:
            await self.ensure()
            await self._request("ping", {}, timeout=CONNECT_TIMEOUT)
            return True
        except MCPError:
            return False

    def alive(self) -> bool:
        return True

    async def close(self):
        pass

    # transport-specific
    async def _request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> dict:
        raise NotImplementedError

    async def _notify(self, method: str, params: dict | None = None):
        raise NotImplementedError

    def _reply(self, message: dict, method: str) -> dict:
        if "error" in message:
            error = message.get("error") or {}
            raise MCPError(f"{self.name} refused {method}: {error.get('message') or error}")
        return message.get("result") or {}


class HTTPConnection(Connection):
    def __init__(self, name: str, url: str, headers: dict | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(name)
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: str | None = None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=CONNECT_TIMEOUT),
                                        transport=transport)

    async def _request(self, method, params, timeout=CALL_TIMEOUT):
        for attempt in range(2):
            mid = next(self._ids)
            try:
                replies = await asyncio.wait_for(
                    self._post({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}, mid), timeout)
            except _SessionExpired:
                # The server restarted and forgot us: handshake again, once.
                self._ready, self.session_id = False, None
                if method == "initialize" or attempt:
                    raise MCPError(f"{self.name} dropped the session; try again")
                await self.ensure()
                continue
            except asyncio.TimeoutError:
                raise MCPError(f"{self.name} did not answer {method} within {int(timeout)}s") from None
            for message in replies:
                if message.get("id") == mid and ("result" in message or "error" in message):
                    return self._reply(message, method)
            raise MCPError(f"{self.name} sent no reply to {method}")
        raise MCPError(f"{self.name} could not be reached")

    async def _notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        await self._post(message, None)

    async def _post(self, message: dict, mid) -> list[dict]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   **self.headers}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            async with self.client.stream("POST", self.url, json=message, headers=headers) as response:
                if response.status_code == 404 and self.session_id:
                    raise _SessionExpired()
                if response.status_code in (401, 403):
                    raise MCPError(f"{self.name} refused the credentials (HTTP {response.status_code}). The owner "
                                   "updates the token in Settings > Integrations > MCP servers.")
                if response.status_code >= 400:
                    body = (await response.aread())[:300].decode("utf-8", "replace")
                    raise MCPError(f"{self.name} answered HTTP {response.status_code}: {body}")
                if response.headers.get("mcp-session-id"):
                    self.session_id = response.headers["mcp-session-id"]
                if response.status_code in (202, 204):
                    return []
                if "text/event-stream" in response.headers.get("content-type", ""):
                    return await self._events(response, mid)
                body = await response.aread()
        except httpx.HTTPError as exc:
            raise MCPError(f"{self.name} is not reachable at {self.url} ({type(exc).__name__}). "
                           "Check that the app serving it is running.") from None
        if not body.strip():
            return []
        try:
            data = json.loads(body)
        except ValueError:
            raise MCPError(f"{self.name} sent something that is not JSON") from None
        return data if isinstance(data, list) else [data]

    async def _events(self, response, mid) -> list[dict]:
        """Read server-sent events until the reply to `mid` arrives."""
        messages, data, size = [], [], 0
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                chunk = line[5:].lstrip()
                size += len(chunk)
                if size > MAX_MESSAGE:
                    raise MCPError(f"{self.name} sent an event larger than {MAX_MESSAGE // 1_000_000} MB")
                data.append(chunk)
            elif not line.strip() and data:
                try:
                    message = json.loads("".join(data))
                except ValueError:
                    message = None
                data, size = [], 0
                if isinstance(message, dict):
                    messages.append(message)
                    if mid is not None and message.get("id") == mid:
                        break
        return messages

    async def close(self):
        if self.session_id:
            try:
                await self.client.delete(self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id},
                                         timeout=3)
            except httpx.HTTPError:
                pass
        await self.client.aclose()


class StdioConnection(Connection):
    """A server that runs as a child process. Started by the MCP runtime service so
    it belongs to that service, not to whichever agent happened to need it first."""

    def __init__(self, name: str, command: str, args: list[str], env: dict | None = None,
                 cwd: str | None = None):
        super().__init__(name)
        self.command, self.args, self.env, self.cwd = command, list(args or []), dict(env or {}), cwd
        self.proc = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = None
        self._stderr_task = None
        self.stderr_tail = collections.deque(maxlen=12)

    async def start(self):
        import runtime
        try:
            self.proc = await processes.create_subprocess_exec(
                self.command, *self.args, cwd=self.cwd or None, env=processes.environment(self.env),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **processes.spawn_options())
        except FileNotFoundError:
            raise MCPError(f"{self.name}: `{self.command}` was not found. Check the command in "
                           "Settings > Integrations > MCP servers.") from None
        except OSError as exc:
            raise MCPError(f"{self.name} could not start: {exc}") from None
        self._reader = runtime.spawn(self._read(), "mcp", f"mcp {self.name} stdout")
        self._stderr_task = runtime.spawn(self._drain_stderr(), "mcp", f"mcp {self.name} stderr")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _send(self, message: dict):
        if not self.alive():
            raise MCPError(self._exit_text())
        self.proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise MCPError(self._exit_text()) from None

    async def _request(self, method, params, timeout=CALL_TIMEOUT):
        mid = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            message = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            try:
                await self._notify("notifications/cancelled", {"requestId": mid, "reason": "timed out"})
            except MCPError:
                pass
            raise MCPError(f"{self.name} did not answer {method} within {int(timeout)}s") from None
        finally:
            self._pending.pop(mid, None)
        return self._reply(message, method)

    async def _notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        await self._send(message)

    async def _read(self):
        # Chunked rather than line-based: one message (a file, a big listing)
        # can be far longer than asyncio's 64 KB line limit.
        buffer = b""
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_MESSAGE:
                    logger.warning("mcp %s: message over %d bytes dropped", self.name, MAX_MESSAGE)
                    buffer = b""
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        await self._dispatch(line)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPError(self._exit_text()))
            self._ready = False

    async def _dispatch(self, line: bytes):
        try:
            message = json.loads(line)
        except ValueError:
            return               # servers that log to stdout get ignored, not obeyed
        if not isinstance(message, dict):
            return
        mid = message.get("id")
        if "method" not in message:
            future = self._pending.get(mid)
            if future and not future.done():
                future.set_result(message)
            return
        method = message.get("method")
        if mid is None:
            if method == "notifications/tools/list_changed":
                self.tools_changed = True
            return
        # The server asked us something. Answer what is harmless; refuse the rest.
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {}}
        elif method == "roots/list":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"roots": []}}
        else:
            reply = {"jsonrpc": "2.0", "id": mid,
                     "error": {"code": -32601, "message": f"Mission Control does not offer {method}"}}
        try:
            await self._send(reply)
        except MCPError:
            pass

    async def _drain_stderr(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text[:300])

    def _exit_text(self) -> str:
        code = self.proc.returncode if self.proc else None
        tail = " | ".join(list(self.stderr_tail)[-3:])
        return (f"{self.name} is not running" + (f" (exited {code})" if code is not None else "")
                + (f": {tail}" if tail else ""))

    async def close(self):
        for task in (self._reader, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            await processes.terminate(self.proc)
        self._ready = False
