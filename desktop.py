"""Mission Control desktop app.

One binary, three modes:
  (no args)   the window: makes sure the daemons are up, then opens a
              native webview. Closing the window leaves the daemons and
              the status bar item running.
  --server    the daemon: runs the FastAPI server (agents, channels,
              webhooks). Spawned detached so it outlives the window.
  --tray      the status bar item: shows team state, can reopen the
              window, restart Mission Control, or quit everything.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))


def _ensure_streams():
    """A --windowed PyInstaller build on Windows has no console, so
    sys.stdout/stderr are None. That crashes uvicorn's log formatter
    (`sys.stdout.isatty()`) and any print(). Point them at a log file so
    the app starts and first-run errors are captured, not lost."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from apphome import ROOT
        path = ROOT / "desktop.log"
    except Exception:
        path = Path(os.path.expanduser("~")) / "missioncontrol-desktop.log"
    try:
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def _resolve_port() -> int:
    try:
        import settings as _cfg
        return _cfg.port("app")
    except Exception:
        return int(os.environ.get("MISSIONCONTROL_PORT", "8899"))


PORT = _resolve_port()
URL = f"http://127.0.0.1:{PORT}"


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def spawn(mode_args: list[str]):
    """Start another mode of this app, detached so it survives us."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + mode_args
    else:
        cmd = [sys.executable, str(Path(__file__).resolve())] + mode_args
    from processes import spawn_options
    subprocess.Popen(cmd, **spawn_options(detached=True),
                     env={**os.environ, "MISSIONCONTROL_PORT": str(PORT)})


def wait_ready(timeout: float = 30.0) -> bool:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{URL}/api/state", timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def run_server():
    import apphome  # noqa: F401  (seeds the data dir on first run)
    import uvicorn
    from main import app, bind_host
    # use_colors=False so uvicorn never probes isatty (belt-and-suspenders
    # with _ensure_streams for the no-console Windows build)
    uvicorn.run(app, host=bind_host(), port=PORT, log_level="info", use_colors=False)


def tray_running() -> bool:
    from apphome import ROOT
    pidfile = ROOT / "tray.pid"
    try:
        pid = int(pidfile.read_text().strip())
        from processes import pid_alive
        return pid_alive(pid)
    except Exception:
        return False


def main():
    _ensure_streams()
    if sys.platform == "darwin" and "--server" not in sys.argv:
        launch_native()
        return
    if "--server" in sys.argv:
        run_server()
        return
    if "--tray" in sys.argv:
        import tray
        tray.main()
        return

    import apphome  # noqa: F401  (seeds the data dir on first run)
    if not port_in_use():
        spawn(["--server"])
    if not wait_ready():
        print("server failed to start; see logs", file=sys.stderr)
        sys.exit(1)
    if not tray_running():
        spawn(["--tray"])

    import webview
    # let attachment chips save files and image links open in the browser
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window("Mission Control", URL, width=1500, height=950,
                          min_size=(980, 640))
    webview.start()
    # Window closed: exit this process only. The server daemon and the
    # status bar item keep running; reopen from the status bar menu.


def launch_native():
    """The macOS window and menu bar now belong to a single AppKit host."""
    root = Path(__file__).resolve().parent
    candidates = [root / "dist/native/Mission Control.app",
                  Path.home() / "Applications/Mission Control.app",
                  Path("/Applications/Mission Control.app")]
    bundle = next((path for path in candidates if (path / "Contents/MacOS/MissionControl").exists()), None)
    if not bundle:
        raise SystemExit("Build the native app first: ./venv/bin/python scripts/build_macos.py")
    subprocess.run(["/usr/bin/open", str(bundle)], check=True)


if __name__ == "__main__":
    main()
