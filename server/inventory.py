"""Read process/launch-service identities; mutations recheck PID birth time.

Only Mission Control roots, their descendants, and tagged children are eligible.
Process environments and full command lines never leave this module.
"""
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from apphome import ROOT, BUNDLE

_KNOWN_SCRIPTS = {"desktop.py", "tray.py", "missioncontrol-preview-launch.py", "native_worker.py"}


def workspace_in_use(root):
    """Detect pre-registry runtimes that cannot participate in the file lock."""
    root = Path(root).resolve()
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            if process.pid == os.getpid():
                continue
            args = process.info["cmdline"] or []
            names = {Path(arg).name for arg in args}
            is_runtime = bool(names & {"MCBackend", "native_worker.py", "main.py"}) or ('desktop.py' in names and '--server' in args)
            if not is_runtime or any(arg in args for arg in ('--inventory', '--stop-process', '--cleanup-owned', '--login-service', '--check-browser', '--check-vault')):
                continue
            cwd = process.cwd()
            if not _mission_command(args, cwd):
                continue
            try:
                home = process.environ().get('MISSIONCONTROL_HOME') or cwd
            except psutil.AccessDenied:
                home = cwd
            if Path(home).resolve() in (root, root / 'server'):
                return process.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return None


def cleanup_instance():
    """Reap surviving tagged tools after a backend crash; leave other hosts alone."""
    import runtime
    for row in process_inventory():
        if row['instance'] == runtime.INSTANCE and row['can_stop']:
            try:
                stop_process(row['pid'], row['created'], owned_only=True)
            except (ValueError, psutil.NoSuchProcess):
                pass


def _mission_command(args, cwd=""):
    if not args:
        return False
    interpreter = Path(args[0]).name.lower().startswith('python')
    candidates = [(0, args[0])]
    if interpreter:
        # Only the interpreter's entry script identifies a runtime. A Python
        # program can take an app path as data without becoming that app.
        for index, arg in enumerate(args[1:], 1):
            if arg in ('-c', '-m', '-'):
                break
            if arg.startswith('-'):
                continue
            candidates.append((index, arg))
            break
    for index, arg in candidates:
        normalized = str(arg).replace("\\", "/")
        if cwd and not normalized.startswith("/") and (normalized.endswith(".py") or normalized in ("MissionControl", "MCBackend")):
            normalized = str(Path(cwd) / normalized)
        if index == 0 and ("/Mission Control.app/Contents/" in normalized or "/Mission Control Native.app/Contents/" in normalized):
            return True
        if normalized.endswith("/missioncontrol/server/main.py"):
            return True
        if Path(normalized).name in _KNOWN_SCRIPTS and ("missioncontrol" in normalized.lower() or "mission control" in normalized.lower()):
            return True
        if Path(normalized).name in ("MCBackend", "MissionControl") and "/Contents/" in normalized:
            return True
    return False


def process_inventory():
    import runtime
    import processes
    registered = processes.registered()
    records, roots, tagged = {}, {os.getpid()}, set()
    host = int(os.environ.get("MISSIONCONTROL_OWNER_PID") or 0)
    if host and host == os.getppid():
        roots.add(host)
        tagged.add(host)
    current_user = psutil.Process().username()
    for process in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time", "username"]):
        try:
            info = process.info
            if info["username"] != current_user:
                continue
            pid, args = info["pid"], info["cmdline"] or []
            match = _mission_command(args)
            if not match and any(arg.endswith(".py") for arg in args):
                try:
                    match = _mission_command(args, process.cwd())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            # Tagged environments also find reparented / double-forked tools.
            try:
                env = process.environ()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                env = {}
            instance = env.get("MISSIONCONTROL_INSTANCE", "")
            if instance == runtime.INSTANCE:
                tagged.add(pid)
            if match or instance or pid in registered:
                roots.add(pid)
            metadata = {key: env.get("MISSIONCONTROL_" + key.upper()) for key in ("service", "agent", "execution", "session")}
            records[pid] = (process, info, match, instance, metadata)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # A readable command remains discoverable even if its environment is not.
            if 'info' in locals() and info.get("pid") == process.pid and _mission_command(info.get("cmdline") or []):
                records[process.pid] = (process, info, True, "", {})
                roots.add(process.pid)
    mine, included = {os.getpid(), *tagged, *registered}, set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (_, info, _, _, _) in records.items():
            if info["ppid"] in included and pid not in included:
                included.add(pid); changed = True
            if info["ppid"] in mine and pid not in mine:
                mine.add(pid); changed = True
    rows = []
    for pid in sorted(included):
        if pid not in records:
            continue
        process, info, match, instance, metadata = records[pid]
        try:
            detail = {**{key: value for key, value in metadata.items() if value}, **registered.get(pid, {})}
            args = info["cmdline"] or []
            script = next((str(Path(arg).name) for arg in args if Path(arg).name in _KNOWN_SCRIPTS or arg.endswith("/server/main.py")), None)
            rows.append({"pid": pid, "parent_pid": info["ppid"], "created": info["create_time"],
                         "name": detail.get("label") or script or info["name"],
                         "status": process.status(), "memory_mb": round(process.memory_info().rss / 1048576, 1),
                         "owned": pid in mine, "instance": instance[:64],
                         "service": detail.get("service", "native_host" if pid == host else "runtime" if pid == os.getpid() else "child_process" if pid in mine else "other_instance"),
                         "agent": detail.get("agent"), "execution": detail.get("execution"), "session": detail.get("session"),
                         "can_stop": pid not in (os.getpid(), os.getppid()),
                         "source": "tracked" if pid in registered else "process_tree" if pid in mine else "detected"})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


def stop_process(pid, created, owned_only=False):
    record = next((row for row in process_inventory() if row["pid"] == pid), None)
    if not record or not record["can_stop"] or abs(record["created"] - created) > .01:
        raise ValueError("Process changed or is not an eligible Mission Control process. Refresh the inventory.")
    if owned_only and not record["owned"]:
        raise ValueError("This process is owned by another runtime.")
    parent = psutil.Process(pid)
    if abs(parent.create_time() - created) > .01:
        raise ValueError("Process identity changed")
    tree = parent.children(recursive=True) + [parent]
    for process in reversed(tree):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, survivors = psutil.wait_procs(tree, timeout=3)
    for process in survivors:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(survivors, timeout=2)


def launch_services():
    if sys.platform != "darwin":
        return []
    paths = [Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")]
    rows = []
    for folder in paths:
        for path in folder.glob("*.plist"):
            try:
                if path.stat().st_size > 100000:
                    continue
                data = plistlib.loads(path.read_bytes())
                args = data.get("ProgramArguments", [])
                label = data.get("Label", "")
                if not (label == "com.missioncontrol" or label.startswith("com.missioncontrol.") or _mission_command(args)):
                    continue
                user_owned = folder == paths[0]
                domain = f"gui/{os.getuid()}" if folder != paths[2] else "system"
                status = subprocess.run(["/bin/launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True, timeout=2)
                rows.append({"id": str(path), "label": label, "path": str(path), "domain": domain,
                             "loaded": status.returncode == 0, "can_manage": user_owned,
                             "program": str(args[0] if args else data.get("Program", "")),
                             "data_home": data.get("EnvironmentVariables", {}).get("MISSIONCONTROL_HOME") or data.get("WorkingDirectory", ""),
                             "run_at_login": bool(data.get("RunAtLoad")), "keep_alive": bool(data.get("KeepAlive"))})
            except (OSError, ValueError, plistlib.InvalidFileException, subprocess.TimeoutExpired):
                continue
    return rows


def launch_action(identifier, action):
    row = next((row for row in launch_services() if row["id"] == identifier), None)
    if not row or not row["can_manage"]:
        raise ValueError("Only this user's detected Mission Control login services can be changed.")
    target = f"{row['domain']}/{row['label']}"
    commands = {
        "disable": [["disable", target], ["bootout", row["domain"], row["path"]]],
        "enable": [["enable", target], ["bootstrap", row["domain"], row["path"]]],
        "restart": [["kickstart", "-k", target]],
    }
    if action not in commands:
        raise ValueError("Use enable, disable, or restart")
    for command in commands[action]:
        result = subprocess.run(["/bin/launchctl", *command], capture_output=True, text=True, timeout=8)
        tolerated = command[0] == "bootout" and not row["loaded"] or command[0] == "bootstrap" and row["loaded"]
        if result.returncode and not tolerated:
            raise ValueError(f"launchctl {command[0]} failed: {result.stderr.strip()[:250]}")
    return {"ok": True}
