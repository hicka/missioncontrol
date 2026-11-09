"""Shared settings.yaml + .env access for all subsystems."""
import copy
import os
import re
import threading
from pathlib import Path

import yaml

from apphome import ROOT

SETTINGS_PATH = ROOT / "settings.yaml"
ENV_PATH = ROOT / ".env"
_lock = threading.RLock()


def write_whole(path: Path, text: str):
    """Replace a file in one step. A plain write empties it first, and whoever reads in
    that instant sees nothing: for settings that meant every default, and a caller that
    then saved what it had read would have written the defaults over the real file."""
    path = path.resolve() if path.is_symlink() else path     # a link stays a link
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        # Private from its first byte: these files hold keys.
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            out.write(text)
        os.chmod(tmp, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


_parsed: tuple | None = None   # (path, mtime_ns, size, data)


def load() -> dict:
    """Parsed once per change to the file. It is read dozens of times a second (every
    service asks whether it is enabled), and each used to be a full YAML parse on the
    event loop. Callers change what they get, so each gets its own copy."""
    global _parsed
    try:
        stat = SETTINGS_PATH.stat()
    except OSError:
        return {}
    key = (str(SETTINGS_PATH), stat.st_mtime_ns, stat.st_size)
    if _parsed is None or _parsed[:3] != key:
        data = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
        _parsed = (*key, data if isinstance(data, dict) else {})
    return copy.deepcopy(_parsed[3])


def save(settings: dict):
    with _lock:
        write_whole(SETTINGS_PATH, yaml.safe_dump(settings, sort_keys=False, allow_unicode=True))


def update(section: str, values: dict) -> dict:
    """Merge values into one top-level section and persist. Returns settings."""
    # Read and written under one lock, or two updates at once keep only the later one.
    with _lock:
        s = load()
        cur = s.get(section) or {}
        cur.update(values)
        s[section] = cur
        save(s)
    return s


PORT_DEFAULTS = {"app": 8899, "public": 8901, "fleet": 8902}
_PORT_ENV = {"app": "MISSIONCONTROL_PORT", "public": "MISSIONCONTROL_PUBLIC_PORT",
             "fleet": "MISSIONCONTROL_SYNC_PORT"}


def port(name: str) -> int:
    """Configured port: env override > settings.yaml ports section > default."""
    import os
    env = os.environ.get(_PORT_ENV.get(name, ""))
    if env and env.isdigit():
        return int(env)
    conf = (load().get("ports") or {}).get(name)
    try:
        return int(conf) if conf else PORT_DEFAULTS[name]
    except (TypeError, ValueError):
        return PORT_DEFAULTS[name]


def env_get(name: str) -> str | None:
    """Read a secret from the project .env (never from committed files)."""
    if not name or not ENV_PATH.exists():
        return None
    m = re.search(rf"^{re.escape(name)}=(.+)$", ENV_PATH.read_text(), re.M)
    return m.group(1).strip() if m else None


def env_all() -> dict:
    """All secrets in the project .env, as a dict. For handing to subprocesses
    that need to reference them by name (never logged)."""
    if not ENV_PATH.exists():
        return {}
    out = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def env_set(name: str, value: str):
    """Write or replace one secret in the project .env (mode 600)."""
    with _lock:
        lines = []
        if ENV_PATH.exists():
            lines = [l for l in ENV_PATH.read_text().splitlines()
                     if not l.startswith(f"{name}=")]
        if value:
            lines.append(f"{name}={value}")
        ENV_PATH.write_text("\n".join(lines) + "\n")
        ENV_PATH.chmod(0o600)
