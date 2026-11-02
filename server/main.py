"""Mission Control server: REST + WebSocket + static frontend."""
import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from typing import Literal

import uvicorn

import channels
import cron as cron_mod
import db
import github
import llm
import settings as cfg
import sync as sync_mod
import watch as watch_mod
import orchestrator as orch_mod
import voice as voice_mod
import budget as budget_mod
import push as push_mod
import skills as skills_mod
import commands as commands_mod
import evolve
import selfimprove
import kernel_store as kernel
import recall as recall_mod
import triggers as triggers_mod
import runtime_store
import tools
import runtime
import processes
import inventory
import laya
import mailsweep
import whatsapp_link
import uploads as uploads_mod
from channels.whatsapp import WEBHOOK_PATH as WHATSAPP_WEBHOOK_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry credentials
logger = logging.getLogger("missioncontrol")

from apphome import BUNDLE

WEB_DIR = BUNDLE / "web"

orch = orch_mod.Orchestrator()
sockets: set[WebSocket] = set()


async def broadcast(event: dict):
    async def deliver(ws):
        try:
            await asyncio.wait_for(ws.send_json(event), 2)
        except Exception:
            sockets.discard(ws)
    await asyncio.gather(*(deliver(ws) for ws in list(sockets)))


async def _channel_message(channel: str, chat_id: str, sender: str, text: str,
                           media: list | None = None):
    import auth_channels
    auth_channels.observe(channel, chat_id, sender)
    if await auth_channels.consume(channel, chat_id, sender, text):
        return  # Codes never enter transcripts, memory, tool arguments or models.
    conf = channels.channel_settings().get(channel) or {}
    aid = conf.get("default_agent") or ""
    if aid not in orch.agents:
        aid = next(iter(orch.agents))
    for item in media or []:
        part = await _describe_media(channel, item)
        text = (text + "\n" + part).strip() if text else part
    if not text:
        return
    logger.info("%s -> dm:%s (%d chars)", channel, aid, len(text))
    try:
        await orch.user_message(f"dm:{aid}", text, source=channel, chat_guid=chat_id)
    except ValueError as exc:
        if orch.channel_send:
            await orch.channel_send(channel, chat_id, str(exc))


async def _describe_media(channel: str, item: dict) -> str:
    """Turn an inbound photo or voice note into text the agent can act on."""
    ch = channels.manager.get(channel)
    data = await ch.fetch_media(item) if ch and hasattr(ch, "fetch_media") else None
    if not data:
        return f"[{item['kind']} attached, but it could not be downloaded]"
    if item["kind"] == "audio":
        transcript = await llm.transcribe_audio(data, item.get("name") or "voice.ogg")
        if transcript:
            return f"[voice note] {transcript}"
        return ("[voice note attached; transcription needs an OpenAI key in "
                "Settings for Whisper]")
    import base64
    desc = await llm.describe_image(base64.b64encode(data).decode(),
                                    item.get("mime") or "image/jpeg")
    return f"[photo] {desc}"


orch.broadcast = broadcast
orch.has_viewers = lambda: bool(sockets)
orch.channel_send = channels.manager.send
whatsapp_link.on_messages = orch.whatsapp_messages
channels.manager.on_message = _channel_message


tunnel = github.Tunnel()
public_url: str | None = None


async def _dispatch_github_event(evt: dict):
    if evt.get("store"):
        kind = evt.get("kind_for_memory") or "github-event"
        await asyncio.to_thread(db.add_memory, "github", evt.get("memory") or evt["text"], kind)
    who = await github.classify(evt)
    text = evt["text"]
    # The login says the owner and they did not do this. Without saying so, "you opened
    # #504" is the obvious thing for the Router to conclude from it.
    if who["under_owner"]:
        text += "\n(An agent did this through the owner's GitHub login. It was not the owner.)"
    await orch.outside_event("github", github.event_thread(evt), text, actor=evt.get("actor", ""),
                             title=(evt.get("memory") or evt["text"])[:120], profile=github.route_agent(evt, orch.agents),
                             dedupe=evt.get("key") or None, from_owner=who["from_owner"],
                             routing=github.routing(evt, who),
                             address={"shared": True} if evt.get("shared_branch") else None)


async def _github_webhook_handler(request: Request):
    body = await request.body()
    secret = github.settings()["secret"]
    if not github.verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    event_name = request.headers.get("x-github-event", "")
    if event_name == "ping":
        return {"status": "pong"}
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    evt = github.parse_webhook(event_name, payload)
    if evt:
        logger.info("github event: %s", evt["text"][:120])
        await _dispatch_github_event(evt)
    return {"status": "ok"}


async def _whatsapp_verify_handler(request: Request):
    ch = channels.manager.get("whatsapp")
    if not ch:
        return JSONResponse({"error": "channel off"}, status_code=404)
    challenge = ch.verify(request.query_params.get("hub.mode", ""),
                          request.query_params.get("hub.verify_token", ""),
                          request.query_params.get("hub.challenge", ""))
    if challenge is None:
        return JSONResponse({"error": "verification failed"}, status_code=403)
    return PlainTextResponse(challenge)


async def _whatsapp_webhook_handler(request: Request):
    ch = channels.manager.get("whatsapp")
    if not ch or not ch.conf.get("enabled"):
        return {"status": "ok"}
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    for msg in ch.parse_inbound(payload):
        if not ch.sender_allowed(msg["sender"]):
            logger.info("whatsapp: ignored unlisted sender %s", msg["sender"])
            continue
        await _channel_message("whatsapp", msg["chat_id"], msg["sender"], msg["text"])
    return {"status": "ok"}


def _webhook_routes() -> list[dict]:
    return cfg.load().get("webhooks") or []


async def _generic_hook_handler(request: Request):
    """User-defined event routes: POST /hook/{name} on the public listener.
    Wakes the route's agent with the payload, or just posts it in the chat
    (mode=notify) with zero model cost."""
    name = request.path_params.get("name", "")
    route = next((r for r in _webhook_routes() if r.get("name") == name), None)
    if not route:
        return JSONResponse({"error": "unknown hook"}, status_code=404)
    secret = route.get("secret") or ""
    body = await request.body()
    supplied = request.query_params.get("token") or request.headers.get("x-webhook-token", "")
    if not secret or not (supplied == secret
                          or github.verify_signature(secret, body,
                                request.headers.get("x-hub-signature-256"))):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        import json as _json
        payload = _json.loads(body.decode() or "{}")
        pretty = _json.dumps(payload, indent=1)[:1500]
    except Exception:
        pretty = body.decode(errors="replace")[:1500]
    aid = route.get("agent") or ""
    if aid not in orch.agents:
        aid = next(iter(orch.agents))
    text = f"webhook '{name}' received:\n{pretty}"
    if route.get("mode") == "notify":
        await orch.post_event_line(aid, f"hook:{name}", text)
    else:
        await orch.outside_event(f"hook:{name}", f"hook:{name}", text, title=f"webhook {name}", profile=aid)
    return {"status": "ok"}


def _public_needed() -> bool:
    wa = channels.channel_settings()["whatsapp"]
    return bool(github.settings()["enabled"] or wa.get("enabled")
                or _webhook_routes())


async def _serve_public():
    public = FastAPI()
    public.post(github.WEBHOOK_PATH)(_github_webhook_handler)
    public.get(WHATSAPP_WEBHOOK_PATH)(_whatsapp_verify_handler)
    public.post(WHATSAPP_WEBHOOK_PATH)(_whatsapp_webhook_handler)
    public.post("/hook/{name}")(_generic_hook_handler)
    server = uvicorn.Server(uvicorn.Config(public, host="127.0.0.1",
                            port=cfg.port("public"), log_level="warning", timeout_graceful_shutdown=2))
    server.capture_signals = nullcontext
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        if server.started:
            await server.shutdown()
        raise
    except SystemExit as exc:
        raise RuntimeError(f"Public listener could not bind port {cfg.port('public')}") from exc
    finally:
        server.should_exit = True


async def _start_tunnel():
    global public_url
    public_url = await tunnel.start()
    if not public_url:
        raise RuntimeError("Tunnel unavailable. Configure cloudflared, then restart this service.")
    if github.settings()["enabled"]:
        await github.register_hooks(public_url)


async def _serve_fleet():
    server = uvicorn.Server(uvicorn.Config(sync_mod.build_listener(), host="0.0.0.0",
                            port=sync_mod.sync_port(), log_level="warning", timeout_graceful_shutdown=2))
    server.capture_signals = nullcontext
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        if server.started:
            await server.shutdown()
        raise
    except SystemExit as exc:
        raise RuntimeError(f"Fleet listener could not bind port {sync_mod.sync_port()}") from exc
    finally:
        server.should_exit = True


async def _github_poll_loop():
    await asyncio.sleep(20)
    while True:
        try:
            async def dispatch(evt):
                logger.info("github poll event: %s", evt["text"][:120])
                await _dispatch_github_event(evt)
            await github.poll_once(dispatch)
            for repo, error in github.poll_trouble():
                await orch.router.notify_owner(f"I have not been able to read {repo} on GitHub for half an hour, so I "
                                               f"may be missing events from it: {error[:120]}")
        except Exception:
            logger.exception("github poll failed")
        await asyncio.sleep(github.settings()["poll_seconds"])


def configure_runtime():
    import browser
    import browser_auth
    import auth_channels
    browser_auth.emit = broadcast
    auth_channels.source_for = lambda aid: orch.owner_channel()
    manager = runtime.Registry()
    manager.emit = broadcast
    runtime.registry = manager

    async def stop_agents():
        # Stopping the engine is not stopping the work: sessions keep their state.
        await orch.close()
        orch.reset_states()

    manager.add("agents", "Agent engine", "The Router, its Workers, their inboxes and tool execution.",
                start=orch.start, stop=stop_agents, settings_tab="llm")
    manager.add("skill_learning", "Skill learning", "Extracts reusable procedures from completed work.",
                dependencies=("agents",), group="harness", settings_tab="memory")
    manager.add("browser", "Agent browser", "On-demand browser sessions and their Chromium processes.",
                stop=browser._reset, dependencies=("agents",), group="harness", settings_tab="browser")
    manager.add("watcher", "Watch engine", "Bounded status polls, deadlines and resolution notifications.",
                run=lambda: watch_mod.watch_loop(orch, broadcast), dependencies=("agents",))
    manager.add("watchdog", "Watchdog", "Checks every session is really getting somewhere, restarts or nudges "
                "what is not, and retries what failed from outside.",
                run=lambda: orch.watchdog.loop(), dependencies=("agents",))
    manager.add("evolution", "Self-improvement", "Learns lessons from your corrections and repeated failures, "
                "trials them, and keeps only what measurably helps.",
                run=lambda: evolve.loop(orch), default=evolve.enabled, dependencies=("agents",),
                group="harness", settings_tab="evolution",
                configure=lambda enabled: cfg.update("evolution", {"enabled": enabled}))
    # Always running: even with self-repair off, a change the owner approved
    # with pushing on is pushed from here once the app is back up on it.
    manager.add("selfrepair", "Self-repair", "Finds Mission Control's own recurring problems, fixes "
                "them on a tested branch, and lands and pushes them when you allow it.",
                run=lambda: selfimprove.loop(orch, broadcast), dependencies=("agents",),
                group="harness", settings_tab="improve")
    manager.add("scheduler", "Scheduled jobs", "Runs owner-approved jobs and monitor checks.",
                run=lambda: cron_mod.scheduler(orch, broadcast), dependencies=("agents",), settings_tab="jobs")
    manager.add("heartbeat", "Heartbeat", "Periodic checks against your heartbeat checklist.",
                run=lambda: cron_mod.heartbeat_loop(orch), dependencies=("agents",),
                default=lambda: cron_mod.heartbeat_settings()["enabled"], settings_tab="channels",
                configure=lambda enabled: cfg.update("heartbeat", {"enabled": enabled}))
    manager.add("brief", "Daily brief", "Delivers your configured daily digest.",
                run=lambda: cron_mod.brief_loop(orch), dependencies=("agents",),
                default=lambda: cron_mod.brief_settings()["enabled"], settings_tab="channels",
                configure=lambda enabled: cfg.update("brief", {"enabled": enabled}))
    manager.add("laya", "Laya decisions", "Self-hosted, Jev-compatible decision model: free typed "
                "decisions on this Mac, loaded on first use and released when idle.",
                run=laya.supervise, default=laya.enabled, health=laya.problem, group="harness",
                configure=lambda enabled: cfg.update("laya", {"enabled": enabled}))
    import mcp_servers
    manager.add("mcp", "MCP servers", "Connections to the owner's MCP tool servers: keeps local "
                "servers running and their tool lists current.",
                run=mcp_servers.supervise, default=mcp_servers.enabled, health=mcp_servers.problem,
                dependencies=("agents",), group="harness", settings_tab="github")
    manager.add("mail_sweep", "Mail cleanup", "Archives bulk mail from the Mail inbox, judged by Laya "
                "with no LLM.",
                run=mailsweep.loop, default=lambda: mailsweep.settings()["enabled"],
                dependencies=("laya",),
                configure=lambda enabled: cfg.update("mail_sweep", {"enabled": enabled}))
    manager.add("whatsapp", "WhatsApp", "Your own WhatsApp as a linked device, read-only: keeps the "
                "bridge running and stores what arrives for agents to read.",
                run=whatsapp_link.supervise, default=whatsapp_link.enabled, settings_tab="github",
                configure=lambda enabled: cfg.update("whatsapp_link", {"enabled": enabled}))
    for name in channels.CHANNEL_TYPES:
        manager.add(f"channel_{name}", f"{name.title()} channel", "Incoming events and outbound replies.",
                    start=lambda name=name: channels.manager.start_one(name),
                    stop=lambda name=name: channels.manager.stop_one(name),
                    default=lambda name=name: channels.channel_settings()[name]["enabled"],
                    configure=lambda enabled, name=name: channels.save_channel(name, {"enabled": enabled}),
                    dependencies=("agents",), group="channels", settings_tab="channels")
    manager.add("public_listener", "Webhook listener", "Local endpoint for configured webhook routes.",
                run=_serve_public, default=_public_needed, dependencies=("agents",), group="network", settings_tab="github")
    manager.add("tunnel", "Webhook tunnel", "cloudflared connection for incoming webhook deliveries.",
                start=_start_tunnel, stop=tunnel.stop, default=_public_needed, auto_restart=True,
                health=lambda: "Tunnel exited; restart to reconnect." if tunnel.proc and tunnel.proc.returncode is not None else "",
                dependencies=("public_listener",), group="network", settings_tab="github")
    manager.add("github_poll", "GitHub monitor", "Periodic repository and CI updates.",
                run=_github_poll_loop, default=lambda: github.settings()["enabled"],
                dependencies=("agents",), group="network", settings_tab="github")
    manager.add("fleet_listener", "Fleet listener", "Key-authenticated connections from your other machines.",
                run=_serve_fleet, default=lambda: bool(sync_mod.sync_key()),
                dependencies=("agents",), group="network", settings_tab="github")
    manager.add("fleet_sync", "Fleet synchronization", "Shares memory, skills and playbooks with paired machines.",
                run=lambda: sync_mod.loop(orch), default=sync_mod.enabled,
                dependencies=("fleet_listener",), group="network", settings_tab="github")
    return manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    from apphome import ROOT
    existing = await asyncio.to_thread(inventory.workspace_in_use, ROOT)
    if existing:
        raise RuntimeError(f"Data folder is already in use by Mission Control PID {existing}.")
    lock = runtime.InstanceLock(ROOT).acquire()
    manager = configure_runtime()
    supervisor = parent_monitor = None
    try:
        parent_monitor = runtime.spawn(runtime.monitor_owner(), "runtime", "Native host monitor")
        supervisor = runtime.spawn(runtime.supervise(), "runtime", "Service supervisor")
        logger.info("managed runtime %s started (pid %s)", runtime.INSTANCE, __import__('os').getpid())
        yield
    finally:
        tasks = [task for task in (supervisor, parent_monitor) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()
        await channels.manager.stop()
        await processes.shutdown_owned()
        lock.release()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def private_input_validation(request: Request, exc: RequestValidationError):
    if request.url.path.startswith('/api/runtime/browser'):
        # FastAPI's default error includes the invalid input, including secrets.
        return JSONResponse({'ok': False, 'error': 'Invalid browser request. Check the required fields and their lengths.'},
                            status_code=422, headers={'Cache-Control': 'no-store'})
    return await request_validation_exception_handler(request, exc)

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}


def _api_token() -> str | None:
    import os as _os
    return _os.environ.get("MC_API_TOKEN") or cfg.env_get("MC_API_TOKEN")


def bind_host() -> str:
    """The address the app listens on: loopback only, unless the owner turned on
    remote access in Settings (which also guarantees an API token exists).
    Every entry point asks here, so the toggle means the same thing whether the
    app was started by the desktop bundle, run.sh, or a launchd service."""
    import os as _os
    env = _os.environ.get("MISSIONCONTROL_HOST")
    if env:
        return env
    try:
        if (cfg.load().get("server") or {}).get("expose"):
            return "0.0.0.0"
    except Exception:
        logger.exception("could not read the expose setting; staying on loopback")
    return "127.0.0.1"


def _request_authorized(client_host: str | None, auth_header: str, token_param: str) -> bool:
    """Local requests are trusted; remote ones need the API token."""
    if client_host in LOCAL_HOSTS:
        return True
    token = _api_token()
    if not token:
        return False  # remote access requires a configured token, always
    import hmac as _hmac
    supplied = ""
    if auth_header.startswith("Bearer "):
        supplied = auth_header[7:].strip()
    elif token_param:
        supplied = token_param
    return bool(supplied) and _hmac.compare_digest(supplied, token)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    host = request.client.host if request.client else None
    if request.url.path.startswith("/api/runtime"):
        # Native process controls belong to the local owner, including when
        # remote chat access is enabled. Cross-origin forms cannot invoke them.
        origin = request.headers.get("origin")
        allowed_origins = {f"http://{name}:{request.url.port or 80}" for name in ("127.0.0.1", "localhost", "[::1]")}
        if host not in LOCAL_HOSTS or (origin and origin not in allowed_origins):
            return JSONResponse({"error": "Runtime management requires the local app."}, status_code=403)
    if not _request_authorized(host, request.headers.get("authorization", ""),
                               request.query_params.get("token", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    response = await call_next(request)
    if request.url.path.startswith('/api/runtime/browser'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get("/")
async def index():
    # the app window must pick up UI changes on its next open, not a cached page
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


class _FreshStatic(StaticFiles):
    """The index page is already served no-cache so the app window picks up UI
    changes on its next open; the scripts and styles it pulls in have to
    revalidate too, or the window reopens on a stale mix of old and new."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _FreshStatic(directory=WEB_DIR), name="static")
uploads_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_mod.UPLOAD_DIR), name="uploads")


@app.get("/api/state")
async def state():
    agents = [
        {"id": a["id"], "name": a["name"], "color": a["color"], "shape": a.get("shape", "face"),
         "order": a.get("order", 99)}
        for a in orch.agents.values()
    ]
    agents.sort(key=lambda a: a["order"])
    threads = []
    for a in agents:
        last = await asyncio.to_thread(db.last_message, f"dm:{a['id']}", a["id"])
        threads.append({"id": f"dm:{a['id']}", "kind": "dm", "agent": a["id"],
                        "name": a["name"], "last": last})
    last_group = await asyncio.to_thread(db.last_message, "group:team")
    threads.append({"id": "group:team", "kind": "group", "agent": None,
                    "name": "Group chat", "last": last_group,
                    "members": [a["id"] for a in agents]})
    return {"user": orch.user, "agents": agents, "threads": threads}


@app.get("/api/commands")
async def command_catalog():
    return {"commands": commands_mod.CATALOG}


class CommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=32000)
    thread: str = ""     # the main chat, unless a command is run from another one


@app.post("/api/command")
async def run_command(body: CommandBody):
    text = body.text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "enter a command or objective"}, status_code=400)
    try:
        return await orch.user_message(body.thread or f"dm:{orch.lead}",
                                       text if text.startswith("/") else "/mission " + text)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/control")
async def control_snapshot():
    import sys
    missions, executions, watches, skill_list, activity, counts, inbox, growth, jobs = await asyncio.gather(
        asyncio.to_thread(runtime_store.missions), asyncio.to_thread(runtime_store.executions, include_running=True),
        asyncio.to_thread(db.list_watches), asyncio.to_thread(skills_mod.list_skills),
        asyncio.to_thread(db.recent_activity), asyncio.to_thread(db.knowledge_counts),
        _inbox(), asyncio.to_thread(_evolution_summary), asyncio.to_thread(db.list_cron))
    running = {}
    for e in executions:
        if e["status"] == "running" and e["kind"] == "tool":
            running.setdefault(e["agent"], e)
    return {"missions": missions, "executions": executions, "watches": watches[:60],
            "skills": skill_list, "activity": activity, "counts": counts,
            "inbox": inbox, "evolution": growth, "jobs": jobs,
            "workers": [{k: w[k] for k in ("id", "profile", "mission", "status", "title", "strength", "speculative", "updated")}
                        for w in await asyncio.to_thread(kernel.sessions, None, "worker", 80)],
            "agents": {aid: {"state": orch.agent_state.get(aid, "idle"),
                              "queued": orch.workers.load(aid),
                              "role": a.get("blurb", ""),
                              "tool": (running.get(aid) or {}).get("name"),
                              "tool_since": (running.get(aid) or {}).get("ts"),
                              "mission": orch.workers.mission_of(aid)} for aid, a in orch.agents.items()},
            "platform": "macOS" if sys.platform == "darwin" else "Windows" if sys.platform == "win32" else "Linux"}


async def _inbox() -> list[dict]:
    """Everything waiting on the owner, wherever it was raised: a command the
    guard held, a recurring job asking to start, a proposed change to the app,
    a website sign-in, a lesson that needs a decision. Each of these used to
    live on its own settings tab, which is where they waited.

    Whatever is approved here runs exactly as shown, so nothing that will run
    is shortened or left out: a held command in full, with what changed since
    a near-identical one was answered; a job's whole task and the shell check
    it runs before every fire."""
    import browser_auth
    every, jobs, proposals = await asyncio.gather(
        asyncio.to_thread(runtime_store.approvals),
        asyncio.to_thread(db.list_cron, "pending"),
        asyncio.to_thread(db.list_improvements, "pending", 20))
    items = []
    # A worker stopped on a question is waiting on the owner as much as a held command
    # is. A question used to appear in one batch and live on only in the chat after that.
    asked = await asyncio.to_thread(kernel.sessions, ("blocked",), "worker", 40)
    for row in asked:
        if row["question"]:
            items.append({"kind": "question", "id": row["id"], "agent": row["profile"], "ts": row["updated"],
                          "title": row["question"], "detail": row["title"], "mission": row.get("mission")})
    for a in every:
        if a["status"] != "pending":
            continue
        if a.get("kind") == "mcp":
            items.append({"kind": "tool", "id": a["id"], "agent": a["agent"], "ts": a["ts"],
                          "title": a["command"], "detail": a["cwd"], "reason": a["reason"]})
            continue
        items.append({"kind": "command", "id": a["id"], "agent": a["agent"], "ts": a["ts"],
                      "title": a["command"], "detail": a["cwd"], "reason": a["reason"],
                      "prior": _approval_context(a, every)})
    for j in jobs:
        items.append({"kind": "job", "id": j["id"], "agent": j["agent"], "ts": j["ts"],
                      "title": j["name"], "detail": j["task"], "schedule": j["schedule"],
                      "monitor_cmd": j.get("monitor_cmd") or "", "max_runs": j.get("max_runs") or 0})
    for p in proposals:
        items.append({"kind": "proposal", "id": p["id"], "agent": p["agent"], "ts": p["ts"],
                      "title": p["title"], "detail": p["rationale"][:600], "stat": p["stat"]})
    for r in browser_auth.pending():
        items.append({"kind": "signin", "id": r.get("id"), "agent": r.get("agent"),
                      "ts": (r.get("expires") or 0) - browser_auth.WAIT_SECONDS,
                      "title": f"Sign-in needed on {r.get('origin') or 'a website'}",
                      "detail": r.get("message") or "", "expires": r.get("expires")})
    try:
        held = await asyncio.to_thread(evolve.lessons, None, evolve.HELD, 40)
        for lesson in held:
            items.append({"kind": "lesson", "id": lesson["id"], "agent": lesson["agent"],
                          "ts": lesson["updated"], "title": lesson["text"], "detail": lesson["note"],
                          "reason": lesson["status"], "status": lesson["status"],
                          "source": lesson["source"]})
        last = await asyncio.to_thread(evolve.runs, "review", 1)
        for note in ((last[0]["report"] or {}).get("attention") or []) if last else []:
            row = await asyncio.to_thread(evolve.get_lesson, note.get("lesson"))
            # Once the owner has acted on a lesson since that review, it is answered.
            if row and row["status"] not in evolve.HELD and row["updated"] <= last[0]["ts"] + 1 and not any(
                    i["kind"] == "lesson" and i["id"] == row["id"] for i in items):
                items.append({"kind": "lesson", "id": row["id"], "agent": row["agent"],
                              "ts": last[0]["ts"], "title": row["text"], "detail": note.get("why", ""),
                              "reason": "attention", "status": row["status"], "source": row["source"]})
    except Exception:
        logger.exception("could not read lessons for the inbox")
    items.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    return items


def _evolution_summary() -> dict:
    """What the home screen shows about the team getting better."""
    try:
        card = evolve.scorecard(14)
        rows = evolve.lessons(None, ("trial", "active"), 200)
        last = evolve.runs("review", 1)
        return {"enabled": evolve.enabled(), "week": card["week"], "previous_week": card["previous_week"],
                "days": [{k: d[k] for k in ("day", "runs", "clean_rate", "corrections", "error_rate")}
                         for d in card["days"]],
                "trial": sum(1 for l in rows if l["status"] == "trial"),
                "active": sum(1 for l in rows if l["status"] == "active"),
                "latest": [{"id": l["id"], "text": l["text"], "status": l["status"], "agent": l["agent"]}
                           for l in sorted(rows, key=lambda l: -l["ts"])[:3]],
                "last_review": last[0]["ts"] if last else None}
    except Exception:
        logger.exception("evolution summary failed")
        return {"enabled": False}


@app.get("/api/evolution")
async def evolution_snapshot():
    data = await asyncio.to_thread(evolve.snapshot)
    clusters = await asyncio.to_thread(evolve.failure_clusters, __import__("time").time() - 14 * 86400,
                                       evolve.CLUSTER_MIN, False)
    data["clusters"] = [{k: c[k] for k in ("signature", "tool", "count", "agents", "last", "lesson", "state")}
                        for c in clusters[:12]]
    data["agents"] = {aid: a["name"] for aid, a in orch.agents.items()}
    return data


class EvolutionBody(BaseModel):
    enabled: bool | None = None
    review_at: str | None = Field(default=None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")


@app.put("/api/evolution")
async def evolution_settings(body: EvolutionBody):
    values = body.model_dump(exclude_none=True)
    if values:
        cfg.update("evolution", values)
    if body.enabled is not None:
        await runtime.registry.reconcile()
    await broadcast({"type": "evolution_updated"})
    return {"ok": True, "settings": evolve.settings()}


@app.post("/api/evolution/review")
async def evolution_review_now():
    """Run the nightly review now. It never blocks the page: the result
    arrives as an evolution_updated event."""
    async def run():
        try:
            await evolve.review(orch, reason="requested by the owner")
        except Exception:
            logger.exception("requested evolution review failed")
            await broadcast({"type": "evolution_updated"})
    runtime.spawn(run(), "evolution", "Evolution review")
    return {"ok": True}


class LessonBody(BaseModel):
    agent: str = "*"
    text: str = Field(min_length=1, max_length=400)


@app.post("/api/lessons")
async def lesson_add(body: LessonBody):
    if body.agent != "*" and body.agent not in orch.agents:
        return {"ok": False, "error": "unknown agent"}
    try:
        lesson = await asyncio.to_thread(evolve.add_lesson, body.agent, body.text, "owner")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    await broadcast({"type": "evolution_updated"})
    return {"ok": True, "lesson": lesson}


class LessonUpdate(BaseModel):
    status: Literal["trial", "active", "retired", "suspended", "proposed"] | None = None
    pinned: bool | None = None
    text: str | None = Field(default=None, max_length=400)


@app.put("/api/lessons/{lid}")
async def lesson_update(lid: int, body: LessonUpdate):
    fields = body.model_dump(exclude_none=True)
    if fields.get("status") and "text" not in fields:
        fields["note"] = {"retired": "retired by the owner", "active": "kept by the owner",
                          "trial": "put back on trial by the owner",
                          "suspended": "suspended by the owner",
                          "proposed": "held for the owner"}[fields["status"]]
    try:
        lesson = await asyncio.to_thread(evolve.update_lesson, lid, **fields)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not lesson:
        return {"ok": False, "error": "no such lesson"}
    await broadcast({"type": "evolution_updated"})
    return {"ok": True, "lesson": lesson}


@app.get("/api/agents/{aid}/profile")
async def agent_profile(aid: str):
    """One agent's work in one place: what it is doing now, what is scheduled
    for it, what it finished, and what it has learned."""
    if aid not in orch.agents:
        return JSONResponse({"ok": False, "error": "no such agent"}, status_code=404)
    a = orch.agents[aid]
    import time as _time
    since = _time.time() - 7 * 86400
    missions, executions, jobs, watches, lessons, card = await asyncio.gather(
        asyncio.to_thread(runtime_store.missions, 200),
        asyncio.to_thread(runtime_store.executions, 200, None, True),
        asyncio.to_thread(db.list_cron), asyncio.to_thread(db.list_watches, "active"),
        asyncio.to_thread(evolve.lessons, aid, ("trial", "active"), 40),
        asyncio.to_thread(evolve.scorecard, 7))
    mine = [m for m in missions if m["agent"] == aid]
    return {
        "ok": True,
        "agent": {"id": aid, "name": a["name"], "role": a.get("blurb", ""), "color": a.get("color"),
                  "model": (a.get("llm") or {}).get("model") or llm.config().get("model"),
                  "own_model": bool((a.get("llm") or {}).get("model")), "cwd": a.get("cwd", "~")},
        "state": orch.agent_state.get(aid, "idle"), "queued": orch.workers.load(aid),
        "now": [e for e in executions if e["agent"] == aid and e["status"] == "running"][:6],
        "active": [m for m in mine if m["status"] in ("queued", "running", "waiting")][:12],
        "scheduled": {"jobs": [j for j in jobs if j["agent"] == aid and j["status"] in ("active", "pending", "paused")],
                      "watches": [w for w in watches if w["agent"] == aid]},
        "done": [m for m in mine if m["status"] in ("completed", "failed", "cancelled")
                 and m["updated"] >= since][:12],
        "recent_tools": [e for e in executions if e["agent"] == aid][:12],
        "lessons": lessons, "stats": card["agents"].get(aid) or {}}


class UserBody(BaseModel):
    name: str
    initials: str | None = None


@app.put("/api/user")
async def set_user(body: UserBody):
    if not body.name.strip():
        return {"ok": False, "error": "name required"}
    user = orch.set_user(body.name, body.initials)
    await broadcast({"type": "user", "user": user})
    return {"ok": True, "user": user}


@app.post("/api/shutdown")
async def shutdown():
    """Graceful exit: run lifespan cleanup and stop owned children."""
    import os as _os
    logger.info("shutdown requested via API")
    asyncio.get_event_loop().call_later(0.3, runtime.request_shutdown)
    return {"ok": True}


@app.get("/api/runtime")
async def runtime_snapshot():
    import os
    from apphome import ROOT
    process_rows, login_rows = await asyncio.gather(
        asyncio.to_thread(inventory.process_inventory), asyncio.to_thread(inventory.launch_services))
    return {"instance": runtime.INSTANCE, "pid": os.getpid(), "started": runtime.STARTED,
            "owner_pid": int(os.environ.get("MISSIONCONTROL_OWNER_PID") or 0),
            "data_home": str(ROOT), "services": runtime.registry.snapshot(),
            "processes": process_rows, "launch_services": login_rows,
            "tasks": runtime.task_snapshot(), "recent_tasks": runtime._history,
            "ports": {name: cfg.port(name) for name in ("app", "public", "fleet")},
            "harness": {"validation": "enforced", "agents": [
                {"id": aid, "name": agent["name"], "state": orch.agent_state.get(aid, "idle"),
                 "queued": orch.workers.load(aid), "max_tool_rounds": agent.get("max_tool_rounds", 40),
                 "max_tool_calls": agent.get("max_tool_calls", 160), "tools": agent.get("tools"),
                 "skills": agent.get("skills"), "skill_run": (orch.harness.active.get(aid) or {}).get("id")}
                for aid, agent in orch.agents.items()]}}


@app.post("/api/runtime/services/{service}/{action}")
async def runtime_service_action(service: str, action: str):
    try:
        await runtime.registry.action(service, action)
        return {"ok": True, "services": runtime.registry.snapshot()}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/runtime/browser")
async def browser_snapshot():
    import browser
    import browser_auth
    import credentials
    snapshot, vault = await asyncio.gather(browser.snapshot(), asyncio.to_thread(credentials.status))
    try:
        entries = await asyncio.to_thread(credentials.list_entries)
    except credentials.VaultError as exc:
        entries, vault = [], {**vault, 'available': False, 'error': str(exc)}
    return {**snapshot, 'vault': vault, 'credentials': entries, 'requests': browser_auth.pending(),
            'auth_channel': (cfg.load().get('browser') or {}).get('auth_channel', 'auto'), "agents": [
        {"id": aid, "name": a["name"], "allowed": a.get("tools") is None or "browser" in a["tools"]}
        for aid, a in orch.agents.items()]}


class BrowserOpen(BaseModel):
    url: str = Field(default="", max_length=4096)


@app.post("/api/runtime/browser/open")
async def browser_open(body: BrowserOpen):
    import browser
    try:
        await asyncio.wait_for(runtime.spawn(browser.open_owner(body.url), "browser", "Owner browser window"), 40)
        await broadcast({"type": "browser_updated"})
        return {"ok": True, **await browser_snapshot()}
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not open the browser ({type(exc).__name__}). Check Runtime Manager and try again."}, status_code=400)


class BrowserSettings(BaseModel):
    headless: bool | None = None
    auth_channel: Literal['auto', 'app', 'imessage', 'telegram', 'whatsapp'] | None = None


@app.put("/api/runtime/browser/settings")
async def browser_settings(body: BrowserSettings):
    import browser
    if body.headless is not None and browser.headless() != body.headless:
        cfg.update("browser", {"headless": body.headless})
        await browser._reset()
    if body.auth_channel is not None:
        cfg.update('browser', {'auth_channel': body.auth_channel})
    await broadcast({"type": "browser_updated"})
    return {"ok": True, **await browser_snapshot()}


class WebsiteCredential(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    username: SecretStr = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=4096)
    label: str = Field(default='', max_length=100)
    credential_id: str | None = Field(default=None, max_length=40)


@app.post('/api/runtime/browser/credentials')
async def website_credential_save(body: WebsiteCredential):
    import credentials
    try:
        saved = await asyncio.to_thread(credentials.save, body.url, body.username.get_secret_value(),
                                       body.password.get_secret_value(), body.label, body.credential_id)
        await broadcast({'type': 'browser_updated'})
        return {'ok': True, 'credential': saved}
    except credentials.VaultError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)


@app.delete('/api/runtime/browser/credentials/{cid}')
async def website_credential_delete(cid: str):
    import credentials
    try:
        await asyncio.to_thread(credentials.delete, cid)
        await broadcast({'type': 'browser_updated'})
        return {'ok': True}
    except credentials.VaultError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)


class WebsiteAuthAnswer(BaseModel):
    username: SecretStr = Field(default=SecretStr(''), max_length=320)
    password: SecretStr = Field(default=SecretStr(''), max_length=4096)
    value: SecretStr = Field(default=SecretStr(''), max_length=4096)
    label: str = Field(default='', max_length=100)
    remember: bool = False
    credential_id: str | None = Field(default=None, max_length=40)
    cancel: bool = False
    confirmed: bool = False


@app.post('/api/runtime/browser/requests/{rid}')
async def website_auth_answer(rid: str, body: WebsiteAuthAnswer):
    import browser_auth
    try:
        values = body.model_dump()
        for key in ('username', 'password', 'value'):
            values[key] = values[key].get_secret_value()
        try:
            await browser_auth.answer(rid, values)
        finally:
            values.clear()
        return {'ok': True}
    except ValueError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)


@app.post("/api/runtime/browser/sessions/{session}/{action}")
async def browser_session_action(session: str, action: str):
    import browser
    try:
        await browser.session_action(session, action)
        await broadcast({"type": "browser_updated"})
        return {"ok": True, **await browser_snapshot()}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": "Browser tab changed. Refresh and try again."}, status_code=400)


class ProcessAction(BaseModel):
    pid: int = Field(gt=1)
    created: float = Field(gt=0)


@app.post("/api/runtime/processes/stop")
async def runtime_process_stop(body: ProcessAction):
    try:
        await asyncio.to_thread(inventory.stop_process, body.pid, body.created)
        await broadcast({"type": "runtime_updated"})
        return {"ok": True}
    except (ValueError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


class LaunchServiceAction(BaseModel):
    id: str
    action: str


@app.post("/api/runtime/login-service")
async def runtime_login_service(body: LaunchServiceAction):
    try:
        return await asyncio.to_thread(inventory.launch_action, body.id, body.action)
    except (ValueError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/runtime/logs")
async def runtime_logs():
    from apphome import ROOT
    from harness import redact
    def read():
        candidate = ROOT / "runtime.log"
        if not candidate.exists():
            candidate = ROOT / "missioncontrol.log"
        if not candidate.exists():
            return "No log file yet. Runtime diagnostics are also written to the launching console."
        with candidate.open("rb") as stream:
            stream.seek(max(0, candidate.stat().st_size - 24000))
            return redact(stream.read().decode(errors="replace"))
    return {"text": await asyncio.to_thread(read)}


@app.post("/api/agents/{aid}/stop")
async def stop_agent(aid: str):
    if aid not in orch.agents:
        return {"ok": False, "error": "no such agent"}
    stopped = await orch.stop_agent(aid)
    return {"ok": True, "stopped": stopped}


# ---------- the kernel: what the agent is doing, and why ----------

@app.get("/api/health")
async def health():
    """What bin/mc-watchdog probes from outside. Answering at all shows the event
    loop is alive; the rest says whether the Router is reading its inbox, and what is
    wrong that restarting would not fix."""
    router = orch.router
    pending = await asyncio.to_thread(kernel.inbox_should_wake, router.id)
    problems = [f"{row['name']}: {row['error'] or 'failed'}" for row in runtime.registry.snapshot()
                if row["status"] == "failed" and row["enabled"]]
    return {"ok": True, "router_alive": bool(router.task and not router.task.done()),
            "router_pending": pending["count"], "workers": len(orch.workers.running()),
            "paused": orch.paused, "problems": problems}


@app.post("/api/kernel/pause")
async def kernel_pause():
    """Stop picking up work, without stopping the app. Events are still recorded."""
    await orch.pause()
    return {"ok": True, "paused": True}


@app.post("/api/kernel/resume")
async def kernel_resume():
    await orch.resume()
    return {"ok": True, "paused": False}


@app.get("/api/kernel")
async def kernel_snapshot():
    import time as _time
    sessions, events, threads, policies, findings, counts = await asyncio.gather(
        asyncio.to_thread(kernel.sessions, None, "worker", 60),
        asyncio.to_thread(kernel.triggers, 80),
        asyncio.to_thread(kernel.threads, 40),
        asyncio.to_thread(kernel.policies),
        asyncio.to_thread(kernel.findings, 30),
        asyncio.to_thread(kernel.trigger_counts, _time.time() - 86_400))
    router = orch.router
    pending = await asyncio.to_thread(kernel.inbox_should_wake, router.id)
    batches = [b for b in router.batches if b]
    return {"paused": orch.paused,
            "router": {"name": orch.agents[orch.lead]["name"], "agent": orch.lead,
                       "state": orch.agent_state.get(orch.lead, "idle"), "pending": pending["count"],
                       "alive": bool(router.task and not router.task.done()),
                       "average_batch": round(sum(batches) / len(batches), 2) if batches else 0},
            "slots": {"limit": orch.workers.slots.limit, "used": orch.workers.slots.used},
            "sessions": sessions, "events": events, "threads": threads, "policies": policies,
            "sources": triggers_mod.source_table(), "findings": findings, "events_24h": counts,
            "outbox": await asyncio.to_thread(kernel.outbox_stuck)}


@app.get("/api/kernel/sessions/{sid}")
async def kernel_session(sid: str):
    row = await asyncio.to_thread(kernel.session_get, sid)
    if not row:
        return JSONResponse({"error": "no such session"}, status_code=404)
    subscriptions, reports, jobs, inbox = await asyncio.gather(
        asyncio.to_thread(kernel.subscriptions_of, sid), asyncio.to_thread(kernel.reports_of, sid),
        asyncio.to_thread(kernel.jobs_of, sid), asyncio.to_thread(kernel.inbox_pending, sid))
    log = await asyncio.to_thread(kernel.log_all, sid, 120)
    return {"session": row, "subscriptions": subscriptions, "reports": reports, "jobs": jobs,
            "inbox": len(inbox), "transcript": log,
            "findings": await asyncio.to_thread(kernel.findings, 20, sid)}


class StepInBody(BaseModel):
    text: str = Field(min_length=1, max_length=32000)


@app.post("/api/kernel/sessions/{sid}/message")
async def kernel_step_in(sid: str, body: StepInBody):
    """The owner stepping into background work, without going through the Router."""
    try:
        await orch.step_in(sid, body.text.strip())
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True}


@app.post("/api/kernel/sessions/{sid}/{action}")
async def kernel_session_action(sid: str, action: str):
    row = await asyncio.to_thread(kernel.session_get, sid)
    if not row or row["kind"] != "worker":
        return JSONResponse({"ok": False, "error": "no such worker"}, status_code=404)
    if action == "stop":
        return {"ok": await orch.workers.stop(sid)}
    if action == "restart":
        await orch.watchdog.restart(sid, "The owner restarted this task. Check the real state before repeating "
                                         "anything with an outward effect, then carry on.")
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)


@app.get("/api/kernel/recall/{oid:path}")
async def kernel_recall(oid: str):
    return {"id": oid, "text": await asyncio.to_thread(recall_mod.lookup, oid)}


class PolicyBody(BaseModel):
    action: Literal["list", "add", "enable", "disable", "remove", "source"]
    id: str | None = None
    text: str | None = Field(default=None, max_length=2000)
    source: str | None = None
    admission: Literal["explicit", "candidate", "ignore"] | None = None


@app.post("/api/kernel/policies")
async def kernel_policy(body: PolicyBody):
    result = await asyncio.to_thread(triggers_mod.apply_policy, body.model_dump(exclude_none=True))
    await broadcast({"type": "kernel_updated"})
    return {"ok": not result.startswith("error"), "result": result}


@app.get("/api/agents-status")
async def agents_status():
    return {"agents": {aid: orch.agent_state.get(aid, "idle") for aid in orch.agents},
            "names": {aid: a["name"] for aid, a in orch.agents.items()}}


PAGE_SIZE = 300


@app.get("/api/thread/{thread_id:path}")
async def thread(thread_id: str, before_ts: float | None = None,
                 before_id: int | None = None, at_id: int | None = None,
                 limit: int = PAGE_SIZE):
    """One page of a thread, newest last.

    Pass the oldest message you hold as before_ts/before_id to walk further
    back, or at_id to land on the page that ends with a particular message
    (how a search result opens). has_more says whether older messages remain;
    at_latest says whether this page runs up to the present.
    """
    agent_id = thread_id.split(":", 1)[1] if thread_id.startswith("dm:") else None
    limit = max(1, min(limit, PAGE_SIZE))
    before = None
    if at_id is not None:
        target = await asyncio.to_thread(db.get_message, at_id)
        if target:
            # ids are integers, so id + 1 is an exclusive bound that still
            # includes the target message itself at the foot of the page
            before = (target["ts"], at_id + 1)
    elif before_ts is not None and before_id is not None:
        before = (before_ts, before_id)
    msgs = await asyncio.to_thread(db.thread_view, thread_id, agent_id, limit, before)
    has_more = False
    if msgs:
        older = await asyncio.to_thread(db.thread_view, thread_id, agent_id, 1,
                                        (msgs[0]["ts"], msgs[0]["id"]))
        has_more = bool(older)
    return {"messages": msgs, "has_more": has_more, "at_latest": before is None}


@app.get("/api/search")
async def search_history(q: str = "", limit: int = 60):
    """Full-text search over every message, however old."""
    if not q.strip():
        return {"results": []}
    msgs = await asyncio.to_thread(db.search_messages, q, max(1, min(limit, 200)))
    return {"results": msgs}


class AgentBody(BaseModel):
    name: str
    prompt: str
    color: str = "#7a68c9"
    cwd: str = "~"
    blurb: str = ""
    llm: dict | None = None


@app.post("/api/agents")
async def create_agent(body: AgentBody):
    try:
        agent = orch.add_agent(body.name, body.prompt, body.color, body.cwd, body.blurb,
                               body.llm)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    await broadcast({"type": "agent_added", "agent": {
        "id": agent["id"], "name": agent["name"], "color": agent["color"],
        "shape": agent["shape"], "order": agent["order"]}})
    return {"ok": True, "agent": agent}


@app.get("/api/agents-full")
async def agents_full():
    return {"agents": [
        {"id": a["id"], "name": a["name"], "color": a["color"],
         "shape": a.get("shape", "face"), "order": a.get("order", 99),
         "cwd": a.get("cwd", "~"), "blurb": a.get("blurb", ""),
         "prompt": a.get("prompt", ""), "llm": a.get("llm") or {},
         "tools": a.get("tools"), "skills": a.get("skills"),
         "max_tool_rounds": a.get("max_tool_rounds", orch_mod.MAX_TOOL_ROUNDS),
         "max_tool_calls": a.get("max_tool_calls", 160)}
        for a in orch.agents.values()
    ]}


class AgentUpdateBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    color: str | None = None
    cwd: str | None = None
    blurb: str | None = None
    # {} puts the agent back on the team default; omitted leaves it alone
    llm: dict | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    max_tool_rounds: int | None = Field(default=None, ge=1, le=200)
    max_tool_calls: int | None = Field(default=None, ge=1, le=800)


@app.put("/api/agents/{aid}")
async def update_agent(aid: str, body: AgentUpdateBody):
    try:
        agent = orch.update_agent(aid, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    await broadcast({"type": "agent_updated", "agent": {
        "id": aid, "name": agent["name"], "color": agent["color"],
        "shape": agent["shape"], "order": agent.get("order", 99)}})
    return {"ok": True, "agent": agent}


@app.delete("/api/agents/{aid}")
async def delete_agent(aid: str):
    try:
        orch.remove_agent(aid)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    fallback = next(iter(orch.agents), "")
    for name, conf in channels.channel_settings().items():
        if conf.get("default_agent") == aid:
            channels.save_channel(name, {"default_agent": fallback})
    await broadcast({"type": "agent_removed", "agent_id": aid})
    return {"ok": True}


# ── Import hub: skills and agent templates from a git URL ───────────────

class ImportBody(BaseModel):
    url: str
    kind: str = "skills"   # skills | agents


@app.post("/api/import")
async def import_from_git(body: ImportBody):
    """Clone a repo shallowly and pull in its skills/ folders or its
    agents.yaml starter roster. Only import sources you trust: skills are
    instructions your agents will follow."""
    import shutil
    import subprocess
    import tempfile
    url = body.url.strip()
    if not (url.startswith("https://") or url.startswith("git@")):
        return {"ok": False, "error": "https or git url required"}
    td = tempfile.mkdtemp(prefix="mc-import-")
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["git", "clone", "--depth", "1", url, td],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "clone failed")[:200]}
        root = Path(td)
        if body.kind == "skills":
            import skills as skills_mod
            src = root / "skills" if (root / "skills").exists() else root
            count = 0
            for d in src.iterdir():
                f = d / "SKILL.md"
                if d.is_dir() and f.exists() and count < 50:
                    meta = skills_mod._parse(f.read_text())
                    skills_mod.save_skill(f"import:{url[:60]}",
                                          meta.get("name", d.name),
                                          meta.get("description", ""),
                                          meta["body"])
                    count += 1
            return {"ok": True, "imported": count, "kind": "skills"}
        if body.kind == "agents":
            import yaml as yaml_mod
            for candidate in ("agents.yaml", "agents.example.yaml"):
                f = root / candidate
                if f.exists():
                    data = yaml_mod.safe_load(f.read_text()) or {}
                    added = []
                    for a in (data.get("agents") or [])[:12]:
                        if a.get("id") in orch.agents or not a.get("name"):
                            continue
                        try:
                            agent = orch.add_agent(a["name"], a.get("prompt", ""),
                                                   a.get("color", "#7a68c9"),
                                                   a.get("cwd", "~"), a.get("blurb", ""))
                            added.append(agent["id"])
                        except ValueError:
                            continue
                    for aid in added:
                        await broadcast({"type": "agent_added", "agent": {
                            "id": aid, "name": orch.agents[aid]["name"],
                            "color": orch.agents[aid]["color"],
                            "shape": orch.agents[aid]["shape"],
                            "order": orch.agents[aid].get("order", 99)}})
                    return {"ok": True, "imported": len(added), "kind": "agents",
                            "agents": added}
            return {"ok": False, "error": "no agents.yaml found in that repo"}
        return {"ok": False, "error": "kind must be skills or agents"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "clone timed out"}
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── Cloud account (metered plan + push) ─────────────────────────────────

import cloudlink


class CloudBody(BaseModel):
    url: str
    email: str
    password: str


@app.get("/api/cloud")
async def cloud_status():
    me = await cloudlink.me()
    return {"connected": cloudlink.connected(), "url": cloudlink.cloud_url(),
            "user": me}


@app.post("/api/cloud/connect")
async def cloud_connect(body: CloudBody):
    return await cloudlink.login(body.url.strip(), body.email.strip(), body.password)


@app.post("/api/cloud/disconnect")
async def cloud_disconnect():
    cloudlink.disconnect()
    return {"ok": True}


# ── Remote access (mobile app) ──────────────────────────────────────────

class RemoteBody(BaseModel):
    expose: bool | None = None
    regenerate: bool = False


@app.get("/api/remote")
async def get_remote():
    return {"expose": bool((cfg.load().get("server") or {}).get("expose")),
            "token_set": bool(_api_token()),
            "token": _api_token() or "",
            "port": cfg.port("app")}


@app.put("/api/remote")
async def set_remote(body: RemoteBody):
    import secrets as _secrets
    if body.regenerate or (body.expose and not _api_token()):
        cfg.env_set("MC_API_TOKEN", _secrets.token_urlsafe(32))
    if body.expose is not None:
        cfg.update("server", {"expose": bool(body.expose)})
    return {"ok": True, "expose": bool((cfg.load().get("server") or {}).get("expose")),
            "token": _api_token() or "",
            "note": "restart Mission Control to apply"}


# ── User-defined webhook routes ─────────────────────────────────────────

class HookBody(BaseModel):
    name: str
    agent: str = ""
    mode: str = "wake"   # wake = agent turn, notify = post only, zero model cost


@app.get("/api/hooks")
async def hooks_list():
    return {"hooks": _webhook_routes(), "public_url": public_url}


@app.post("/api/hooks")
async def hooks_add(body: HookBody):
    import re as _re
    import secrets as _secrets
    name = _re.sub(r"[^a-z0-9-]", "-", body.name.strip().lower())[:40].strip("-")
    if not name:
        return {"ok": False, "error": "name required"}
    routes = [r for r in _webhook_routes() if r.get("name") != name]
    route = {"name": name, "agent": body.agent, "mode": body.mode,
             "secret": _secrets.token_hex(16)}
    routes.append(route)
    s = cfg.load(); s["webhooks"] = routes; cfg.save(s)
    return {"ok": True, "hooks": routes}


@app.delete("/api/hooks/{name}")
async def hooks_delete(name: str):
    routes = [r for r in _webhook_routes() if r.get("name") != name]
    s = cfg.load(); s["webhooks"] = routes; cfg.save(s)
    return {"ok": True, "hooks": routes}


# ── Heartbeat + usage ───────────────────────────────────────────────────

class HeartbeatBody(BaseModel):
    enabled: bool | None = None
    agent: str | None = None
    every_minutes: int | None = None
    checklist: str | None = None


class BriefBody(BaseModel):
    enabled: bool | None = None
    agent: str | None = None
    at: str | None = None      # local HH:MM


@app.get("/api/settings/brief")
async def get_brief():
    return cron_mod.brief_settings()


@app.put("/api/settings/brief")
async def set_brief(body: BriefBody):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if "at" in values and not __import__("re").fullmatch(r"[0-2]?\d:[0-5]\d", values["at"]):
        return {"ok": False, "error": "time must be HH:MM"}
    if values:
        cfg.update("brief", values)
        if "enabled" in values:
            cfg.update("runtime_services", {"brief": values["enabled"]})
        await runtime.registry.reconcile()
    return {"ok": True, **cron_mod.brief_settings()}


@app.get("/api/heartbeat")
async def get_heartbeat():
    return {**cron_mod.heartbeat_settings(),
            "checklist": cron_mod.heartbeat_file().read_text()}


@app.put("/api/heartbeat")
async def set_heartbeat(body: HeartbeatBody):
    values = {k: v for k, v in body.model_dump().items()
              if v is not None and k != "checklist"}
    if values:
        cfg.update("heartbeat", values)
        if "enabled" in values:
            cfg.update("runtime_services", {"heartbeat": values["enabled"]})
        await runtime.registry.reconcile()
    if body.checklist is not None:
        cron_mod.heartbeat_file().write_text(body.checklist)
    return {"ok": True, **cron_mod.heartbeat_settings()}


@app.get("/api/usage")
async def usage(days: int = 7):
    return await asyncio.to_thread(db.usage_summary, min(days, 90))


# ── Phones: push tokens, no account in between ──────────────────────────

class DeviceBody(BaseModel):
    expo_push_token: str
    name: str = "phone"


@app.get("/api/devices")
async def devices_list():
    return {"devices": push_mod.devices()}


@app.post("/api/devices")
async def devices_add(body: DeviceBody):
    if not body.expo_push_token.startswith("ExponentPushToken["):
        return {"ok": False, "error": "that is not an Expo push token"}
    return {"ok": True, "devices": push_mod.register(body.expo_push_token, body.name)}


@app.delete("/api/devices/{token}")
async def devices_remove(token: str):
    return {"ok": True, "devices": push_mod.remove(token)}


@app.post("/api/devices/test")
async def devices_test():
    ok = await push_mod.notify("test", "Mission Control", "Push notifications are working.",
                               force=True)
    return {"ok": ok, "error": None if ok else "no device answered; check the phone is registered"}


# ── Budget ──────────────────────────────────────────────────────────────

class BudgetBody(BaseModel):
    daily_tokens: int


@app.get("/api/settings/budget")
async def get_budget():
    return budget_mod.status()


@app.put("/api/settings/budget")
async def set_budget(body: BudgetBody):
    if body.daily_tokens < 0:
        return {"ok": False, "error": "budget cannot be negative"}
    cfg.update("budget", {"daily_tokens": body.daily_tokens})
    budget_mod.reset_notes()
    return {"ok": True, **budget_mod.status()}


# ── Memory, skills, playbooks: what the team believes, editable ─────────

@app.get("/api/memory")
async def memory_list(q: str = "", limit: int = 200):
    if q.strip():
        rows = await asyncio.to_thread(db.search_memories, q, max(1, min(limit, 200)))
    else:
        rows = await asyncio.to_thread(db.list_memories, max(1, min(limit, 500)))
    return {"memories": rows}


class MemoryBody(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    kind: str | None = None


@app.put("/api/memory/{mid}")
async def memory_update(mid: int, body: MemoryBody):
    if not body.text.strip():
        return {"ok": False, "error": "empty"}
    ok = await asyncio.to_thread(db.update_memory, mid, body.text.strip(), body.kind)
    await broadcast({"type": "memory_updated"})
    return {"ok": ok, "error": None if ok else "no such memory"}


@app.delete("/api/memory/{mid}")
async def memory_delete(mid: int):
    ok = await asyncio.to_thread(db.delete_memory, mid)
    await broadcast({"type": "memory_updated"})
    return {"ok": ok, "error": None if ok else "no such memory"}


class AutolearnBody(BaseModel):
    autolearn: bool


@app.get("/api/skills")
async def skills_list():
    return {"skills": skills_mod.list_skills(),
            "autolearn": bool((cfg.load().get("skills") or {}).get("autolearn", True))}


@app.put("/api/skills-autolearn")
async def skills_autolearn(body: AutolearnBody):
    """Whether agents reflect after a long turn and write down what they learned."""
    cfg.update("skills", {"autolearn": bool(body.autolearn)})
    return {"ok": True, "autolearn": bool(body.autolearn)}


@app.get("/api/skills/{slug}")
async def skills_read(slug: str):
    content = skills_mod.read_skill(slug)
    if content is None:
        return {"ok": False, "error": "no such skill"}
    return {"ok": True, "slug": slug, "content": content}


class SkillBody(BaseModel):
    content: str


@app.put("/api/skills/{slug}")
async def skills_write(slug: str, body: SkillBody):
    try:
        ok = await asyncio.to_thread(skills_mod.write_skill, slug, body.content)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    await broadcast({"type": "skills_updated"})
    return {"ok": ok, "error": None if ok else "no such skill"}


@app.delete("/api/skills/{slug}")
async def skills_delete(slug: str):
    ok = skills_mod.delete_skill(slug)
    return {"ok": ok, "error": None if ok else "no such skill"}


@app.get("/api/playbooks")
async def playbooks_list():
    return {"playbooks": await asyncio.to_thread(db.list_playbooks)}


class PlaybookRef(BaseModel):
    site: str
    title: str


@app.post("/api/playbooks/delete")
async def playbooks_delete(body: PlaybookRef):
    ok = await asyncio.to_thread(db.delete_playbook, body.site, body.title)
    return {"ok": ok, "error": None if ok else "no such playbook"}


# ── Voice: one configured voice everywhere ──────────────────────────────

class VoiceBody(BaseModel):
    provider: str | None = None
    voice: str | None = None
    speed: float | None = None
    speak_replies: bool | None = None


@app.get("/api/settings/voice")
async def get_voice():
    return {**voice_mod.settings(), "providers": voice_mod.providers()}


@app.put("/api/settings/voice")
async def set_voice(body: VoiceBody):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if "provider" in values and values["provider"] not in ("auto", "minimax", "openai", "local"):
        return {"ok": False, "error": "unknown provider"}
    if "speed" in values and not (0.5 <= values["speed"] <= 2.0):
        return {"ok": False, "error": "speed must be between 0.5 and 2"}
    if values:
        cfg.update("voice", values)
    return {"ok": True, **voice_mod.settings(), "providers": voice_mod.providers()}


@app.get("/api/settings/voice/voices")
async def voice_list():
    return {"voices": await voice_mod.list_voices()}


class TTSBody(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/tts")
async def tts(body: TTSBody):
    """Speak text; returns a cached audio url under /uploads/tts."""
    if not body.text.strip():
        return {"ok": False, "error": "empty"}
    out = await voice_mod.tts_file(body.text, body.voice)
    if not out:
        return {"ok": False, "error": "no voice available"}
    return {"ok": True, **out}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile):
    data = await file.read()
    text = await voice_mod.transcribe(data, file.filename or "voice.m4a")
    if text is None:
        return {"ok": False, "error": "no transcriber: install faster-whisper on the "
                                      "server or add an OPENAI_API_KEY"}
    return {"ok": True, "text": text}


# ── Apple apps: one switch per app, all off until the owner says ────────

class AppleBody(BaseModel):
    app: str
    enabled: bool


@app.get("/api/apple")
async def apple_status():
    """Reachable and connected are different questions: macOS answers the first,
    the owner the second."""
    import apple as apple_mod
    return {"apps": list(apple_mod.APPS),
            "connected": {a: apple_mod.enabled(a) for a in apple_mod.APPS},
            "reachable": await apple_mod.status(),
            "remindctl": bool(apple_mod.remindctl())}


@app.put("/api/apple")
async def apple_connect(body: AppleBody):
    import apple as apple_mod
    if body.app not in apple_mod.APPS:
        return {"ok": False, "error": f"unknown app '{body.app}'"}
    cfg.update("apple", {body.app: bool(body.enabled)})
    return {"ok": True, "app": body.app, "enabled": bool(body.enabled)}


# ── MCP servers: tools from other apps, through one gate ────────────────
# Adding a stdio server runs a command on this Mac, so these are for the local
# owner only, even when remote access to the chat is on.

class MCPServerBody(BaseModel):
    transport: str = "http"
    url: str = ""
    command: str = ""
    args: list[str] = []
    cwd: str = ""
    enabled: bool = True
    agents: list[str] | None = None
    approval: str = "destructive"
    token: str | None = None
    env: dict[str, str] | None = None


class MCPImportBody(BaseModel):
    config: str


def _local_owner(request: Request) -> bool:
    host = request.client.host if request.client else None
    origin = request.headers.get("origin")
    allowed = {f"http://{name}:{request.url.port or 80}" for name in ("127.0.0.1", "localhost", "[::1]")}
    return host in LOCAL_HOSTS and (not origin or origin in allowed)


@app.get("/api/mcp")
async def mcp_status():
    import mcp_servers
    return {"servers": await asyncio.to_thread(mcp_servers.status),
            "service": runtime.registry.enabled("mcp") if runtime.registry else False}


@app.put("/api/mcp/{name}")
async def mcp_save(name: str, body: MCPServerBody, request: Request):
    import mcp_servers
    if not _local_owner(request):
        return JSONResponse({"ok": False, "error": "MCP servers are managed from this Mac."}, status_code=403)
    conf = body.model_dump(exclude={"token", "env"})
    try:
        await asyncio.to_thread(mcp_servers.save_server, name, conf, body.token, body.env)
    except mcp_servers.GateError as exc:
        return {"ok": False, "error": str(exc)}
    await runtime.registry.reconcile()
    if not conf.get("enabled"):
        await mcp_servers.reconcile()
        return {"ok": True, "error": None, "tools": 0}
    state = await mcp_servers.refresh(name, starting=True)
    return {"ok": True, "error": state.get("error"), "tools": len(state.get("tools") or [])}


@app.post("/api/mcp/import")
async def mcp_import(body: MCPImportBody, request: Request):
    import mcp_servers
    if not _local_owner(request):
        return JSONResponse({"ok": False, "error": "MCP servers are managed from this Mac."}, status_code=403)
    try:
        added, problems = await asyncio.to_thread(mcp_servers.import_config, body.config)
    except mcp_servers.GateError as exc:
        return {"ok": False, "error": str(exc)}
    await runtime.registry.reconcile()
    for name in added:
        await mcp_servers.refresh(name, starting=True)
    return {"ok": bool(added), "added": added, "problems": problems,
            "error": "; ".join(problems) if not added else None}


@app.post("/api/mcp/{name}/refresh")
async def mcp_refresh(name: str):
    import mcp_servers
    state = await mcp_servers.refresh(name, starting=True)
    return {"ok": not state.get("error"), "error": state.get("error"), "tools": len(state.get("tools") or [])}


@app.delete("/api/mcp/{name}")
async def mcp_remove(name: str, request: Request):
    import mcp_servers
    if not _local_owner(request):
        return JSONResponse({"ok": False, "error": "MCP servers are managed from this Mac."}, status_code=403)
    removed = await asyncio.to_thread(mcp_servers.remove_server, name)
    await mcp_servers.reconcile()
    return {"ok": removed}


# ── Laya and the mail cleanup it runs ───────────────────────────────────

@app.get("/api/laya")
async def laya_status():
    return {"enabled": laya.enabled(), "url": laya.url(), "health": await laya.health(),
            "problem": laya.problem()}


@app.get("/api/mail/sweep")
async def mail_sweep_status():
    return mailsweep.status()


class MailSweepBody(BaseModel):
    enabled: bool | None = None
    dry_run: bool | None = None
    every_minutes: int | None = Field(default=None, ge=15, le=1440)
    bulk_at: float | None = Field(default=None, ge=0.5, le=1.0)
    keep_at: float | None = Field(default=None, ge=0.0, le=1.0)


@app.put("/api/mail/sweep")
async def mail_sweep_settings(body: MailSweepBody):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if values:
        cfg.update("mail_sweep", values)
        if "enabled" in values:
            cfg.update("runtime_services", {"mail_sweep": values["enabled"]})
            await runtime.registry.reconcile()
    return {"ok": True, **mailsweep.status()}


class MailSweepRunBody(BaseModel):
    dry_run: bool | None = None


@app.post("/api/mail/sweep/run")
async def mail_sweep_run(body: MailSweepRunBody):
    return mailsweep.start(body.dry_run)


class MailSenderBody(BaseModel):
    sender: str
    action: Literal["keep", "always"]
    restore: bool = False


@app.post("/api/mail/sweep/sender")
async def mail_sweep_sender(body: MailSenderBody):
    if body.action == "always":
        return await mailsweep.always_archive(body.sender)
    return await mailsweep.keep_sender(body.sender, body.restore)


# ── WhatsApp: the owner's own account, read-only ───────────────────────

@app.get("/api/whatsapp")
async def whatsapp_status():
    return await whatsapp_link.status()


class WhatsAppBody(BaseModel):
    enabled: bool | None = None
    groups: bool | None = None
    include: list[str] | None = None
    ignore: list[str] | None = None
    agents: list[str] | None = None          # an empty list opens it to every agent
    keep_days: int | None = Field(default=None, ge=7, le=3650)


@app.put("/api/whatsapp")
async def whatsapp_settings(body: WhatsAppBody):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    for key in ("include", "ignore", "agents"):
        if key in values:
            values[key] = [x.strip() for x in values[key] if x.strip()]
    if "agents" in values and not values["agents"]:
        values["agents"] = None
    if values:
        cfg.update("whatsapp_link", values)
        if "enabled" in values:
            cfg.update("runtime_services", {"whatsapp": values["enabled"]})
        await runtime.registry.reconcile()
    return {"ok": True, **await whatsapp_link.status()}


class WhatsAppLinkBody(BaseModel):
    phone: str = Field(default="", max_length=32)


@app.post("/api/whatsapp/link")
async def whatsapp_link_start(body: WhatsAppLinkBody):
    return await whatsapp_link.link(body.phone)


@app.post("/api/whatsapp/unlink")
async def whatsapp_unlink():
    return await whatsapp_link.unlink()


# ── Self-improvement: agents propose, the owner decides ─────────────────

class ImproveBody(BaseModel):
    enabled: bool | None = None
    autofix: bool | None = None
    auto_apply: bool | None = None
    push: bool | None = None


@app.get("/api/improvements")
async def improvements_list():
    import selfimprove as si
    s = si.settings()
    issues = []
    if s["enabled"]:
        try:
            issues = await asyncio.to_thread(si.issues, orch.agent_llm)
        except Exception:
            logger.exception("could not read self-repair issues")
    return {"improvements": await asyncio.to_thread(db.list_improvements, None, 50),
            "enabled": s["enabled"], "settings": s, "issues": issues,
            "last_check": si.last_tick, "busy": si._busy(orch),
            "blocked": si.readiness() if s["enabled"] else None,
            "protected": list(si.PROTECTED)}


@app.put("/api/improvements-enabled")
async def improvements_enabled(body: ImproveBody):
    import selfimprove as si
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if changes:
        cfg.update("selfimprove", changes)
    return {"ok": True, **si.settings()}


@app.post("/api/improvements/check")
async def improvements_check():
    """Look for problems now instead of at the next tick, and start on one."""
    import selfimprove as si
    runtime.spawn(si.tick(orch, broadcast), "selfrepair", "Self-repair check")
    return {"ok": True}


@app.get("/api/improvements/{iid}/diff")
async def improvement_diff(iid: int):
    """The whole patch, so approving is a decision about code you have read."""
    import selfimprove as si
    row = await asyncio.to_thread(db.get_improvement, iid)
    if not row:
        return {"ok": False, "error": "no such proposal"}
    code, out = await asyncio.to_thread(si._git, "diff", f"master...{row['branch']}")
    return {"ok": code == 0, "diff": out[:200000], "title": row["title"]}


@app.post("/api/improvements/{iid}/approve")
async def improvement_approve(iid: int):
    import selfimprove as si
    return await si.land(iid, broadcast, orch)


@app.post("/api/improvements/{iid}/reject")
async def improvement_reject(iid: int):
    import selfimprove as si
    row = await asyncio.to_thread(db.get_improvement, iid)
    if not row:
        return {"ok": False, "error": "no such proposal"}
    await asyncio.to_thread(si.drop_worktree, row["branch"], row["worktree"])
    await asyncio.to_thread(db.set_improvement, iid, "rejected", "")
    await broadcast({"type": "improvements_updated"})
    return {"ok": True}


# ── Scheduled jobs (owner approval) ─────────────────────────────────────

@app.get("/api/cron")
async def cron_list():
    return {"jobs": await asyncio.to_thread(db.list_cron),
            "agents": {aid: a["name"] for aid, a in orch.agents.items()}}


def _approval_context(pending: dict, history: list[dict]) -> dict | None:
    """The nearest command this owner already decided on, and how it differs.

    An agent iterating on a script produces a run of nearly identical requests,
    and three truncated commands that look the same are impossible to tell
    apart. This does not decide anything — a prior yes is never carried over to
    different bytes, or approving once would license every later edit — it only
    says what changed since the one already answered.
    """
    import difflib
    best, best_ratio = None, 0.0
    for row in history:
        if row["id"] == pending["id"] or row["agent"] != pending["agent"]:
            continue
        if row["status"] not in ("approved", "denied", "ran"):
            continue
        ratio = difflib.SequenceMatcher(None, row["command"], pending["command"]).ratio()
        if ratio > best_ratio:
            best, best_ratio = row, ratio
    if not best or best_ratio < 0.6:
        return None
    identical = best["command"] == pending["command"] and best["cwd"] == pending["cwd"]
    diff = [] if identical else [
        line for line in difflib.unified_diff(
            best["command"].splitlines(), pending["command"].splitlines(),
            lineterm="", n=1) if line[:1] in "+-" and line[:3] not in ("---", "+++")][:40]
    return {"id": best["id"], "ts": best["ts"], "status": best["status"],
            "identical": identical, "diff": diff}


@app.get("/api/approvals")
async def approvals_list():
    rows = await asyncio.to_thread(runtime_store.approvals)
    for row in rows:
        if row["status"] == "pending":
            row["prior"] = _approval_context(row, rows)
    return {"approvals": rows,
            "agents": {aid: a["name"] for aid, a in orch.agents.items()}}


@app.post("/api/approvals/{aid}/{action}")
async def approval_action(aid: str, action: str):
    """Approve or decline one held command. Approving runs it as written."""
    if action not in ("approve", "deny"):
        return {"ok": False, "error": "unknown action"}
    request = await asyncio.to_thread(
        runtime_store.approval_claim, aid, "approved" if action == "approve" else "denied")
    if not request:
        return {"ok": False, "error": "already decided"}
    if action == "deny":
        what = "MCP CALL" if request.get("kind") == "mcp" else "COMMAND"
        await orch.external_event(
            request["agent"], "approval",
            f"the owner declined THIS ONE {what}: `{request['command']}`. Do not run it, "
            "and do not reach for a variant of it that does the same thing. The rest of "
            "the task still stands — if another step was already agreed, carry on with "
            "it. Say what you will do instead, or reply exactly NO_REPLY.",
            session=request.get("session"), thread=f"approval:{aid}")
    elif request.get("kind") == "mcp":
        import mcp_servers
        out = await mcp_servers.run_approved(request)
        await asyncio.to_thread(runtime_store.approval_finish, aid, out)
        await orch.external_event(
            request["agent"], "approval",
            f"the owner approved the MCP call `{request['command']}` and it has been run.\n\n"
            f"Result:\n{out[:3000]}\n\nCarry on from here, or reply exactly NO_REPLY.",
            session=request.get("session"), thread=f"approval:{aid}", owner_decision=True)
    else:
        out = await tools.run_approved(request)
        await asyncio.to_thread(runtime_store.approval_finish, aid, out)
        await orch.external_event(
            request["agent"], "approval",
            f"the owner approved `{request['command']}` and it has been run.\n\n"
            f"Output:\n{out[:3000]}\n\nCarry on from here, or reply exactly NO_REPLY.",
            session=request.get("session"), thread=f"approval:{aid}", owner_decision=True)
    await broadcast({"type": "approvals_updated"})
    return {"ok": True, "approvals": await asyncio.to_thread(runtime_store.approvals)}


@app.post("/api/cron/{jid}/{action}")
async def cron_action(jid: int, action: str):
    job = await asyncio.to_thread(db.get_cron, jid)
    if not job:
        return {"ok": False, "error": "no such job"}
    if action == "approve" or action == "resume":
        nxt = cron_mod.next_run(job["schedule"])
        if nxt is None:
            return {"ok": False, "error": "schedule has no future runs"}
        await asyncio.to_thread(db.update_cron, jid, status="active", next_run=nxt)
        if action == "approve":
            await orch.external_event(job["agent"], "cron",
                f"the owner approved your scheduled job '{job['name']}' ({job['schedule']}). "
                "It is now active. Reply exactly NO_REPLY unless something needs saying.")
    elif action == "pause":
        await asyncio.to_thread(db.update_cron, jid, status="paused")
    elif action == "delete":
        await asyncio.to_thread(db.delete_cron, jid)
    else:
        return {"ok": False, "error": "unknown action"}
    await broadcast({"type": "cron_updated"})
    return {"ok": True, "jobs": await asyncio.to_thread(db.list_cron)}


@app.get("/api/watches")
async def watches_list(history: bool = False):
    return {"watches": (await asyncio.to_thread(db.list_watches, None if history else "active"))[:200],
            "agents": {aid: a["name"] for aid, a in orch.agents.items()}}


@app.post("/api/watches/{wid}/cancel")
async def watch_cancel(wid: int):
    ok = await asyncio.to_thread(db.finish_watch_poll, wid, "cancelled", "Stopped by owner.")
    if ok:
        await broadcast({"type": "watch_updated"})
    return {"ok": ok, "watches": await asyncio.to_thread(db.list_watches, "active")}


# ── Settings API ────────────────────────────────────────────────────────

@app.get("/api/settings/full")
async def settings_full():
    llm_conf = llm.config()
    resolved = llm._resolved(llm_conf)
    presets = {k: {"label": v["label"], "wire": v["wire"], "base_url": v["base_url"],
                   "key_env": v["key_env"], "models": v["models"]}
               for k, v in llm.PRESETS.items()
               if k != "missioncontrol-cloud" or cloudlink.connected()}
    return {
        "llm": {**llm_conf, "key_set": bool(resolved["key"]),
                "key_env": resolved["key_env"], "presets": presets,
                "ready": llm.ready(llm_conf)},
        "channels": {"settings": channels.channel_settings(),
                     "status": channels.manager.statuses(),
                     "secret_env": channels.SECRET_ENV},
        "github": github.settings(),
        "fleet": sync_mod.status(),
        "ports": {n: cfg.port(n) for n in ("app", "public", "fleet")},
        "public_url": public_url,
        "agents": list(orch.agents.keys()),
        "budget": budget_mod.status(),
        "devices": [{"name": d.get("name"), "added": d.get("added")} for d in push_mod.devices()],
    }


class LLMBody(BaseModel):
    provider: str
    model: str
    base_url: str = ""
    max_tokens: int = 3000
    api_key: str | None = None
    fallbacks: list[dict] | None = None
    effort: str = llm.DEFAULT_EFFORT


SETUP_GREETING = (
    "The owner just plugged a model into Mission Control and is looking at the app "
    "for the first time. Say hello in two or three short, warm sentences: who you are, "
    "what you can take on for them and the roles your tasks can run as (in a sentence), and "
    "ask what they would like help with first. No lists, no markdown. Say it with the reply tool."
)


@app.put("/api/settings/llm")
async def set_llm(body: LLMBody):
    if body.provider not in llm.PRESETS:
        return {"ok": False, "error": "unknown provider"}
    was_ready = llm.ready()
    if body.api_key:
        key_env = llm.PRESETS[body.provider]["key_env"]
        if key_env:
            cfg.env_set(key_env, body.api_key.strip())
    values = {"provider": body.provider, "model": body.model,
              "base_url": body.base_url, "max_tokens": body.max_tokens,
              "effort": body.effort if body.effort in llm.EFFORTS else llm.DEFAULT_EFFORT}
    if body.fallbacks is not None:
        values["fallbacks"] = [
            {"provider": f.get("provider"), "model": f.get("model"),
             "base_url": f.get("base_url", "")}
            for f in body.fallbacks
            if f.get("provider") in llm.PRESETS and f.get("model")][:4]
    cfg.update("llm", values)
    # the first time the team can think, the lead introduces everyone
    if not was_ready and llm.ready() and orch.agents:
        runtime.spawn(orch.router.system_note(SETUP_GREETING), "agents", "First-run greeting")
    return {"ok": True, "ready": llm.ready()}


@app.post("/api/settings/llm/test")
async def test_llm(body: LLMBody):
    if body.api_key:
        key_env = llm.PRESETS.get(body.provider, {}).get("key_env")
        if key_env:
            cfg.env_set(key_env, body.api_key.strip())
    return await llm.test_connection(body.model_dump(exclude={"api_key"}))


@app.get("/api/settings/llm/models")
async def llm_models(provider: str, base_url: str = ""):
    return {"models": await llm.list_models(provider, base_url)}


class ChannelBody(BaseModel):
    enabled: bool | None = None
    default_agent: str | None = None
    allowed_senders: list[str] | None = None
    server_url: str | None = None
    phone_number_id: str | None = None
    verify_token: str | None = None
    imap_host: str | None = None
    smtp_host: str | None = None
    address: str | None = None
    secret: str | None = None


@app.put("/api/settings/channels/{name}")
async def set_channel(name: str, body: ChannelBody):
    if name not in channels.CHANNEL_TYPES:
        return {"ok": False, "error": "unknown channel"}
    values = {k: v for k, v in body.model_dump().items()
              if v is not None and k != "secret"}
    if body.secret:
        cfg.env_set(channels.SECRET_ENV[name], body.secret.strip())
    if values:
        channels.save_channel(name, values)
    if f"channel_{name}" in runtime.registry.services:
        cfg.update("runtime_services", {f"channel_{name}": channels.channel_settings()[name]["enabled"]})
        await runtime.registry.action(f"channel_{name}", "restart" if channels.channel_settings()[name]["enabled"] else "stop")
    else:
        await channels.manager.restart()
    return {"ok": True, "status": channels.manager.statuses()}


class GithubBody(BaseModel):
    enabled: bool | None = None
    repos: list[str] | None = None
    route_ci: str | None = None
    route_code: str | None = None
    review_agent: str | None = None


@app.put("/api/settings/github")
async def set_github(body: GithubBody):
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if values:
        cfg.update("github", values)
        if "enabled" in values:
            cfg.update("runtime_services", {"github_poll": values["enabled"],
                                            "public_listener": _public_needed(), "tunnel": _public_needed()})
        await runtime.registry.reconcile()
    return {"ok": True, "github": github.settings(),
            "note": "services updated; manage their status in Runtime Manager"}


class PortsBody(BaseModel):
    app: int | None = None
    public: int | None = None
    fleet: int | None = None


@app.put("/api/settings/ports")
async def set_ports(body: PortsBody):
    import os
    if os.environ.get("MISSIONCONTROL_OWNER_PID") and body.app is not None and body.app != cfg.port("app"):
        return {"ok": False, "error": "Change the app port in Runtime Manager while the runtime is stopped."}
    values = {}
    for name in ("app", "public", "fleet"):
        v = getattr(body, name)
        if v is not None:
            if not (1024 <= v <= 65535):
                return {"ok": False, "error": f"{name} port must be 1024-65535"}
            values[name] = v
    if len(set(values.values())) != len(values):
        return {"ok": False, "error": "ports must be distinct"}
    if values:
        cfg.update("ports", values)
    return {"ok": True, "ports": {n: cfg.port(n) for n in ("app", "public", "fleet")},
            "note": "restart Mission Control to apply new ports"}


async def _best_url() -> tuple[str, dict]:
    """The address other machines should reach this one at: tailscale if it is
    up (works across networks), otherwise the LAN hostname."""
    import socket as _socket
    import tailnet
    port = cfg.port("fleet")
    lan = f"http://{_socket.gethostname().split('.')[0].lower()}.local:{port}"
    ts = await tailnet.http_url(port)
    st = await tailnet.status()
    return (ts or lan), {"lan": lan, "tailscale": ts,
                         "tailscale_status": st}


@app.get("/api/fleet/addresses")
async def fleet_addresses():
    """This machine's fleet URLs and the tailnet state."""
    _, info = await _best_url()
    return info


@app.get("/api/fleet/joincode")
async def fleet_joincode():
    """A code to paste on another machine so it joins this fleet."""
    url, info = await _best_url()
    code = sync_mod.make_join_code(url)
    return {"code": code, "url": url, "node": sync_mod.node_name(),
            "over_tailscale": bool(info["tailscale"]), "addresses": info}


class JoinBody(BaseModel):
    code: str


@app.post("/api/fleet/join")
async def fleet_join(body: JoinBody):
    """Paste a code from another machine: adopt its key, add it as a peer, and
    register back so both sides collaborate immediately, no restart."""
    d = sync_mod.read_join_code(body.code)
    if not d:
        return {"ok": False, "error": "that is not a valid join code"}
    if d["node"] == sync_mod.node_name():
        return {"ok": False, "error": "that code is from this same machine"}
    cfg.env_set("SYNC_KEY", d["key"])
    sync_mod.add_peer(d["node"], d["url"])
    my_url, _ = await _best_url()
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(d["url"].rstrip("/") + "/sync/join",
                                  headers={"x-sync-key": d["key"]},
                                  json={"node": sync_mod.node_name(), "url": my_url})
        if r.status_code != 200:
            return {"ok": False, "error": f"the other machine refused (HTTP {r.status_code}); "
                    "is it reachable at " + d["url"] + "?"}
        peer = r.json()
    except Exception as exc:
        return {"ok": False, "error": f"could not reach {d['url']}: {exc.__class__.__name__}. "
                "On different networks, both machines need Tailscale."}
    return {"ok": True, "joined": d["node"], "peer_agents": peer.get("agents"),
            "fleet": sync_mod.status()}


class TailscaleBody(BaseModel):
    action: str = "status"      # status | install | up
    authkey: str | None = None


@app.post("/api/fleet/tailscale")
async def fleet_tailscale(body: TailscaleBody):
    import tailnet
    if body.action == "install":
        return await tailnet.install()
    if body.action == "up":
        return await tailnet.up(body.authkey)
    return await tailnet.status()


class FleetBody(BaseModel):
    node_name: str | None = None
    peers: list[dict] | None = None
    sync_key: str | None = None


@app.put("/api/settings/fleet")
async def set_fleet(body: FleetBody):
    if body.sync_key is not None:
        cfg.env_set("SYNC_KEY", body.sync_key.strip())
    values = {}
    if body.node_name is not None:
        values["node_name"] = body.node_name.strip().lower() or None
    if body.peers is not None:
        peers = []
        for p in body.peers:
            name = (p.get("name") or "").strip().lower()
            url = (p.get("url") or "").strip().rstrip("/")
            if name and url:
                peers.append({"name": name, "url": url})
        values["peers"] = peers
    if values:
        cfg.update("fleet", values)
    await runtime.registry.reconcile()
    return {"ok": True, "fleet": sync_mod.status(),
            "note": "services updated; manage their status in Runtime Manager"}


# Back-compat for the existing agent sheet's iMessage radio
class SettingsBody(BaseModel):
    imessage_default_agent: str


@app.get("/api/settings")
async def get_settings():
    s = channels.channel_settings()["imessage"]
    return {"imessage_default_agent": s.get("default_agent", ""),
            "imessage_enabled": s.get("enabled", False)}


@app.post("/api/settings")
async def set_settings(body: SettingsBody):
    if body.imessage_default_agent not in orch.agents:
        return {"ok": False, "error": "unknown agent"}
    channels.save_channel("imessage", {"default_agent": body.imessage_default_agent})
    await broadcast({"type": "settings", "imessage_default_agent": body.imessage_default_agent})
    return {"ok": True}


@app.post("/imessage-webhook")
async def imessage_webhook(request: Request):
    ch = channels.manager.get("imessage")
    if not ch:
        return {"status": "ok"}
    pw = ch.password()
    if not pw or request.query_params.get("password") != pw:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    inbound = ch.parse_inbound(payload)
    if not inbound or not ch.conf.get("enabled"):
        return {"status": "ok"}
    if not ch.sender_allowed(inbound["sender"]):
        logger.info("iMessage from unlisted sender ignored: %s", inbound["sender"])
        return {"status": "ok"}
    await _channel_message("imessage", inbound["chat_id"], inbound["sender"],
                           inbound["text"], inbound.get("media"))
    return {"status": "ok"}


@app.post("/api/upload")
async def upload(file: UploadFile):
    data = await file.read()
    if len(data) > uploads_mod.MAX_SIZE:
        return JSONResponse({"error": "file too large (50MB max)"}, status_code=413)
    path = await asyncio.to_thread(uploads_mod.save, data, file.filename or "file")
    kind = uploads_mod.kind_of(path)
    desc = ""
    if kind == "image":
        import base64
        mime = file.content_type or "image/png"
        desc = await llm.describe_image(base64.b64encode(data).decode(), mime)
    else:
        desc = await asyncio.to_thread(uploads_mod.preview, path)
    return {"ok": True, "attachment": {
        "name": path.name, "path": str(path), "url": f"/uploads/{path.name}",
        "mime": file.content_type or "", "kind": kind,
        "size": len(data), "desc": desc[:1500]}}


class SendBody(BaseModel):
    thread: str
    text: str = Field(max_length=32000)
    attachments: list[dict] | None = None
    voice: bool = False


@app.post("/api/send")
async def send(body: SendBody):
    text = body.text.strip()
    if not text and not body.attachments:
        return {"ok": False}
    try:
        return await orch.user_message(body.thread, text or "(sent a file)",
                                       attachments=body.attachments, voice=body.voice)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    host = ws.client.host if ws.client else None
    if not _request_authorized(host, ws.headers.get("authorization", ""),
                               ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(ws)


if __name__ == "__main__":
    # use_colors=False so uvicorn never probes isatty (the Windows build has no
    # console); the host comes from the Settings toggle, not the command line.
    uvicorn.run(app, host=bind_host(), port=cfg.port("app"),
                log_level="info", use_colors=False)
