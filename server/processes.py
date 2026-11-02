"""Process ownership shared by tools, watches and desktop launchers."""
import asyncio
import os
import signal
import subprocess
import contextvars
import time
from contextlib import contextmanager
from pathlib import Path

_scope = contextvars.ContextVar("mc_process_scope", default={})
_owned = {}
# Every process group a session's commands started, by leader pid. The leader can exit
# with a `nohup` still running behind it, and then only the group still finds that.
_groups: dict[int, dict] = {}


@contextmanager
def scope(**values):
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


def _spawn_metadata(kwargs):
    import runtime
    env = dict(kwargs.get("env") or os.environ)
    env["MISSIONCONTROL_INSTANCE"] = runtime.INSTANCE
    from apphome import ROOT
    env["MISSIONCONTROL_HOME"] = str(ROOT)
    kwargs["env"] = env
    for key, value in spawn_options().items():
        kwargs.setdefault(key, value)
    metadata = {"service": runtime.owner.get(), **_scope.get(), "started": time.time()}
    for key in ("service", "agent", "execution", "session"):
        if metadata.get(key):
            env["MISSIONCONTROL_" + key.upper()] = str(metadata[key])
    return metadata


def _track_group(proc, metadata):
    if os.name != "nt" and metadata.get("session"):
        _groups[proc.pid] = {"session": metadata["session"], "started": metadata["started"]}


async def create_subprocess_exec(*args, **kwargs):
    metadata = _spawn_metadata(kwargs)
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    _owned[proc.pid] = {**metadata, "process": proc, "label": Path(str(args[0])).name}
    _track_group(proc, metadata)
    return proc


async def create_subprocess_shell(command, **kwargs):
    metadata = _spawn_metadata(kwargs)
    proc = await asyncio.create_subprocess_shell(command, **kwargs)
    _owned[proc.pid] = {**metadata, "process": proc, "label": "Terminal command"}
    _track_group(proc, metadata)
    return proc


def registered():
    for pid, row in list(_owned.items()):
        if row["process"].returncode is not None:
            _owned.pop(pid, None)
    return {pid: {k: v for k, v in row.items() if k != "process"} for pid, row in _owned.items()}


async def stop_service(service=None, agent=None):
    targets = [row["process"] for row in list(_owned.values())
               if (service is None or row.get("service") == service)
               and (agent is None or row.get("agent") == agent)]
    await asyncio.gather(*(terminate(proc) for proc in targets), return_exceptions=True)
    # A shell can exit after starting a detached descendant. Its service and
    # agent tags survive, so disabling the service must reap those tools too.
    import inventory
    rows = await asyncio.to_thread(inventory.process_inventory)
    for row in rows:
        if (row.get("owned") and row.get("can_stop")
                and (service is None or row.get("service") == service)
                and (agent is None or row.get("agent") == agent)):
            try:
                await asyncio.to_thread(inventory.stop_process, row["pid"], row["created"], owned_only=True)
            except (OSError, ValueError):
                pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def still_running(pid: int) -> bool:
    """Whether a command that has returned left anything running in its group."""
    return os.name != "nt" and _group_alive(pid)


def _kill_group(pgid: int):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


async def stop_session(session: str):
    """Everything a session's commands started, including what outlived the shell that
    started it. A backgrounded test run kept writing for minutes after being stopped."""
    await asyncio.gather(*(terminate(row["process"]) for row in list(_owned.values())
                           if row.get("session") == session), return_exceptions=True)
    for pgid, row in list(_groups.items()):
        if row["session"] == session:
            _kill_group(pgid)
            _groups.pop(pgid, None)
    # Only what daemonized out of its group is left, and only its tag finds that.
    import inventory
    for row in await asyncio.to_thread(inventory.process_inventory):
        if row.get("session") == session and row.get("owned") and row.get("can_stop"):
            try:
                await asyncio.to_thread(inventory.stop_process, row["pid"], row["created"], owned_only=True)
            except (OSError, ValueError):
                pass


def reap_left_behind(active: set, limit: float, now: float | None = None) -> int:
    """What a task left running after it stopped working: fine for a while (a dev server
    someone wanted to look at), but nobody is watching it, so not for ever."""
    now, reaped = now or time.time(), 0
    for pgid, row in list(_groups.items()):
        if not _group_alive(pgid):
            _groups.pop(pgid, None)
        elif row["session"] not in active and now - row["started"] > limit:
            _kill_group(pgid)
            _groups.pop(pgid, None)
            reaped += 1
    return reaped


async def shutdown_owned():
    await stop_service()
    # Reap tagged descendants that deliberately detached from their shell.
    import inventory
    for record in await asyncio.to_thread(inventory.process_inventory):
        if record.get("owned") and record["pid"] != os.getpid():
            try:
                await asyncio.to_thread(inventory.stop_process, record["pid"], record["created"], owned_only=True)
            except (OSError, ValueError):
                pass


def environment(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    paths = [str(Path.home() / ".local" / "bin")]
    if os.name != "nt":
        paths += ["/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([*paths, env.get("PATH", "")])
    env.pop("CLAUDECODE", None)
    env.update(extra or {})
    return env


def spawn_options(detached: bool = False) -> dict:
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if detached:
            flags |= subprocess.DETACHED_PROCESS
        return {"creationflags": flags}
    return {"start_new_session": True}


async def terminate(proc):
    """Stop the owned process tree and reap it, including on task cancellation. A shell
    having exited says nothing about what it started, so for a session's commands the
    group goes whether or not its leader is still there. Anything else that has been
    reaped has given up its pid, and that number could be someone else's by now."""
    if os.name != "nt" and (proc.returncode is None or proc.pid in _groups):
        _kill_group(proc.pid)
        _groups.pop(proc.pid, None)
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await asyncio.wait_for(killer.wait(), 5)
    except (ProcessLookupError, PermissionError, OSError, asyncio.TimeoutError):
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass


async def capture(proc, timeout: float, limit: int = 16000) -> str:
    """Drain output continuously while keeping a bounded head and tail."""
    async def read():
        head, tail, total = b"", b"", 0
        while chunk := await proc.stdout.read(65536):
            total += len(chunk)
            remaining = max(0, limit // 2 - len(head))
            head += chunk[:remaining]
            tail = (tail + chunk[remaining:])[-limit // 2:]
        await proc.wait()
        gap = b"\n... output truncated ...\n" if total > limit else b""
        return (head + gap + tail).decode("utf-8", errors="replace")

    try:
        return await asyncio.wait_for(read(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await terminate(proc)
        raise


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # os.kill(pid, 0) calls TerminateProcess on Windows. Query the handle instead.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)
