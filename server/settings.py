"""Shared settings.yaml + .env access for all subsystems."""
import re
import threading
from pathlib import Path

import yaml

from apphome import ROOT

SETTINGS_PATH = ROOT / "settings.yaml"
ENV_PATH = ROOT / ".env"
_lock = threading.Lock()


def load() -> dict:
    if SETTINGS_PATH.exists():
        data = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
        if isinstance(data, dict):
            return data
    return {}


def save(settings: dict):
    with _lock:
        SETTINGS_PATH.write_text(
            yaml.safe_dump(settings, sort_keys=False, allow_unicode=True))


def update(section: str, values: dict) -> dict:
    """Merge values into one top-level section and persist. Returns settings."""
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
