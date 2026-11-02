"""One command grammar for the command deck, chat and connected channels."""
import asyncio
import json
import re

import db
import kernel_store as kernel
import runtime_store as store
import skills
import triggers as triggers_mod

CATALOG = [
    {"name": "mission", "usage": "/mission [@agent] objective", "description": "Launch a tracked task; name someone, or the Router picks who."},
    {"name": "tasks", "usage": "/tasks", "description": "List open tasks and who is on them. No model call."},
    {"name": "events", "usage": "/events", "description": "Recent events and what became of each. No model call."},
    {"name": "policy", "usage": "/policy [add text|off id|on id|source name explicit|candidate|ignore]",
     "description": "What the agent acts on unprompted. No model call."},
    {"name": "watchdog", "usage": "/watchdog question", "description": "Ask the watchdog why something happened, or to correct something."},
    {"name": "skill", "usage": '/skill slug {"input": "value"}', "description": "Run a skill with validated inputs and a bounded tool budget."},
    {"name": "status", "usage": "/status", "description": "See agents, active missions and watches. No model call."},
    {"name": "agents", "usage": "/agents", "description": "Show each profile and what it is doing."},
    {"name": "stop", "usage": "/stop [@agent|all]", "description": "Cancel current and queued work for a profile, or all of it."},
    {"name": "pause", "usage": "/pause", "description": "Stop picking up work, without losing any. Events are still recorded. No model call."},
    {"name": "unpause", "usage": "/unpause", "description": "Carry on with everything that waited. No model call."},
    {"name": "resume", "usage": "/resume mission-id", "description": "Resume an interrupted mission from its checkpoint."},
    {"name": "remember", "usage": "/remember fact", "description": "Save a fact to persistent shared memory."},
    {"name": "recall", "usage": "/recall search terms", "description": "Search shared memory, including older entries."},
    {"name": "watches", "usage": "/watches", "description": "Inspect background watches and their latest results."},
    {"name": "watch", "usage": "/watch cancel id", "description": "Stop a background watch."},
    {"name": "model", "usage": "/model [provider] model | /model reset",
     "description": "Pick this agent's model (or show it). reset returns it to the default. No model call."},
    {"name": "effort", "usage": "/effort none|low|medium|high|max | reset",
     "description": "How hard this agent's model thinks; lower answers faster. No model call."},
    {"name": "help", "usage": "/help", "description": "Show available commands."},
]


def _model_line(orch, aid: str) -> str:
    import llm
    r = llm._resolved(orch.agent_llm(aid))
    own = orch.agents[aid].get("llm") or {}
    source = "own setting" if own.get("model") or own.get("provider") else "default"
    effort_src = "own setting" if own.get("effort") else "default"
    return (f"@{aid} runs {r['provider']}/{r['model']} ({source}) at {r['effort']} effort ({effort_src}). "
            f"Change with /model [provider] model and /effort level.")


def _set_model(orch, aid: str, rest: str) -> str:
    import llm
    if not rest:
        return _model_line(orch, aid)
    current = dict(orch.agents[aid].get("llm") or {})
    if rest == "reset":
        current.pop("provider", None); current.pop("model", None); current.pop("base_url", None)
        orch.update_agent(aid, {"llm": current})
        return _model_line(orch, aid)
    words = rest.split()
    if len(words) == 2 and words[0] in llm.PRESETS:
        provider, model = words
    elif len(words) == 1:
        model = words[0]
        owners = [p for p, preset in llm.PRESETS.items() if model in preset["models"]]
        if not owners:
            known = ", ".join(f"{p} {m}" for p, preset in llm.PRESETS.items() for m in preset["models"][:3])
            raise ValueError(f"which provider serves '{model}'? Use /model provider model. Known: {known}")
        provider = owners[0]
    else:
        raise ValueError("use /model model, /model provider model, or /model reset")
    team = llm.config()
    if provider == team.get("provider") and model == team.get("model"):
        current.pop("provider", None); current.pop("model", None)
    else:
        current.update(provider=provider, model=model)
    current.pop("base_url", None)
    orch.update_agent(aid, {"llm": current})
    r = llm._resolved(orch.agent_llm(aid))
    if r["key_env"] and not r["key"]:
        return _model_line(orch, aid) + f" Warning: no {r['key_env']} in .env yet, so calls will fail until it is added."
    return _model_line(orch, aid)


def _set_effort(orch, aid: str, rest: str) -> str:
    import llm
    if not rest:
        return _model_line(orch, aid)
    current = dict(orch.agents[aid].get("llm") or {})
    if rest == "reset":
        current.pop("effort", None)
    elif rest in llm.EFFORTS:
        current["effort"] = rest
    else:
        raise ValueError("use /effort none, low, medium, high, max, or reset")
    orch.update_agent(aid, {"llm": current})
    return _model_line(orch, aid)


def target(orch, thread: str, text: str):
    aid = thread[3:] if thread.startswith("dm:") else next(iter(orch.agents))
    match = re.match(r"^@([\w-]+)(?:\s+|$)", text)
    if match:
        aid, text = match[1], text[match.end():].strip()
    if aid not in orch.agents:
        raise ValueError(f"unknown agent '{aid}'; use /agents to see the roster")
    return aid, text


def _policy_args(rest: str) -> dict:
    words = rest.split()
    if not words:
        return {"action": "list"}
    head, tail = words[0].lower(), " ".join(words[1:])
    if head == "add":
        return {"action": "add", "text": tail}
    if head in ("on", "off"):
        return {"action": "enable" if head == "on" else "disable", "id": tail}
    if head == "remove":
        return {"action": "remove", "id": tail}
    if head == "source" and len(words) == 3:
        return {"action": "source", "source": words[1], "admission": words[2]}
    return {"action": "?"}


async def execute(orch, thread: str, text: str, source="gui", chat_guid=None):
    """None means ordinary conversation. Slash commands never fall into an LLM."""
    if not text.startswith("/"):
        return None
    parts = re.split(r"\s+", text, maxsplit=1)
    command, rest = parts[0][1:].lower(), parts[1].strip() if len(parts) > 1 else ""
    command = {"run": "mission", "memory": "recall"}.get(command, command)
    if command not in {c["name"] for c in CATALOG}:
        raise ValueError(f"unknown command /{command}; use /help")
    aid, _ = target(orch, thread, "")
    if command in ("mission", "skill"):
        named = rest.startswith("@") or (thread.startswith("dm:") and thread[3:] != orch.lead)
        aid, body = target(orch, thread, rest)
        if not body:
            raise ValueError(next(c["usage"] for c in CATALOG if c["name"] == command))
        invocation = None
        if command == "skill":
            name, _, raw_inputs = body.partition(" ")
            try:
                inputs = json.loads(raw_inputs) if raw_inputs.strip() else {}
            except ValueError as exc:
                raise ValueError("skill inputs must be a JSON object") from exc
            meta = await asyncio.to_thread(skills.manifest, name, aid, inputs)
            permitted = orch.agents[aid].get("skills")
            if permitted is not None and meta["slug"] not in permitted:
                raise ValueError("this skill is not enabled for the selected agent")
            invocation = {"name": meta["slug"], "inputs": inputs}
            body = f"Run skill {meta['slug']} with inputs {json.dumps(inputs, ensure_ascii=False)}"
        mission = await orch.submit_mission(thread, aid, body, invocation, named, source, chat_guid)
        return {"ok": True, "command": command, "mission": mission}
    if command == "resume":
        mission = await asyncio.to_thread(store.mission_get, rest)
        if not mission:
            raise ValueError("unknown mission id")
        if mission["status"] not in ("waiting", "failed", "cancelled"):
            raise ValueError("only waiting, failed or cancelled missions can be resumed")
        if mission["agent"] not in orch.agents:
            raise ValueError("the mission's lead agent no longer exists")
        await asyncio.to_thread(store.mission_resume, mission["id"])
        await orch._post(mission["thread"], "user",
                         f"Resume mission {mission['id']}: {mission['objective']}\n"
                         f"Checkpoint: {mission['result']}\nVerify current state before repeating any action.",
                         meta={"mission": mission["id"]})
        await orch.resume_mission(mission)
        return {"ok": True, "command": command, "mission": await asyncio.to_thread(store.mission_get, mission["id"])}
    if command == "help":
        reply = "\n".join(f"{c['usage']} — {c['description']}" for c in CATALOG)
    elif command == "agents":
        reply = "\n".join(f"@{key} · {a['name']} · {orch.agent_state.get(key, 'idle')}"
                          for key, a in orch.agents.items())
    elif command == "status":
        missions, watches = await asyncio.gather(asyncio.to_thread(store.missions), asyncio.to_thread(db.active_watches))
        busy = len(orch.workers.running())
        active = [m for m in missions if m["status"] in ("queued", "running", "waiting")]
        reply = f"{busy} workers running · {len(active)} open tasks · {len(watches)} active watches"
        reply += "".join(f"\n{m['id']} · {m['status']} · {m['agent']} · {m['objective'][:120]}" for m in active[:8])
    elif command == "tasks":
        rows = await asyncio.to_thread(kernel.sessions, ("queued", "working", "waiting", "blocked", "failed", "stalled"),
                                       "worker", 20)
        reply = "\n".join(f"{r['id']} · {r['profile']} · {r['status']} · {r['title'][:100]}"
                          + (f"\n  waiting on you: {r['question']}" if r["question"] else "")
                          for r in rows) or "Nothing open."
    elif command == "events":
        rows = await asyncio.to_thread(kernel.triggers, 15)
        reply = "\n".join(f"{t['id']} · {t['source']} · {t['disposition']} · {' '.join(t['text'].split())[:70]}"
                          for t in rows) or "No events yet."
    elif command == "policy":
        reply = await asyncio.to_thread(triggers_mod.apply_policy, _policy_args(rest))
    elif command == "watchdog":
        if not rest:
            raise ValueError("use /watchdog followed by your question")
        await orch._post(thread, "user", text)
        await orch.watchdog.ask(thread, rest, source if source != "gui" else None, chat_guid)
        return {"ok": True, "command": command}
    elif command == "stop":
        selected = rest.removeprefix("@") or aid
        if selected != "all" and selected not in orch.agents:
            raise ValueError("unknown agent; use /stop @agent or /stop all")
        targets = list(orch.agents) if selected == "all" else [selected]
        stopped = sum([bool(await orch.stop_agent(a)) for a in targets])
        reply = f"Stopped work for {stopped} agent(s), with their processes, watches and held commands."
        await orch._emit({"type": "mission_updated"})
    elif command == "pause":
        await orch.pause()
        reply = "Paused. Nothing new gets picked up, and events are still recorded. /unpause carries on from here."
    elif command == "unpause":
        await orch.resume()
        reply = "Carrying on with whatever waited."
    elif command == "remember":
        if not rest:
            raise ValueError("use /remember followed by the fact to save")
        mid = await asyncio.to_thread(db.add_memory, "user", rest)
        reply = f"Saved memory [{mid}]: {rest}"
        await orch._emit({"type": "memory_updated"})
    elif command == "recall":
        if not rest:
            raise ValueError("use /recall followed by search terms")
        rows = await asyncio.to_thread(db.search_memories, rest, 12)
        reply = "\n".join(f"[{m['id']}] {m['text']}" for m in rows) or "No matching memories."
    elif command == "watches":
        rows = await asyncio.to_thread(db.list_watches)
        reply = "\n".join(f"[{w['id']}] {w['name']} · {w['status']} · {w['checks']}/{w['max_checks']} checks"
                          for w in rows[:20]) or "No watches yet. Ask an agent to watch a status endpoint or a running task."
    elif command in ("model", "effort"):
        aid, body = target(orch, thread, rest)
        reply = (_set_model if command == "model" else _set_effort)(orch, aid, body.strip().lower() if command == "effort" else body.strip())
        await orch._emit({"type": "agent_updated", "agent": {
            "id": aid, "name": orch.agents[aid]["name"], "color": orch.agents[aid].get("color"),
            "shape": orch.agents[aid].get("shape"), "order": orch.agents[aid].get("order", 99)}})
    elif command == "watch":
        match = re.fullmatch(r"cancel\s+(\d+)", rest)
        if not match:
            raise ValueError("use /watch cancel id")
        ok = await asyncio.to_thread(db.finish_watch_poll, int(match[1]), "cancelled", "Stopped by owner.")
        reply = "Watch cancelled." if ok else "That watch is not active."
        await orch._emit({"type": "watch_updated"})
    await orch._post(thread, "user", text)
    await orch._post(thread, "control", reply, kind="sys", meta={"command": command})
    if source != "gui" and chat_guid:
        await orch._relay(source, chat_guid, thread, "control", reply)
    return {"ok": True, "command": command, "reply": reply}
