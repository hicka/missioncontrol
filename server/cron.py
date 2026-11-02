"""Scheduled agent jobs, owner-approved.

Agents create jobs with the cron tool; every job starts as 'pending' and
runs only after the owner approves it (Settings > Jobs). The scheduler
fires due jobs as automated event lines in the agent's chat, so runs are
visible and the agent replies NO_REPLY on quiet ticks.

Schedule grammar (validated on create):
  every N minutes | every N hours | every N days
  daily HH:MM
  weekly mon|tue|wed|thu|fri|sat|sun HH:MM
  once YYYY-MM-DD HH:MM
  standard 5-field cron: "m h dom mon dow" with * , - */n
"""
import asyncio
import processes
import datetime
import logging
import re
import time

import db
import settings as cfg

logger = logging.getLogger("missioncontrol.cron")


# A transient "watch until it resolves" is not a scheduled job; it belongs on the
# watch tool (background poll, no approval). Detect that shape so cron can redirect.
_WATCH_INTENT = re.compile(
    r"\b(until it|to completion|once it (finishes|completes|is done)|"
    r"when it (finishes|completes|is done|lands|deploys|turns green|passes|fails)|"
    r"report when|hold(s)? for|then deploy|then merge|poll(ing)? until)\b"
    r"|\bwatch\b[^.]*\b(deploy|ci|run|pipeline|build|release)\b"
    r"|\bmonitor\b[^.]*\b(to completion|until|deploy)\b", re.I)


# The regex above names the phrasings somebody listed. "follow run 88123 and ping
# me with the conclusion" is not among them and never will be, because there is
# no end to how a bounded task can be worded.
#
# Measured over fourteen tasks on a short interval: genuinely recurring work sits
# at 0.05-0.07 with no spread at all, while bounded follow-until-done tasks run
# 0.56-0.93. 0.35 sits in a band that wide with room on both sides, which matters
# here because a false positive refuses a legitimate recurring job.
WATCH_INTENT_P = 0.35
_INTENT_QUESTION = (
    "Is this task following ONE thing that is already in flight, waiting for it to "
    "reach an end state so it can report once and stop? Or is it ongoing work that "
    "should keep running on a schedule indefinitely?")
_INTENT_CRITERIA = {
    "true": "One thing in flight; it finishes, gets reported once, and there is "
            "nothing left to do. A deploy, a build, a review, a migration.",
    "false": "Recurring work with no end: it should still be running next week, "
             "doing the same job again each time."}


async def looks_like_watch(schedule: str, task: str) -> bool:
    """True for a bounded 'follow X until it finishes' task on a short interval,
    which should use the watch tool rather than a scheduled cron job.

    A long interval is not a watch whatever it says, so that stays a cheap local
    check and nothing leaves the machine for a daily job. The pattern list keeps
    first say; the judgement only covers wordings it does not contain.
    """
    import decide
    import runtime_store
    short = bool(re.match(r"every\s+\d+\s+(second|minute|min|hour)", (schedule or "").strip(), re.I))
    if not short:
        return False
    if _WATCH_INTENT.search(task or ""):
        return True
    probability = await decide.noul({"task": task or "", "schedule": schedule or ""},
                                    _INTENT_QUESTION, _INTENT_CRITERIA)
    if probability is None:
        return False          # unreachable: the pattern list stands on its own
    redirect = probability >= WATCH_INTENT_P
    await asyncio.to_thread(
        runtime_store.decision_record, "watch_intent", "",
        {"task": (task or "")[:200], "probability": probability,
         "threshold": WATCH_INTENT_P}, redirect)
    return redirect

MIN_INTERVAL_MINUTES = 5
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int] | None:
    """One cron field -> allowed set, or None for '*' (any)."""
    if field == "*":
        return None
    out: set[int] = set()
    for part in field.split(","):
        m = re.fullmatch(r"\*/(\d+)", part)
        if m:
            step = int(m.group(1))
            if step < 1:
                raise ValueError(f"bad step in '{field}'")
            out.update(range(lo, hi + 1, step))
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if not (lo <= a <= b <= hi):
                raise ValueError(f"range out of bounds in '{field}'")
            out.update(range(a, b + 1))
            continue
        if not part.isdigit() or not (lo <= int(part) <= hi):
            raise ValueError(f"bad value '{part}' in cron field")
        out.add(int(part))
    return out


def validate(schedule: str) -> str:
    """Normalize + validate; raises ValueError with a helpful message."""
    s = " ".join(schedule.strip().lower().split())
    m = re.fullmatch(r"every (\d+) (minutes?|hours?|days?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        minutes = n * {"minute": 1, "hour": 60, "day": 1440}[unit]
        if minutes < MIN_INTERVAL_MINUTES:
            raise ValueError(f"minimum interval is {MIN_INTERVAL_MINUTES} minutes")
        return s
    if re.fullmatch(r"daily \d{1,2}:\d{2}", s):
        _hhmm(s.split()[1])
        return s
    m = re.fullmatch(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d{1,2}:\d{2})", s)
    if m:
        _hhmm(m.group(2))
        return s
    m = re.fullmatch(r"once (\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2})", s)
    if m:
        dt = datetime.datetime.fromisoformat(f"{m.group(1)} {_hhmm(m.group(2))}")
        if dt.timestamp() < time.time() - 60:
            raise ValueError("that time is in the past")
        return s
    fields = s.split()
    if len(fields) == 5:
        for f, (lo, hi) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]):
            _parse_cron_field(f, lo, hi)
        return s
    raise ValueError(
        "unrecognized schedule; use 'every N minutes/hours/days', 'daily HH:MM', "
        "'weekly mon HH:MM', 'once YYYY-MM-DD HH:MM', or a 5-field cron expression")


def _hhmm(t: str) -> str:
    h, mi = t.split(":")
    if not (0 <= int(h) <= 23 and 0 <= int(mi) <= 59):
        raise ValueError(f"bad time '{t}'")
    return f"{int(h):02d}:{mi}"


def next_run(schedule: str, after: float | None = None) -> float | None:
    """Next fire time (epoch) strictly after `after`; None when finished."""
    s = " ".join(schedule.strip().lower().split())
    base = after if after is not None else time.time()

    m = re.fullmatch(r"every (\d+) (minutes?|hours?|days?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        return base + n * {"minute": 60, "hour": 3600, "day": 86400}[unit]

    dt = datetime.datetime.fromtimestamp(base)
    if s.startswith("daily "):
        h, mi = map(int, s.split()[1].split(":"))
        cand = dt.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand.timestamp() <= base:
            cand += datetime.timedelta(days=1)
        return cand.timestamp()
    if s.startswith("weekly "):
        _, day, hhmm = s.split()
        h, mi = map(int, hhmm.split(":"))
        cand = dt.replace(hour=h, minute=mi, second=0, microsecond=0)
        ahead = (DAYS[day] - cand.weekday()) % 7
        cand += datetime.timedelta(days=ahead)
        if cand.timestamp() <= base:
            cand += datetime.timedelta(days=7)
        return cand.timestamp()
    if s.startswith("once "):
        _, d, t = s.split()
        ts = datetime.datetime.fromisoformat(f"{d} {t}").timestamp()
        return ts if ts > base else None

    # 5-field cron: scan forward minute by minute (bounded)
    fields = s.split()
    mins, hrs, doms, mons, dows = (
        _parse_cron_field(f, lo, hi) for f, (lo, hi)
        in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]))
    cand = (dt + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(527040):  # one year of minutes
        ok = ((mins is None or cand.minute in mins)
              and (hrs is None or cand.hour in hrs)
              and (mons is None or cand.month in mons)
              and (doms is None or cand.day in doms)
              and (dows is None or (cand.weekday() + 1) % 7 in dows))
        if ok:
            return cand.timestamp()
        cand += datetime.timedelta(minutes=1)
    return None


async def _run_monitor(cmd: str) -> str:
    """Cheap pre-check for monitor jobs: a URL GET or a shell command."""
    cmd = cmd.strip()
    if cmd.startswith(("http://", "https://")):
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(cmd)
            return f"HTTP {r.status_code}\n" + r.text[:4000]
    proc = await processes.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        return await processes.capture(proc, 60, 4000)
    except asyncio.TimeoutError:
        return "(monitor command timed out after 60s)"


async def _fire_job(orch, broadcast, job: dict, now: float):
    aid = job["agent"]
    runs = (job.get("runs") or 0) + 1
    max_runs = job.get("max_runs") or 0
    nxt = next_run(job["schedule"], now)
    capped = max_runs > 0 and runs >= max_runs   # this fire is the last allowed one
    if capped:
        nxt = None
    db.update_cron(job["id"], last_run=now, next_run=nxt, runs=runs,
                   **({} if nxt else {"status": "done"}))

    monitor_block = ""
    if job.get("monitor_cmd"):
        try:
            out = await _run_monitor(job["monitor_cmd"])
        except Exception as exc:
            out = f"(monitor failed: {exc.__class__.__name__}: {str(exc)[:200]})"
        # It goes to a model, so it is masked like any tool's output.
        import harness
        out = harness.redact(out)
        if out.strip() == (job.get("monitor_last") or "").strip() and not capped:
            logger.info("cron job %s (%s): monitor unchanged, skipping the model",
                        job["id"], job["name"])
            await broadcast({"type": "cron_updated"})
            return
        db.update_cron(job["id"], monitor_last=out)
        prev = (job.get("monitor_last") or "").strip()
        monitor_block = ("\nMONITOR OUTPUT (changed since last run):\n" + out.strip()
                         + ("\n\nPREVIOUS OUTPUT:\n" + prev[:1200] if prev else ""))

    note_block = ""
    if job.get("note"):
        note_block = ("\nYOUR NOTEPAD from previous runs (update it with the cron "
                      "tool, action=note): " + job["note"])

    # Any job can retire itself the moment its purpose is served, so a watch job
    # stops on the first success/failure instead of polling out the clock.
    resolve_block = (
        f"\nThis is job [{job['id']}]. If it was waiting for something and that has now "
        "happened (a deploy finished, a PR merged, a check passed or failed, a condition "
        f"met), report the result to the owner and call cron(action='resolve', "
        f"job_id={job['id']}) so it stops now instead of running again. For a genuinely "
        "recurring job, just do the work.")
    attempt_block = f" (check {runs} of {max_runs})" if max_runs else ""
    final_block = (
        "\nThis is the FINAL scheduled check for this job; it stops automatically after "
        "this run, so give the owner a conclusive answer." if capped else "")

    logger.info("cron firing job %s (%s) for %s%s", job["id"], job["name"], aid,
                f" [{runs}/{max_runs}]" if max_runs else "")
    text = (f"scheduled job '{job['name']}' fired{attempt_block}. Task: {job['task']}"
            f"{monitor_block}{note_block}{resolve_block}{final_block}\n"
            "Do the work now with your tools. Report to the owner only if "
            "something needs attention; otherwise reply exactly NO_REPLY.")
    await orch.external_event(aid, "cron", text, thread=f"cron:{job['id']}")
    await broadcast({"type": "cron_updated"})


async def scheduler(orch, broadcast):
    """Fire due approved jobs as automated events in the agent's chat."""
    await asyncio.sleep(15)
    while True:
        try:
            now = time.time()
            # A job due during a pause fires once it is over, not once per missed slot.
            for job in [] if orch.paused else db.due_cron(now):
                if job["agent"] not in orch.agents:
                    db.update_cron(job["id"], status="paused")
                    continue
                await _fire_job(orch, broadcast, job, now)
        except Exception:
            logger.exception("cron scheduler tick failed")
        await asyncio.sleep(30)


HEARTBEAT_TEMPLATE = """# Heartbeat

The lead agent reads this list on every pulse and acts on anything that
needs it. Edit freely; keep items short. The agent replies NO_REPLY when
all is quiet, so pulses cost little and say nothing unless it matters.

- Anything urgent in recent conversations that was promised but not delivered?
- Any scheduled job, deploy, or watched system that looks stuck or failed?
- Anything in team memory marked as waiting on the owner that is now overdue?
"""


def heartbeat_settings() -> dict:
    h = cfg.load().get("heartbeat") or {}
    return {"enabled": bool(h.get("enabled")),
            "agent": h.get("agent") or "",
            "every_minutes": max(5, int(h.get("every_minutes") or 30))}


def heartbeat_file():
    from apphome import ROOT
    f = ROOT / "HEARTBEAT.md"
    if not f.exists():
        f.write_text(HEARTBEAT_TEMPLATE)
    return f


def _last_pulse_file():
    from apphome import ROOT
    return ROOT / "heartbeat.last"


def load_last_pulse() -> float:
    try:
        return float(_last_pulse_file().read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def save_last_pulse(ts: float):
    try:
        _last_pulse_file().write_text(f"{ts:.0f}")
    except OSError:
        logger.exception("could not record the heartbeat time")


def pulse_due(last_fire: float, every_minutes: int, now: float | None = None) -> bool:
    return (now or time.time()) - last_fire >= every_minutes * 60


async def heartbeat_loop(orch):
    """OpenClaw-style pulse: the lead agent proactively reviews a checklist.
    The last pulse time is kept on disk so a server restart does not
    trigger an extra pulse; the cadence simply continues."""
    await asyncio.sleep(60)
    last_fire = load_last_pulse()
    while True:
        try:
            h = heartbeat_settings()
            if h["enabled"] and not orch.paused and pulse_due(last_fire, h["every_minutes"]):
                aid = h["agent"] if h["agent"] in orch.agents else next(iter(orch.agents))
                checklist = heartbeat_file().read_text()[:4000]
                last_fire = time.time()
                save_last_pulse(last_fire)
                await orch.external_event(aid, "heartbeat",
                    "heartbeat pulse. Review this checklist against what you know "
                    "and can check cheaply. Only act or speak if something needs "
                    "it; otherwise reply exactly NO_REPLY.\n\n" + checklist)
        except Exception:
            logger.exception("heartbeat failed")
        await asyncio.sleep(60)


# ---------- daily brief: a proactive digest, not a silent checklist ----------

BRIEF_PROMPT = (
    "Compile the owner's daily brief. Look at what happened since yesterday: GitHub "
    "activity on watched repos (PRs opened/merged, new issues, CI), messages that came in "
    "on channels, scheduled job outcomes, and anything the team flagged or is waiting on "
    "the owner for. Use your tools to check what you are unsure of. Write it as a short, "
    "scannable brief, five to eight lines, most important first, and make that your "
    "report (it reaches their phone). If genuinely nothing of note happened, "
    "say so in one line. This is proactive: the owner did not ask, so be worth the "
    "interruption or be brief."
)


def brief_settings() -> dict:
    b = cfg.load().get("brief") or {}
    return {"enabled": bool(b.get("enabled")),
            "agent": b.get("agent") or "",
            "at": b.get("at") or "08:00"}


def _brief_file():
    from apphome import ROOT
    return ROOT / "brief.last"


def _brief_due(now, last_date: str, at: str) -> bool:
    """True once per day, at or after the configured local time."""
    hh, mm = (int(x) for x in at.split(":"))
    if now.strftime("%Y-%m-%d") == last_date:
        return False
    return (now.hour, now.minute) >= (hh, mm)


async def brief_loop(orch):
    import datetime
    await asyncio.sleep(90)
    try:
        last = _brief_file().read_text().strip()
    except OSError:
        last = ""
    while True:
        try:
            b = brief_settings()
            now = datetime.datetime.now()
            if b["enabled"] and not orch.paused and _brief_due(now, last, b["at"]):
                aid = b["agent"] if b["agent"] in orch.agents else next(iter(orch.agents))
                last = now.strftime("%Y-%m-%d")
                try:
                    _brief_file().write_text(last)
                except OSError:
                    pass
                await orch.external_event(aid, "brief", BRIEF_PROMPT)
        except Exception:
            logger.exception("brief loop failed")
        await asyncio.sleep(60)
