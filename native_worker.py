"""Bundled runtime and inventory helper for the native macOS host."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))


def check_port(port):
    import socket
    with socket.socket() as check:
        # Permit TIME_WAIT sockets from the previous runtime without allowing
        # two live listeners to bind the same address (no SO_REUSEPORT).
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", port))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--module-host":
        # A frozen app has no separate Python, so a module's process is this one.
        import module_host
        return module_host.main(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--cleanup-owned", action="store_true")
    parser.add_argument("--check-browser", action="store_true")
    parser.add_argument("--check-vault", action="store_true")
    parser.add_argument("--stop-process", type=int)
    parser.add_argument("--created", type=float)
    parser.add_argument("--login-service")
    parser.add_argument("--action")
    args = parser.parse_args()
    if args.check_vault:
        import credentials
        import secrets
        record = None
        try:
            password = secrets.token_urlsafe(32)
            record = credentials.save('https://missioncontrol-vault-check.invalid', 'diagnostic', password, 'Temporary storage check')
            ok = credentials.get(record['id'], record['origin'])['password'] == password
            print(json.dumps({'ok': ok, 'storage': credentials.status()['name']}))
            return 0 if ok else 1
        finally:
            if record:
                credentials.delete(record['id'])
    if args.check_browser:
        import asyncio
        import browser
        async def check():
            try:
                page = await browser._ensure_page("native-diagnostic")
                await page.goto("data:text/html,<title>Mission Control browser check</title>")
                return {"ok": await page.title() == "Mission Control browser check"}
            finally:
                await browser._reset()
        print(json.dumps(asyncio.run(check())))
        return 0
    if args.inventory or args.stop_process or args.login_service or args.cleanup_owned:
        import inventory
        try:
            if args.cleanup_owned:
                inventory.cleanup_instance()
                result = {"ok": True}
            elif args.stop_process:
                inventory.stop_process(args.stop_process, args.created or 0)
                result = {"ok": True}
            elif args.login_service:
                result = inventory.launch_action(args.login_service, args.action)
            else:
                # This short-lived scanner is shown by the host while it runs,
                # but must not leave a stale PID in the returned inventory.
                result = {"processes": [row for row in inventory.process_inventory() if row["pid"] != os.getpid()],
                          "launch_services": inventory.launch_services()}
            print(json.dumps(result))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        return 0
    # The host can terminate the entire owned process group after a grace period.
    if os.name != "nt":
        try:
            os.setsid()
        except PermissionError:
            pass
    import uvicorn
    import settings
    import inventory
    from apphome import ROOT
    existing = inventory.workspace_in_use(ROOT)
    if existing:
        print(f"Data folder already in use by Mission Control PID {existing}. Stop it in Runtime Manager first.", file=sys.stderr)
        return 78
    try:
        check_port(settings.port("app"))
    except OSError:
        print(f"App port {settings.port('app')} is occupied. Choose another port in Runtime Manager.", file=sys.stderr)
        return 78
    import selfupdate
    selfupdate.note_app_version(os.environ.get("MISSIONCONTROL_APP_VERSION"))
    from main import app, bind_host
    uvicorn.run(app, host=bind_host(), port=settings.port("app"), log_level="info", use_colors=False,
                timeout_graceful_shutdown=4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
