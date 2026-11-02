"""Cross-platform Tailscale glue: find it, install it, bring it up, and
report this machine's tailnet address. Works on macOS, Windows and Linux
so the fleet reaches across networks without a static IP.
"""
import asyncio
import processes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

WIN = os.name == "nt"


def binary() -> str | None:
    """The tailscale CLI, wherever the OS put it."""
    if shutil.which("tailscale"):
        return shutil.which("tailscale")
    cands = []
    if WIN:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cands.append(str(Path(base) / "Tailscale" / "tailscale.exe"))
    else:
        cands += ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
                  "/usr/bin/tailscale"]
    for c in cands:
        if Path(c).exists():
            return c
    return None


async def _run(args: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        proc = await processes.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return 124, "timed out"
    except FileNotFoundError:
        return 127, "not found"
    except Exception as exc:
        return 1, f"{exc.__class__.__name__}: {exc}"


async def status() -> dict:
    """installed / running / this node's tailnet host."""
    b = binary()
    if not b:
        return {"installed": False, "running": False, "host": None}
    code, out = await _run([b, "status", "--json"], 8)
    if code != 0:
        return {"installed": True, "running": False, "host": None,
                "detail": out.strip()[:150]}
    try:
        st = json.loads(out)
    except ValueError:
        return {"installed": True, "running": False, "host": None}
    self_ = st.get("Self") or {}
    dns = (self_.get("DNSName") or "").rstrip(".")
    ips = self_.get("TailscaleIPs") or []
    host = dns or (ips[0] if ips else None)
    return {"installed": True, "running": st.get("BackendState") == "Running",
            "host": host, "backend": st.get("BackendState")}


async def http_url(port: int) -> str | None:
    st = await status()
    return f"http://{st['host']}:{port}" if st.get("host") else None


async def install() -> dict:
    """Best-effort install with the platform's package manager. Some paths
    need elevation; when they do we say the one command to run."""
    if binary():
        return {"ok": True, "note": "already installed"}
    if WIN:
        code, out = await _run(
            ["winget", "install", "--id", "tailscale.tailscale", "--silent",
             "--accept-package-agreements", "--accept-source-agreements"], 600)
        if binary():
            return {"ok": True, "note": "installed with winget"}
        return {"ok": False, "note": out.strip()[-200:] or
                "run in an admin PowerShell: winget install tailscale.tailscale"}
    if sys.platform == "darwin":
        if shutil.which("brew"):
            code, out = await _run(["brew", "install", "--cask", "tailscale"], 900)
            if binary():
                return {"ok": True, "note": "installed with Homebrew"}
            return {"ok": False, "note": out.strip()[-200:] or
                    "run: brew install --cask tailscale"}
        return {"ok": False, "note": "install Homebrew, or get Tailscale from "
                "https://tailscale.com/download"}
    code, out = await _run(["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"], 900)
    return {"ok": binary() is not None, "note": out.strip()[-200:]}


async def up(authkey: str | None = None) -> dict:
    """Bring the machine onto the tailnet. Without an auth key, `tailscale up`
    prints a login URL and waits; we surface that URL and let it finish in the
    background so the owner can click it. With an auth key it's headless."""
    b = binary()
    if not b:
        return {"ok": False, "note": "Tailscale is not installed"}
    st = await status()
    if st.get("running"):
        return {"ok": True, "note": "already up", "url": None}
    args = [b, "up"]
    if authkey:
        args += ["--authkey", authkey]
    if authkey:
        code, out = await _run(args, 60)
        ok = (await status()).get("running")
        return {"ok": bool(ok), "note": out.strip()[-200:], "url": None}
    # interactive login: read early output for the URL, leave it running
    import re
    try:
        proc = await processes.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=not WIN)
    except Exception as exc:
        return {"ok": False, "note": f"{exc.__class__.__name__}: {exc}"}
    buf = ""
    try:
        for _ in range(40):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
            if not line:
                break
            buf += line.decode("utf-8", errors="replace")
            m = re.search(r"https://login\.tailscale\.com/\S+", buf)
            if m:
                return {"ok": True, "url": m.group(0),
                        "note": "open this link to add the machine to your tailnet"}
    except asyncio.TimeoutError:
        pass
    # no URL captured (often means the GUI app is already signed in)
    st = await status()
    return {"ok": bool(st.get("running")), "url": None,
            "note": "brought up" if st.get("running") else (buf.strip()[-200:] or
            "run `tailscale up` once to sign this machine in")}
