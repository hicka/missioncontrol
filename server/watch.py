"""Ephemeral status watches: poll something until it resolves, then wake the
owning agent once with the result.

Unlike cron jobs, watches need no owner approval, because they are bounded,
read-only, and self-terminating: an agent starts one to follow a thing that is
already in flight (a CI run, a deployment, a build queue, a service coming up,
a file appearing, an API returning a state) and it stops itself the moment the
thing resolves or the check budget runs out. This is deliberately generic: the
agent supplies the poll command or URL and either a pattern that means "done" or
a plain-words goal, so it works for anything, not a fixed list of integrations.

A poll for work the owner asked for runs with the project .env secrets available as
environment variables, so it can reference e.g. $ENVOYER_API_KEY without ever putting
the secret in the command text. No other poll gets them, and what a poll may do at all
is no more than the terminal would let that work do.
"""
import asyncio
import logging
import os
import re
import time

import db
import decide
import harness
import kernel_store as store
import ledger
import runtime_store
import settings
import processes
import tools

logger = logging.getLogger("missioncontrol.watch")

POLL_TICK = 10          # how often the loop wakes to see which watches are due
MIN_INTERVAL = 20       # floor on how often a single watch may poll (seconds)
MAX_INTERVAL = 3600
DEFAULT_INTERVAL = 30
DEFAULT_CHECKS = 40
MAX_CHECKS = 240        # hard ceiling so a watch can never poll forever
POLL_TIMEOUT = 30       # per-poll timeout (seconds)
# The same poll error this many times in a row means the check itself is broken, such as
# a wrong address or a missing login, and waiting out every check would only keep the
# Worker waiting on nothing.
BROKEN_AFTER = 3

# A watch polls the SAME command repeatedly on a timer, so it must be read-only.
# Refuse anything that looks like it changes state; the agent can still use the
# terminal tool for one-off mutating commands.
_DANGER = re.compile(
    r"(\brm\s+-|\bmkfs\b|\bdd\s+if=|\bshutdown\b|\breboot\b|\bkillall\b|:\(\)\s*\{|"
    r"\bgit\s+push\b|\bgit\s+reset\s+--hard\b|\beas\s+submit\b|\bdeploy\b|\bsubmit\b|"
    r"\bpublish\b|--force\b|-X\s*['\"]?(POST|PUT|DELETE|PATCH)|--request\s+(POST|PUT|DELETE|PATCH)|"
    r"\bcurl\b[^|;&]*\s-d\b|\bcurl\b[^|;&]*--data|\brm\b|>\s*/)",
    re.I)


def guard(poll: str, strength: str | None = None) -> str | None:
    """Return a rejection reason if this poll command is not safe to repeat, else None.
    strength is the authority of the work asking: a watch is a shell that runs unattended,
    so it cannot reach what the terminal would hold for that same work."""
    if not poll or not poll.strip():
        return "poll command or URL is required"
    poll = poll.strip()
    if strength == "inferred" and (poll.startswith(("http://", "https://")) or ledger.reaches_network(poll)):
        return ("refused: no person asked for this work, so a watch cannot reach the network except through gh, "
                "which reads GitHub. Report what you would need, or poll with a read-only gh command.")
    if strength in ("inferred", "standing") and ledger.touches_secrets(poll):
        return "refused: no person asked for this work, so a watch cannot touch the owner's keys or private data."
    if poll.startswith(("http://", "https://")):
        return None
    if _DANGER.search(poll) or ledger.is_outward(poll) or ledger.posts_on_github(poll) or tools._blocked(poll):
        return ("a watch polls the same command repeatedly, so it must be read-only; "
                "this looks like it changes state. Point it at a status endpoint or a "
                "read-only command (a GET, gh run view, a status query), and use the "
                "terminal tool for anything that mutates.")
    return None


def _poll_env(keys: bool = False) -> dict:
    env = processes.environment()
    try:
        if keys:
            env.update({k: v for k, v in settings.env_all().items() if v})
        else:
            for name in settings.env_all():
                env.pop(name, None)
    except Exception:
        logger.exception("watch: could not load .env secrets for poll")
    env.pop("CLAUDECODE", None)
    import github
    env.update(github.agent_env("") or {})
    return env


def _holds_keys(w: dict) -> bool:
    """Only a poll for what the owner asked for gets their keys, and only while they are
    still asking. A session that is gone is nobody's say-so."""
    row = store.session_get(w["session"]) if w.get("session") else None
    return bool(row) and row["strength"] == "explicit"


async def _run_poll(poll: str, cwd: str | None = None, keys: bool = False) -> str:
    """Run one poll and return its text output. A bare http(s) URL is fetched;
    anything else is run as a shell command, with the .env secrets if it has keys."""
    poll = poll.strip()
    if poll.startswith(("http://", "https://")):
        import httpx
        async def fetch():
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT, follow_redirects=True) as client:
                async with client.stream("GET", poll) as r:
                    if not r.is_success:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    chunks, size = [], 0
                    async for chunk in r.aiter_text(chunk_size=4000):
                        chunks.append(chunk[:8000 - size])
                        size += len(chunks[-1])
                        if size >= 8000:
                            break
                    return f"HTTP {r.status_code}\n" + "".join(chunks)
        return await asyncio.wait_for(fetch(), POLL_TIMEOUT)
    proc = await processes.create_subprocess_shell(
        poll, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=_poll_env(keys), cwd=os.path.expanduser(cwd or "~"), **processes.spawn_options())
    try:
        out = await processes.capture(proc, POLL_TIMEOUT, 8000)
    except asyncio.TimeoutError:
        raise RuntimeError(f"poll timed out after {POLL_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}: {out[:1000]}")
    return out


# Resolving wakes the agent and stops the watch, and the agent may act on it —
# so a reading has to clear this before it counts. Anything short of it is
# 'pending', which costs one more poll against a ceiling that already exists.
#
# Measured over ten sample polls: unambiguous outputs answer at 0.99-1.00 either
# way, and outputs that say nothing yet answer 'pending' at 1.00, so the two
# populations are nowhere near each other. The only close call was a terse
# success ("Vercel: Ready [2m 14s] Production"), which came back 0.74, 0.79,
# 0.81 and 0.83 on four runs — the answers are not identical between calls, so a
# threshold sitting inside that spread would resolve the same deploy on one poll
# and not the next. 0.7 clears it on every run and is still far above anything
# that has not actually finished.
RESOLVE_P = 0.7


# Poll output is many lines (one per check, one per PR), and agents write
# patterns for it line by line: ^ and $ mean a line, and . has to be able to
# cross a newline for a count like (…){3} to span several lines. Without
# these flags a pattern anchored at ^ could only ever match the first line.
PATTERN_FLAGS = re.MULTILINE | re.DOTALL


def _by_pattern(out: str, done_re: str | None, fail_re: str | None):
    """The regexes an agent supplied. Deterministic, free, and authoritative."""
    if fail_re:
        try:
            if re.search(fail_re, out, PATTERN_FLAGS):
                return "failed"
        except re.error:
            pass
    if done_re:
        try:
            if re.search(done_re, out, PATTERN_FLAGS):
                return "done"
        except re.error:
            pass
    return None


async def _outcome(w: dict, out: str):
    """Decide if this output resolves the watch. Returns 'done', 'failed', or None.

    A pattern match wins outright: it is exact, it costs nothing, and watches
    that predate this read exactly as they always did. A stated goal is the
    fallback for what the pattern could not anticipate — which is the common
    case, since the agent has to write the pattern before the thing has run.
    """
    result = _by_pattern(out, w.get("done_re"), w.get("fail_re"))
    # A watch with a pattern but no stated goal is still named for what it
    # waits on ("acmeshop CI 476/477/479"); when the pattern cannot match the
    # output the agent never saw, that name is the best goal there is, and far
    # better than riding thirty checks to expiry while the owner waits.
    goal = (w.get("goal") or "").strip() or (w.get("name") or "").strip()
    if result or not goal:
        return result
    # The same output cannot mean something new. A watch sitting on 'queued'
    # for twenty checks should ask once, not twenty times. The trade is that an
    # output judged short of the threshold is not reconsidered while it stays
    # byte-identical, so a borderline reading rides to expiry — which still
    # wakes the agent, with this output attached.
    if out.strip() and out.strip() == (w.get("last_out") or "").strip():
        return None
    verdict = await decide.choice(
        {"goal": goal, "poll_command": w.get("poll") or "", "latest_output": out},
        "The watcher is waiting for the goal described in the state. Going by the "
        "latest output only, has it happened yet?",
        {"done": "The goal has been reached: the thing being watched finished, and "
                 "it succeeded.",
         "failed": "The thing being watched finished, but it failed, errored, was "
                   "rejected or was cancelled.",
         "pending": "Still queued, still running, unknown, or the output does not "
                    "say either way. Anything short of finished."})
    if not verdict:
        return None
    picked, probability = verdict
    resolved = picked if picked in ("done", "failed") and probability >= RESOLVE_P else None
    logger.info("watch %s goal check: %s p=%.2f -> %s",
                w.get("id"), picked, probability, resolved or "pending")
    await asyncio.to_thread(
        runtime_store.decision_record, "watch_resolve", w.get("agent") or "",
        {"watch": w.get("id"), "choice": picked, "probability": probability,
         "threshold": RESOLVE_P}, bool(resolved))
    return resolved


async def _resolve(orch, w: dict, status: str, out: str, errors: int = 0):
    if not await asyncio.to_thread(db.finish_watch_poll, w["id"], status, out, errors):
        return
    await _notify(orch, {**w, "status": status, "last_out": out, "errors": errors})


def _task_finished(w: dict) -> bool:
    """The Worker that started the watch has ended, or gone idle with its task closed."""
    if not w.get("session"):
        return False
    row = store.session_get(w["session"])
    if not row or row["status"] == "ended":
        return True
    mission = runtime_store.mission_get(row["mission"]) if row.get("mission") else None
    return row["status"] in ("idle", "failed") and (mission is None or mission["status"] in ("completed", "cancelled", "failed"))


async def _notify(orch, w: dict):
    if w["agent"] not in orch.agents:
        await asyncio.to_thread(db.finish_watch_notice, w["id"], True)
        return
    if not await asyncio.to_thread(db.claim_watch_notice, w["id"]):
        return
    status, out = w["status"], w.get("last_out") or ""
    broken = status == "expired" and (w.get("errors") or 0) >= BROKEN_AFTER
    if broken and await asyncio.to_thread(_task_finished, w):
        # A broken check says nothing about what it watched, and its task is over, so
        # there is nothing to wake it for: it would only be told to carry on with work
        # that is done.
        logger.info("watch %s (%s) stopped on poll errors after its task finished; not waking it", w["id"], w["name"])
        await asyncio.to_thread(db.finish_watch_notice, w["id"], True)
        return
    if broken:
        text = (f"watch '{w['name']}' stopped: its poll failed the same way {w['errors']} times in a "
                f"row, so the check itself is not working (a wrong address, a missing login, or a "
                f"command that cannot run here). It says nothing about the thing you were "
                f"watching.\n\nThe error:\n{out[:3000]}\n\nFind out now, another way that works "
                "(the service's own CLI, say), whether it has finished, then carry on with the "
                "task. Do not start the same watch again.")
    else:
        verb = {"done": "resolved: the thing you were watching succeeded / completed",
                "failed": "resolved as FAILED",
                "expired": f"did not resolve after {w['max_checks']} checks"}[status]
        text = (f"watch '{w['name']}' {verb}.\n\nLatest poll output:\n{out[:3000]}\n\n"
                "Take any obvious follow-up, then report the outcome. This watch has stopped; do "
                "not recreate it just to re-check. If nothing needs saying, reply exactly NO_REPLY.")
    logger.info("watch %s (%s) -> %s", w["id"], w["name"], status)
    try:
        await orch.external_event(w["agent"], "watch", text, mission=w.get("mission"),
                                  session=w.get("session"), thread=f"watch:{w['id']}")
    except BaseException:
        await asyncio.to_thread(db.finish_watch_notice, w["id"], False)
        raise
    await asyncio.to_thread(db.finish_watch_notice, w["id"], True)


async def _tick(orch, broadcast, now: float):
    semaphore = asyncio.Semaphore(4)
    async def announce(wid, phase=None):
        """Publish observations without making UI delivery part of poll success."""
        try:
            record = await asyncio.to_thread(db.get_watch, wid)
            if record:
                fields = ("id", "ts", "agent", "name", "status", "interval", "max_checks", "checks", "last_poll")
                await broadcast({"type": "watch_poll" if phase else "watch_updated",
                                 "phase": phase, "ts": time.time(),
                                 "watch": {key: record[key] for key in fields}})
        except Exception:
            logger.debug("watch telemetry delivery failed", exc_info=True)

    for notice in await asyncio.to_thread(db.pending_watch_notices):
        try:
            await _notify(orch, notice)
        except Exception:
            logger.exception("watch notification will be retried")

    async def poll_one(w):
        if w["agent"] not in orch.agents:
            changed = await asyncio.to_thread(db.finish_watch_poll, w["id"], "cancelled", "Owning agent was removed.")
            await announce(w["id"])
            return changed
        if w["last_poll"] and (now - w["last_poll"]) < w["interval"]:
            return False
        async with semaphore:
            if not await asyncio.to_thread(db.claim_watch, w["id"], w["checks"], now):
                return False
            checks = w["checks"] + 1
            await announce(w["id"], "polling")
            errors = 0
            try:
                keys = await asyncio.to_thread(_holds_keys, w)
                # What a poll prints goes to a model, so it is masked like any tool's output.
                out = harness.redact(await _run_poll(w["poll"], w.get("cwd"), keys))
                result = await _outcome(w, out)
            except Exception as exc:
                # Failed commands / HTTP responses are never success signals.
                out = harness.redact(f"(poll error: {exc.__class__.__name__}: {str(exc)[:300]})")
                result = None
                # A different error starts the count again: only the same one, again and
                # again, says the poll is broken rather than the service having a bad minute.
                errors = (w.get("errors") or 0) + 1 if out == (w.get("last_out") or "") else 1
            try:
                if result or checks >= w["max_checks"] or errors >= BROKEN_AFTER:
                    await _resolve(orch, w, result or "expired", out, errors)
                else:
                    await asyncio.to_thread(db.finish_watch_poll, w["id"], "active", out, errors)
            finally:
                await announce(w["id"])
            return True

    results = await asyncio.gather(*(poll_one(w) for w in await asyncio.to_thread(db.active_watches)),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("watch poll failed: %s", result)
    if any(r is True for r in results):
        await broadcast({"type": "watch_updated"})


async def watch_loop(orch, broadcast):
    await asyncio.sleep(8)
    logger.info("watch loop started (tick %ss)", POLL_TICK)
    while True:
        try:
            if not orch.paused:
                await _tick(orch, broadcast, time.time())
        except Exception:
            logger.exception("watch loop tick failed")
        await asyncio.sleep(POLL_TICK)
