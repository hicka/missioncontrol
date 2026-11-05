"""Modules: what agents build to add to Mission Control while it runs.

A module is a folder under `modules/<slug>/` with a manifest (`module.json`) and Python
code whose `apply(ctx)` registers what the module adds: tools the agents can call, cards
the apps draw on the owner's desktop, timers, and things to tell the owner or hand to an
agent. Nothing about what a module is for is written here. A site check, a price
tracker, a build board and a pomodoro timer are all the same thing to this file.

The design follows DeepSeek Harness's "everything is a plugin": everything a module
registers belongs to it and goes when it stops, so a new version starts from nothing and
a module that is gone leaves nothing running behind it. Its cards stay up across a
restart, so a reload does not blink, and a card the new version does not draw again is
taken down soon after it is ready.

Each module runs as its own process (`module_host.py`), so a crash takes only itself
down. On macOS that process runs in a sandbox: it reads nothing in the home folder or the
data folder except its own code, writes only to its own `data/` folder, sends no Apple
events, reads no clipboard or keychain, cannot signal or inspect other processes, and
reaches the network only if its manifest says so. The owner's keys reach it only by
name, as its manifest lists them.

Trust follows the rest of the app. Inferred work cannot touch modules at all. A version
an agent wrote because the owner asked for it runs at once, since the sandbox holds what
it could otherwise reach. Anything else waits in the Inbox: a version written by standing
work, a folder that appeared or changed on disk outside the module tool, and every
version on a system without the sandbox. Approval is of exact code: the files are hashed,
and the hash the owner or their request approved is the only one that runs.
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path

import db
import module_ui
import processes
import runtime
import settings
from apphome import BUNDLE, ROOT

log = logging.getLogger("missioncontrol.modules")

FOLDER = ROOT / "modules"
# Server-owned: sandbox profiles. A module writes only to its own data folder.
RUN = FOLDER / ".run"
MANIFEST = "module.json"
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
FILE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
FILE_TYPES = {".py", ".json", ".txt", ".md", ".csv", ".yaml", ".yml", ".toml", ".html", ".css", ".js", ".svg"}

MAX_FILES = 40
MAX_FILE = 128_000
MAX_TOTAL = 512_000
MAX_TOOLS = 16
MAX_CARDS = 8
MAX_LINE = 1_000_000
MAX_STATE = 256_000
MAX_CONFIG = 32_000
START_WAIT = 12        # how long the tool waits for a new version to be ready
APPLY_LIMIT = 60       # an apply(ctx) still running after this is stuck
SETTLE = 2.5           # and then for its first timers to draw or fail
CALL_TIMEOUT = 90
STOP_WAIT = 4
TICK = 2
SCAN_EVERY = 6         # seconds between looks at the files on disk
SWEEP_AFTER = 15       # a card the new version has not drawn by now is gone
LOG_LINES = 300
# What a module may say to people, and to agents, which costs a model run each time.
NOTIFY_GAP, NOTIFY_DAY = 60, 20
ASK_GAP, ASK_DAY = 600, 12
RENDERS_PER_MINUTE = 240
# A crash comes back after these waits. One that keeps crashing stops and says so.
RESTART_WAITS = (5, 15, 45, 120, 300)
STEADY = 180           # up this long, and its crashes are forgiven

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

orch = None
emit = None

_running: dict[str, "Running"] = {}
_logs: dict[str, deque] = {}
_said: dict[str, dict] = {}        # slug -> {"notify": deque of times, "ask": ...}
_crashes: dict[str, list] = {}     # slug -> [times it crashed]
_retry_at: dict[str, float] = {}
_cards: dict[tuple, dict] = {}     # (slug, card id) -> card as the apps get it
_dirty: set = set()
# Being written by the module tool right now, which the look at the disk leaves alone.
_busy: set = set()
_tables_on = None      # the connection the tables were made on


class ModuleError(ValueError):
    pass


# ---------- storage ----------

def _ensure():
    global _tables_on
    c = db._db()
    if c is _tables_on:
        return
    c.execute(
        """CREATE TABLE IF NOT EXISTS modules (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'stopped',
            enabled INTEGER NOT NULL DEFAULT 1,
            hash TEXT NOT NULL DEFAULT '',
            approved TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '{}',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            started REAL
        )""")
    c.execute(
        """CREATE TABLE IF NOT EXISTS module_cards (
            module TEXT NOT NULL,
            id TEXT NOT NULL,
            card TEXT NOT NULL,
            desktop INTEGER NOT NULL DEFAULT 1,
            updated REAL NOT NULL,
            PRIMARY KEY (module, id)
        )""")
    c.commit()
    _tables_on = c


_COLS = ("slug", "name", "description", "author", "status", "enabled", "hash", "approved",
         "reason", "error", "state", "created", "updated", "started")


def _row(r) -> dict:
    row = dict(zip(_COLS, r))
    row["enabled"] = bool(row["enabled"])
    try:
        row["state"] = json.loads(row["state"] or "{}")
    except ValueError:
        row["state"] = {}
    return row


def get(slug: str) -> dict | None:
    with db._lock:
        _ensure()
        r = db._db().execute(f"SELECT {', '.join(_COLS)} FROM modules WHERE slug=?", (slug,)).fetchone()
    return _row(r) if r else None


def rows() -> list[dict]:
    with db._lock:
        _ensure()
        found = db._db().execute(f"SELECT {', '.join(_COLS)} FROM modules ORDER BY created").fetchall()
    return [_row(r) for r in found]


def _put(slug: str, **fields):
    now = time.time()
    if "state" in fields:
        fields["state"] = json.dumps(fields["state"], default=str)
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    with db._lock:
        _ensure()
        c = db._db()
        if c.execute("SELECT 1 FROM modules WHERE slug=?", (slug,)).fetchone():
            fields["updated"] = now
            c.execute(f"UPDATE modules SET {', '.join(k + '=?' for k in fields)} WHERE slug=?",
                      (*fields.values(), slug))
        else:
            fields.setdefault("name", slug)
            fields.update(slug=slug, created=now, updated=now)
            c.execute(f"INSERT INTO modules ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                      tuple(fields.values()))
        c.commit()


def _forget(slug: str):
    with db._lock:
        _ensure()
        db._db().execute("DELETE FROM modules WHERE slug=?", (slug,))
        db._db().execute("DELETE FROM module_cards WHERE module=?", (slug,))
        db._db().commit()
    for key in [k for k in _cards if k[0] == slug]:
        _cards.pop(key, None)
        _dirty.discard(key)


def _load_cards():
    with db._lock:
        _ensure()
        found = db._db().execute("SELECT module, id, card, desktop, updated FROM module_cards").fetchall()
    for module, cid, raw, desktop, updated in found:
        try:
            card = json.loads(raw)
        except ValueError:
            continue
        _cards[(module, cid)] = {**card, "module": module, "id": cid, "desktop": bool(desktop), "updated": updated}


def _flush_cards():
    if not _dirty:
        return
    batch = [(k, _cards.get(k)) for k in list(_dirty)]
    _dirty.clear()
    with db._lock:
        _ensure()
        c = db._db()
        for (module, cid), card in batch:
            if card is None:
                c.execute("DELETE FROM module_cards WHERE module=? AND id=?", (module, cid))
                continue
            body = {k: v for k, v in card.items() if k not in ("module", "id", "desktop", "updated")}
            c.execute("INSERT INTO module_cards (module, id, card, desktop, updated) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(module, id) DO UPDATE SET card=excluded.card, desktop=excluded.desktop, "
                      "updated=excluded.updated",
                      (module, cid, json.dumps(body), int(card.get("desktop", True)), card.get("updated") or time.time()))
        c.commit()


def cards(slug: str | None = None) -> list[dict]:
    known = {r["slug"]: r for r in rows()}
    return [{**card, "module_status": (known.get(card["module"]) or {}).get("status", "stopped"),
             "module_name": (known.get(card["module"]) or {}).get("name", card["module"])}
            for key, card in sorted(_cards.items()) if slug is None or key[0] == slug]


# ---------- files ----------

def folder(slug: str) -> Path:
    return FOLDER / slug


def _code_files(slug: str) -> list[Path]:
    base = folder(slug)
    found = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if rel.parts[0] in ("data", "__pycache__") or not path.is_file() or path.is_symlink():
            continue
        found.append(path)
    return found


def code_hash(slug: str) -> str:
    """What exactly would run: every file but the module's own data, by name and content."""
    digest = hashlib.sha256()
    base = folder(slug)
    for path in _code_files(slug):
        digest.update(str(path.relative_to(base)).encode() + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"(unreadable)")
        digest.update(b"\0")
    return digest.hexdigest()


def manifest(slug: str) -> dict:
    try:
        raw = json.loads((folder(slug) / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModuleError(f"{slug} has no {MANIFEST}")
    except ValueError as exc:
        raise ModuleError(f"{MANIFEST} is not valid JSON: {exc}")
    return check_manifest(raw, slug)


def check_manifest(raw: dict, slug: str = "") -> dict:
    if not isinstance(raw, dict):
        raise ModuleError(f"{MANIFEST} holds an object")
    name = str(raw.get("name") or slug).strip()
    if not 1 <= len(name) <= 80:
        raise ModuleError("name is 1 to 80 characters")
    entry = str(raw.get("entry") or "main.py")
    if not FILE_PATH.match(entry) or not entry.endswith(".py") or entry.startswith("data/"):
        raise ModuleError("entry is the module's own .py file, such as main.py")
    perms = raw.get("permissions") or {}
    if not isinstance(perms, dict):
        raise ModuleError("permissions is an object: network, keys, read")
    unknown = set(perms) - {"network", "keys", "read"}
    if unknown:
        raise ModuleError(f"unknown permissions: {', '.join(sorted(unknown))} (there are network, keys and read)")
    keys = perms.get("keys") or []
    if not isinstance(keys, list) or not all(isinstance(k, str) and KEY_NAME.match(k) for k in keys):
        raise ModuleError("permissions.keys lists names from the .env file, such as UPTIME_API_KEY")
    reads = perms.get("read") or []
    if not isinstance(reads, list) or not all(isinstance(p, str) and os.path.isabs(os.path.expanduser(p)) for p in reads):
        raise ModuleError("permissions.read lists absolute paths the module may read")
    reads = [str(Path(os.path.expanduser(p)).resolve()) for p in reads]
    root = str(ROOT.resolve())
    for path in reads:
        if path == root or path.startswith(root + os.sep) or root.startswith(path + os.sep):
            raise ModuleError("a module cannot read Mission Control's data folder; the owner's keys go "
                              "through permissions.keys")
    config = raw.get("config") or {}
    if not isinstance(config, dict) or len(json.dumps(config, default=str)) > MAX_CONFIG:
        raise ModuleError(f"config is an object of at most {MAX_CONFIG // 1000} KB")
    schema = raw.get("config_schema")
    if schema is not None:
        from jsonschema import Draft202012Validator
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise ModuleError(f"config_schema is not a valid JSON schema: {str(exc)[:200]}")
        error = next(Draft202012Validator(schema).iter_errors(config), None)
        if error:
            raise ModuleError(f"config does not match config_schema: {error.message}")
    return {"name": name, "description": str(raw.get("description") or "")[:600], "entry": entry,
            "permissions": {"network": bool(perms.get("network")), "keys": list(dict.fromkeys(keys)),
                            "read": reads},
            "config": config, **({"config_schema": schema} if schema is not None else {})}


def _check_files(files: dict) -> dict:
    if not isinstance(files, dict):
        raise ModuleError("files maps a path in the module to its full text, or to null to delete it")
    out = {}
    for path, text in files.items():
        if not isinstance(path, str) or not FILE_PATH.match(path) or ".." in path.split("/"):
            raise ModuleError(f"{path!r} is not a path inside the module")
        if path.split("/")[0] in ("data", "__pycache__") or path == MANIFEST:
            raise ModuleError(f"{path}: data/ is the module's own to write, and {MANIFEST} is written from "
                              "this tool's fields")
        if Path(path).suffix.lower() not in FILE_TYPES:
            raise ModuleError(f"{path}: a module holds text files ({', '.join(sorted(FILE_TYPES))})")
        if text is not None:
            if not isinstance(text, str):
                raise ModuleError(f"{path}: the file's full text, as a string")
            if len(text.encode()) > MAX_FILE:
                raise ModuleError(f"{path}: at most {MAX_FILE // 1000} KB a file")
            if path.endswith(".py"):
                try:
                    compile(text, path, "exec")   # only parsed: nothing runs here
                except SyntaxError as exc:
                    raise ModuleError(f"{path} line {exc.lineno}: {exc.msg}")
        out[path] = text
    return out


def _plan(slug: str, files: dict, man: dict):
    """The module as it would be after this write, checked before anything is written."""
    base = folder(slug)
    sizes = {str(p.relative_to(base)): p.stat().st_size for p in _code_files(slug)} if base.exists() else {}
    sizes.pop(MANIFEST, None)
    for path, text in files.items():
        if text is None:
            sizes.pop(path, None)
        else:
            sizes[path] = len(text.encode())
    if len(sizes) + 1 > MAX_FILES:
        raise ModuleError(f"a module holds at most {MAX_FILES} files")
    if sum(sizes.values()) > MAX_TOTAL:
        raise ModuleError(f"a module holds at most {MAX_TOTAL // 1000} KB of code")
    if man["entry"] not in sizes:
        raise ModuleError(f"the entry file {man['entry']} is not among the module's files")


def _write(slug: str, files: dict, man: dict):
    _plan(slug, files, man)
    base = folder(slug)
    (base / "data").mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        target = base / path
        if text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp-write")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    (base / MANIFEST).write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")


def read_files(slug: str, limit: int = MAX_TOTAL) -> list[dict]:
    base, out, used = folder(slug), [], 0
    for path in _code_files(slug):
        text = path.read_text(encoding="utf-8", errors="replace")
        used += len(text)
        out.append({"path": str(path.relative_to(base)), "size": path.stat().st_size,
                    "text": text if used <= limit else text[:max(0, limit - used + len(text))]})
    return out


# ---------- the sandbox ----------

def sandboxed() -> bool:
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _sb(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(slug: str, man: dict) -> str:
    """What the module's process may do, in the macOS sandbox's own language. The last
    rule that matches wins, so each section closes everything and then opens what it needs."""
    base = folder(slug).resolve()
    readable = {base, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                (BUNDLE / "server").resolve(), *map(Path, man["permissions"]["read"])}
    if getattr(sys, "frozen", False):
        readable.add(BUNDLE.resolve())
    hidden = {Path("/Users"), Path("/Volumes"), Path("/private/var/folders"), Path.home().resolve(),
              ROOT.resolve(), Path("/private/tmp"), Path("/tmp").resolve()}
    lines = [
        "(version 1)",
        "(allow default)",
        # Names and sizes stay visible, since walking a path needs them; contents do not.
        "(deny file-read-data file-read-xattr " + " ".join(f"(subpath {_sb(p)})" for p in sorted(hidden)) + ")",
        "(allow file-read-data file-read-xattr " + " ".join(f"(subpath {_sb(p)})" for p in sorted(readable)) + ")",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sb(base / 'data')}) (literal \"/dev/null\") (literal \"/dev/zero\") "
        "(literal \"/dev/tty\") (literal \"/dev/dtracehelper\") (subpath \"/dev/fd\"))",
        "(deny appleevent-send)",
        '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd.xpc") '
        '(global-name "com.apple.pasteboard.1") (global-name "com.apple.coreservices.appleevents"))',
        "(deny signal)",
        "(allow signal (target same-sandbox))",
        "(deny process-info*)",
        "(allow process-info* (target same-sandbox))",
    ]
    if not man["permissions"]["network"]:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def _env(slug: str, man: dict) -> dict:
    data = folder(slug).resolve() / "data"
    (data / "tmp").mkdir(parents=True, exist_ok=True)
    env = {"PATH": processes.environment().get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
           "LANG": "en_US.UTF-8", "TMPDIR": str(data / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUNBUFFERED": "1", "MISSIONCONTROL_MODULE": slug}
    wanted = man["permissions"]["keys"]
    if wanted:
        try:
            values = settings.env_all()
        except Exception:
            values = {}
        env.update({k: values[k] for k in wanted if values.get(k)})
    return env


def _command(slug: str, man: dict) -> list[str]:
    base = str(folder(slug).resolve())
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--module-host", base, man["entry"]]
    else:
        command = [sys.executable, "-I", "-B", str((BUNDLE / "server" / "module_host.py").resolve()),
                   base, man["entry"]]
    if not sandboxed():
        return command
    RUN.mkdir(parents=True, exist_ok=True)
    profile = RUN / f"{slug}.sb"
    profile.write_text(sandbox_profile(slug, man), encoding="utf-8")
    return [SANDBOX_EXEC, "-f", str(profile), *command]


@contextlib.contextmanager
def _as_service():
    """A module outlives the task that made it, so its process belongs to this service,
    not to the session whose tool call started it: stopping that task must not stop it."""
    scope, owner = processes._scope.set({}), runtime.owner.set("modules")
    try:
        yield
    finally:
        processes._scope.reset(scope)
        runtime.owner.reset(owner)


# ---------- running ----------

class Running:
    def __init__(self, slug: str, man: dict, code: str):
        self.slug, self.manifest, self.hash = slug, man, code
        self.proc = None
        self.tasks: list[asyncio.Task] = []
        self.tools: dict[str, dict] = {}
        self.calls: dict[int, asyncio.Future] = {}
        self.ready = asyncio.Event()
        self.ended = asyncio.Event()
        self.failure = ""
        self.ready_at = 0.0
        self.swept = False
        self.claimed: set = set()
        self.renders: deque = deque()
        self.stopping = False
        self.started = time.time()
        self.seq = 0

    def send(self, message: dict) -> bool:
        if not self.proc or self.proc.returncode is not None or not self.proc.stdin:
            return False
        try:
            self.proc.stdin.write((json.dumps(message, default=str) + "\n").encode())
            return True
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return False


def _log(slug: str, level: str, text: str):
    import harness
    ring = _logs.setdefault(slug, deque(maxlen=LOG_LINES))
    ring.append({"ts": time.time(), "level": level, "text": harness.redact(text, 4000)})


def logs(slug: str, limit: int = 60) -> list[dict]:
    return list(_logs.get(slug) or [])[-limit:]


async def _say(event: dict):
    if emit:
        try:
            await emit(event)
        except Exception:
            log.exception("modules: could not broadcast %s", event.get("type"))


async def _changed():
    await _say({"type": "modules_updated"})


async def start(slug: str) -> dict:
    """Run the module's approved code, replacing whatever version of it is running."""
    await stop(slug, status=None)
    row = await asyncio.to_thread(get, slug)
    if not row:
        raise ModuleError(f"no module {slug}")
    try:
        man = await asyncio.to_thread(manifest, slug)
    except ModuleError as exc:
        await asyncio.to_thread(_put, slug, status="failed", error=str(exc))
        await _changed()
        raise
    code = await asyncio.to_thread(code_hash, slug)
    if code != row["approved"]:
        await asyncio.to_thread(_put, slug, status="pending", hash=code,
                                reason=row["reason"] or "this version has not been approved")
        await _changed()
        return await asyncio.to_thread(get, slug)
    run = Running(slug, man, code)
    _running[slug] = run
    command, env = _command(slug, man), _env(slug, man)
    with _as_service():
        run.proc = await processes.create_subprocess_exec(
            *command, cwd=str(folder(slug).resolve()), env=env, limit=MAX_LINE,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        run.tasks = [asyncio.create_task(_read(run)), asyncio.create_task(_read_errors(run))]
    run.send({"op": "init", "slug": slug, "name": man["name"], "config": man["config"],
              "state": row["state"], "data": str(folder(slug).resolve() / "data")})
    _log(slug, "info", f"starting version {code[:10]}" + ("" if sandboxed() else " (no sandbox on this system)"))
    await asyncio.to_thread(_put, slug, status="starting", hash=code, error="", reason="", started=time.time(),
                            name=man["name"], description=man["description"])
    await _changed()
    return await asyncio.to_thread(get, slug)


async def stop(slug: str, status: str | None = "stopped", reason: str = ""):
    """Stop it, and with it everything it registered but its cards."""
    run = _running.pop(slug, None)
    if run:
        run.stopping = True
        if run.proc and run.proc.returncode is None:
            run.send({"op": "stop"})
            try:
                await asyncio.wait_for(run.proc.wait(), STOP_WAIT)
            except asyncio.TimeoutError:
                await processes.terminate(run.proc)
        for task in run.tasks:
            task.cancel()
        for future in run.calls.values():
            if not future.done():
                future.set_exception(ModuleError("the module stopped"))
        run.ended.set()
    _retry_at.pop(slug, None)
    if status and await asyncio.to_thread(get, slug):
        await asyncio.to_thread(_put, slug, status=status, **({"reason": reason} if reason else {}))
        await _changed()
        await _say({"type": "module_cards", "module": slug})


async def _read(run: Running):
    reader = run.proc.stdout
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            _log(run.slug, "error", f"a message over {MAX_LINE // 1000} KB was dropped")
            continue
        if not line:
            break
        try:
            message = json.loads(line)
        except ValueError:
            _log(run.slug, "info", line.decode("utf-8", "replace").rstrip()[:2000])
            continue
        if isinstance(message, dict):
            try:
                await _on_message(run, message)
            except Exception as exc:
                log.exception("module %s: message %s", run.slug, message.get("op"))
                _log(run.slug, "error", f"{message.get('op')}: {type(exc).__name__}: {exc}")
    await _exited(run, await run.proc.wait())


async def _read_errors(run: Running):
    while line := await run.proc.stderr.readline():
        _log(run.slug, "error", line.decode("utf-8", "replace").rstrip()[:2000])


def _allowed(slug: str, kind: str, gap: int, per_day: int) -> str:
    times = _said.setdefault(slug, {}).setdefault(kind, deque(maxlen=200))
    now = time.time()
    while times and now - times[0] > 86400:
        times.popleft()
    if times and now - times[-1] < gap:
        return f"{kind} refused: too soon after the last one (one every {gap // 60} min at most)"
    if len(times) >= per_day:
        return f"{kind} refused: {per_day} a day at most, and today's are used"
    times.append(now)
    return ""


async def _on_message(run: Running, message: dict):
    slug, op = run.slug, message.get("op")
    if op == "log":
        level = message.get("level") if message.get("level") in ("info", "warn", "error") else "info"
        _log(slug, level, str(message.get("text") or ""))
    elif op == "ready":
        run.ready_at = time.time()
        run.ready.set()
        await asyncio.to_thread(_put, slug, status="running", error="")
        _log(slug, "info", "ready: tools " + (", ".join(sorted(run.tools)) or "none")
             + "; cards " + (", ".join(sorted(run.claimed)) or "none"))
        await _changed()
    elif op == "failed":
        run.failure = (str(message.get("error") or "failed") + "\n" + str(message.get("trace") or "")).strip()
        _log(slug, "error", run.failure)
    elif op == "tool":
        name = str(message.get("name") or "")
        schema = message.get("schema") or {"type": "object", "properties": {}}
        if not TOOL_NAME.match(name):
            _log(slug, "error", f"tool {name!r}: a tool name is lower case letters, digits and _")
            return
        if name not in run.tools and len(run.tools) >= MAX_TOOLS:
            _log(slug, "error", f"tool {name}: a module adds at most {MAX_TOOLS} tools")
            return
        try:
            from jsonschema import Draft202012Validator
            Draft202012Validator.check_schema(schema)
            if schema.get("type", "object") != "object":
                raise ValueError("a tool's inputs are an object")
        except Exception as exc:
            _log(slug, "error", f"tool {name}: its schema is not usable: {str(exc)[:300]}")
            return
        run.tools[name] = {"name": name, "description": str(message.get("description") or "")[:600],
                           "schema": schema}
    elif op == "card":
        await _card(run, message)
    elif op == "uncard":
        key = (slug, str(message.get("id") or ""))
        if _cards.pop(key, None) is not None:
            _dirty.add(key)
            await _say({"type": "module_card", "module": slug, "id": key[1], "removed": True})
    elif op == "state":
        value = message.get("value")
        if not isinstance(value, dict):
            return
        if len(json.dumps(value, default=str)) > MAX_STATE:
            _log(slug, "error", f"state not kept: at most {MAX_STATE // 1000} KB")
            return
        await asyncio.to_thread(_put, slug, state=value)
    elif op == "notify":
        refused = _allowed(slug, "notify", NOTIFY_GAP, NOTIFY_DAY)
        if refused:
            _log(slug, "warn", refused)
            return
        text = str(message.get("text") or "").strip()
        if text and orch:
            asyncio.create_task(_notify(run, text))
    elif op == "ask":
        refused = _allowed(slug, "ask", ASK_GAP, ASK_DAY)
        if refused:
            _log(slug, "warn", refused)
            return
        text = str(message.get("text") or "").strip()
        if text and orch:
            asyncio.create_task(_ask(run, text))
    elif op == "result":
        future = run.calls.pop(message.get("id"), None)
        if future and not future.done():
            future.set_result(message)


async def _card(run: Running, message: dict):
    slug = run.slug
    try:
        card = module_ui.check_card(message)
    except module_ui.CardError as exc:
        _log(slug, "error", f"card {message.get('id')!r} not drawn: {exc}")
        return
    key = (slug, card["id"])
    if key not in _cards and sum(1 for k in _cards if k[0] == slug) >= MAX_CARDS:
        _log(slug, "error", f"card {card['id']} not drawn: a module has at most {MAX_CARDS} cards")
        return
    now = time.time()
    while run.renders and now - run.renders[0] > 60:
        run.renders.popleft()
    if len(run.renders) >= RENDERS_PER_MINUTE:
        if len(run.renders) == RENDERS_PER_MINUTE:
            _log(slug, "warn", f"drawing more than {RENDERS_PER_MINUTE} times a minute; the rest are dropped")
            run.renders.append(now)
        return
    run.renders.append(now)
    previous = _cards.get(key)
    # Where a card is shown is the owner's choice once made; the module only proposes it.
    desktop = previous["desktop"] if previous else bool(message.get("desktop", True))
    _cards[key] = {**card, "module": slug, "desktop": desktop, "updated": now}
    run.claimed.add(card["id"])
    _dirty.add(key)
    await _say({"type": "module_card", "card": {**_cards[key], "module_status": "running",
                                                  "module_name": run.manifest["name"]}})


async def _exited(run: Running, code: int):
    slug = run.slug
    run.ended.set()
    if _running.get(slug) is run:
        _running.pop(slug, None)
    for future in run.calls.values():
        if not future.done():
            future.set_exception(ModuleError(f"the module exited (code {code})"))
    if run.stopping:
        return
    if run.failure:
        # Its own code failed to load: running it again would fail the same way.
        await asyncio.to_thread(_put, slug, status="failed", error=run.failure[:4000])
        await _changed()
        return
    now = time.time()
    crashes = [t for t in _crashes.get(slug, []) if now - t < 3600]
    if run.ready_at and now - run.ready_at > STEADY:
        crashes = []
    crashes.append(now)
    _crashes[slug] = crashes
    tail = "\n".join(line["text"] for line in logs(slug, 8) if line["level"] == "error")[-1500:]
    if len(crashes) > len(RESTART_WAITS):
        error = f"exited with code {code} and has crashed {len(crashes)} times in an hour, so it is stopped"
        await asyncio.to_thread(_put, slug, status="failed", error=(error + "\n" + tail).strip())
        _log(slug, "error", error)
        await _changed()
        if orch:
            name = run.manifest["name"]
            asyncio.create_task(_tell_owner(f"The module {name} kept crashing, so it is stopped. Its log is "
                                            "under Modules; an agent can fix it with the module tool."))
        return
    wait = RESTART_WAITS[len(crashes) - 1]
    _retry_at[slug] = now + wait
    await asyncio.to_thread(_put, slug, status="failed",
                            error=(f"exited with code {code}; starting again in {wait} s\n{tail}").strip())
    _log(slug, "warn", f"exited with code {code}; starting again in {wait} s")
    await _changed()


async def _tell_owner(text: str):
    try:
        await orch.router.notify_owner(text)
    except Exception:
        log.exception("modules: could not tell the owner")


async def _notify(run: Running, text: str):
    await _tell_owner(f"{run.manifest['name']}: {text}")


async def _ask(run: Running, text: str):
    row = await asyncio.to_thread(get, run.slug) or {}
    aid = row.get("author") if row.get("author") in (orch.agents or {}) else orch.lead
    note = (f"The module '{run.manifest['name']}' ({run.slug}) asks: {text}\n\n"
            f"It runs on its own; its tools are reachable with module(action='call', module='{run.slug}', ...), "
            "and module(action='read') shows its code. Report to the owner only if something needs their "
            "attention; otherwise reply exactly NO_REPLY.")
    try:
        await orch.external_event(aid, f"module:{run.slug}", note, thread=f"module:{run.slug}")
    except Exception:
        log.exception("modules: could not hand %s's request to an agent", run.slug)


async def call(slug: str, tool: str, args: dict, timeout: float = CALL_TIMEOUT):
    run = _running.get(slug)
    if not run or not run.ready.is_set():
        row = await asyncio.to_thread(get, slug)
        raise ModuleError(f"{slug} is not running" + (f" ({row['status']})" if row else ""))
    spec = run.tools.get(tool)
    if not spec:
        raise ModuleError(f"{slug} has no tool {tool!r}; it has: {', '.join(sorted(run.tools)) or 'none'}")
    from jsonschema import Draft202012Validator
    error = next(Draft202012Validator(spec["schema"]).iter_errors(args or {}), None)
    if error:
        path = ".".join(map(str, error.absolute_path)) or "args"
        raise ModuleError(f"invalid {path}: {error.message}")
    run.seq += 1
    cid = run.seq
    future = asyncio.get_running_loop().create_future()
    run.calls[cid] = future
    if not run.send({"op": "call", "id": cid, "tool": tool, "args": args or {}}):
        run.calls.pop(cid, None)
        raise ModuleError(f"{slug} is not taking calls")
    try:
        reply = await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        run.calls.pop(cid, None)
        raise ModuleError(f"{slug}.{tool} did not answer within {int(timeout)} s")
    if not reply.get("ok"):
        raise ModuleError(f"{slug}.{tool} failed: {reply.get('error')}")
    return reply.get("value")


async def act(slug: str, card: str, action: str, value=None, values: dict | None = None) -> bool:
    """A button or toggle on one of its cards."""
    run = _running.get(slug)
    if not run or not run.ready.is_set():
        return False
    return run.send({"op": "action", "card": card, "action": action, "value": value, "values": values or {}})


async def set_desktop(slug: str, cid: str, desktop: bool) -> dict | None:
    card = _cards.get((slug, cid))
    if not card:
        return None
    card["desktop"] = bool(desktop)
    _dirty.add((slug, cid))
    await asyncio.to_thread(_flush_cards)
    await _say({"type": "module_card", "card": {**card, "module_status": (get(slug) or {}).get("status")}})
    return card


# ---------- what changed, approval and removal ----------

async def approve(slug: str, who: str = "owner", version: str = "") -> dict:
    """Run this version. Given the version the owner was shown, a different one is refused:
    what they looked at is what runs."""
    row = await asyncio.to_thread(get, slug)
    if not row:
        raise ModuleError(f"no module {slug}")
    code = await asyncio.to_thread(code_hash, slug)
    if version and version != code:
        await _hold(slug, "its code changed while it waited", row["author"])
        raise ModuleError("The code changed since it was shown to you. Look at it again before approving.")
    await asyncio.to_thread(_put, slug, approved=code, hash=code, enabled=True, reason="", error="")
    _crashes.pop(slug, None)
    _log(slug, "info", f"version {code[:10]} approved by {who}")
    return await start(slug)


async def decline(slug: str) -> dict:
    await stop(slug, status="stopped", reason="the owner declined this version")
    await asyncio.to_thread(_put, slug, enabled=False)
    await _changed()
    return await asyncio.to_thread(get, slug)


async def remove(slug: str):
    await stop(slug, status=None)
    await asyncio.to_thread(_forget, slug)
    await asyncio.to_thread(shutil.rmtree, folder(slug), True)
    (RUN / f"{slug}.sb").unlink(missing_ok=True)
    _logs.pop(slug, None)
    _crashes.pop(slug, None)
    await _changed()
    await _say({"type": "module_cards", "module": slug})


async def _hold(slug: str, reason: str, author: str = ""):
    """This version waits for the owner, who is told in code, once per version."""
    row = await asyncio.to_thread(get, slug)
    code = await asyncio.to_thread(code_hash, slug)
    if row and row["status"] == "pending" and row["hash"] == code:
        return
    await stop(slug, status=None)
    await asyncio.to_thread(_put, slug, status="pending", hash=code, reason=reason)
    _log(slug, "info", f"version {code[:10]} waits for the owner: {reason}")
    await _changed()
    await _say({"type": "approvals_updated"})
    if orch:
        name = (row or {}).get("name") or slug
        who = orch.agents.get(author, {}).get("name") if author else ""
        line = f"{who + ' built' if who else 'There is'} a module, {name}, that waits for your OK to run: {reason}. It's in your Inbox."
        try:
            await orch._ask_owner(f"module:{slug}:{code[:10]}", line, f"Run the module {name}?")
        except Exception:
            log.exception("modules: could not ask the owner about %s", slug)


async def _scan():
    """Folders are the source of truth: one that appears is a module waiting for the
    owner, one that goes is a module gone, and code that changed outside the module tool
    is a version nobody approved."""
    if not FOLDER.exists():
        return
    known = {r["slug"]: r for r in await asyncio.to_thread(rows) if r["slug"] not in _busy}
    present = {p.name for p in FOLDER.iterdir() if p.is_dir() and SLUG.match(p.name)} - _busy
    for slug in present - set(known):
        if (folder(slug) / MANIFEST).is_file():
            try:
                man = await asyncio.to_thread(manifest, slug)
            except ModuleError as exc:
                _log(slug, "error", str(exc))
                continue
            await asyncio.to_thread(_put, slug, name=man["name"], description=man["description"],
                                    author="", enabled=True)
            await _hold(slug, "its folder appeared outside the module tool")
    for slug in set(known) - present:
        await stop(slug, status=None)
        await asyncio.to_thread(_forget, slug)
        await _changed()
    for slug in present & set(known):
        row = known[slug]
        if not row["enabled"]:
            continue
        code = await asyncio.to_thread(code_hash, slug)
        if code != row["approved"] and (row["status"] != "pending" or code != row["hash"]):
            await _hold(slug, "its code changed outside the module tool")


async def _tick():
    now = time.time()
    for slug, at in list(_retry_at.items()):
        if now >= at:
            _retry_at.pop(slug, None)
            row = await asyncio.to_thread(get, slug)
            if row and row["enabled"] and slug not in _running:
                try:
                    await start(slug)
                except Exception as exc:
                    _log(slug, "error", f"could not start: {exc}")
    for slug, run in list(_running.items()):
        if not run.ready.is_set() and run.started and now - run.started > APPLY_LIMIT:
            await stop(slug, status=None)
            await asyncio.to_thread(_put, slug, status="failed",
                                    error=f"apply(ctx) did not return within {APPLY_LIMIT} s; start long work in a timer")
            _log(slug, "error", f"apply(ctx) did not return within {APPLY_LIMIT} s")
            await _changed()
            continue
        if run.ready_at and not run.swept and now - run.ready_at > SWEEP_AFTER:
            run.swept = True
            for key in [k for k in _cards if k[0] == slug and k[1] not in run.claimed]:
                _cards.pop(key, None)
                _dirty.add(key)
                await _say({"type": "module_card", "module": slug, "id": key[1], "removed": True})
    await asyncio.to_thread(_flush_cards)


async def supervise():
    """The modules service: start what is approved, bring back what crashed, notice what
    changed on disk, and stop all of it when the service stops."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_ensure)
    await asyncio.to_thread(_load_cards)
    try:
        await _scan()
        for row in await asyncio.to_thread(rows):
            if row["enabled"] and row["status"] != "pending" and row["slug"] not in _running:
                try:
                    await start(row["slug"])
                except Exception as exc:
                    _log(row["slug"], "error", f"could not start: {exc}")
        scanned = time.time()
        while True:
            await asyncio.sleep(TICK)
            try:
                await _tick()
                if time.time() - scanned >= SCAN_EVERY:
                    scanned = time.time()
                    await _scan()
            except Exception:
                log.exception("modules: supervisor tick")
    finally:
        await stop_all()


async def stop_all():
    for slug in list(_running):
        try:
            await stop(slug, status=None)
        except Exception:
            log.exception("modules: stopping %s", slug)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(_flush_cards)


# ---------- what the apps and the agents read ----------

def describe(row: dict, with_files: bool = False) -> dict:
    slug = row["slug"]
    run = _running.get(slug)
    try:
        man = manifest(slug)
    except ModuleError:
        man = {"permissions": {"network": False, "keys": [], "read": []}, "entry": "", "config": {}}
    out = {**{k: row[k] for k in ("slug", "name", "description", "author", "status", "enabled", "reason",
                                  "error", "created", "updated", "started")},
           "version": (row["hash"] or "")[:10], "approved_version": (row["approved"] or "")[:10],
           "hash": row["hash"],
           "permissions": man["permissions"], "entry": man["entry"], "config": man["config"],
           "tools": sorted((run.tools if run else {}).values(), key=lambda t: t["name"]),
           "cards": [c["id"] for c in cards(slug)], "sandboxed": sandboxed(),
           "retry_at": _retry_at.get(slug)}
    if with_files:
        out["files"] = read_files(slug)
        out["logs"] = logs(slug, 120)
    return out


def snapshot() -> dict:
    return {"modules": [describe(r) for r in rows()], "cards": cards(), "sandboxed": sandboxed()}


def pending() -> list[dict]:
    out = []
    for row in rows():
        if row["status"] != "pending" or not row["enabled"]:
            continue
        info = describe(row)
        out.append({"kind": "module", "id": row["slug"], "agent": row["author"], "ts": row["updated"],
                    "title": row["name"], "detail": row["description"], "reason": row["reason"],
                    "permissions": info["permissions"], "version": info["version"], "hash": row["hash"],
                    "files": [{"path": f["path"], "size": f["size"]} for f in read_files(row["slug"], 0)]})
    return out


def index_block() -> str:
    """For a session's context: what modules there are, so a request a module answers
    goes to it, and one that wants a new module knows the tool exists."""
    found = rows()
    if not found:
        return ""
    parts = []
    for row in found[:20]:
        run = _running.get(row["slug"])
        tools = ", ".join(sorted(run.tools)) if run else ""
        mine = [c["id"] for k, c in _cards.items() if k[0] == row["slug"]]
        parts.append(f"{row['slug']} ({row['name']}, {row['status']})"
                     + (f": tools {tools}" if tools else "") + (f"; cards {', '.join(mine)}" if mine else ""))
    return ("\nMODULES (the module tool: action=call runs a module's tool, action=read shows its code, "
            "action=guide explains how to build or change one): " + "; ".join(parts) + "\n")


# ---------- the agents' tool ----------

GUIDE = """\
A MODULE adds to Mission Control while it runs: tools you can call, cards the owner sees
natively on their Mac desktop, timers, and things to tell the owner or hand to an agent.
You write it; nothing about what it does is built in. Build one when the owner wants
something that keeps going on its own or that they want to see at a glance.

FILES: module(action="create", module="<slug>", name="…", description="…",
files={"main.py": "<full text>", …}, permissions={…}, config={…}). The slug is lower
case letters, digits and '-'. main.py (or `entry`) defines apply(ctx). Text files only,
40 files and 512 KB at most. update takes the same fields; files maps a path to its full
new text, or to null to delete it, and leaves other files as they were.

PERMISSIONS (what the sandbox lets it reach; ask for no more than it needs):
  network: true          reach the internet or the local network
  keys: ["NAME", …]      .env values it gets as environment variables (os.environ["NAME"])
  read: ["/abs/path", …] files or folders outside its own it may read
It always reads its own folder and writes only to ctx.data (its data/ folder). It cannot
read the owner's home folder, Mission Control's data or keys it was not given, the
clipboard or keychain, send Apple events, or touch other processes. Python's standard
library and httpx are available; nothing can be installed.

THE CONTEXT (ctx), given to apply(ctx):
  ctx.config, ctx.state (a dict kept across restarts), ctx.save(), ctx.data (a Path)
  @ctx.tool(name, description, schema)   a tool: fn(args) returns anything JSON can carry
  card = ctx.card(id, title=…, subtitle=…, symbol="<SF Symbol>", size="small|medium|large")
  card.render(view, tone=…, title=…)     replace what the card shows
  @card.on("action")                     fn(event) for a button or toggle: event has
                                         action, value, and values (the card's fields)
  @ctx.every(seconds, run_now=True)      a timer (5 s at least)
  ctx.effect(start)                      start() now; what it returns runs on stop
  ctx.notify(text)                       tell the owner (one a minute, 20 a day)
  ctx.ask(text)                          hand an agent something to look into, as a job
                                         would (one every 10 minutes, 12 a day)
  ctx.log(...) or print(...)             its log: module(action="logs")
Timers, tools and actions each run on their own thread. Keep apply(ctx) quick: do the
first piece of work in a timer with run_now=True, not in apply.

CARDS are a tree of nodes, built with ctx.ui or written as dicts. A list is a column.
  ctx.ui.vstack(*nodes) / hstack(*nodes) / grid(*nodes, columns=2)
  text(text, style=title|headline|body|caption|footnote|mono, tone)   title(t) caption(t)
  stat(label, value, unit, detail, tone, trend=up|down|flat)
  status(label, tone, detail)       a status light with a label
  badge(text, tone)  progress(0..1, label, tone)  chart(values, kind=line|bar, label, tone)
  symbol(name, tone, size)  table(rows, columns)  link(label, url)  divider()  spacer()
  button(label, action, value, style=primary|plain|destructive, symbol)
  field(name, placeholder, value)   toggle(name, label, value, action)
Tones: ok, warn, bad, info, muted, accent. The card says by itself how long ago it was
drawn, so it needs no "updated at" line. A card's tone colours its edge: give it the
worst status on it.

EXAMPLE (any module is shaped like this):
    import time

    def apply(ctx):
        ui = ctx.ui
        card = ctx.card("main", title=ctx.config.get("title", "Example"), symbol="sparkles")

        def draw():
            n = ctx.state.get("count", 0)
            card.render([ui.stat("Count", n), ui.button("Add one", "add")],
                        tone="ok" if n < 10 else "warn")

        @card.on("add")
        def add(event):
            ctx.state["count"] = ctx.state.get("count", 0) + 1
            draw()

        @ctx.tool("count", "The current count")
        def count(args):
            return {"count": ctx.state.get("count", 0)}

        @ctx.every(60)
        def tick():
            draw()

AFTER create or update the result says whether it started, which tools and cards it
registered, and its first log lines. If it failed, read the error, fix the code and
update. Check it does what was asked, with module(action="call") or by its log, before
you say it works. A version the owner did not ask for waits for their approval.
"""

STATUS_WORDS = {"running": "is running", "starting": "is starting", "pending": "waits for the owner's approval",
                "failed": "FAILED", "stopped": "is stopped"}


async def _outcome(slug: str) -> str:
    """What became of a start: wait for it to be ready or fail, then for its first draws."""
    run = _running.get(slug)
    if run:
        ready = asyncio.create_task(run.ready.wait())
        ended = asyncio.create_task(run.ended.wait())
        await asyncio.wait({ready, ended}, timeout=START_WAIT, return_when=asyncio.FIRST_COMPLETED)
        for task in (ready, ended):
            task.cancel()
        if run.ready.is_set():
            await asyncio.wait([asyncio.create_task(run.ended.wait())], timeout=SETTLE)
    row = await asyncio.to_thread(get, slug)
    if not row:
        return f"{slug} is gone"
    info = describe(row)
    lines = [f"module {slug} ({row['name']}) version {info['version']} {STATUS_WORDS.get(row['status'], row['status'])}."]
    if row["status"] == "starting":
        lines.append(f"It has not said it is ready after {START_WAIT} s: apply(ctx) should return quickly.")
    if row["reason"]:
        lines.append(f"Why: {row['reason']}")
    if row["error"]:
        lines.append("Error:\n" + row["error"][:3000])
    if info["tools"]:
        lines.append("Tools: " + "; ".join(f"{t['name']} ({t['description'][:80]})" for t in info["tools"]))
    shown = cards(slug)
    if shown:
        lines.append("Cards: " + "; ".join(f"{c['id']} \"{c['title']}\"" + (" on the owner's desktop" if c["desktop"] else "")
                                         for c in shown))
    recent = logs(slug, 25)
    if recent:
        lines.append("Log:\n" + "\n".join(f"  [{r['level']}] {r['text'][:400]}" for r in recent))
    return "\n".join(lines)


def _strength(ctx) -> str | None:
    return (getattr(ctx, "row", None) or {}).get("strength")


async def tool(orchestrator, ctx, aid: str, args: dict) -> str:
    action = (args.get("action") or "").strip()
    slug = (args.get("module") or "").strip().lower()
    strength = _strength(ctx)
    asked = strength in (None, "explicit")
    if strength == "inferred":
        return "refused: no person asked for this work, so it cannot build, change or use modules."
    if action == "guide":
        return GUIDE
    if action == "list":
        found = await asyncio.to_thread(rows)
        if not found:
            return "no modules yet. action=guide explains how to build one."
        out = []
        for row in found:
            info = describe(row)
            tools = ", ".join(t["name"] for t in info["tools"])
            out.append(f"{row['slug']} ({row['name']}) {row['status']} v{info['version']}"
                       + (f" | tools: {tools}" if tools else "") + (f" | cards: {', '.join(info['cards'])}" if info["cards"] else "")
                       + (f" | {row['error'].splitlines()[0][:160]}" if row["error"] else "")
                       + (f" | {row['description'][:120]}" if row["description"] else ""))
        return "\n".join(out)
    if not SLUG.match(slug):
        return "module is the module's slug: 2 to 40 lower case letters, digits and '-', starting with a letter"
    row = await asyncio.to_thread(get, slug)
    if action in ("create", "update"):
        if action == "create" and (row or folder(slug).exists()):
            return f"{slug} exists already; use action=update, or another slug"
        if action == "update" and not row:
            return f"no module {slug}; use action=create"
        if slug in _busy:
            return f"{slug} is being written by another call; try again in a moment"
        _busy.add(slug)
        try:
            return await _build(slug, row, aid, args, asked)
        finally:
            _busy.discard(slug)
    if not row:
        return f"no module {slug}. action=list shows them"
    return await _manage(slug, row, aid, args, action, asked)


async def _build(slug: str, row: dict | None, aid: str, args: dict, asked: bool) -> str:
    action = "update" if row else "create"
    try:
        files = _check_files(args.get("files") or {})
        if action == "create" and not files:
            return "files are required: at least the entry file, main.py, with apply(ctx)"
        old = {}
        if row:
            with contextlib.suppress(ModuleError):
                old = await asyncio.to_thread(manifest, slug)
        man = check_manifest({
            "name": args.get("name") or old.get("name") or slug,
            "description": args["description"] if "description" in args else old.get("description", ""),
            "entry": args.get("entry") or old.get("entry") or "main.py",
            "permissions": args["permissions"] if "permissions" in args else old.get("permissions") or {},
            "config": args["config"] if "config" in args else old.get("config") or {},
            **({"config_schema": args.get("config_schema") or old.get("config_schema")}
               if (args.get("config_schema") or old.get("config_schema")) else {}),
        }, slug)
        await asyncio.to_thread(_write, slug, files, man)
    except ModuleError as exc:
        if action == "create":
            await asyncio.to_thread(shutil.rmtree, folder(slug), True)
        return f"error: {exc}"
    except OSError as exc:
        return f"error: could not write the module: {exc}"
    if not row:
        await asyncio.to_thread(_put, slug, name=man["name"], description=man["description"], author=aid,
                                status="stopped", enabled=True)
    else:
        await asyncio.to_thread(_put, slug, name=man["name"], description=man["description"], enabled=True)
    code = await asyncio.to_thread(code_hash, slug)
    if asked and sandboxed():
        await asyncio.to_thread(_put, slug, approved=code, author=aid if not row else row["author"] or aid)
        _crashes.pop(slug, None)
        _log(slug, "info", f"version {code[:10]} written by {aid} at the owner's request")
        try:
            await start(slug)
        except ModuleError as exc:
            return f"error: {exc}"
        return await _outcome(slug)
    reason = ("the owner did not ask for this version" if not asked
              else "this system has no sandbox for modules, so every version needs the owner's approval")
    await _hold(slug, reason, aid)
    return (await _outcome(slug)) + "\nThe owner has been told. It runs once they approve it in the Inbox."


async def _manage(slug: str, row: dict, aid: str, args: dict, action: str, asked: bool) -> str:
    if action == "read":
        info = describe(row, with_files=True)
        out = [f"{slug} ({row['name']}) {row['status']} v{info['version']} entry {info['entry']}",
               f"permissions: {json.dumps(info['permissions'])}", f"config: {json.dumps(info['config'])[:2000]}"]
        for f in info["files"]:
            out.append(f"--- {f['path']} ({f['size']} bytes)\n{f['text']}")
        return "\n".join(out)
    if action == "logs":
        limit = max(1, min(int(args.get("limit") or 60), LOG_LINES))
        found = logs(slug, limit)
        if not found:
            return f"{slug} has logged nothing since Mission Control started"
        return "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} [{r['level']}] {r['text']}" for r in found)
    if action == "call":
        tool_name = (args.get("tool") or "").strip()
        if not tool_name:
            return "tool is required: the name of one of the module's tools"
        try:
            value = await call(slug, tool_name, args.get("args") or {})
        except ModuleError as exc:
            return f"error: {exc}"
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if action in ("start", "restart"):
        if row["status"] == "pending":
            return f"{slug} waits for the owner's approval of this version; it cannot start before that"
        await asyncio.to_thread(_put, slug, enabled=True)
        _crashes.pop(slug, None)
        try:
            await start(slug)
        except ModuleError as exc:
            return f"error: {exc}"
        return await _outcome(slug)
    if action == "stop":
        await stop(slug, status="stopped", reason=str(args.get("reason") or "stopped by an agent")[:200])
        await asyncio.to_thread(_put, slug, enabled=False)
        return f"{slug} stopped; its cards stay up, marked stopped. action=start runs it again"
    if action == "remove":
        if not asked:
            return "refused: only work the owner asked for removes a module. Say why it should go instead."
        await remove(slug)
        return f"{slug} removed, with its cards and data"
    return "action is guide, list, read, logs, create, update, call, start, restart, stop or remove"


TOOL_DEF = {
    "name": "module",
    "description": "Build and run MODULES: add-ons you write that extend Mission Control while it runs. "
                   "A module is Python code whose apply(ctx) adds tools you can call, live cards the owner "
                   "sees on their Mac desktop, timers, notices to the owner and requests to an agent. Use one "
                   "for anything the owner wants kept going on its own or shown at a glance, whatever it "
                   "is: what it does is up to you. It runs sandboxed, with only the network, keys and paths "
                   "its permissions name. Read action=guide before your first module. action=create or "
                   "update writes it and starts it, and says if it failed; list, read, logs; call runs one "
                   "of its tools; start, restart, stop, remove. A version the owner did not ask for waits "
                   "for their approval.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["guide", "list", "read", "logs", "create", "update", "call",
                                                  "start", "restart", "stop", "remove"]},
            "module": {"type": "string", "description": "the module's slug, e.g. site-health"},
            "name": {"type": "string", "description": "create/update: its name as the owner sees it"},
            "description": {"type": "string", "description": "create/update: what it does, in a sentence"},
            "files": {"type": "object", "additionalProperties": {"type": ["string", "null"]},
                      "description": "create/update: path -> full file text (null deletes). main.py holds apply(ctx)"},
            "entry": {"type": "string", "description": "create/update: the file with apply(ctx), main.py by default"},
            "permissions": {"type": "object",
                            "properties": {"network": {"type": "boolean"},
                                           "keys": {"type": "array", "items": {"type": "string"}},
                                           "read": {"type": "array", "items": {"type": "string"}}},
                            "additionalProperties": False,
                            "description": "create/update: what it may reach; nothing by default"},
            "config": {"type": "object", "description": "create/update: values its code reads as ctx.config"},
            "config_schema": {"type": "object", "description": "optional JSON schema config must match"},
            "tool": {"type": "string", "description": "call: which of the module's tools"},
            "args": {"type": "object", "description": "call: that tool's inputs"},
            "limit": {"type": "integer", "description": "logs: how many lines (60 by default)"},
            "reason": {"type": "string", "description": "stop: why, shown to the owner"},
        },
        "required": ["action"],
    },
}
