"""MCP servers the owner connected: what they offer, and who may use it.

The owner adds a server in Settings > Integrations, by hand or by pasting the
`mcpServers` JSON other apps print (Claude Desktop, Cursor). Its
secrets, a bearer token or a local command's environment, go to .env like every
other key; settings.yaml only names them, so the settings payload never carries
a credential.

Agents reach every server through one `mcp` tool rather than one tool per
server tool. One app alone can bring two dozen tools, and offering all of
them on every turn would crowd out the work and the smaller local models. The
context lists what is connected with each tool's required inputs, and an agent
describes a tool before its first call.

The gate decides, not the model. A server can be limited to some agents. A
tool the server marks destructive (or does not mark at all, which the protocol
says to read as destructive) waits in the Inbox like a risky shell command, and
approving runs exactly the arguments shown. A write that already ran in the
last hour is refused unless the agent passes again, the rule the terminal
follows for outward commands.
"""
import asyncio
import base64
import hashlib
import json
import logging
import re
import time

import settings as cfg
import mcp_client
from mcp_client import MCPError

logger = logging.getLogger("missioncontrol.mcp")

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRANSPORTS = ("http", "stdio")
APPROVAL = ("destructive", "writes", "none")
REFRESH_EVERY = 300          # seconds between tool list refreshes
INDEX_TOOLS = 40             # tools named per server in an agent's context
RESULT_CAP = 12000
LEDGER_WINDOW = 3600

DEFAULTS = {
    "transport": "http", "url": "", "headers": {}, "secret_headers": [],
    "command": "", "args": [], "env_keys": [], "cwd": "",
    "enabled": True, "agents": None, "approval": "destructive",
}
KEYS = tuple(DEFAULTS)

# Tests swap in an httpx.MockTransport here.
http_transport = None

_connections: dict[str, mcp_client.Connection] = {}
_state: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


class GateError(Exception):
    """A refusal written for the agent: what was wrong and what to do instead."""


# ── configuration ───────────────────────────────────────────────────────

def servers() -> dict[str, dict]:
    raw = ((cfg.load().get("mcp") or {}).get("servers")) or {}
    return {name: {**DEFAULTS, **{k: v for k, v in (conf or {}).items() if k in KEYS}}
            for name, conf in raw.items() if NAME.match(str(name))}


def enabled() -> bool:
    return any(conf["enabled"] for conf in servers().values())


def secret_name(server: str, part: str = "TOKEN") -> str:
    return "MCP_" + re.sub(r"[^A-Z0-9]", "_", f"{server}_{part}".upper())


def slug(raw: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lower()).strip("-_")[:32]
    return text if NAME.match(text) else ""


def _looks_secret(header: str) -> bool:
    return bool(re.search(r"(?i)auth|token|key|secret|cookie|session|pass", header))


def save_server(name: str, conf: dict, token: str | None = None, env_values: dict | None = None,
                header_values: dict | None = None) -> dict:
    """Validate and store one server. Secrets go to .env; None leaves a stored one alone."""
    if not NAME.match(name or ""):
        raise GateError("a server name is 1-32 lowercase letters, digits, - or _")
    clean = {**DEFAULTS, **(servers().get(name) or {}), **{k: v for k, v in conf.items() if k in KEYS}}
    if clean["transport"] not in TRANSPORTS:
        raise GateError("transport is http or stdio")
    if clean["approval"] not in APPROVAL:
        raise GateError("approval is destructive, writes or none")
    if clean["transport"] == "http":
        if not re.match(r"^https?://", clean["url"] or ""):
            raise GateError("an http server needs a URL starting with http:// or https://")
    elif not (clean["command"] or "").strip():
        raise GateError("a stdio server needs the command that starts it")
    if clean["agents"] is not None:
        clean["agents"] = [str(a) for a in clean["agents"] if str(a).strip()] or None
    clean["args"] = [str(a) for a in clean["args"] or []]
    clean["headers"] = {str(k): str(v) for k, v in (clean["headers"] or {}).items() if not _looks_secret(str(k))}
    clean["enabled"] = bool(clean["enabled"])

    if token is not None:
        cfg.env_set(secret_name(name), token.strip())
    for key, value in (env_values or {}).items():
        key = re.sub(r"[^A-Za-z0-9_]", "_", str(key))
        cfg.env_set(secret_name(name, "ENV_" + key), str(value))
        if key not in clean["env_keys"]:
            clean["env_keys"] = [*clean["env_keys"], key]
    for header, value in (header_values or {}).items():
        cfg.env_set(secret_name(name, "HEADER_" + header), str(value))
        if header not in clean["secret_headers"]:
            clean["secret_headers"] = [*clean["secret_headers"], header]

    everything = {n: c for n, c in servers().items()}
    everything[name] = clean
    cfg.update("mcp", {"servers": everything})
    _state.pop(name, None)
    return clean


def remove_server(name: str) -> bool:
    everything = servers()
    conf = everything.pop(name, None)
    if conf is None:
        return False
    cfg.update("mcp", {"servers": everything})
    cfg.env_set(secret_name(name), "")
    for key in conf.get("env_keys") or []:
        cfg.env_set(secret_name(name, "ENV_" + key), "")
    for header in conf.get("secret_headers") or []:
        cfg.env_set(secret_name(name, "HEADER_" + header), "")
    _state.pop(name, None)
    return True


def import_config(text: str) -> tuple[list[str], list[str]]:
    """Add the servers in an `mcpServers` (or VS Code `servers`) JSON block."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GateError(f"that is not JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(data, dict):
        raise GateError("expected a JSON object with an mcpServers section")
    block = data.get("mcpServers") or data.get("servers")
    if block is None and data and all(isinstance(v, dict) for v in data.values()):
        block = data
    if not isinstance(block, dict) or not block:
        raise GateError("no servers found; paste a block like {\"mcpServers\": {\"name\": {...}}}")
    added, problems = [], []
    for raw_name, spec in block.items():
        name = slug(raw_name)
        if not name or not isinstance(spec, dict):
            problems.append(f"{raw_name}: skipped, not a valid server entry")
            continue
        kind = str(spec.get("type") or spec.get("transport") or "").lower()
        try:
            if spec.get("url"):
                if kind == "sse":
                    problems.append(f"{raw_name}: the older SSE transport is not supported; use the "
                                    "server's streamable HTTP endpoint")
                    continue
                headers, token, secrets = {}, None, {}
                for header, value in (spec.get("headers") or {}).items():
                    value = str(value)
                    if header.lower() == "authorization" and value.lower().startswith("bearer "):
                        token = value[7:].strip()
                    elif _looks_secret(header):
                        secrets[header] = value
                    else:
                        headers[header] = value
                save_server(name, {"transport": "http", "url": spec["url"], "headers": headers,
                                   "enabled": not spec.get("disabled", False)},
                            token=token, header_values=secrets)
            elif spec.get("command"):
                save_server(name, {"transport": "stdio", "command": str(spec["command"]),
                                   "args": spec.get("args") or [], "cwd": spec.get("cwd") or "",
                                   "enabled": not spec.get("disabled", False)},
                            env_values=spec.get("env") or {})
            else:
                problems.append(f"{raw_name}: needs a url or a command")
                continue
            added.append(name)
        except GateError as exc:
            problems.append(f"{raw_name}: {exc}")
    return added, problems


# ── connections ─────────────────────────────────────────────────────────

def _fingerprint(name: str, conf: dict) -> str:
    secrets = [cfg.env_get(secret_name(name)) or ""]
    secrets += [cfg.env_get(secret_name(name, "ENV_" + k)) or "" for k in conf.get("env_keys") or []]
    secrets += [cfg.env_get(secret_name(name, "HEADER_" + h)) or "" for h in conf.get("secret_headers") or []]
    raw = json.dumps(conf, sort_keys=True) + "\0".join(secrets)
    return hashlib.sha256(raw.encode()).hexdigest()


def _build(name: str, conf: dict) -> mcp_client.Connection:
    if conf["transport"] == "http":
        headers = dict(conf.get("headers") or {})
        for header in conf.get("secret_headers") or []:
            value = cfg.env_get(secret_name(name, "HEADER_" + header))
            if value:
                headers[header] = value
        token = cfg.env_get(secret_name(name))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return mcp_client.HTTPConnection(name, conf["url"], headers, transport=http_transport)
    env = {key: cfg.env_get(secret_name(name, "ENV_" + key)) or "" for key in conf.get("env_keys") or []}
    return mcp_client.StdioConnection(name, conf["command"], conf["args"], env, conf.get("cwd") or None)


def _service_running() -> bool:
    try:
        import runtime
        service = runtime.registry.services.get("mcp")
        return bool(service) and runtime.registry.enabled("mcp") and service.state == "running"
    except Exception:
        return False


async def connection(name: str, conf: dict | None = None, starting: bool = False) -> mcp_client.Connection:
    """The live connection to a server. HTTP servers connect on demand; local
    commands are only started by the MCP service (`starting=True`), so the
    process belongs to it and not to the agent that happened to ask first."""
    conf = conf or servers().get(name)
    if not conf or not conf["enabled"]:
        raise GateError(f"{name} is not a connected MCP server")
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        fingerprint = _fingerprint(name, conf)
        current = _connections.get(name)
        if current and getattr(current, "fingerprint", None) == fingerprint and current.alive():
            return current
        if conf["transport"] == "stdio" and not starting:
            error = (_state.get(name) or {}).get("error")
            if not _service_running():
                raise MCPError(f"{name} runs as a local command, and MCP servers are switched off in "
                               "Runtime Manager")
            raise MCPError(f"{name} is not running yet" + (f": {error}" if error else "; try again shortly"))
        if current:
            await current.close()
            _connections.pop(name, None)
        built = _build(name, conf)
        built.fingerprint = fingerprint
        if isinstance(built, mcp_client.StdioConnection):
            await built.start()
        _connections[name] = built
        return built


async def refresh(name: str, conf: dict | None = None, starting: bool = False) -> dict:
    """Connect if needed and reload the tool list. Returns the server's state."""
    state = _state.setdefault(name, {"tools": [], "error": None, "checked": 0.0, "failures": 0})
    try:
        conn = await connection(name, conf, starting=starting)
        tools = await conn.list_tools()
        state.update(tools=tools, error=None, checked=time.time(), failures=0, retry_at=0,
                     server=conn.server_info, instructions=conn.instructions)
    except (MCPError, GateError) as exc:
        failures = state.get("failures", 0) + 1
        state.update(error=str(exc), checked=time.time(), failures=failures,
                     retry_at=time.time() + min(300, 5 * 2 ** min(failures, 6)))
        broken = _connections.pop(name, None)
        if broken:
            await broken.close()
    return state


async def reconcile():
    configured = servers()
    for name in list(_connections):
        if name not in configured or not configured[name]["enabled"]:
            conn = _connections.pop(name)
            await conn.close()
    for name in list(_state):
        if name not in configured or not configured[name]["enabled"]:
            _state.pop(name, None)
    now = time.time()
    for name, conf in configured.items():
        if not conf["enabled"]:
            continue
        state = _state.get(name) or {}
        conn = _connections.get(name)
        current = (conn is not None and conn.alive()
                   and getattr(conn, "fingerprint", None) == _fingerprint(name, conf))
        if not current:
            if now >= state.get("retry_at", 0):
                await refresh(name, conf, starting=True)
        elif conn.tools_changed or now - state.get("checked", 0) > REFRESH_EVERY:
            await refresh(name, conf, starting=True)


async def supervise():
    """The MCP runtime service: keeps local servers running and tool lists fresh."""
    try:
        while True:
            try:
                await reconcile()
            except Exception:
                logger.exception("mcp reconcile failed")
            await asyncio.sleep(5)
    finally:
        await close_all()


async def close_all():
    for name in list(_connections):
        conn = _connections.pop(name)
        try:
            await conn.close()
        except Exception:
            logger.exception("closing mcp %s failed", name)


def problem() -> str | None:
    """Errors from the servers that are switched on; a disabled one is not a problem."""
    configured = servers()
    broken = [f"{name}: {s['error']}" for name, s in _state.items()
              if s.get("error") and configured.get(name, {}).get("enabled")]
    return "; ".join(broken)[:300] or None


def status() -> list[dict]:
    rows = []
    for name, conf in servers().items():
        state = _state.get(name) or {}
        info = state.get("server") or {}
        rows.append({
            "name": name, **{k: conf[k] for k in ("transport", "url", "command", "args", "cwd", "enabled",
                                                   "agents", "approval", "headers", "env_keys", "secret_headers")},
            "token_set": bool(cfg.env_get(secret_name(name))),
            "connected": bool(state.get("tools")) and not state.get("error"),
            "tools": [t["name"] for t in state.get("tools") or []],
            "error": state.get("error"),
            "title": info.get("title") or info.get("name") or "",
            "version": info.get("version") or "",
            "checked": state.get("checked") or 0,
        })
    return rows


# ── what agents see ─────────────────────────────────────────────────────

def visible(aid: str) -> dict[str, dict]:
    return {name: conf for name, conf in servers().items()
            if conf["enabled"] and (conf["agents"] is None or aid in conf["agents"])}


def _annotations(tool: dict) -> dict:
    return tool.get("annotations") or {}


def _read_only(tool: dict) -> bool:
    return _annotations(tool).get("readOnlyHint") is True


def _destructive(tool: dict) -> bool:
    # The protocol's default: a tool that is not read-only is destructive unless it says otherwise.
    return not _read_only(tool) and _annotations(tool).get("destructiveHint", True) is not False


def _signature(tool: dict) -> str:
    required = (tool.get("inputSchema") or {}).get("required") or []
    return f"{tool['name']}({', '.join(required)})" if required else tool["name"]


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[:limit - 1] + "…"


def index_block(aid: str) -> str:
    rows = []
    for name in visible(aid):
        state = _state.get(name) or {}
        tools = state.get("tools") or []
        info = state.get("server") or {}
        title = info.get("title") or info.get("name") or name
        if not tools:
            reason = state.get("error") or "connecting"
            rows.append(f"{name} ({title}): unavailable, {reason[:120]}")
            continue
        names = ", ".join(_signature(t) for t in tools[:INDEX_TOOLS])
        if len(tools) > INDEX_TOOLS:
            names += f", and {len(tools) - INDEX_TOOLS} more (mcp list)"
        rows.append(f"{name} ({title}): {names}")
    if not rows:
        return ""
    return ("\nMCP SERVERS (the mcp tool; run action=describe for a tool's inputs before its first "
            "call): " + "; ".join(rows) + "\n")


async def _tools_for(aid: str, server: str) -> tuple[dict, list[dict]]:
    allowed = visible(aid)
    if server not in allowed:
        names = ", ".join(allowed) or "none"
        raise GateError(f"{server or '(no server)'} is not an MCP server you can use. Yours: {names}. "
                        "The owner connects servers in Settings > Integrations > MCP servers.")
    conf = allowed[server]
    state = _state.get(server) or {}
    if not state.get("tools") or state.get("error"):
        state = await refresh(server, conf)
        if state.get("error"):
            raise GateError(f"{server} is unavailable: {state['error']}")
    return conf, state.get("tools") or []


async def overview(aid: str) -> str:
    allowed = visible(aid)
    if not allowed:
        return ("no MCP servers are connected for you. The owner adds them in Settings > "
                "Integrations > MCP servers.")
    out = []
    for name, conf in allowed.items():
        try:
            _, tools = await _tools_for(aid, name)
        except GateError as exc:
            out.append(f"## {name}\n{exc}")
            continue
        state = _state.get(name) or {}
        info = state.get("server") or {}
        head = f"## {name}: {info.get('title') or info.get('name') or name} {info.get('version') or ''}".rstrip()
        lines = [head]
        if state.get("instructions"):
            lines.append(state["instructions"][:1200])
        for tool in tools:
            marks = " [read-only]" if _read_only(tool) else (" [needs owner approval]" if _needs_approval(conf, tool) else "")
            lines.append(f"- {_signature(tool)}{marks}: {_first_sentence(tool.get('description') or '')}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


async def describe(aid: str, server: str, tool: str) -> str:
    conf, tools = await _tools_for(aid, server)
    spec = next((t for t in tools if t["name"] == tool), None)
    if spec is None:
        raise GateError(f"{server} has no tool named {tool or '(none)'}. Its tools: "
                        + ", ".join(t["name"] for t in tools))
    schema = json.dumps(spec.get("inputSchema") or {"type": "object"}, indent=1, ensure_ascii=False)
    note = ("Read-only." if _read_only(spec) else
            "Held for the owner's approval before it runs." if _needs_approval(conf, spec) else
            "Changes data; a repeat within the hour needs again=true." if not _annotations(spec).get("idempotentHint")
            else "Changes data; safe to repeat.")
    return (f"{server}.{tool}: {spec.get('description') or ''}\n{note}\n"
            f"Input schema:\n{schema[:6000]}")


def _needs_approval(conf: dict, tool: dict) -> bool:
    policy = conf.get("approval") or "destructive"
    if policy == "none":
        return False
    if policy == "writes":
        return not _read_only(tool)
    return _destructive(tool)


async def prepare_call(aid: str, server: str, tool: str, arguments, file: str | None = None) -> dict:
    """Everything decided before a call goes out: access, the tool, its arguments.
    `file` names a file Mission Control holds, for a tool that takes file_base64."""
    conf, tools = await _tools_for(aid, server)
    spec = next((t for t in tools if t["name"] == tool), None)
    if spec is None:
        raise GateError(f"{server} has no tool named {tool or '(none)'}. Its tools: "
                        + ", ".join(t["name"] for t in tools))
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise GateError("arguments must be an object matching the tool's input schema")
    arguments = coerce(spec.get("inputSchema") or {}, arguments)
    # A file named inside the tool's own arguments ("file_path": "whatsapp:12")
    # is what the agent meant by `file`: the server could never open that path.
    takes_files = "file_base64" in ((spec.get("inputSchema") or {}).get("properties") or {})
    for key in FILE_KEYS if takes_files else ():
        value = arguments.get(key)
        if isinstance(value, str) and _file_ref(value):
            file = file or value.strip()
            arguments = {k: v for k, v in arguments.items() if k != key}
    if file:
        if file.strip().startswith("whatsapp:") and file.strip()[9:].isdigit():
            import whatsapp_link        # a file from history is fetched from the phone first
            await whatsapp_link.ensure_file(int(file.strip()[9:]))
        arguments = with_file(spec, arguments, file)
    problem = validate(spec, arguments)
    if problem:
        raise GateError(f"invalid arguments for {server}.{tool}: {problem}. Run mcp action=describe "
                        f"server={server} tool={tool} for the schema.")
    return {"conf": conf, "spec": spec, "arguments": arguments,
            "approval": _needs_approval(conf, spec),
            "outward": not _read_only(spec) and not _annotations(spec).get("idempotentHint")}


FILE_KEYS = ("file", "file_path", "file_url", "file_base64", "path", "receipt")


def _file_ref(value: str) -> bool:
    value = value.strip()
    if re.fullmatch(r"whatsapp:\d+", value):
        return True
    import uploads
    return value.startswith(str(uploads.UPLOAD_DIR))


def coerce(schema: dict, value, depth: int = 0):
    """Arguments made to fit the types the tool declared, where that is unambiguous.

    Models get JSON types wrong in ways that are plainly fixable: an amount sent
    as 1180 to a tool that takes the string "1180.00", "true" for a boolean, a
    list sent as a JSON string, or a list wrapped the way XML tool calls wrap
    one ({"item": [...]}). Each of those was a failed call and a retry; a value
    that does not convert cleanly is left as it is for validation to refuse.
    """
    if depth > 6 or not isinstance(schema, dict):
        return value
    kinds = schema.get("type")
    kinds = set(kinds) if isinstance(kinds, list) else {kinds} if kinds else set()
    if not kinds and schema.get("properties"):
        kinds = {"object"}
    if isinstance(value, str) and kinds & {"array", "object"} and value.strip()[:1] in "[{":
        try:
            parsed = json.loads(value)
            value = parsed
        except ValueError:
            pass
    if "array" in kinds and isinstance(value, dict) and len(value) == 1:
        inner = next(iter(value.values()))
        if isinstance(inner, list):
            value = inner                          # {"item": [...]} is the list
        elif isinstance(inner, dict):
            value = [inner]                        # {"item": {...}} is a list of one
    if "object" in kinds and isinstance(value, dict):
        props = schema.get("properties") or {}
        return {k: coerce(props.get(k) or {}, v, depth + 1) if k in props else v for k, v in value.items()}
    if "array" in kinds and isinstance(value, list):
        return [coerce(schema.get("items") or {}, v, depth + 1) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if "string" in kinds and isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else repr(value)
    if isinstance(value, str) and not kinds & {"string"}:
        text = value.strip()
        if "boolean" in kinds and text.lower() in ("true", "false"):
            return text.lower() == "true"
        if "integer" in kinds and re.fullmatch(r"-?\d+", text):
            return int(text)
        if "number" in kinds and re.fullmatch(r"-?\d+(\.\d+)?", text):
            return float(text) if "." in text else int(text)
    return value


def validate(spec: dict, arguments: dict) -> str | None:
    schema = spec.get("inputSchema")
    if not isinstance(schema, dict):
        return None
    try:
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(schema)
    except Exception:
        return None          # a server's broken schema is not a reason to refuse every call
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.path) or "arguments"
    return f"{where}: {first.message}"


async def call(server: str, tool: str, arguments: dict):
    conn = await connection(server)
    return render(await conn.call_tool(tool, await asyncio.to_thread(expand_files, arguments)))


# ── files ───────────────────────────────────────────────────────────────
#
# An agent cannot paste a photo into a tool call: it would have to write out
# megabytes of base64. It names the file instead, and the call carries a
# reference (FILE_MARK + the name) in file_base64 until the moment it is sent.
# The approval the owner reads and the ledger both show the reference, and the
# bytes are read at send time. Only files Mission Control was given can be
# named: a WhatsApp attachment, or a file sent into the chat. Nothing else on
# this Mac can be handed to a server this way.

FILE_MARK = "mc-file:"
FILE_CAP = 20 * 1024 * 1024


def attachment(ref: str) -> tuple[bytes, str, str]:
    """(bytes, file name, MIME type) for a file reference. Raises GateError."""
    import mimetypes
    from pathlib import Path
    ref = (ref or "").strip()
    if ref.startswith("whatsapp:"):
        import whatsapp_link
        try:
            return whatsapp_link.file_for(ref)
        except ValueError as exc:
            raise GateError(str(exc)) from None
    import uploads
    folder = uploads.UPLOAD_DIR.resolve()
    path = Path(ref).expanduser()
    path = (path if path.is_absolute() else folder / path.name).resolve()
    if folder not in path.parents or not path.is_file():
        raise GateError(f"{ref or '(no file)'} is not a file you can send: name a WhatsApp file "
                        "(whatsapp:<id>) or a file sent into the chat")
    if path.stat().st_size > FILE_CAP:
        raise GateError(f"{path.name} is larger than {FILE_CAP // 1048576} MB")
    return path.read_bytes(), path.name.split("-", 1)[-1], mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def with_file(spec: dict, arguments: dict, ref: str) -> dict:
    props = (spec.get("inputSchema") or {}).get("properties") or {}
    if "file_base64" not in props:
        raise GateError(f"{spec['name']} does not take a file (it has no file_base64 input)")
    _, name, mime = attachment(ref)             # refuse now, not at send time
    out = {**arguments, "file_base64": FILE_MARK + ref.strip()}
    for key in ("file_path", "file_url"):
        out.pop(key, None)
    if "filename" in props and not out.get("filename"):
        out["filename"] = name
    if "content_type" in props and not out.get("content_type"):
        out["content_type"] = mime
    return out


def expand_files(arguments: dict) -> dict:
    value = arguments.get("file_base64")
    if not (isinstance(value, str) and value.startswith(FILE_MARK)):
        return arguments
    data, _, _ = attachment(value[len(FILE_MARK):])
    return {**arguments, "file_base64": base64.b64encode(data).decode()}


def render(result: dict):
    """Tool output as text, with images passed through for models that can see them."""
    from harness import ToolResult
    parts, images = [], []
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "image" and block.get("data"):
            images.append(block)
            parts.append(f"[image {block.get('mimeType') or ''} attached]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            if "text" in resource:
                parts.append(resource["text"])
            else:
                size = len(resource.get("blob") or "") * 3 // 4
                parts.append(f"[{resource.get('mimeType') or 'file'} {resource.get('uri') or ''}, {size} bytes]")
        elif kind == "resource_link":
            parts.append(f"[link {block.get('uri') or ''}]")
        elif kind:
            parts.append(f"[{kind} content]")
    if not any(p.strip() for p in parts) and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p).strip() or "(no output)"
    if len(text) > RESULT_CAP:
        text = text[:RESULT_CAP - 2000] + f"\n... [{len(text)} chars, truncated] ...\n" + text[-1500:]
    if result.get("isError"):
        text = "error: " + text
    if not images:
        return text
    blocks = [{"type": "text", "text": text}] + [
        {"type": "image", "source": {"type": "base64", "media_type": img.get("mimeType") or "image/png",
                                     "data": img["data"]}} for img in images[:3]]
    return ToolResult(text, content=blocks)


# ── approvals and the ledger ────────────────────────────────────────────

def approval_command(server: str, tool: str, arguments: dict) -> str:
    """What the owner reads and approves, and exactly what runs after a yes."""
    return f"{server}.{tool} {json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


def parse_approval(command: str) -> tuple[str, str, dict]:
    head, _, body = command.partition(" ")
    server, _, tool = head.partition(".")
    arguments = json.loads(body or "{}")
    if not (server and tool and isinstance(arguments, dict)):
        raise GateError("not an MCP call")
    return server, tool, arguments


def ledger_key(server: str, tool: str, arguments: dict) -> str:
    return "mcp " + approval_command(server, tool, arguments)


def repeat_refusal(prior: dict) -> str:
    import datetime
    when = datetime.datetime.fromtimestamp(prior["ts"]).strftime("%H:%M")
    outcome = (prior.get("outcome") or "").strip().replace("\n", " ")[:200]
    return (f"NOT RUN: this exact call already ran at {when} by {prior['agent']} (result: "
            f"{outcome or 'no output'}). Running it again would repeat the change. Check the current "
            "state instead; if it genuinely must run again, call it with again=true and say why.")


async def run_approved(request: dict) -> str:
    """Run one call the owner approved, with the stored arguments and nothing else."""
    try:
        server, tool, arguments = parse_approval(request["command"])
        if server not in servers() or not servers()[server]["enabled"]:
            return f"not run: {server} is no longer connected"
        result = await call(server, tool, arguments)
        return str(result)
    except (GateError, MCPError, ValueError) as exc:
        return f"failed: {exc}"
