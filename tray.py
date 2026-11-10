"""Mission Control status bar item (macOS menu bar / Windows system tray).

Shows the Mission Control mark color-coded by what the team is doing:
gray = idle, amber = thinking (a model call is in flight), green = doing
(an agent is running tools). The menu lists each agent's state and can
reopen the window, restart Mission Control, or quit everything. Runs as
its own small process so it outlives the window.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))

def _resolve_port() -> int:
    try:
        import settings as _cfg
        return _cfg.port("app")
    except Exception:
        return int(os.environ.get("MISSIONCONTROL_PORT", "8899"))


PORT = _resolve_port()
URL = f"http://127.0.0.1:{PORT}"
POLL_SECONDS = 2.0

if getattr(sys, "frozen", False):
    ASSETS = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
else:
    ASSETS = Path(__file__).resolve().parent / "assets"

ICONS = {state: Image.open(ASSETS / f"tray-{state}.png")
         for state in ("idle", "thinking", "doing")}
STATE_MARK = {"idle": "", "thinking": "…", "doing": "▸"}

_status = {"agents": {}, "names": {}}
_overall = "idle"


def _pidfile() -> Path:
    from apphome import ROOT
    return ROOT / "tray.pid"


def _claim_single_instance() -> bool:
    pf = _pidfile()
    try:
        pid = int(pf.read_text().strip())
        from processes import pid_alive
        if pid_alive(pid):
            return False  # another tray is alive
    except Exception:
        pass
    pf.write_text(str(os.getpid()))
    return True


def _spawn_app(mode_args: list[str]):
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + mode_args
    else:
        desktop = Path(__file__).resolve().parent / "desktop.py"
        cmd = [sys.executable, str(desktop)] + mode_args
    from processes import spawn_options
    subprocess.Popen(cmd, **spawn_options(detached=True),
                     env={**os.environ, "MISSIONCONTROL_PORT": str(PORT)})


def _server_up() -> bool:
    try:
        return httpx.get(f"{URL}/api/state", timeout=2.0).status_code == 200
    except Exception:
        return False


def _poll():
    global _overall
    while True:
        try:
            r = httpx.get(f"{URL}/api/agents-status", timeout=3.0)
            data = r.json()
            _status["agents"] = data.get("agents", {})
            _status["names"] = data.get("names", {})
            states = set(_status["agents"].values())
            _overall = ("doing" if "doing" in states
                        else "thinking" if "thinking" in states else "idle")
        except Exception:
            _overall = "idle"
            _status["agents"] = {}
        time.sleep(POLL_SECONDS)


def _restart_server():
    """Stop the server; if no supervisor (launchd) brings it back within a
    few seconds, start our own daemon process."""
    try:
        httpx.post(f"{URL}/api/shutdown", timeout=3.0)
    except Exception:
        pass
    time.sleep(1.5)
    for _ in range(6):
        if _server_up():
            return  # a supervisor already restarted it
        time.sleep(1.0)
    _spawn_app(["--server"])


def main():
    if sys.platform == "darwin":
        from desktop import launch_native
        launch_native()
        return
    if sys.platform == "darwin":
        # Menu bar only: no Dock icon, no app switcher entry for the tray
        # process (the main window app keeps its own Dock presence). And
        # when the user opens the app from Finder/Spotlight while only
        # this tray instance is running, LaunchServices sends the reopen
        # event here: answer it by spawning a fresh window process.
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            from Foundation import NSObject

            class _ReopenDelegate(NSObject):
                def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
                    _spawn_app([])
                    return False

            ns_app = NSApplication.sharedApplication()
            ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            global _reopen_delegate  # keep a strong ref; NSApp holds it weakly
            _reopen_delegate = _ReopenDelegate.alloc().init()
            ns_app.setDelegate_(_reopen_delegate)
        except Exception:
            pass

    if not _claim_single_instance():
        return

    import pystray
    from pystray import Menu, MenuItem

    def agent_items():
        if not _status["agents"]:
            yield MenuItem("server not running", None, enabled=False)
            return
        for aid, state in _status["agents"].items():
            name = _status["names"].get(aid, aid)
            mark = STATE_MARK.get(state, "")
            yield MenuItem(f"{name}  {mark} {state}".rstrip(), None, enabled=False)

    def open_app(icon, item):
        _spawn_app([])

    def open_bar(icon, item):
        _spawn_app(["--bar"])

    def restart_app(icon, item):
        threading.Thread(target=_restart_server, daemon=True).start()

    def quit_all(icon, item):
        try:
            httpx.post(f"{URL}/api/shutdown", timeout=3.0)
        except Exception:
            pass
        try:
            _pidfile().unlink()
        except Exception:
            pass
        icon.stop()

    icon = pystray.Icon(
        "missioncontrol", ICONS["idle"], "Mission Control",
        menu=Menu(lambda: (
            MenuItem("Mission Control", None, enabled=False),
            Menu.SEPARATOR,
            *agent_items(),
            Menu.SEPARATOR,
            MenuItem("Open Mission Control", open_app, default=True),
            MenuItem("Show floating bar", open_bar),
            MenuItem("Restart Mission Control", restart_app),
            MenuItem("Quit Mission Control", quit_all),
        )),
    )

    threading.Thread(target=_poll, daemon=True).start()

    def refresh():
        last = None
        last_menu = None
        while True:
            state = _overall
            if state != last:
                icon.icon = ICONS[state]
                icon.title = f"Mission Control: {state}"
                last = state
            # pystray does not re-evaluate a dynamic menu on open (macOS
            # builds the NSMenu once); push updates when content changes.
            snapshot = (state, tuple(sorted(_status["agents"].items())))
            if snapshot != last_menu:
                try:
                    icon.update_menu()
                except Exception:
                    pass
                last_menu = snapshot
            time.sleep(1.0)

    threading.Thread(target=refresh, daemon=True).start()
    try:
        icon.run()
    finally:
        try:
            _pidfile().unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
