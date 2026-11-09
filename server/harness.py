"""Runtime-enforced skill contracts and observable, bounded tool calls.

Skills are instructions, never executable plugins. All effects still go through
the same validated tool dispatcher and the agent's configured permissions.
"""
import asyncio
import difflib
import json
import logging
import re
import time

from jsonschema import Draft202012Validator

import llm as llm_mod
import runtime_store as store
import settings
import skills
import processes
import secret_mask

log = logging.getLogger("missioncontrol.harness")


# What credentials look like, for the ones that are not this app's own: a token read out
# of some other config file is no less a token for not being in our .env. A key's body is
# only ever base64 lines, so a BEGIN marker mentioned in some code does not eat what follows.
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-ant-[\w-]{10,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|"
    r"xox[abprs]-[\w-]{10,})\b"
    r"|-----BEGIN [^-\n]*PRIVATE KEY-----(?:\r?\n[A-Za-z0-9+/=]{1,200})*(?:\r?\n-----END [^-\n]*PRIVATE KEY-----)?")


def redact(value, limit: int | None = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r'(?i)("?(?:api[_-]?key|password|secret(?:_access_key)?|token|authorization)"?\s*[:=]\s*)'
                  r'["\']?(?:(?:bearer|token|basic)\s+)?[^\s,"\'}]+', r'\1[redacted]', text)
    text = _TOKEN_SHAPES.sub("[redacted]", text)
    try:
        for secret in settings.env_all().values():
            if secret and len(secret) >= 6:
                text = text.replace(secret, "[redacted]")
                # Sourced by a shell, a token of the id|secret kind splits at the pipe,
                # and the error prints the secret half on its own.
                for part in secret.split("|"):
                    if len(part) >= 16:
                        text = text.replace(part, "[redacted]")
    except Exception:
        pass
    return secret_mask.redact(text[:limit] if limit else text)


# What one tool result may put in the transcript. A cut has to say so: a model told
# nothing reads the first 8,000 characters of a log as the whole log.
RESULT_CAP = 9000


def for_model(value) -> str:
    text = redact(value, None)
    if len(text) <= RESULT_CAP:
        return text
    return (f"{text[:RESULT_CAP]}\n[cut here: this is the first {RESULT_CAP} of {len(text)} characters. "
            "Narrow what you asked for to see the rest.]")


class ToolResult(str):
    def __new__(cls, value, status="completed", content=None):
        obj = super().__new__(cls, value)
        obj.status = status
        # Images belong in the model request, never in the durable audit text.
        obj.content = content
        return obj


def result_status(text: str) -> str:
    if re.match(r"^(?:blocked|refused|not sent|unknown |no such |failed|timed out|\[error\]|error:|.*? failed:)", text, re.I):
        return "failed"
    exit_code = re.match(r"^(?:claude )?exit (\d+)", text)
    if exit_code and exit_code[1] != "0":
        return "failed"
    return "completed"


# Workers report to the Router now. Skills and profiles saved before that still
# name these, and an old list should not stop anything from working.
RETIRED_TOOLS = {"message_agent", "message_user"}


# The same call failing the same way twice in a row is not bad luck. The
# execution log has agents running an identical `cd` into a folder that does
# not exist three and four times in a row, each attempt a model round spent to
# learn nothing new. The third identical attempt is refused before it runs, with
# the error it would have hit, so the round goes on changing the approach.
# Anything succeeding in between resets the count: starting the server a health
# check was failing on is exactly what makes the third check worth running.
REPEAT_FAILURES = 2
# Tools whose call can carry a line for the owner on what the step is for.
NOTED_TOOLS = ("terminal", "claude_code")


def _call_key(name: str, args) -> str:
    import hashlib
    # Without the note: the same failing command described in new words is the same command.
    if name in NOTED_TOOLS and isinstance(args, dict) and "note" in args:
        args = {k: v for k, v in args.items() if k != "note"}
    try:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(args)
    return name + ":" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


class Harness:
    def __init__(self, agents: dict, definitions: list, emit, work=None, always=()):
        self.agents, self.emit = agents, emit
        self.definitions = {d["name"]: d for d in definitions}
        # Tools a profile's permissions govern, and what every Worker has regardless.
        self.work = set(work) if work is not None else set(self.definitions)
        self.always = frozenset(always)
        self.bound: dict[str, dict] = {}
        self.validators = {name: Draft202012Validator({**d["input_schema"], "additionalProperties": False})
                           for name, d in self.definitions.items()}
        self.active: dict[str, dict] = {}
        self.turns: dict[str, dict] = {}
        self.failures: dict[str, dict] = {}   # aid -> {call key: (times failed, last error)}
        self.refused: dict[str, int] = {}     # aid -> repeats refused this turn

    def bind(self, key: str, aid: str, allowed=None, denied=()):
        """A session runs as a profile. Several can at once, each with its own
        budgets and failures, so all of that is keyed by session, not by profile."""
        self.bound[key] = {"aid": aid, "allowed": None if allowed is None else frozenset(allowed),
                           "denied": frozenset(denied)}

    def unbind(self, key: str):
        self.bound.pop(key, None)

    def owner_of(self, key: str) -> str:
        return (self.bound.get(key) or {}).get("aid", key)

    def permits(self, key: str, name: str) -> bool:
        binding = self.bound.get(key)
        if binding and binding["allowed"] is not None:
            return name in binding["allowed"]
        if binding and name in binding["denied"]:
            return False
        if binding and name in self.always:
            return True
        return self._profile_permits(key, name)

    def _profile_permits(self, key: str, name: str) -> bool:
        allowed = self.agents[self.owner_of(key)].get("tools")
        return name in self.work and (allowed is None or name in allowed)

    def refusal(self, key: str, name: str) -> str:
        """Why a tool is refused. A tool the profile has, held back from a session acting
        on nobody's word, is not a setting anyone can flip: saying 'not enabled' sent the
        Router to the owner to switch Claude Code back on, twice in a day, when it was on."""
        binding = self.bound.get(key)
        if binding and binding["allowed"] is None and name in binding["denied"] and self._profile_permits(key, name):
            return (f"tool '{name}' is held back: no person asked for this work (it came from an event or a "
                    f"policy, or follows a task already reported finished), and {name} is not enabled for work "
                    "nobody asked for. This is not a setting. It comes back when the owner says to go on; "
                    "report what you would have it do, and wait.")
        return f"tool '{name}' is not enabled for this agent"

    def tools_for(self, key: str) -> list:
        return [d for name, d in self.definitions.items() if self.permits(key, name)]

    def start_turn(self, aid: str, mission: str | None = None):
        raw = self.agents[self.owner_of(aid)].get("max_tool_calls", 160)
        limit = max(1, min(int(raw), 800)) if isinstance(raw, (int, float)) else 160
        self.turns[aid] = {"count": 0, "limit": limit, "mission": mission}
        self.failures[aid] = {}
        self.refused[aid] = 0

    async def activate(self, aid: str, name: str, inputs: dict) -> str:
        if aid in self.active:
            raise ValueError("finish the active skill before activating another")
        profile = self.owner_of(aid)
        agent_skills = self.agents[profile].get("skills")
        meta = await asyncio.to_thread(skills.manifest, name, profile, inputs)
        if agent_skills is not None and meta["slug"] not in agent_skills:
            raise ValueError(f"skill '{meta['slug']}' is not enabled for this agent")
        unknown = set(meta.get("allowed_tools", [])) - self.definitions.keys() - RETIRED_TOOLS
        if unknown:
            raise ValueError("unknown tools in skill contract: " + ", ".join(sorted(unknown)))
        record = await asyncio.to_thread(store.execution_start, "skill", profile, meta["slug"],
            {"version": meta["version"], "revision": meta["revision"],
             "inputs": redact(inputs), "max_tool_calls": meta["max_tool_calls"],
             "allowed_tools": meta.get("allowed_tools")},
            self.turns.get(aid, {}).get("mission"))
        self.active[aid] = {"meta": meta, "id": record["id"], "count": 0,
                            "deadline": time.monotonic() + meta["timeout_seconds"]}
        await asyncio.to_thread(skills.record_use, meta["slug"])
        await self.emit({"type": "execution", "execution": record})
        return (f"ACTIVE SKILL: {meta.get('name', meta['slug'])} v{meta['version']} "
                f"(revision {meta['revision']}).\n"
                f"Inputs: {json.dumps(inputs, ensure_ascii=False)}\n"
                f"Budget: {meta['max_tool_calls']} tool calls, {meta['timeout_seconds']} seconds.\n"
                f"Allowed tools: {', '.join(meta.get('allowed_tools', self.definitions))}.\n"
                "Follow these instructions using ordinary tools. Skills never grant additional "
                "permissions. When finished, call skill action=complete with a result and "
                "concrete evidence; use action=fail when blocked.\n\n" + meta["body"])

    async def finish(self, aid: str, status: str, result: str) -> str:
        session = self.active.pop(aid, None)
        if not session:
            return "no active skill"
        record = await asyncio.to_thread(store.execution_finish, session["id"], status, redact(result))
        await self.emit({"type": "execution", "execution": record})
        return f"skill {session['meta']['slug']}: {status}"

    async def end_turn(self, aid: str, status="unverified"):
        await self.finish(aid, status, "Turn ended without an explicit skill outcome and evidence.")
        self.turns.pop(aid, None)
        self.failures.pop(aid, None)

    async def execute(self, aid: str, name: str, args, dispatch, call_id=None) -> ToolResult:
        turn, session = self.turns.get(aid, {}), self.active.get(aid)
        audit_args = args
        if name == "browser" and isinstance(args, dict) and 'text' in args:
            audit_args = {**args, "text": "[typed text hidden]"}
        profile = self.owner_of(aid)
        record = await asyncio.to_thread(store.execution_start, "tool", profile, name,
            {"arguments": redact(audit_args), "call_id": call_id, "session": aid}, turn.get("mission"),
            session["id"] if session else None)
        import workview
        await self.emit({"type": "execution", "execution": workview.labelled(record)})
        status, result, content = "failed", "tool did not finish", None
        try:
            validator = self.validators.get(name)
            if isinstance(args, dict) and llm_mod.UNPARSED in args:
                raise ValueError(f"the arguments of that {name} call were not valid JSON, most likely cut off or "
                                 "badly escaped, so nothing ran. Send the call again with its arguments as one "
                                 "JSON object. If the content is long, write it to a file in smaller pieces.")
            if validator is None:
                if name in RETIRED_TOOLS:
                    raise ValueError(f"{name} no longer exists: use report, which reaches the Router")
                # Say what there is. "unknown tool" alone had a model guess a second name.
                have = sorted(n for n in self.validators if self.permits(aid, n))
                near = difflib.get_close_matches(name, have, n=3, cutoff=0.5)
                raise ValueError(f"unknown tool {name}. " + (f"Did you mean {', '.join(near)}? " if near else "")
                                 + f"Your tools are: {', '.join(have)}")
            error = next(validator.iter_errors(args), None)
            if error:
                path = ".".join(map(str, error.absolute_path)) or "arguments"
                raise ValueError(f"invalid {name} {path}: {error.message}")
            if not self.permits(aid, name):
                raise ValueError(self.refusal(aid, name))
            closing = name == "skill" and args.get("action") in ("complete", "fail")
            key = _call_key(name, args)
            seen = self.failures.get(aid, {}).get(key)
            if seen and seen[0] >= REPEAT_FAILURES and not closing:
                self.refused[aid] = self.refused.get(aid, 0) + 1
                raise ValueError(
                    f"refused: this exact {name} call already failed {seen[0]} times in a row, last with: "
                    f"{seen[1][:300]}\nRunning it again unchanged will fail the same way. Change something "
                    "real (the path, the flags, the approach), check your assumption first, or say what is "
                    "blocking you. If you are waiting for something to change, arm a watch instead of "
                    "re-running the check.")
            if turn and not closing:
                if turn["count"] >= turn["limit"]:
                    raise ValueError("turn tool budget exhausted; report a checkpoint")
                turn["count"] += 1
            timeout = 1900 if name in ("claude_code", "improve") else 180
            if name == "terminal" and aid in self.bound:
                timeout = 1900   # it runs on as a background job rather than holding anyone up
            if name == 'browser' and args.get('action') in ('login', 'request_input'):
                timeout = 375  # Owner input is awaited without polling the model.
            if session and not closing:
                meta = session["meta"]
                allowed = meta.get("allowed_tools")
                if name != "skill" and name not in self.always and allowed is not None and name not in allowed:
                    raise ValueError(f"active skill does not allow '{name}'")
                if session["count"] >= meta["max_tool_calls"]:
                    raise ValueError("skill tool budget exhausted; complete or fail the skill")
                remaining = session["deadline"] - time.monotonic()
                if remaining <= 0:
                    raise ValueError("skill deadline reached; complete or fail the skill")
                session["count"] += 1
                timeout = min(timeout, remaining)
            with processes.scope(agent=profile, session=aid, execution=record["id"], mission=turn.get("mission")):
                output = await asyncio.wait_for(dispatch(), timeout)
                result, content = str(output), getattr(output, "content", None)
            status = result_status(result)
        except asyncio.CancelledError:
            status, result = "cancelled", "Cancelled by the owner or server shutdown."
            raise
        except asyncio.TimeoutError:
            status, result = "failed", "error: tool timed out; inspect its state before retrying"
        except ValueError as exc:
            result = f"error: {exc}"
        except Exception as exc:
            log.exception("tool %s failed for %s", name, aid)
            result = f"error: {name} failed ({type(exc).__name__}): {redact(str(exc))[:400]}"
        finally:
            record = await asyncio.to_thread(store.execution_finish, record["id"], status, redact(result))
            await self.emit({"type": "execution", "execution": workview.labelled(record)})
            if status == "failed" and not result.startswith("error: refused: this exact"):
                book = self.failures.setdefault(aid, {})
                key = _call_key(name, args)
                book[key] = ((book.get(key) or (0, ""))[0] + 1, redact(result))
            elif status == "completed":
                self.failures.get(aid, {}).clear()
        return ToolResult(result, status, content)
