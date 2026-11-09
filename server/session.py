"""One context and the loop that drives it.

A Session is an asynchronous ReAct loop over a transcript kept in the database.
Three things separate it from a turn that blocks until it is done:

- A slow tool does not hold the loop. It runs in the background and the model is
  told "Started"; its result comes back later as a new message, so whoever is
  waiting on this session can still get a word in.
- New input is read at every step, not when the work is over. What arrived during
  one inference is merged into the next call, so a burst costs one model call
  rather than one each.
- Nothing lives only in memory. A restart loads the transcript and carries on, and
  a tool call the restart cut short is answered with what actually happened to it.
"""
import asyncio
import contextvars
import datetime
import functools
import json
import logging
import re
import time

import budget
import evolve
import harness
import kernel_store as store
import llm

logger = logging.getLogger("missioncontrol.session")

# The session the current task belongs to. Tools and the guards around them read
# it to know whose work this is without every call site carrying it.
CURRENT: contextvars.ContextVar = contextvars.ContextVar("mc_session", default=None)

MAX_TOOL_ROUNDS = 40

# Inside one turn every model round re-sends every tool result so far, so a
# forty-round browser session pays for its first page forty times over, and a
# screenshot taken at round three is still being uploaded at round thirty. The
# newest results stay whole; older ones are cut down to their opening once the
# turn's tool output passes the budget, and only the newest screenshots travel.
CONTEXT_BUDGET = 90_000      # characters of tool output kept whole per turn
CONTEXT_KEEP = 6             # newest tool results never trimmed
CONTEXT_KEEP_IMAGES = 2      # newest screenshots kept as images
TRIMMED_HEAD = 600
# Four failures in a row and the next call is usually the fifth. One plain
# prompt to stop and rethink, once a turn, costs a sentence.
FLAIL_RUN = 4

# Models say "NO_REPLY." and "No reply" as often as the exact token. Every
# check for silence goes through this, or a teammate gets woken by the word.
_SILENCE = re.compile(r"\W*no[_ ]?reply\W*", re.I)


def is_silence(text: str) -> bool:
    return not (text or "").strip() or bool(_SILENCE.fullmatch(text.strip()))


def split_reply(text: str) -> list[tuple[str, dict | None]]:
    """Blank lines split a reply into bubbles. A bubble that starts with
    [text] is shown but never spoken (how voice mode sends detail)."""
    out = []
    for part in re.split(r"\n\s*\n", text or ""):
        part = part.strip()
        if not part:
            continue
        meta = None
        if part.lower().startswith("[text]"):
            part = part[6:].strip()
            meta = {"silent": True}
        if not part or _SILENCE.fullmatch(part):
            continue   # a trailing NO_REPLY is silence, not a bubble
        out.append((part, meta))
    return out


def _result_size(block: dict) -> int:
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text") or "") if b.get("type") == "text" else 0 for b in content)
    return len(str(content or ""))


def trim_tool_context(messages: list) -> int:
    """Shrink older tool results in place to keep a long turn inside its
    budget. Returns the characters saved. Pairing of tool_use and tool_result
    is never touched: only what a result says is shortened, never whether it
    exists."""
    results = [b for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    saved = 0
    images = 0
    for block in reversed(results):
        content = block.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image":
                images += 1
                if images > CONTEXT_KEEP_IMAGES:
                    kept.append({"type": "text", "text": "[an earlier screenshot was removed to keep this "
                                                         "turn small; take a new one if you need to see it]"})
                    saved += 1000
                    continue
            kept.append(part)
        block["content"] = kept
    total = sum(_result_size(b) for b in results)
    for block in results[:-CONTEXT_KEEP] if len(results) > CONTEXT_KEEP else []:
        if total <= CONTEXT_BUDGET:
            break
        content = block.get("content")
        text = content if isinstance(content, str) else "\n".join(
            p.get("text") or "" for p in (content or []) if isinstance(p, dict) and p.get("type") == "text")
        if len(text) <= TRIMMED_HEAD + 200 or text.endswith("to keep this turn within its budget]"):
            continue
        short = (text[:TRIMMED_HEAD] + f"\n[... {len(text) - TRIMMED_HEAD} more characters cut from this "
                 "older result to keep this turn within its budget]")
        block["content"] = short
        total -= len(text) - len(short)
        saved += len(text) - len(short)
    return saved


# An agent reported a deployment id it had never queried: it had planned the
# deploy, been woken by an unrelated CI watch, and written the plan up as a
# result, reaching back into old conversation for an id that looked right. The
# id was four hours stale and belonged to a different PR.
#
# Outcome words alone cannot be trusted and neither can the agent's own account,
# but the identifier is checkable: if a turn claims a result for id X, X has to
# appear in something that turn actually saw.
_CITED_ID = re.compile(r"\b\d{8,12}\b")
# Only verbs that assert an outcome here and now. The bare noun "deploy" was in
# this list, which made every status round-up a warning: "latest deploy 21213534
# already reported clean at 03:07" recollects something checked an hour earlier,
# and flagging that teaches the owner to scroll past the flag that matters.
_OUTCOME_WORD = re.compile(
    r"\b(deployed|landed|finished|completed|succeeded|shipped|"
    r"is\s+live|went\s+green|has\s+passed)\b", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+")


# A watch wakes the session that armed it and nobody else. A session told by
# another that a watch is armed can conclude it will hear about the result, end
# its turn to wait, and never be woken — which is a stall with no error anywhere,
# because every individual step succeeded. Saying you are waiting to be woken is
# a claim about your own state, and it is checkable: either you own a live watch
# or a running job, or you are not going to wake up.
_AWAITING_WAKE = re.compile(
    r"\b(wait(ing)?\s+(for|on)\s+the\s+watch|watch(er)?\s+will\s+wake|wake\s+me|"
    r"woken\s+by|wait\s+for\s+the\s+(green\s+)?wake|fires?\s+on\s+the\s+\w+\s+wake|"
    r"pick\s+it\s+up\s+on\s+the\s+wake|report\s+on\s+its\s+own|"
    r"watch(er)?\s+armed,?\s+will)\b", re.I)


def awaiting_a_wake(text: str) -> bool:
    """True when a reply ends the turn by saying something will wake it later."""
    return bool(_AWAITING_WAKE.search(text or ""))


def unevidenced_ids(text: str, evidence: list[str]) -> list[str]:
    """Identifiers a reply claims an outcome for without having seen them.

    Evidence is this turn's tool output and the messages that woke it — never
    older history, because that is exactly where a stale id gets picked up.
    """
    seen = "\n".join(evidence or [])
    out = set()
    # The claim and the identifier have to share a sentence, or a round-up that
    # mentions a finished thing and, separately, an id reads as a fabrication.
    for sentence in _SENTENCE.findall(text or ""):
        if not _OUTCOME_WORD.search(sentence):
            continue
        out.update(i for i in _CITED_ID.findall(sentence) if i not in seen)
    return sorted(out)


class Streamer:
    """Hands the model's text to the chat as it is written: deltas are
    coalesced into a few events a second, and None (a fresh attempt)
    tells the UI to drop what it has shown so far."""
    def __init__(self, emit, aid, thread):
        self.emit, self.aid, self.thread = emit, aid, thread
        self.buf, self.thought, self.last, self.shown = "", "", 0.0, False

    async def __call__(self, delta, kind="text"):
        if delta is None:
            self.buf = self.thought = ""
            if self.shown:
                self.shown = False
                await self.emit({"type": "stream", "agent": self.aid, "thread": self.thread, "reset": True})
            return
        if kind == "thinking":
            self.thought += delta
        else:
            self.buf += delta
        if len(self.buf) + len(self.thought) >= 48 or time.monotonic() - self.last > 0.15:
            await self.flush()

    async def flush(self):
        if not (self.buf or self.thought):
            return
        event = {"type": "stream", "agent": self.aid, "thread": self.thread}
        if self.thought:
            event["thinking"], self.thought = self.thought, ""
        if self.buf:
            event["text"], self.buf = self.buf, ""
        self.shown, self.last = True, time.monotonic()
        await self.emit(event)


def _blocks(content) -> list:
    return content if isinstance(content, list) else [{"type": "text", "text": str(content)}]


def _storable(content):
    """What goes in the log: images belong in the request that uses them, never
    in the durable record, and a reload asks for a fresh one if it still matters."""
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            parts = [p if not (isinstance(p, dict) and p.get("type") == "image") else
                     {"type": "text", "text": "[screenshot not kept; take a new one if you need to see it]"}
                     for p in block["content"]]
            out.append({**block, "content": parts})
        else:
            out.append(block)
    return out


INTERRUPTED = ("This tool call was cut off by a restart and its result is lost. It may or may not "
               "have taken effect: check the real state before running it again.")
DIGEST_HEAD = "EARLIER IN THIS SESSION (condensed; ids still resolve through recall):\n"
# Written after a report that ended the turn, so that a session whose last word is not
# its own always has something unanswered. A restart can then tell the two apart.
TURN_ENDED = "(That report ended this turn.)"
IMAGE_WEIGHT = 1000
# Tool output in a digest's input can be cut hard, what a person said much less so.
DIGEST_SAID = 2400
DIGEST_INPUT = 60_000
DIGEST_KEPT = 6000
COMPACT_RETRY = 300


def _weight(content) -> int:
    """How much of the window a message takes. Thinking signatures go along with the
    message but do not take up window, and an image's base64 is not its cost. Counting
    them made up about half of the Router's measured size, and it compacted three times
    in 45 minutes for it, each time forgetting a little more."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return len(json.dumps(content, ensure_ascii=False, default=str))
    total = 0
    for block in content:
        if not isinstance(block, dict):
            total += len(str(block))
        elif block.get("type") in ("thinking", "redacted_thinking"):
            continue
        elif block.get("type") == "image":
            total += IMAGE_WEIGHT
        elif block.get("type") == "text":
            total += len(block.get("text") or "")
        elif block.get("type") == "tool_result":
            total += _weight(block.get("content") or "")
        else:
            total += len(json.dumps(block, ensure_ascii=False, default=str))
    return total


class Transcript:
    """The messages a model call needs, with the log row each one reaches back to."""
    def __init__(self, digest: str, rows: list[dict], pinned: str = ""):
        self.messages: list[dict] = []
        self.marks: list[int] = []
        if digest:
            # What the session is for is not the digest's to lose. It stays word for
            # word in front of every summary of what has happened since.
            self.messages.append({"role": "user", "content": _blocks(
                (f"{pinned}\n\n" if pinned else "") + DIGEST_HEAD + digest)})
            self.marks.append(0)
        for row in rows:
            self.add(row["role"], row["content"], row["id"])
        self._repair()

    def add(self, role: str, content, mark: int = 0):
        if self.messages and self.messages[-1]["role"] == role:
            self.messages[-1]["content"] = _blocks(self.messages[-1]["content"]) + _blocks(content)
            self.marks[-1] = mark or self.marks[-1]
            return
        self.messages.append({"role": role, "content": content})
        self.marks.append(mark)

    def _repair(self):
        """Every tool_use gets its result, ahead of anything else in that message.
        A process that died between the call and its result left one dangling,
        which providers reject outright and which would wedge the session for good."""
        i = 0
        while i < len(self.messages):
            message = self.messages[i]
            used = [b["id"] for b in _blocks(message["content"])
                    if message["role"] == "assistant" and isinstance(b, dict) and b.get("type") == "tool_use"]
            if used:
                following = self.messages[i + 1] if i + 1 < len(self.messages) else None
                blocks = _blocks(following["content"]) if following else []
                answered = {b.get("tool_use_id") for b in blocks if isinstance(b, dict)
                            and b.get("type") == "tool_result"}
                missing = [{"type": "tool_result", "tool_use_id": uid, "content": INTERRUPTED, "is_error": True}
                           for uid in used if uid not in answered]
                if missing and following:
                    following["content"] = missing + blocks
                elif missing:
                    self.messages.append({"role": "user", "content": missing})
                    self.marks.append(self.marks[i])
            i += 1

    @property
    def awaiting_answer(self) -> bool:
        return bool(self.messages) and self.messages[-1]["role"] == "user"

    def size(self) -> int:
        return sum(_weight(m["content"]) for m in self.messages)


class Session:
    kind = "worker"
    max_rounds = MAX_TOOL_ROUNDS
    # Characters of transcript before the oldest part is condensed, and how much
    # of the newest is always left as it was written.
    compact_at = 280_000
    keep_tail = 80_000
    # Seconds a tool may run in the foreground before it moves to the background.
    # Not listed means it always completes in line.
    foreground: dict[str, float] = {}
    local_tools: frozenset = frozenset()
    # Whether new input earns a fresh allowance of rounds. Yes for a session whose job
    # is to read a stream, or the last message of a long run gets no answer at all.
    rounds_per_batch = False
    # Whether pausing the agent stops this session.
    pausable = True
    # Whether the next run only picks up where this one stopped, so what this one did still counts.
    carrying_on = False
    checkpoint_prompt = ("You have hit your tool budget for this stretch and can use no more tools. "
                         "{acted}\nReply now with a CHECKPOINT in three short lines: DONE (finished, "
                         "with ids), IN FLIGHT (running or waiting, with ids), NEXT (the single next "
                         "step). You will carry on from here when asked, so make it exact.")

    def __init__(self, orch, row: dict):
        self.orch, self.row, self.id = orch, row, row["id"]
        self.profile = row["profile"]
        self.mission = row.get("mission")
        self.jobs: dict[str, asyncio.Task] = {}
        self.stream_thread = None
        self.stop_after_tools = False
        self.trail, self.actions, self.skills_used, self.evidence = [], [], [], []
        self.rec = self._blank_record()
        self.compact_after = 0.0
        self.stopped_jobs: set[str] = set()

    # ---------- what differs between the Router, a Worker and the Watchdog ----------

    def system_prompt(self) -> str:
        raise NotImplementedError

    def tool_defs(self) -> list:
        return self.orch.harness.tools_for(self.id)

    def llm_conf(self) -> dict | None:
        return self.orch.agent_llm(self.profile)

    def render(self, items: list[dict]) -> str:
        # The time is here and not in the system prompt, whose bytes have to stay the same
        # from one call to the next for the provider to read them back from its cache.
        now = datetime.datetime.now().strftime("%a %H:%M")
        return "\n\n".join([f"[{now}]", *(i["text"] for i in items)])

    def record_thread(self) -> str:
        return self.row.get("conversation") or f"session:{self.id}"

    @property
    def activity_thread(self) -> str:
        """Where this session's tool lines go. Never a chat a person reads for answers."""
        return f"session:{self.id}"

    async def on_final(self, text: str):
        """The model stopped calling tools."""

    async def on_checkpoint(self, text: str):
        """It stopped short of finishing and said where it got to."""

    async def on_tool_limit(self, text: str):
        """It used up its tools for this stretch and said where it got to."""
        await self.on_checkpoint(text)

    async def on_budget_stop(self, note: str | None):
        """The daily budget is spent, so nothing in the inbox was read."""

    async def local_tool(self, name: str, args: dict):
        raise ValueError(f"unknown tool {name}")

    def on_drained(self, items: list[dict]):
        pass

    async def on_more_input(self, items: list[dict]):
        """A run that had said its piece has something new to read and carries on."""

    def pinned(self) -> str:
        """What must survive compaction word for word."""
        return ""

    def on_compacted(self):
        """The oldest messages just left the window."""

    @property
    def owner_here(self) -> bool:
        """Whether what this session is handling right now came from the owner."""
        return False

    @property
    def cwd(self) -> str:
        import os
        agent = self.orch.agents.get(self.profile) or {}
        return os.path.expanduser(agent.get("cwd") or "~")

    # ---------- the scorecard entry for one run ----------

    @staticmethod
    def _blank_record() -> dict:
        return {"id": None, "rounds": 0, "tools": 0, "errors": 0, "streak": 0, "nudged": False,
                "flags": set(), "tokens": {"in": 0, "out": 0}, "outcome": None, "stopped": False}

    async def _open_record(self, trigger: str):
        self.rec = self._blank_record()
        try:
            self.rec["id"] = await asyncio.to_thread(evolve.turn_start, self.profile, self.record_thread(),
                                                     trigger, self.mission)
        except Exception:
            logger.exception("could not open the turn record for %s", self.id)

    async def _close_record(self, outcome: str, error: str = ""):
        rec = self.rec
        if not rec["id"]:
            return
        if self.orch.harness.refused.get(self.id):
            rec["flags"].add("repeat_refused")
        final = rec["outcome"] or ("stopped" if outcome == "cancelled" and rec["stopped"] else outcome)
        try:
            await asyncio.to_thread(evolve.turn_finish, rec["id"], final, rec["rounds"], rec["tools"],
                                    rec["errors"], rec["tokens"]["in"], rec["tokens"]["out"], rec["flags"], error)
        except Exception:
            logger.exception("could not close the turn record for %s", self.id)
        rec["id"] = None

    # ---------- the loop ----------

    async def _load(self) -> Transcript:
        row = await asyncio.to_thread(store.session_get, self.id)
        rows = await asyncio.to_thread(store.log_live, self.id)
        return Transcript((row or {}).get("digest") or "", rows, self.pinned())

    async def _append(self, transcript: Transcript, role: str, content):
        mark = await asyncio.to_thread(store.log_append, self.id, role, _storable(content))
        transcript.add(role, content, mark)

    async def _drain(self, transcript: Transcript) -> list[dict]:
        # A note that asks nothing waits for something that does. Reading one earns no
        # model call of its own: 11 calls in a row once went on notes alone.
        if not (await asyncio.to_thread(store.inbox_should_wake, self.id))["count"]:
            return []
        items = await asyncio.to_thread(store.inbox_pending, self.id)
        if not items:
            return []
        self.on_drained(items)
        text = self.render(items)
        mark = await asyncio.to_thread(store.inbox_take, self.id, [i["id"] for i in items], "user", text)
        transcript.add("user", text, mark)
        self.evidence.append(text)
        return items

    async def _compact(self, transcript: Transcript):
        """Spill the oldest part of a long session out of the window. Only ever at a
        turn boundary, where every tool_use still has its result next to it."""
        if transcript.size() < self.compact_at or time.time() < self.compact_after:
            return
        messages, marks = transcript.messages, transcript.marks
        kept, cut = 0, None
        bound = self._history_bound()
        for i in range(len(messages) - 1, 0, -1):
            kept += _weight(messages[i]["content"])
            if kept < self.keep_tail:
                continue
            plain_user = messages[i]["role"] == "user" and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(messages[i]["content"]))
            # Where the provider binds thinking to the exact history a tail may
            # only start at a real user message; elsewhere between pairs is fine.
            if plain_user or (messages[i]["role"] == "assistant" and not bound):
                cut = i
                break
        if not cut or not marks[cut - 1]:
            return
        lines = []
        for m in messages[:cut]:
            for block in _blocks(m["content"]):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "")
                    # The last digest goes in whole, below, or each compaction would
                    # keep the opening of the one before and forget a little more.
                    if DIGEST_HEAD in text[:DIGEST_SAID]:
                        continue
                    lines.append(f"{m['role']}: {text[:DIGEST_SAID if m['role'] == 'user' else 1200]}")
                elif block.get("type") == "tool_use":
                    lines.append(f"called {block.get('name')}: {json.dumps(block.get('input'), default=str)[:300]}")
                elif block.get("type") == "tool_result":
                    body = block.get("content")
                    lines.append(f"result: {(body if isinstance(body, str) else json.dumps(body, default=str))[:400]}")
        row = await asyncio.to_thread(store.session_get, self.id)
        previous = (row or {}).get("digest") or ""
        source = "\n\n".join(([f"THE DIGEST SO FAR:\n{previous}"] if previous else [])
                              + ["WHAT HAPPENED SINCE:\n" + "\n".join(lines)[-DIGEST_INPUT:]])
        try:
            digest = await llm.quick(
                "Condense the earlier part of an agent session into a working digest under 4000 characters. Keep "
                "every identifier exactly as written (tr_, w_, rp_, j_, task ids, PR numbers, commit hashes, paths), "
                "what was decided and why, what was done and what it returned, what is still open and who owns it, and "
                "anything the owner asked for or ruled out. Carry forward whatever in the digest so far is still true. "
                "Drop chatter and output nobody needs again. Plain lines.",
                source, max_tokens=1500, agent=self.profile, conf=self.llm_conf())
        except Exception as exc:
            # Running long costs a little. Failing the turn because a summary could not
            # be written costs the answer, and the transcript is all still here.
            logger.warning("%s: could not compact, carrying on without: %s", self.id, str(exc)[:200])
            self.compact_after = time.time() + COMPACT_RETRY
            return
        if not (digest or "").strip():
            self.compact_after = time.time() + COMPACT_RETRY
            return
        digest = digest.strip()[:DIGEST_KEPT]
        await asyncio.to_thread(store.log_compact, self.id, marks[cut - 1], digest)
        logger.info("%s: spilled %d messages of transcript", self.id, cut)
        tail = [{"id": mark, "role": m["role"], "content": m["content"]}
                for m, mark in zip(messages[cut:], marks[cut:])]
        fresh = Transcript(digest, tail, self.pinned())
        transcript.messages, transcript.marks = fresh.messages, fresh.marks
        self.on_compacted()

    def _history_bound(self) -> bool:
        try:
            return llm.history_is_bound(self.llm_conf())
        except Exception:
            return True

    async def _budget_gate(self) -> bool:
        verdict, note = budget.check(thread=self.record_thread())
        if verdict == "stop":
            self.rec["outcome"] = "budget"
            await self.on_budget_stop(note)
            return True
        if verdict == "warn" and note:
            await self.orch._post(f"dm:{self.orch.lead}", "budget", note, kind="sys")
        return False

    async def run(self, trigger: str = "") -> str:
        """Work until nothing is left to answer, then return. A wake runs it again."""
        token = CURRENT.set(self)
        h = self.orch.harness
        self.stop_after_tools = False
        carried, self.carrying_on = self.carrying_on, False
        if not carried:
            self.trail, self.actions, self.skills_used = [], [], []
        self.last_said = ""
        self.evidence = self.evidence[-60:]
        h.start_turn(self.id, self.mission)
        await self._open_record(trigger or self.kind)
        llm.TURN_USAGE.set(self.rec["tokens"])
        outcome, error = "completed", ""
        try:
            if await self._budget_gate():
                outcome = "waiting"
                return outcome
            transcript = await self._load()
            outcome = await self._work(transcript)
            return outcome
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome, error = "failed", harness.redact(str(exc))[:400]
            raise
        finally:
            # A run that got nowhere leaves things as the last one did, for whichever picks this up.
            if outcome == "failed" or self.rec["outcome"] in ("budget", "paused"):
                self.carrying_on = self.carrying_on or carried
            await self.orch.session_state(self, "idle")
            await self._close_record(outcome, error)
            await h.end_turn(self.id, "cancelled" if outcome == "cancelled" else "unverified")
            CURRENT.reset(token)

    async def _work(self, transcript: Transcript) -> str:
        h = self.orch.harness
        system = self.system_prompt()
        trim = not self._history_bound()
        streamer = Streamer(self.orch._emit, self.profile, self.stream_thread)
        rounds = cut_offs = 0
        if not self.evidence:
            # Reloaded after a restart: what it saw before still counts as seen.
            self.evidence = [json.dumps(m["content"], ensure_ascii=False, default=str)[:8000]
                             for m in transcript.messages[-16:]]
        await self._drain(transcript)
        while True:
            if not transcript.awaiting_answer:
                return "completed"
            if self.pausable and self.orch.paused:
                # What is in the transcript stays there and is answered on resume.
                self.rec["outcome"] = "paused"
                return "paused"
            turn_budget = h.turns.get(self.id, {})
            if rounds >= self.max_rounds or (turn_budget and turn_budget["count"] >= turn_budget["limit"]):
                return await self._checkpoint(system, transcript)
            await self._compact(transcript)
            await self.orch.session_state(self, "thinking")
            if trim:
                saved = trim_tool_context(transcript.messages)
                if saved:
                    logger.info("%s: trimmed %d characters of older tool output", self.id, saved)
            self.rec["rounds"] += 1
            rounds += 1
            # Streams to whoever is waiting in a chat, and to nobody otherwise.
            on_text = streamer if self.stream_thread else None
            if on_text:
                streamer.thread = self.stream_thread
                await streamer(None)
            resp = await llm.complete(system, transcript.messages, self.tool_defs(), agent=self.profile,
                                      conf=self.llm_conf(), on_text=on_text)
            if on_text:
                await streamer.flush()
            if llm.truncated(resp):
                # Cut off at the output limit, usually mid-way through one big
                # tool call. Its text is not an answer and its half-written
                # calls must not run; ending here left the session idle in the
                # middle of the job.
                cut_offs += 1
                self.rec["flags"].add("cut_off")
                kept = [b for b in resp.get("content", []) if b.get("type") != "tool_use"]
                if cut_offs <= 2:
                    logger.info("%s: reply cut off at the output limit; asking for smaller steps", self.id)
                    await self._append(transcript, "assistant", kept or [{"type": "text", "text": "(cut off)"}])
                    await self._append(transcript, "user",
                        "Your last reply hit the output limit and was cut off before it finished, so "
                        "nothing in it ran. Carry on from where you were in smaller steps: one tool call "
                        "at a time, at most about 15 items in any list (split a big batch over several "
                        "calls), and keep the text around it short.")
                    continue
                text = (llm.text_of({"content": kept}) + "\n\n(My replies keep getting cut off at the "
                        "output limit, so I stopped here. Say continue and I'll go on in smaller steps.)").strip()
                await self._append(transcript, "assistant", [{"type": "text", "text": text}])
                await self.on_checkpoint(text)
                return "waiting"
            uses = llm.tool_uses(resp)
            content = resp.get("content") or [{"type": "text", "text": "(no reply)"}]
            await self._append(transcript, "assistant", content)
            said = llm.text_of(resp)
            if uses:
                self.last_said = said or self.last_said
            else:
                # A model often says its piece alongside its last tool call, then has
                # nothing to add. That earlier text was the answer, and treating the
                # empty ending as the reply lost it. NO_REPLY is not empty: it is a
                # decision to stay quiet, and stays one.
                final, self.last_said = said or self.last_said, ""
                await self.on_final(final)
                more = await self._drain(transcript)
                if more:
                    rounds = 0 if self.rounds_per_batch else rounds
                    await self.on_more_input(more)
                continue
            await self.orch.session_state(self, "doing")
            results = []
            try:
                for use in uses:
                    out = await self.call_tool(use)
                    self.evidence.append(str(out)[:8000])
                    failed = getattr(out, "status", "completed") != "completed"
                    self.rec["tools"] += 1
                    self.rec["errors"] += failed
                    self.rec["streak"] = self.rec["streak"] + 1 if failed else 0
                    results.append({"type": "tool_result", "tool_use_id": use["id"],
                                    "content": getattr(out, "content", None) or str(out), "is_error": failed})
            except asyncio.CancelledError:
                # Stopped part-way through several calls. What did finish is kept, or on the
                # way back every one of them reads as "may or may not have taken effect",
                # which is an invitation to do the finished ones again.
                if results:
                    await self._append(transcript, "user", results)
                raise
            if self.rec["streak"] >= FLAIL_RUN and not self.rec["nudged"]:
                self.rec["nudged"] = True
                self.rec["flags"].add("flailing")
                results.append({"type": "text", "text":
                    f"Your last {self.rec['streak']} tool calls all failed. Before you call another tool, "
                    "stop and work out why: which assumption is wrong (a path, a flag, a name, "
                    "whether the thing exists at all)? Check that first, or change approach. If "
                    "you cannot get past it, say plainly what is blocking you and stop."})
            await self._append(transcript, "user", results)
            more = await self._drain(transcript)
            if more and self.rounds_per_batch:
                rounds = 0
            if self.stop_after_tools:
                # A terminal report says everything; another model call to say so
                # again costs a round and buys nothing. Anything that arrived in
                # the meantime still gets its answer.
                self.stop_after_tools = False
                pending = await asyncio.to_thread(store.inbox_should_wake, self.id)
                if not pending["count"] and not self._arrived_since(transcript):
                    await self._append(transcript, "assistant", [{"type": "text", "text": TURN_ENDED}])
                    return "completed"
                await self.on_more_input(more)

    def _arrived_since(self, transcript: Transcript) -> bool:
        """Text after the tool results means something came in with them."""
        last = transcript.messages[-1]
        return any(isinstance(b, dict) and b.get("type") == "text" for b in _blocks(last["content"]))

    async def _checkpoint(self, system: str, transcript: Transcript) -> str:
        logger.warning("%s exhausted its tool rounds", self.id)
        self.rec["outcome"] = "checkpoint"
        acted = ("Outward actions already executed, which must NOT be repeated: " + "; ".join(self.actions)
                 if self.actions else "No outward actions (submits, deploys, pushes, messages) ran.")
        await self._append(transcript, "user", self.checkpoint_prompt.format(acted=acted))
        resp = await llm.complete(system, transcript.messages, None, agent=self.profile, conf=self.llm_conf())
        text = llm.text_of(resp) or "Hit my tool budget mid-task; say continue to pick it back up."
        await self._append(transcript, "assistant", [{"type": "text", "text": text}])
        await self.on_tool_limit(text)
        return "waiting"

    # ---------- tools ----------

    def _dispatcher(self, name: str, args: dict):
        if name in self.local_tools:
            return functools.partial(self.local_tool, name, args)
        return functools.partial(self.orch._dispatch_tool, self.profile, self.cwd, name, args)

    async def call_tool(self, use: dict):
        out = await self._run_tool(use)
        # Credentials never go to a model, whoever asked for the work. A worker grepping
        # for where a token lived got it back in plain text, and that output is sent to
        # the provider on the next step and kept in the transcript.
        if getattr(out, "content", None):
            return out
        return harness.ToolResult(harness.for_model(str(out)), getattr(out, "status", "completed"))

    async def _run_tool(self, use: dict):
        import runtime
        name, args, call_id = use.get("name", ""), use.get("input", {}), use.get("id")
        execute = self.orch.harness.execute(self.id, name, args, self._dispatcher(name, args), call_id)
        grace = self.foreground.get(name)
        if grace is None:
            return await execute
        task = runtime.spawn(execute, "agents", f"{self.id}:{name}", self.profile)
        try:
            done, _ = await asyncio.wait({task}, timeout=grace)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task in done:
            return task.result()
        detail = next((str(v) for v in args.values() if isinstance(v, str) and v.strip()), "") if isinstance(args, dict) else ""
        job = await asyncio.to_thread(store.job_add, self.id, name, " ".join(detail.split())[:160])
        self.jobs[job["id"]] = task
        task.add_done_callback(functools.partial(self._job_done, job))
        return harness.ToolResult(
            f"Started in the background as job {job['id']}. Nothing is blocked: carry on with anything else, "
            "or end your turn. The result will arrive as a new message and wake you. job action=stop ends it.")

    def _job_done(self, job: dict, task: asyncio.Task):
        import runtime
        self.jobs.pop(job["id"], None)
        runtime.spawn(self._finish_job(job, task), "agents", f"{self.id}:job", self.profile)

    async def _finish_job(self, job: dict, task: asyncio.Task):
        if task.cancelled():
            # Being told to stop is what stopped means. Anything else cancelling it is the
            # host going down: left as running, the next start says the job was lost and
            # wakes whoever was waiting on it.
            if job["id"] not in self.stopped_jobs:
                return
            status, result = "cancelled", "Stopped."
        elif task.exception():
            status, result = "failed", f"error: {harness.redact(str(task.exception()))[:400]}"
        else:
            out = task.result()
            status = "completed" if getattr(out, "status", "completed") == "completed" else "failed"
            result = harness.redact(str(out), None)
        await asyncio.to_thread(store.job_finish, job["id"], status, result)
        if status == "cancelled":
            return
        # Work stopped on a person's decision is not moved along by a job finishing. It is
        # kept for when they answer. Waking it made a late build result into "ready" and
        # completed a task whose question was still open.
        row = await asyncio.to_thread(store.session_get, self.id)
        held = bool(row) and row["status"] in ("blocked", "failed")
        head = result if len(result) <= 6000 else (result[:6000] + f"\n[... {len(result) - 6000} more characters: "
                                                   f"job action=read job_id={job['id']}]")
        await asyncio.to_thread(store.inbox_put, self.id, "job",
                                f"Background job {job['id']} ({job['tool']}: {job['label']}) {status}:\n{head}",
                                ref=job["id"], wakes=not held)
        if not held:
            self.orch.wake_session(self.id)
