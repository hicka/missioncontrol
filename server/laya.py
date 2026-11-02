"""Self-hosted Laya: Jev's typed decisions, answered on this machine.

Laya is the open-source, Jev-compatible System 1 decision model
(convaiinnovations/laya), run with ONNX Runtime by the small Node service in
laya/. It takes the same request as Jev's /v1/systemone and returns the same
calibrated answers, but a question costs nothing and nothing leaves the Mac.
That changes what is worth asking: a sweep can put a question to every email
instead of a sample, and an agent can classify a hundred items without
reading each one through its model.

The runtime service supervises the Node process and restarts it if it dies;
the process loads the ~2 GB model on its first request and releases it after
idle_seconds without one. Every caller must cope with None: Laya not running
means "decide this yourself", the same contract as decide.py.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

import processes
import settings as cfg
from apphome import ROOT, BUNDLE

log = logging.getLogger("missioncontrol.laya")

DEFAULT_PORT = 8935
NODE_MAJOR = 20
# A cold start reads 1.7 GB of weights before the first answer; a warm question
# takes about a tenth of a second.
TIMEOUT = 120.0

_state: dict = {"proc": None, "error": "", "node": None}


def settings() -> dict:
    s = cfg.load().get("laya") or {}
    return {"enabled": bool(s.get("enabled", False)),
            "port": int(s.get("port") or DEFAULT_PORT),
            "node": s.get("node") or "",
            "idle_seconds": int(s.get("idle_seconds") or 900)}


def enabled() -> bool:
    return settings()["enabled"]


def url() -> str:
    return f"http://127.0.0.1:{settings()['port']}"


def service_dir() -> Path:
    local = ROOT / "laya"
    return local if (local / "server.mjs").exists() else BUNDLE / "laya"


def _node_major(path: str) -> int:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = re.match(r"v(\d+)\.", out.stdout.strip())
    # A node that cannot start (a Homebrew build against a newer ICU, say)
    # exits non-zero and prints nothing useful: not a candidate.
    return int(m.group(1)) if out.returncode == 0 and m else 0


def find_node(configured: str | None = None) -> str | None:
    """A Node.js new enough for onnxruntime-node that actually starts. The
    WhatsApp bridge passes its own configured path; None means Laya's."""
    configured = settings()["node"] if configured is None else configured
    candidates = [configured] if configured else []
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        def version(p: Path):
            return tuple(int(x) for x in re.findall(r"\d+", p.name)[:3])
        candidates += [str(p / "bin" / "node") for p in sorted(nvm.iterdir(), key=version, reverse=True)]
    candidates += ["/opt/homebrew/bin/node", "/usr/local/bin/node", shutil.which("node") or ""]
    for path in candidates:
        if path and os.access(path, os.X_OK) and _node_major(path) >= NODE_MAJOR:
            return path
    return None


async def health() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url()}/health")
            return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def problem() -> str:
    """What the runtime manager shows when the service is not working."""
    return _state["error"]


async def supervise():
    """Keep one Laya server running for as long as the service is on."""
    backoff = 2.0
    while True:
        if await health():
            # Already served on this port (a server started by hand): use it.
            _state["error"] = ""
            await asyncio.sleep(30)
            continue
        node = await asyncio.to_thread(find_node)
        folder = service_dir()
        missing = ""
        if not node:
            missing = (f"Laya needs Node.js {NODE_MAJOR} or newer; set laya.node in "
                       "settings.yaml to its path")
        elif not (folder / "node_modules" / "@receptron" / "laya").is_dir():
            missing = f"Laya is not installed: run npm install in {folder}"
        if missing:
            _state["error"] = missing
            log.warning("%s", missing)
            await asyncio.sleep(300)
            continue
        s = settings()
        with open(ROOT / "laya.log", "ab") as out:
            proc = await processes.create_subprocess_exec(
                node, "server.mjs", cwd=str(folder), stdout=out, stderr=subprocess.STDOUT,
                env=processes.environment({"LAYA_PORT": str(s["port"]),
                                           "LAYA_IDLE_SECONDS": str(s["idle_seconds"])}),
                **processes.spawn_options())
        _state.update(proc=proc, node=node, error="")
        log.info("laya started (pid %s, %s)", proc.pid, node)
        try:
            code = await proc.wait()
        finally:
            await processes.terminate(proc)
        _state["error"] = f"Laya exited with code {code}; restarting (see laya.log)"
        log.warning("%s", _state["error"])
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300.0)


async def _post(path: str, body: dict, timeout: float) -> dict | None:
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
            r = await client.post(f"{url()}{path}", json=body)
    except httpx.HTTPError as exc:
        log.info("laya unreachable: %s", exc)
        return None
    if r.status_code != 200:
        log.info("laya HTTP %s: %s", r.status_code, r.text[:300])
        try:
            return {"error": r.json().get("error") or {"message": r.text[:300]}}
        except ValueError:
            return {"error": {"message": r.text[:300]}}
    return r.json()


async def system_one(state, questions: dict, timeout: float = TIMEOUT) -> dict | None:
    """Every question about one state, in one forward pass.

    `questions` is Jev's shape: name -> {"type": "noul"|"choice"|"score",
    "instructions": str, "criteria": ...}. Returns {"answers": {...}, "usage": ...},
    {"error": {...}} when the request itself was wrong, or None when Laya is
    off or unreachable."""
    return await _post("/v1/systemone", {"state": state, "questions": questions}, timeout)


async def batch(items: list[dict], timeout: float = 900.0) -> list[dict] | None:
    """Many {"state", "questions"} items in one request, answered in order.
    An item that could not be answered comes back as {"error": ...}."""
    got = await _post("/v1/systemone/batch", {"items": items}, timeout)
    if got is None:
        return None
    if "results" not in got:
        # the request as a whole was refused: every item gets the reason
        return [{"error": got.get("error") or {"message": "unexpected reply"}} for _ in items]
    return got["results"]
