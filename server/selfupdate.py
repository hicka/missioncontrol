"""Keeping an installation current.

The signed app updates itself through Sparkle (native/macos/Updates.swift). A source
checkout follows GitHub from here, under the same rules self-improvement lands by:

1. Fast-forward only, on a clean tree, on master. A checkout with work of its own in it
   is left alone and says why.
2. Never while a task is running. An update waits for quiet.
3. The restart runs behind the rollback watchdog. A version that does not come back up is
   put back, remembered, and not tried again.
4. The database is copied first, so a migration that goes wrong can be undone by hand.

It follows tagged releases by default, not master. Self-repair pushes to master on its
own, and a plain push must not reach every machine that installed this.
"""
import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

from apphome import ROOT

logger = logging.getLogger("selfupdate")

SETTLE = 25
TICK = 1800
RETRY_BUSY = 300
KEEP_BACKUPS = 2
_RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

# What the last look found, for the settings screen.
status: dict = {"state": "unknown", "note": "", "checked": None, "target": None}


def settings() -> dict:
    import settings as cfg
    saved = cfg.load().get("updates") or {}
    return {"auto": bool(saved.get("auto", False)),
            "channel": saved.get("channel") if saved.get("channel") in ("release", "master") else "release",
            "skip": list(saved.get("skip") or [])}


def _save(**values):
    import settings as cfg
    cfg.update("updates", values)


def _git(*args, timeout=120) -> tuple[int, str]:
    import selfimprove as si
    return si._git(*args, timeout=timeout, env=_NO_PROMPT)


def newest_release(listing: str) -> tuple[str, str] | None:
    """The highest vX.Y.Z in `git ls-remote --tags`, with the commit it points at. An
    annotated tag lists both itself and, as ^{}, its commit; the commit is the one wanted."""
    found: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        peeled = name.endswith("^{}")
        name = name.removesuffix("^{}")
        if _RELEASE.match(name) and (peeled or name not in found):
            found[name] = sha.strip()
    if not found:
        return None
    best = max(found, key=lambda name: tuple(int(part) for part in _RELEASE.match(name).groups()))
    return best, found[best]


def _target(channel: str) -> tuple[str, str] | None:
    if channel == "master":
        code, out = _git("ls-remote", "origin", "refs/heads/master", timeout=60)
        return ("master", out.split()[0]) if code == 0 and out.strip() else None
    code, out = _git("ls-remote", "--tags", "origin", "refs/tags/v*", timeout=60)
    return newest_release(out) if code == 0 else None


def look() -> dict:
    """Is there something to move to, and could this checkout move to it?"""
    import selfimprove as si
    found = {"checked": time.time(), "target": None, "sha": None}
    if getattr(sys, "frozen", False):
        return {**found, "state": "app", "note": "This app updates itself from signed releases."}
    if not (si.REPO / ".git").exists():
        return {**found, "state": "none", "note": "Not a git checkout, so there is nothing to follow."}
    code, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or branch != "master":
        return {**found, "state": "blocked", "note": f"On {branch}, not master, so this checkout stays where it is."}
    code, dirty = _git("status", "--porcelain")
    if code != 0 or dirty.strip():
        return {**found, "state": "blocked", "note": "The working tree has uncommitted changes."}
    config = settings()
    target = _target(config["channel"])
    if not target:
        return {**found, "state": "current", "note": "No release to follow yet." if config["channel"] == "release" else "Could not reach GitHub."}
    name, sha = target
    found.update(target=name, sha=sha)
    if sha in config["skip"]:
        return {**found, "state": "blocked", "note": f"{name} did not come back up last time, so it waits for a newer version."}
    code, _ = _git("merge-base", "--is-ancestor", sha, "HEAD")
    if code == 0:
        return {**found, "state": "current", "note": f"Up to date with {name}."}
    ref = "refs/heads/master" if config["channel"] == "master" else f"refs/tags/{name}"
    code, out = _git("fetch", "--no-tags", "origin", ref, timeout=180)
    if code != 0:
        return {**found, "state": "blocked", "note": f"Could not fetch {name}: {out[:200]}"}
    code, _ = _git("merge-base", "--is-ancestor", "HEAD", sha)
    if code != 0:
        return {**found, "state": "blocked", "note": f"This checkout has commits {name} does not, so it cannot fast-forward."}
    return {**found, "state": "available", "note": f"{name} is available."}


def _under_launchd() -> bool:
    if sys.platform != "darwin":
        return False
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.missioncontrol"], capture_output=True, timeout=10)
    return result.returncode == 0


def backup_database(label: str) -> str | None:
    """A consistent copy of the live database, safe while it is being written. The name
    keeps it covered by the same ignore rule as the database itself."""
    source = ROOT / "missioncontrol.db"
    if not source.exists():
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label)
    destination = ROOT / f"missioncontrol.db-before-{safe}"
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live, sqlite3.connect(destination) as copy:
        live.backup(copy)
    older = sorted(ROOT.glob("missioncontrol.db-before-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in older[KEEP_BACKUPS:]:
        path.unlink(missing_ok=True)
    return str(destination)


def note_app_version(version: str | None):
    """The signed app, starting up. If this version differs from the one that last used
    this data, copy the database before anything migrates it."""
    if not version:
        return
    marker = ROOT / ".last-app-version"
    previous = marker.read_text().strip() if marker.exists() else ""
    if previous and previous != version:
        try:
            backup_database(previous)
        except Exception:
            logger.exception("could not copy the database before moving to %s", version)
    if previous != version:
        marker.write_text(version)


def _needs_hand(sha: str) -> str | None:
    """What fast-forwarding cannot finish alone. Python packages it can install first."""
    code, out = _git("diff", "--name-only", "HEAD", sha)
    changed = out.splitlines() if code == 0 else []
    node = [name for name in changed if name in ("whatsapp/package-lock.json", "laya/package-lock.json")]
    return f"This update changes Node packages ({', '.join(node)}); run npm install, then update." if node else None


def _install_requirements(sha: str) -> str | None:
    code, out = _git("diff", "--name-only", "HEAD", sha, "--", "requirements.txt")
    if code != 0 or not out.strip():
        return None
    code, text = _git("show", f"{sha}:requirements.txt")
    if code != 0:
        return "could not read the new requirements"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
        listing.write(text)
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", listing.name],
                                capture_output=True, text=True, timeout=600, env=_NO_PROMPT)
    finally:
        os.unlink(listing.name)
    return None if result.returncode == 0 else f"pip install failed: {result.stderr[-300:]}"


async def apply(orch, found: dict) -> str:
    """Move to what look() found, and restart onto it."""
    import selfimprove as si
    import settings as cfg
    busy = si._busy(orch)
    if busy:
        return f"Waiting for {', '.join(busy)} to finish."
    if not await asyncio.to_thread(_under_launchd):
        return f"{found['target']} is available. Update this checkout and restart it yourself: nothing here can restart it."
    sha, name = found["sha"], found["target"]
    blocked = await asyncio.to_thread(_needs_hand, sha) or await asyncio.to_thread(_install_requirements, sha)
    if blocked:
        return blocked
    code, previous = await asyncio.to_thread(_git, "rev-parse", "HEAD")
    if code != 0:
        return "Could not read the current commit."
    await asyncio.to_thread(backup_database, previous[:10])
    _save(attempt={"sha": sha, "previous": previous, "name": name, "ts": time.time()})
    code, out = await asyncio.to_thread(_git, "merge", "--ff-only", sha)
    if code != 0:
        _save(attempt=None)
        return f"Could not fast-forward: {out[:200]}"
    si._arm_watchdog(previous, cfg.port("app"))
    logger.info("updating %s -> %s (%s)", previous[:8], sha[:8], name)
    await asyncio.to_thread(si.restart)
    return f"Updating to {name}."


async def settle(orch):
    """After a restart: did the update take, or did the watchdog put the old code back?"""
    import selfimprove as si
    attempt = settings_attempt()
    if not attempt:
        return
    code, head = await asyncio.to_thread(_git, "rev-parse", "HEAD")
    if code != 0:
        return
    if head == attempt.get("sha"):
        _save(attempt=None)
        await si._tell_owner(orch, f"Updated to {attempt.get('name')}.")
    elif head == attempt.get("previous"):
        _save(attempt=None, skip=[*settings()["skip"], attempt.get("sha")])
        await si._tell_owner(orch, f"{attempt.get('name')} did not come back up, so the previous version was put back. "
                                   "It will not be tried again; a newer version will.")


def settings_attempt() -> dict | None:
    import settings as cfg
    return (cfg.load().get("updates") or {}).get("attempt")


async def check(orch, install: bool | None = None) -> dict:
    """One look. Installs what it finds when automatic updates are on, or when asked to."""
    found = await asyncio.to_thread(look)
    status.update(found)
    if found["state"] == "available" and (settings()["auto"] if install is None else install):
        status["note"] = await apply(orch, found)
    return dict(status)


async def loop(orch, broadcast=None):
    await asyncio.sleep(SETTLE)
    try:
        await settle(orch)
    except Exception:
        logger.exception("could not settle the last update")
    while True:
        wait = TICK
        try:
            result = await check(orch)
            if result["state"] == "available" and result["note"].startswith("Waiting"):
                wait = RETRY_BUSY
        except Exception:
            logger.exception("update check failed")
        await asyncio.sleep(wait)
