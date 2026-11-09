"""Work as it is being done, in a shape a chat can draw.

The conversation shows what the agent says. This is the part in between: each task the
conversation started, who has it, what they are doing this moment, the steps so far and
what it is waiting on. Read by any client; none of it decides anything.
"""
import json
import re
import time

import db
import kernel_store as store
import runtime_store

RECENT = 36 * 3600          # finished work stays in the conversation this long
STEPS = 60                  # steps kept per task
# Looking something up or checking on a job is part of a step, not one the owner follows.
QUIET_TOOLS = {"job", "recall", "memory_search", "skill", "watch", "remember", "forget", "decide"}
LIVE = ("queued", "working", "waiting", "stalled", "blocked")


def _args(detail) -> dict:
    raw = (detail or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
        return got if isinstance(got, dict) else {}
    except ValueError:
        # Cut off by the audit's length limit. The first string in it is still readable.
        found = re.search(r'"(?:note|command|task|text|query|url)":\s*"((?:[^"\\]|\\.){1,300})', raw or "")
        return {"command": found.group(1).encode().decode("unicode_escape", "ignore")} if found else {}


def _plain(command: str) -> str:
    """A command as someone would say it: without the walk to its folder, the noise sent
    to nowhere, or the second and third things it also does."""
    text = " ".join((command or "").split())
    text = re.sub(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", text)
    text = re.sub(r"\s+2>\s*(?:&1|/dev/null)", "", text)
    first = re.split(r"\s*(?:&&|\|\||;)\s*", text, maxsplit=1)[0]
    first = re.split(r"\s+\|\s+", first, maxsplit=1)[0]
    return first[:90] + ("…" if len(first) > 90 else "")


def step_label(name: str, detail) -> str:
    """What a tool call is, in a line. The agent's own note when it gave one."""
    args = _args(detail)
    note = " ".join(str(args.get("note") or "").split()) if name in ("terminal", "claude_code") else ""
    if note:
        return note[:90]
    if name == "terminal":
        return _plain(str(args.get("command") or "")) or "Ran a command"
    if name == "claude_code":
        task = " ".join(str(args.get("task") or "").split())
        return "Writing code: " + (task[:70] + ("…" if len(task) > 70 else "")) if task else "Writing code"
    if name == "browser":
        where = re.sub(r"^https?://(?:www\.)?", "", str(args.get("url") or "")).split("/")[0]
        action = str(args.get("action") or "").replace("_", " ")
        return f"Browser: {action} {where}".strip()
    if name == "report":
        return {"ready": "Reported it finished", "progress": "Reported progress", "waiting": "Reported it is waiting",
                "blocked": "Asked a question", "handback": "Handed it on", "failed": "Reported it failed",
                "dropped": "Nothing needed doing"}.get(str(args.get("status") or ""), "Reported")
    if name == "watch":
        return "Watching: " + str(args.get("name") or "a result")[:60]
    if name == "research":
        return "Researching: " + " ".join(str(args.get("query") or args.get("question") or "").split())[:70]
    if name == "generate_image":
        return "Making an image"
    first = next((str(v) for v in args.values() if isinstance(v, str) and v.strip()), "")
    return (name.replace("_", " ").capitalize() + (": " + " ".join(first.split())[:60] if first else ""))


def labelled(record: dict) -> dict:
    """An execution record with the line a person reads, for the live event."""
    if record and record.get("kind") == "tool":
        record = {**record, "label": step_label(record.get("name") or "", record.get("detail")),
                  "quiet": record.get("name") in QUIET_TOOLS}
    return record


def _steps(mission: str) -> list[dict]:
    with db._lock:
        runtime_store._ensure()
        rows = runtime_store._rows(db._db().execute(
            "SELECT id, ts, updated, agent, name, status, detail FROM executions WHERE mission=? AND kind='tool' "
            "ORDER BY ts DESC LIMIT ?", (mission, STEPS * 3)))
    out = []
    for row in reversed(rows):
        if row["name"] in QUIET_TOOLS:
            continue
        out.append({"id": row["id"], "ts": row["ts"], "agent": row["agent"], "tool": row["name"],
                    "label": step_label(row["name"], row.get("detail")), "status": row["status"],
                    "seconds": round(max(0.0, row["updated"] - row["ts"]), 1)})
    return out[-STEPS:]


def _waiting_on(orch, session: dict, watches: list, approvals: list) -> dict | None:
    sid = session["id"]
    if session["status"] == "blocked":
        return {"kind": "you", "text": session.get("question") or "It has a question for you."}
    held = [a for a in approvals if a.get("session") == sid]
    if held:
        return {"kind": "you", "text": "Your OK to run a command", "approval": held[0]["id"]}
    if session["status"] == "stalled":
        return {"kind": "provider", "text": "The model provider is not answering"}
    if session["status"] != "waiting":
        return None
    jobs = store.jobs_of(sid, "running")
    if jobs:
        return {"kind": "job", "text": step_label(jobs[0].get("tool") or "", {"arguments": {"command": jobs[0].get("label") or ""}})}
    mine = [w for w in watches if w.get("session") == sid]
    if mine:
        return {"kind": "watch", "text": mine[0].get("name") or "a result"}
    threads = store.threads_that_wake(sid)
    if threads:
        first = threads[0]
        known = store.thread_get(first.get("thread") or "") or {}
        return {"kind": "thread", "text": known.get("title") or first.get("thread") or "news"}
    return {"kind": "nothing", "text": "nothing that will wake it"}


def _phase(session: dict, steps: list, alive: bool, waiting: dict | None) -> str:
    status = session["status"]
    if status in ("blocked",) or (waiting and waiting["kind"] == "you"):
        return "needs_you"
    if status == "queued":
        return "queued"
    if status in ("waiting", "stalled"):
        return "waiting"
    if status == "working":
        running = steps and steps[-1]["status"] == "running"
        if not running:
            return "thinking"
        # Trying again after something went wrong is its own thing to watch.
        earlier = [s for s in steps[:-1] if s["tool"] != "report"]
        return "fixing" if earlier and earlier[-1]["status"] == "failed" else "working"
    return "done"


def view(orch, thread: str = "", limit: int = 12) -> list[dict]:
    """Tasks a conversation started, newest last: those under way, and those that ended
    lately. One entry per task, whoever has had a hand in it."""
    now = time.time()
    rows = [r for r in store.sessions(None, "worker", 200)
            if r.get("mission") and not r["direct"] and not r["speculative"]
            and not (r.get("meta") or {}).get("one_shot")
            and (not thread or r.get("conversation") == thread)
            and (r["status"] in LIVE or now - r["updated"] < RECENT)]
    by_mission: dict[str, list] = {}
    for row in rows:
        by_mission.setdefault(row["mission"], []).append(row)
    watches = [w for w in db.list_watches("active") if w.get("session")]
    approvals = runtime_store.approvals("pending", 500)
    tasks_alive = getattr(orch.workers, "tasks", {})
    out = []
    for mission_id, members in by_mission.items():
        members.sort(key=lambda r: r["created"])
        # Whoever is on it now. Failing that, whoever touched it last.
        active = [m for m in members if m["status"] in LIVE]
        current = max(active or members, key=lambda r: r["updated"])
        mission = runtime_store.mission_get(mission_id) or {}
        steps = _steps(mission_id)
        alive = current["id"] in tasks_alive and not tasks_alive[current["id"]].done()
        waiting = _waiting_on(orch, current, watches, approvals)
        reports = store.reports_of(current["id"], 1)
        last = reports[0] if reports else None
        phase = _phase(current, steps, alive, waiting)
        outcome = ""
        if phase == "done":
            outcome = "failed" if (last and last["status"] == "failed") or current["status"] == "failed" else (
                "stopped" if mission.get("status") == "cancelled" else "finished")
        agent = orch.agents.get(current["profile"], {})
        out.append({
            "id": mission_id, "session": current["id"], "thread": current.get("conversation") or "",
            "title": mission.get("objective") or current["title"],
            "agent": current["profile"], "agent_name": agent.get("name") or current["profile"],
            "others": [{"agent": m["profile"], "name": orch.agents.get(m["profile"], {}).get("name") or m["profile"]}
                       for m in members if m["profile"] != current["profile"]],
            "phase": phase, "outcome": outcome, "status": current["status"],
            "waiting_on": waiting, "started": members[0]["created"], "updated": max(m["updated"] for m in members),
            "steps": steps, "checks": (current.get("meta") or {}).get("finish_line") or [],
            "summary": (last or {}).get("summary") or "", "live": current["status"] in LIVE,
        })
    out.sort(key=lambda t: t["started"])
    return out[-limit:]
