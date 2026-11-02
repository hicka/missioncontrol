"""Fleet sync: several Mission Control nodes acting as one system.

Each node keeps its own agents, chats, and schedules, but knowledge
replicates: team memory (with deletions), skills, and web playbooks
flow to every connected node, and agents can message teammates on other
machines: the Router dispatches work to "agent@node".

Transport: every node runs a small sync listener on SYNC_PORT (LAN,
0.0.0.0) that serves only /sync/* routes, authenticated with the shared
SYNC_KEY from .env, which must match across your machines. Peers are
pull-based: each node polls its peers for changes since its last
cursor, with an overlap window so clock skew between machines cannot
drop rows (guids make the merge idempotent).

settings.yaml:
  fleet:
    node_name: studio          # defaults to the hostname
    peers:
      - name: laptop
        url: http://192.168.1.20:8902
"""
import asyncio
import hmac
import logging
import os
import socket
import time

import httpx

import db
import settings as cfg
import skills

logger = logging.getLogger("missioncontrol.sync")

def sync_port() -> int:
    return cfg.port("fleet")
OVERLAP = 300.0  # seconds of re-fetch to absorb clock skew between nodes

# peer name -> {"agents": [...], "node": str, "last_sync": float, "ok": bool, "detail": str}
peer_state: dict[str, dict] = {}
_orch = None


def fleet_settings() -> dict:
    f = cfg.load().get("fleet") or {}
    return {"node_name": (f.get("node_name") or socket.gethostname().split(".")[0]).lower(),
            "peers": f.get("peers") or []}


def sync_key() -> str | None:
    return cfg.env_get("SYNC_KEY")


def enabled() -> bool:
    return bool(sync_key() and fleet_settings()["peers"])


def node_name() -> str:
    return fleet_settings()["node_name"]


def _auth_ok(request) -> bool:
    key = sync_key()
    got = request.headers.get("x-sync-key", "")
    return bool(key) and hmac.compare_digest(got, key)


def remote_agents() -> list[dict]:
    """Flat roster of teammates on connected machines: id is 'agent@node'."""
    out = []
    for peer, st in peer_state.items():
        if not st.get("ok"):
            continue
        for a in st.get("agents") or []:
            out.append({"id": f"{a['id']}@{peer}", "name": a.get("name", a["id"]),
                        "blurb": a.get("blurb", ""), "node": peer})
    return out


def add_peer(name: str, url: str) -> None:
    """Add or update a peer in the fleet settings, live (the pull loop and
    listener read settings fresh, so no restart is needed)."""
    name = (name or "").strip().lower()
    url = (url or "").strip().rstrip("/")
    if not name or not url:
        return
    s = cfg.load()
    fleet = s.setdefault("fleet", {})
    peers = [p for p in (fleet.get("peers") or []) if p.get("name") != name]
    peers.append({"name": name, "url": url})
    fleet["peers"] = peers
    cfg.save(s)


def ensure_key() -> str:
    """This fleet's shared secret, minting one on first use."""
    k = sync_key()
    if not k:
        import secrets
        k = secrets.token_urlsafe(24)
        cfg.env_set("SYNC_KEY", k)
    return k


def make_join_code(url: str) -> str:
    """A single opaque string another machine pastes to join this fleet."""
    import base64
    import json as _json
    payload = {"v": 1, "node": node_name(), "url": url.rstrip("/"), "key": ensure_key()}
    return base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")


def read_join_code(code: str) -> dict | None:
    import base64
    import json as _json
    try:
        pad = "=" * (-len(code.strip()) % 4)
        d = _json.loads(base64.urlsafe_b64decode(code.strip() + pad))
        if d.get("node") and d.get("url") and d.get("key"):
            return {"node": str(d["node"]).lower(), "url": d["url"], "key": d["key"]}
    except Exception:
        pass
    return None


def find_peer(name: str) -> dict | None:
    for p in fleet_settings()["peers"]:
        if p.get("name") == name:
            return p
    return None


async def send_remote(target: str, from_label: str, text: str, hop: int) -> str:
    """Deliver a cross-node agent message: target is 'agent@peer'."""
    aid, _, peer_name = target.partition("@")
    peer = find_peer(peer_name)
    if not peer:
        return f"unknown machine '{peer_name}'; connected: {', '.join(peer_state) or 'none'}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{peer['url'].rstrip('/')}/sync/message",
                headers={"x-sync-key": sync_key() or ""},
                json={"to_agent": aid, "from_label": from_label,
                      "text": text, "hop": hop})
            if r.status_code == 200:
                return f"delivered to {aid} on {peer_name}"
            return f"peer {peer_name} refused: HTTP {r.status_code}"
    except Exception as exc:
        return f"could not reach {peer_name}: {exc.__class__.__name__}"


def build_listener():
    """The LAN-facing app: only /sync/* routes, key-authed."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/sync/export")
    async def export(request: Request, since: float = 0.0):
        if not _auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        since = max(0.0, since - OVERLAP)
        mems, tombs = await asyncio.to_thread(db.export_memories, since)
        return {
            "node": node_name(),
            "now": time.time(),
            "memories": mems,
            "tombstones": tombs,
            "skills": await asyncio.to_thread(skills.export_skills, since),
            "playbooks": await asyncio.to_thread(db.export_playbooks, since),
            "agents": [{"id": a["id"], "name": a["name"], "blurb": a.get("blurb", "")}
                       for a in (_orch.agents.values() if _orch else [])],
        }

    @app.post("/sync/message")
    async def message(request: Request):
        if not _auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        to_agent = (body.get("to_agent") or "").strip()
        from_label = (body.get("from_label") or "").strip()
        text = (body.get("text") or "").strip()
        hop = int(body.get("hop") or 0)
        if not (_orch and to_agent and from_label and text):
            return JSONResponse({"error": "bad request"}, status_code=400)
        if to_agent not in _orch.agents:
            return JSONResponse({"error": f"no agent '{to_agent}' here"}, status_code=404)
        await _orch.remote_message(to_agent, from_label, text, hop)
        return {"ok": True}

    @app.post("/sync/join")
    async def join(request: Request):
        """A peer registers itself back with us (the second half of a join,
        so both sides know each other after one paste)."""
        if not _auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        name = (body.get("node") or "").strip().lower()
        url = (body.get("url") or "").strip()
        if not name or not url:
            return JSONResponse({"error": "node and url required"}, status_code=400)
        add_peer(name, url)
        return {"ok": True, "node": node_name(),
                "agents": len(_orch.agents) if _orch else 0}

    @app.get("/sync/ping")
    async def ping(request: Request):
        if not _auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return {"node": node_name(), "agents": len(_orch.agents) if _orch else 0}

    return app


async def _pull_peer(peer: dict):
    name = peer.get("name")
    st = peer_state.setdefault(name, {"cursor": 0.0})
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{peer['url'].rstrip('/')}/sync/export",
                headers={"x-sync-key": sync_key() or ""},
                params={"since": st.get("cursor") or 0.0})
        if r.status_code != 200:
            st.update(ok=False, detail=f"HTTP {r.status_code}")
            return
        data = r.json()
        applied = await asyncio.to_thread(
            db.merge_memories, data.get("memories") or [], data.get("tombstones") or [])
        applied += await asyncio.to_thread(skills.merge_skills, data.get("skills") or [])
        applied += await asyncio.to_thread(db.merge_playbooks, data.get("playbooks") or [])
        st.update(ok=True, detail="", agents=data.get("agents") or [],
                  node=data.get("node"), last_sync=time.time(),
                  cursor=data.get("now") or time.time())
        if applied:
            logger.info("sync: merged %d changes from %s", applied, name)
    except Exception as exc:
        st.update(ok=False, detail=f"{exc.__class__.__name__}")


async def loop(orch):
    global _orch
    _orch = orch
    await asyncio.sleep(10)
    while True:
        if enabled():
            for peer in fleet_settings()["peers"]:
                await _pull_peer(peer)
        await asyncio.sleep(60)


def status() -> dict:
    f = fleet_settings()
    return {"node_name": f["node_name"], "key_set": bool(sync_key()),
            "port": sync_port(),
            "peers": [{**{"name": p.get("name"), "url": p.get("url")},
                       **{k: v for k, v in (peer_state.get(p.get("name")) or {}).items()
                          if k in ("ok", "detail", "last_sync", "node")}}
                      for p in f["peers"]]}
