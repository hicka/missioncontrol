"""Where user data lives.

Running from a source checkout, everything sits next to the code (the
project directory), same as always. Running as a frozen desktop app
(PyInstaller), data moves to a per-user application folder and the first
run is seeded from the bundled example configs. MISSIONCONTROL_HOME
overrides either way.
"""
import os
import shutil
import sys
from pathlib import Path


def _bundle_dir() -> Path:
    """Read-only bundled resources (web/, example configs)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _default_root() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "MissionControl"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "MissionControl"
    else:
        base = Path.home() / ".missioncontrol"
    return base


BUNDLE = _bundle_dir()
ROOT = Path(os.environ.get("MISSIONCONTROL_HOME") or _default_root())


def ensure_seeded():
    """Create the data dir and seed configs from bundled examples on first run."""
    ROOT.mkdir(parents=True, exist_ok=True)
    for example, target in (("agents.example.yaml", "agents.yaml"),
                            ("settings.example.yaml", "settings.yaml")):
        src = BUNDLE / example
        dst = ROOT / target
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


ensure_seeded()
