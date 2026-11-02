"""Where every event enters: filter it, record it, send it where it belongs.

Most of what arrives needs nothing, and a model reading each event from scratch
would spend its budget saying so. Frequent, stable judgements therefore run as
code, and only what is left costs a model call:

- Admission (code). Each source is set to answer only explicit requests, to offer
  its events as candidates, or to be ignored; the owner is always explicit.
- A free local read (Laya), which may only ever drop what it is sure is noise.
- Pass-through (code). An event on a Thread a Worker follows goes straight to that
  Worker. The Router sees a one-line note but spends no decision on it.
- Router (model). What is left: cold starts of new work, judged against policies
  written in plain words.

Every event gets a row saying what became of it, ignored ones included, so "why
did it not react to that?" has an answer that does not require reconstructing it.
"""
import asyncio
import logging
import time

import kernel_store as store
import laya
import recall
import runtime_store
import settings as cfg

logger = logging.getLogger("missioncontrol.triggers")

EXPLICIT, CANDIDATE, IGNORE, SYSTEM = "explicit", "candidate", "ignore", "system"
# After the owner stops a task, events on its threads start nothing for this long. A CI
# failure a minute later is not a reason to begin again; one next week may be.
STOP_HOLDS_THREAD = 24 * 3600
RECOVER_EVENTS_FOR = 6 * 3600
# Sessions that are in the middle of something, which a notice can usefully reach.
ACTIVE = ("queued", "working", "waiting")
ADMISSIONS = (EXPLICIT, CANDIDATE, IGNORE)
# Automated events that carry their own complete instructions, written or approved by
# the owner, so each can start a Worker. Anything else raised for nobody in particular
# goes to the Router. A notice with no session behind it once started a Worker with no
# context, which found itself something to do: it approved a PR nobody had asked about.
STANDING_SOURCES = {"cron", "heartbeat", "brief"}

# Channels only ever deliver allow-listed senders, so a message on one is the
# owner asking. Everything arriving from the outside world starts as a candidate.
SOURCE_DEFAULTS = {
    "gui": EXPLICIT, "imessage": EXPLICIT, "telegram": EXPLICIT, "whatsapp": EXPLICIT, "email": EXPLICIT,
    "github": CANDIDATE, "hook": CANDIDATE, "fleet": CANDIDATE,
    # The owner's own WhatsApp. Other people's private messages do not reach a model
    # unless the owner says so; agents can still read them on request, as before.
    "whatsapp_link": IGNORE,
}

# Measured the way the group route was: a clear kind reads above 0.9, a message
# that could be either reads near 0.5. The bar is high because dropping a real
# request silently is much worse than one Router decision spent on chatter.
NOISE_P = 0.9
# An event should not wait on this. Laya loads its model on first use; until it is
# ready the Router simply sees a little more.
NOISE_TIMEOUT = 8.0
NOISE_KINDS = {"chatter", "status"}

# Seeded once, on a first run with none, so proactive behaviour after the move
# matches what the event routes did before it, in words the owner can then edit.
DEFAULT_POLICIES = (
    "A CI failure on a default branch or an open pull request: dispatch a worker to find the cause. "
    "Tell the owner only if it needs them, or once there is a fix to look at.",
    "A pull request or issue someone else opened: dispatch a worker to review or triage it on GitHub. "
    "Do not tell the owner unless it needs a decision.",
    "Merges, closes, and comments that ask nothing: stay quiet.",
    "A message from an agent on one of the owner's connected machines: handle it as a teammate's request.",
)


def settings() -> dict:
    return cfg.load().get("superagent") or {}


def source_base(source: str) -> str:
    return (source or "").split(":", 1)[0]


def source_table() -> dict:
    return {**SOURCE_DEFAULTS, **(settings().get("sources") or {})}


def admission(source: str, actor: str = "", from_owner: bool = False) -> str:
    """The Fast Path: decided from where an event came from, not what it says."""
    if from_owner:
        return EXPLICIT
    base = source_base(source)
    ignored = (settings().get("ignore_actors") or {}).get(base) or []
    if actor and actor in ignored:
        return IGNORE
    table = source_table()
    verdict = table.get(source) or table.get(base) or CANDIDATE
    return verdict if verdict in ADMISSIONS else CANDIDATE


def seed_policies():
    if not store.policies():
        for text in DEFAULT_POLICIES:
            store.policy_add(text, "system")


def apply_policy(args: dict) -> str:
    """Natural-language policies, and which sources feed them. Shared by the
    Router, the Watchdog and the settings page."""
    action = (args.get("action") or "").strip()
    if action == "list":
        rows = store.policies()
        lines = [f"[{p['id']}] {'on' if p['enabled'] else 'off'}: {p['text']}" for p in rows]
        lines += [f"source {k}: {v}" for k, v in sorted(source_table().items())]
        return "\n".join(lines) or "no policies"
    if action == "add":
        text = (args.get("text") or "").strip()
        if not text:
            return "error: give the policy in plain words"
        return f"policy {store.policy_add(text)['id']} is on from the next decision"
    if action in ("enable", "disable"):
        ok = store.policy_set(args.get("id") or "", action == "enable")
        return f"policy {action}d" if ok else "error: no such policy"
    if action == "remove":
        return "policy removed" if store.policy_remove(args.get("id") or "") else "error: no such policy"
    if action == "source":
        source, verdict = (args.get("source") or "").strip(), (args.get("admission") or "").strip()
        if not source or verdict not in ADMISSIONS:
            return "error: give source and admission (explicit, candidate or ignore)"
        conf = settings()
        cfg.update("superagent", {"sources": {**(conf.get("sources") or {}), source: verdict}})
        return f"source {source} is now {verdict}"
    return "error: action must be list, add, enable, disable, remove or source"


class Intake:
    def __init__(self, orch):
        self.orch = orch

    async def ingest(self, source: str, thread: str, text: str, *, actor: str = "", from_owner: bool = False,
                     system: bool = False, kind: str = "topic", title: str = "", address: dict | None = None,
                     meta: dict | None = None, profile: str | None = None, session: str | None = None,
                     mission: str | None = None, dedupe: str | None = None) -> dict:
        verdict = SYSTEM if system else await asyncio.to_thread(admission, source, actor, from_owner)
        await asyncio.to_thread(store.thread_upsert, thread, source, kind, title, address)
        meta = {**(meta or {}), **({"profile": profile} if profile else {}), **({"mission": mission} if mission else {}),
                **({"session": session} if session else {})}
        trigger, fresh = await asyncio.to_thread(store.trigger_add, source, thread, text, actor, from_owner,
                                                 verdict, meta, dedupe)
        # Seen before and dealt with is the same event twice. Seen before and still new
        # means a restart cut it off on the way in, and this is its second chance.
        if not fresh and trigger["disposition"] != "new":
            return trigger
        if fresh:
            await self.orch._emit({"type": "trigger", "trigger": {k: trigger[k] for k in
                                  ("id", "ts", "source", "thread", "actor", "from_owner", "admission")}})
        await self._route(trigger)
        return trigger

    async def recover(self):
        """Events a restart caught between being recorded and being routed. Older than
        that, they are history: nobody wants last week's comment acted on today."""
        for trigger in reversed(await asyncio.to_thread(store.triggers, 200, None, "new")):
            try:
                if time.time() - trigger["ts"] > RECOVER_EVENTS_FOR:
                    await self._dispose(trigger, "read", "cut off by a restart too long ago to act on")
                else:
                    await self._route(trigger)
            except Exception:
                logger.exception("could not route %s after a restart", trigger["id"])

    async def _route(self, trigger: dict):
        meta = trigger.get("meta") or {}
        verdict, thread, profile = trigger["admission"], trigger["thread"], meta.get("profile")
        owner = bool(trigger.get("from_owner"))
        if verdict == IGNORE:
            await self._dispose(trigger, "ignored", "source or sender is set to ignore")
            return
        # An event addressed to one session: the watch it armed, the command it asked about.
        target = await asyncio.to_thread(self._addressed, meta.get("session"), meta.get("mission"))
        if target:
            await self._pass_through(trigger, [target])
            return
        if owner:
            await asyncio.to_thread(store.thread_stop, thread, False)
        followers = await asyncio.to_thread(store.subscribers, thread)
        if meta.get("closes"):
            await self._thread_closed(trigger, followers)
            return
        if followers:
            # Following a thread for context carries no authority. What the owner says on
            # one is a request nobody has been given yet, so it needs a decision.
            working_on_it = [f for f in followers if f.get("sub_mode") != "context"]
            if meta.get("needs_decision") or (owner and not working_on_it):
                await self._hand_to(trigger, followers)
                await self.orch.router.submit(trigger)
            else:
                await self._pass_through(trigger, followers)
            return
        # What stopping a task holds back is starting it again. Anyone else still working
        # on that thread, like the reviewer on a PR whose author was stopped, still hears.
        if not owner:
            stopped = await asyncio.to_thread(store.thread_stopped, thread)
            if stopped and time.time() - stopped < STOP_HOLDS_THREAD:
                await self._dispose(trigger, "ignored", "the owner stopped the work on this thread")
                return
        # Nothing here for anyone to decide, unless someone was already on it. Waking the
        # Router to say so cost tens of thousands of tokens a time, for echoes of our own.
        if meta.get("followers_only"):
            await self._dispose(trigger, "ignored", "nobody is following this")
            return
        if verdict == SYSTEM:
            if source_base(trigger["source"]) in STANDING_SOURCES:
                await self.orch.workers.start_for_event(trigger, profile)
            else:
                await self.orch.router.submit(trigger)
            return
        if verdict == CANDIDATE and await self._is_noise(trigger):
            return
        await self.orch.router.submit(trigger)

    @staticmethod
    def _addressed(session: str | None, mission: str | None) -> dict | None:
        row = store.session_get(session) if session else None
        if not row and mission:
            row = store.session_for_mission(mission)
        return row if row and row["status"] != "ended" else None

    async def _dispose(self, trigger: dict, disposition: str, detail: str = ""):
        await asyncio.to_thread(store.trigger_dispose, trigger["id"], disposition, detail)
        trigger["disposition"], trigger["detail"] = disposition, detail

    async def _hand_to(self, trigger: dict, followers: list[dict]):
        meta = trigger.get("meta") or {}
        owner = bool(trigger.get("from_owner"))
        for follower in followers:
            if follower["kind"] == "remote":
                await self.orch.finish_remote(follower, trigger)
                continue
            # Work stopped on a person's decision is not moved along by a bot. The event
            # is kept for when it wakes, but only a person, or their decision, wakes it.
            held = follower["status"] in ("blocked", "failed") and not (owner or meta.get("owner_decision"))
            # A notice that changes nothing for a task already over is there if it wakes.
            calm = meta.get("quiet") and follower["status"] not in ACTIVE
            # Not woken, it is not being asked anything. Its authority should not wait there
            # for the next stranger's comment to wake it with.
            await self.deliver(trigger, follower["id"], wakes=not (held or calm),
                               promote=follower.get("sub_mode") != "context" and not calm)

    async def _pass_through(self, trigger: dict, followers: list[dict]):
        names = ", ".join(s["id"] for s in followers)
        await self._hand_to(trigger, followers)
        await self._dispose(trigger, "passed", names)
        await self.orch.router.note(trigger, names)

    async def _thread_closed(self, trigger: dict, followers: list[dict]):
        """A PR or issue ended. The task that was about it ends with it, and a Worker that
        had finished does not need waking to be told. Only one still busy hears about it."""
        for follower in followers:
            if follower["kind"] == "remote":
                continue
            meta = follower.get("meta") or {}
            mine = follower.get("sub_mode") != "context"
            others = [t for t in (meta.get("prs") or {}) if t != trigger["thread"]]
            # A direct line is the owner's to the teammate, not a task about this PR.
            over = mine and follower["status"] == "idle" and not others and not follower["direct"]
            await self.deliver(trigger, follower["id"], wakes=mine and follower["status"] in ACTIVE, promote=False)
            if over:
                await self.orch.workers.finish(follower["id"], trigger["text"][:200])
            await asyncio.to_thread(store.unsubscribe, trigger["thread"], follower["id"])
        if followers:
            await self._dispose(trigger, "passed", ", ".join(f["id"] for f in followers))
            await self.orch.router.note(trigger, ", ".join(f["id"] for f in followers))
        else:
            await self._dispose(trigger, "ignored", "it closed, and nobody was following it")

    async def deliver(self, trigger: dict, sid: str, note: str = "", wakes: bool = True, promote: bool = True):
        """promote=False keeps the owner's words from granting anything: they are not to
        this Worker, or they are from long enough ago to say nothing about now."""
        owner = bool(trigger.get("from_owner"))
        speaks = owner and promote
        text = recall.card(trigger, 6000) + (f"\nRouter's note: {note}" if note else "")
        priority = int(owner or bool((trigger.get("meta") or {}).get("owner_decision")))
        await asyncio.to_thread(store.inbox_put, sid, "trigger", text, trigger["id"],
                                {"from_owner": speaks, "source": trigger["source"]},
                                wakes, priority, grants=speaks)
        if speaks:
            await self.orch.workers.owner_spoke_to(sid)
        if wakes:
            self.orch.wake_session(sid, priority=bool(priority))

    async def _is_noise(self, trigger: dict) -> bool:
        """Between the code and the Router: a free read of what kind of event this
        is, from Laya on this machine. Other people's words stay here, which is why
        this is not a call to a hosted model. It can only ever drop an event when sure,
        and when Laya is off or slow the event simply goes on to the Router."""
        if not laya.enabled():
            return False
        got = await laya.system_one(
            {"source": trigger["source"], "from": trigger["actor"], "text": trigger["text"][:1500]},
            {"kind": {"type": "choice",
                      "instructions": "What kind of event is this, from the point of view of someone "
                                      "maintaining this work?",
                      "criteria": {"request": "someone asking for something to be done, changed or answered",
                                   "problem": "something failed or looks broken: an error, a failure, a bug report",
                                   "status": "an automated status notice that asks nothing of anyone",
                                   "chatter": "conversation that needs no action"}}},
            timeout=NOISE_TIMEOUT)
        answer = ((got or {}).get("answers") or {}).get("kind") or {}
        kind = answer.get("choice")
        if not kind:
            return False
        probability = (answer.get("probabilities") or {}).get(kind, 0.0)
        noise = kind in NOISE_KINDS and probability >= NOISE_P
        await asyncio.to_thread(runtime_store.decision_record, "trigger_filter", "",
                                {"trigger": trigger["id"], "kind": kind, "probability": probability,
                                 "threshold": NOISE_P}, noise)
        if noise:
            await self._dispose(trigger, "ignored", f"read as {kind} (p={probability:.2f})")
        return noise
