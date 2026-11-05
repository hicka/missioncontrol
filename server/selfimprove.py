"""Agents improving Mission Control itself, without being able to free themselves.

An agent that can edit the code it runs inside can also edit the code that
restrains it, so the whole design is about keeping those two things apart.

The rules, in the order they bite:

1. Off unless the owner turns it on (Settings > Improve). Nothing here runs
   on a default install.
2. Only from a git checkout with a clean tree. A frozen desktop build has no
   repo and cannot self-modify at all, and a dirty tree means the owner has
   work in progress that a proposal would sweep up.
3. The agent never touches the running tree. Work happens in a git worktree
   on its own branch; the live code is untouched until the owner says yes.
4. PROTECTED paths are refused outright. An agent may not edit the terminal
   blocklist, the budget, the ledger, the tool harness, the learning engine,
   this file, or CI — the machinery that says no to it. A proposal that needs
   one of those is a conversation with the owner, not a patch.
5. The change has to be small enough to read. A diff nobody reviews is a diff
   nobody approved.
6. The test suite must pass in the worktree before the owner is even shown the
   proposal. A red proposal is not a proposal.
7. The owner approves in the app, explicitly, having seen the diffstat. There
   are two exceptions, each only once the owner has switched it on: self-repair
   (below) landing its own fixes, and agents approving each other's proposals.
   An agent never approves its own, because an agent can be talked into things
   by what it reads, and a second one has to be talked into the same thing.
8. Applying fast-forwards the branch, restarts, and health-checks. If the app
   does not come back, a detached watchdog rolls the merge back and restarts
   again — because a bad merge takes away the very UI you would fix it from.
9. Nothing is pushed to a remote unless the owner switched pushing on, and
   then only after the app came back up on the change, only as a fast-forward,
   never forced.

Self-repair is the part nobody has to ask for. Some problems are the app's own
fault and no lesson in a prompt can fix them: webdev's replies were cut off at
the output limit turn after turn because the model's thinking used up the
whole allowance, and the fix was a number in llm.py. So the turns the team
runs are read for problems like that (replies cut off, turns crashing the same
way), and one that keeps happening on the code running now is handed to
Claude Code with its evidence, through the same worktree, review and tests as
any proposal. Two attempts that do not make it stop, and it is the owner's.
"""
import asyncio
import processes
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import settings as cfg
from apphome import BUNDLE

logger = logging.getLogger("missioncontrol.selfimprove")

REPO = BUNDLE
WORKTREES = Path("/tmp/mc-improve")

# The machinery that restrains agents. Editing it is the one change that
# cannot be reviewed by the thing being edited.
PROTECTED = (
    "server/selfimprove.py",   # this file: the leash
    "server/tools.py",         # the terminal blocklist
    "server/budget.py",        # the spend cap
    "server/ledger.py",        # once-only outward actions
    "server/harness.py",       # tool permissions, per-turn and per-skill limits
    "server/router.py",        # what only the owner can do, and what a reply may attach
    "server/workers.py",       # what work no person asked for may not reach
    "server/triggers.py",      # who is the owner, and which sources may start anything
    "server/watchdog.py",      # the check on all of the above
    "server/orchestrator.py",  # which commands are held, and who a tool answers to
    "server/session.py",       # what the loop masks before a model reads it
    "server/recall.py",        # what work no person asked for may read back
    "server/github.py",        # who counts as the owner on GitHub, and whose login agents use
    "server/watch.py",         # what may poll unattended, and with whose keys
    "server/cron.py",          # what may run later, unattended
    "server/runtime_store.py", # what is waiting on the owner, and that each answer counts once
    "server/main.py",          # who may call the API, and what answering a held command does
    "server/secret_mask.py",   # what is masked
    "server/decide.py",        # the judgement behind the terminal guard
    "bin/mc-watchdog",         # and the check on that
    "server/evolve.py",        # what agents learn, and the check that a lesson
                               # never loosens a rule: learning cannot edit itself
    "server/mcp_servers.py",   # which MCP tools agents reach, and which wait for the owner
    "server/whatsapp_link.py", # who reads the owner's WhatsApp, and what reaches a model
    "whatsapp/",               # the bridge that can only listen, never send as the owner
    ".github/",                # CI, release signing
    "requirements.txt",        # dependency swaps are a supply-chain change
    "server/selfupdate.py",    # what every installed copy will run next
    "native/macos/Updates.swift",  # and how the signed app takes it
    "scripts/build_macos.py",  # what goes into a signed release
    "scripts/setup_release.sh",  # where the signing keys are sent
    "packaging/",              # entitlements and the frozen runtime's contents
)
PROTECTED_PATTERNS = (
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)\.git/"),
    re.compile(r"(^|/)secrets?/"),
    re.compile(r"\.p8$|\.pem$|\.key$"),
)

MAX_FILES = 12
MAX_LINES = 600          # added + removed across the whole diff
TEST_TIMEOUT = 600
HEALTH_TIMEOUT = 90


def enabled() -> bool:
    return bool((cfg.load().get("selfimprove") or {}).get("enabled", False))


def settings() -> dict:
    s = cfg.load().get("selfimprove") or {}
    return {"enabled": bool(s.get("enabled", False)), "autofix": bool(s.get("autofix", False)),
            "auto_apply": bool(s.get("auto_apply", False)), "push": bool(s.get("push", False)),
            "peer_approve": bool(s.get("peer_approve", False))}


def _git(*args, cwd=None, timeout=120, env=None) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(cwd or REPO), capture_output=True,
                       text=True, timeout=timeout, env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


def readiness() -> str | None:
    """Why self-improvement cannot run right now, or None if it can."""
    import sys
    if getattr(sys, "frozen", False):
        return "Bundled application code requires a rebuild. Run from a source checkout to propose and apply self-improvements."
    if not enabled():
        return ("self-improvement is off; the owner turns it on in "
                "Settings > Improve")
    if not (REPO / ".git").exists():
        return ("Mission Control is not running from a git checkout, so there is "
                "nothing to branch from")
    code, out = _git("status", "--porcelain")
    if code != 0:
        return f"git is unhappy in {REPO}: {out[:200]}"
    if out.strip():
        return ("the working tree has uncommitted changes; a proposal would sweep "
                "them up. Ask the owner to commit or stash first")
    return None


def protected_hits(paths: list[str]) -> list[str]:
    hits = []
    for p in paths:
        if any(p == q or p.startswith(q) for q in PROTECTED) or \
           any(rx.search(p) for rx in PROTECTED_PATTERNS):
            hits.append(p)
    return hits


def _diff_stat(branch: str, cwd: Path) -> dict:
    code, out = _git("diff", "--numstat", f"master...{branch}", cwd=cwd)
    files, added, removed = [], 0, 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, name = parts
        files.append(name)
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    return {"files": files, "added": added, "removed": removed}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "change"


async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = await processes.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", errors="replace")


def _venv_python(root: Path) -> str:
    """The interpreter to test with: the checkout's own venv is not copied into
    a worktree, so borrow the live one."""
    for p in (root / "venv" / "bin" / "python", REPO / "venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    import sys
    return sys.executable


async def run_tests(root: Path) -> tuple[bool, str]:
    code, out = await _run([_venv_python(root), "-m", "pytest", "-q"], root, TEST_TIMEOUT)
    tail = "\n".join(out.strip().splitlines()[-25:])
    return code == 0, tail


def make_worktree(title: str) -> tuple[str, Path]:
    branch = f"improve/{_slug(title)}-{int(time.time())}"
    WORKTREES.mkdir(parents=True, exist_ok=True)
    path = WORKTREES / branch.split("/", 1)[1]
    code, out = _git("worktree", "add", "-b", branch, str(path), "HEAD")
    if code != 0:
        raise RuntimeError(f"could not create a worktree: {out[:300]}")
    return branch, path


def drop_worktree(branch: str, path: str | None = None):
    if path:
        _git("worktree", "remove", "--force", str(path))
    shutil.rmtree(WORKTREES / branch.split("/", 1)[-1], ignore_errors=True)
    _git("worktree", "prune")
    _git("branch", "-D", branch)


def review(branch: str, path: Path, message: str = "proposal") -> dict:
    """What the owner would be asked to approve, and every reason to refuse."""
    code, _ = _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    stat = _diff_stat(branch, path)
    problems = []
    if not stat["files"]:
        problems.append("the change is empty; nothing was modified")
    hits = protected_hits(stat["files"])
    if hits:
        problems.append(
            "touches protected files (" + ", ".join(hits) + "). Those hold the "
            "limits agents run under and cannot be changed by an agent; raise it "
            "with the owner instead")
    if len(stat["files"]) > MAX_FILES:
        problems.append(f"spans {len(stat['files'])} files, over the {MAX_FILES} "
                        f"a proposal may touch")
    total = stat["added"] + stat["removed"]
    if total > MAX_LINES:
        problems.append(f"changes {total} lines, over the {MAX_LINES} line limit; "
                        f"split it into smaller proposals")
    return {**stat, "branch": branch, "problems": problems}


# ---------- applying, and undoing a bad apply ----------

WATCHDOG = r"""#!/bin/bash
# Applied a self-improvement and restarted. If the app does not answer within
# the window, put the code back and restart again: a merge that breaks startup
# also removes the UI the owner would use to undo it.
sleep 3
for i in $(seq 1 {tries}); do
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:{port}/api/state"; then
    echo "healthy after ${{i}} checks" >> {log}
    exit 0
  fi
  sleep 2
done
echo "unhealthy after {tries} checks; rolling back to {prev}" >> {log}
cd {repo} || exit 1
git reset --hard {prev} >> {log} 2>&1
launchctl kickstart -k "gui/$(id -u)/com.missioncontrol" >> {log} 2>&1 || true
"""


def _arm_watchdog(prev_sha: str, port: int) -> Path:
    WORKTREES.mkdir(parents=True, exist_ok=True)
    log = WORKTREES / "watchdog.log"
    script = WORKTREES / "watchdog.sh"
    script.write_text(WATCHDOG.format(tries=HEALTH_TIMEOUT // 2, port=port,
                                      repo=str(REPO), prev=prev_sha, log=str(log)))
    script.chmod(0o755)
    subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return log


async def apply(branch: str, path: str | None) -> dict:
    """Fast-forward the branch onto master, then restart behind a watchdog."""
    block = readiness()
    if block:
        return {"ok": False, "error": block}
    wt = Path(path) if path and Path(path).exists() else None
    if wt:
        ok, tail = await run_tests(wt)
        if not ok:
            return {"ok": False, "error": f"tests fail on the branch now:\n{tail}"}

    code, prev = _git("rev-parse", "HEAD")
    if code != 0:
        return {"ok": False, "error": "could not read HEAD"}
    code, out = _git("merge", "--ff-only", branch)
    if code != 0:
        # never merge-commit our way past a conflict; master moved, so rebase
        # the proposal or drop it
        return {"ok": False, "error": f"cannot fast-forward; master has moved. {out[:200]}"}

    code, new = _git("rev-parse", "HEAD")
    log = _arm_watchdog(prev, cfg.port("app"))
    logger.info("self-improvement %s applied (%s -> %s); watchdog armed at %s",
                branch, prev[:8], new[:8], log)
    return {"ok": True, "from": prev[:8], "to": new[:8], "watchdog": str(log)}


def restart():
    """Hand the process back to launchd, which brings it up on the new code."""
    subprocess.Popen(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.missioncontrol"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- self-repair: finding and fixing its own problems ----------

AUTO = "selfimprove"          # the agent column of a fix no agent asked for
ISSUE_MIN = 3                 # once is bad luck; three times on the same code is a bug
ISSUE_WINDOW = 86400
MAX_FIX_ATTEMPTS = 2          # then it is the owner's
FIXES_PER_DAY = 4
FIX_TICK = 600
SETTLE = 20                   # seconds after startup before trusting that the app is up
# Counting starts with the code that is running now: a problem from before the
# last restart may well be what that restart fixed.
STARTED = time.time()

# A provider down, a plan used up or a network blip fails turns too, and no
# change to this code would stop it.
_NOT_CODE = re.compile(
    r"LLM HTTP (401|402|403|408|429|5\d\d)|usage limit|quota|insufficient|credit|balance|"
    r"billing|plan exhausted|timed? ?out|timeout|connect(ion)? (error|refused|reset)|"
    r"overloaded|temporarily unavailable|No model configured", re.I)
_NOISE = re.compile(r"\b[0-9a-f]{7,40}\b|\b\d+(\.\d+)*\b")

_lock = asyncio.Lock()
last_tick: dict = {"ts": None, "result": "not run yet"}


def _error_key(error: str) -> str:
    lines = (error or "").strip().splitlines()
    return _NOISE.sub("#", lines[0]).strip()[:140] if lines else ""


def _fix_rows(limit: int = 200) -> list[dict]:
    import db
    return [r for r in db.list_improvements(None, limit) if (r.get("stat") or {}).get("issue")]


def _model_line(agent: str, agent_llm) -> str:
    import llm
    try:
        r = llm._resolved(agent_llm(agent) if agent_llm else None)
        return (f"{agent}: {r['provider']}/{r['model']}, effort {r['effort']}, "
                f"max_tokens {r['max_tokens']} as sent")
    except Exception:
        return f"{agent}: model unknown"


def _recent_outputs(agent: str, since: float) -> str:
    import db
    with db._lock:
        db._ensure_usage()
        rows = db._db().execute(
            "SELECT model, out_tokens FROM usage WHERE agent=? AND ts>? ORDER BY ts DESC LIMIT 12",
            (agent, since)).fetchall()
    return ", ".join(str(r[1]) for r in rows) if rows else "none recorded"


def detect(agent_llm=None, now: float | None = None) -> list[dict]:
    """Recurring problems that are Mission Control's own fault, worst first.

    agent_llm(aid) gives an agent's model override, so the evidence says what
    each agent actually sent rather than the team default."""
    import db
    import evolve
    now = now or time.time()
    floor = max(now - ISSUE_WINDOW, STARTED)
    with db._lock:
        c = evolve._ensure()
        turns = evolve._rows(c.execute(
            "SELECT * FROM turns WHERE ts>? AND ended IS NOT NULL "
            "AND (outcome='failed' OR flags LIKE '%cut_off%') ORDER BY ts", (floor,)))
    attempted = {}
    for r in _fix_rows():
        sig = r["stat"]["issue"]
        attempted[sig] = max(attempted.get(sig, 0), r["ts"])

    groups: dict[str, list] = {}
    for t in turns:
        if "cut_off" in (t.get("flags") or []):
            groups.setdefault("cut_off", []).append(t)
        if t["outcome"] == "failed":
            key = _error_key(t.get("error") or "")
            if key and not _NOT_CODE.search(t.get("error") or ""):
                groups.setdefault(f"failed:{key}", []).append(t)

    found = []
    for sig, rows in groups.items():
        # After an attempt, only what happened since counts against it.
        rows = [t for t in rows if t["ts"] > attempted.get(sig, 0)]
        if len(rows) < ISSUE_MIN:
            continue
        agents: dict[str, int] = {}
        for t in rows:
            agents[t["agent"]] = agents.get(t["agent"], 0) + 1
        since = time.strftime("%H:%M", time.localtime(rows[0]["ts"]))
        if sig == "cut_off":
            title = "Replies cut off at the output limit"
            evidence = (
                f"{len(rows)} agent turns since {since} had a model reply stop at the output "
                f"limit (stop_reason max_tokens). The turn loop in server/session.py then "
                f"asks the model for smaller steps, and after two more cut-offs gives up and "
                f"tells the owner to say continue, so the work stalls.\n"
                f"Turns cut off, by agent: " + ", ".join(f"{a} {n}" for a, n in agents.items()) + "\n"
                f"Model settings:\n  " + "\n  ".join(_model_line(a, agent_llm) for a in agents) + "\n"
                f"Output tokens of each agent's latest model calls, newest first:\n  "
                + "\n  ".join(f"{a}: {_recent_outputs(a, floor)}" for a in agents))
        else:
            errors = []
            for t in reversed(rows):
                if t["error"] not in errors:
                    errors.append(t["error"])
            title = f"Turns fail with: {sig[7:90]}"
            evidence = (
                f"{len(rows)} agent turns since {since} ended in an exception instead of an "
                f"answer: " + ", ".join(f"{a} {n}" for a, n in agents.items()) + ".\n"
                f"The error, as the owner saw it:\n  " + "\n  ".join(e[:400] for e in errors[:3]))
        found.append({"signature": sig, "title": title, "count": len(rows), "agents": agents,
                      "first": rows[0]["ts"], "last": rows[-1]["ts"], "evidence": evidence})
    return sorted(found, key=lambda i: -i["count"])


IN_FLIGHT = ("fixing", "pending")


def issues(agent_llm=None, now: float | None = None) -> list[dict]:
    """What detect() found, with where each one stands."""
    found = detect(agent_llm, now)
    rows = _fix_rows()
    for i in found:
        tries = [r for r in rows if r["stat"]["issue"] == i["signature"]]
        live = next((r for r in tries if r["status"] in IN_FLIGHT), None)
        i["attempts"], i["fix"] = len(tries), (live or (tries[0] if tries else {})).get("id")
        i["state"] = (live["status"] if live else
                      "gave_up" if len(tries) >= MAX_FIX_ATTEMPTS else "open")
    return found


def _busy(orch) -> list[str]:
    return orch.workers.running()


def _log_path() -> str:
    from apphome import ROOT
    for name in ("missioncontrol.log", "runtime.log"):
        if (ROOT / name).exists():
            return str(ROOT / name)
    return ""


def brief_for(issue: dict, path: Path) -> str:
    log = _log_path()
    return (
        "You are fixing a bug in Mission Control itself, in an isolated git worktree of its "
        f"repo (your working directory). The live app runs from {REPO}; change nothing there "
        "or anywhere outside this worktree.\n\n"
        f"PROBLEM: {issue['title']}\n\n{issue['evidence']}\n\n"
        + (f"The live server log, with tracebacks, is {log}. Read it if it helps; do not edit it.\n\n" if log else "")
        + "What to do:\n"
        "- Find the root cause in the code and fix that, not the symptom: the goal is that "
        "this stops happening, not that it is reported differently.\n"
        "- If the cause is not in the code (a provider outage, an account limit, a setting "
        "only the owner should change), or fixing it needs a file listed below, change "
        "nothing and say why in one short paragraph.\n"
        "- Keep the change as small as it can be and still be right. Match the surrounding "
        "style; this codebase comments the why, not the what.\n"
        "- Add or update a test that fails without your change.\n"
        f"- Run `{_venv_python(path)} -m pytest -q` and leave it green.\n"
        f"- Do NOT edit {', '.join(PROTECTED)}, secrets or .env files. Those are the limits "
        "agents run under, and a change that touches them is refused.\n"
        "- Do not commit, push, or touch git remotes. Just leave the edits.\n"
        "- End with two or three plain sentences: what the cause was and what you changed."
    )


async def _emit(broadcast):
    if broadcast:
        try:
            await broadcast({"type": "improvements_updated"})
        except Exception:
            pass


async def _tell_owner(orch, text: str):
    lead = next(iter(getattr(orch, "agents", {}) or {}), None)
    if lead:
        try:
            await orch._post(f"dm:{lead}", "improve", text, kind="sys")
        except Exception:
            logger.exception("could not post the self-repair notice")


async def fix(orch, issue: dict, broadcast=None) -> int:
    """Write, review and test a fix for one problem. It ends pending, waiting
    to be landed, or discarded with the reason."""
    import db
    import tools
    title = issue["title"][:90]
    branch, path = await asyncio.to_thread(make_worktree, title)
    iid = await asyncio.to_thread(db.add_improvement, AUTO, title, issue["evidence"], branch,
                                  str(path), {"issue": issue["signature"]})
    await asyncio.to_thread(db.update_improvement, iid, status="fixing",
                            note="Claude Code is looking for the cause")
    await _emit(broadcast)
    logger.info("self-repair %s: fixing %s on %s", iid, issue["signature"], branch)
    shown = {"t": 0.0}

    async def progress(line: str):
        # The row is the live view of the run; a write per step is plenty.
        if time.time() - shown["t"] < 3:
            return
        shown["t"] = time.time()
        await asyncio.to_thread(db.update_improvement, iid, note=line[:300])
        await _emit(broadcast)

    async def discard(why: str) -> int:
        await asyncio.to_thread(drop_worktree, branch, str(path))
        await asyncio.to_thread(db.update_improvement, iid, status="discarded", note=why[:1500])
        await _emit(broadcast)
        logger.info("self-repair %s discarded: %s", iid, why[:200])
        return iid

    try:
        out = await tools.claude_code(brief_for(issue, path), str(path), str(path), progress)
    except Exception as exc:
        return await discard(f"Claude Code failed: {type(exc).__name__}: {str(exc)[:300]}")
    summary = (out or "").strip()[-700:]
    message = f"{title}\n\n{summary or issue['evidence'][:700]}\n\nFound and fixed by Mission Control's self-repair."
    result = await asyncio.to_thread(review, branch, path, message)
    if result["problems"]:
        return await discard("; ".join(result["problems"]) + (f"\n\nClaude Code said: {summary}" if summary else ""))
    await asyncio.to_thread(db.update_improvement, iid, note="running the test suite")
    await _emit(broadcast)
    ok, tail = await run_tests(path)
    if not ok:
        return await discard(f"the fix does not pass the tests:\n{tail[-1000:]}")
    stat = {k: result[k] for k in ("files", "added", "removed")}
    await asyncio.to_thread(db.update_improvement, iid, status="pending",
                            stat={**stat, "issue": issue["signature"]},
                            note=summary or "tests green")
    await _emit(broadcast)
    if not settings()["auto_apply"]:
        await _tell_owner(orch, f"Self-repair has a tested fix for “{title}” waiting on you "
                                f"in Settings > Improve.")
    return iid


async def land(iid: int, broadcast=None, orch=None) -> dict:
    """Apply a tested proposal and restart onto it behind the watchdog. With
    pushing on, the push happens after the restart, once the app is back."""
    import db
    row = await asyncio.to_thread(db.get_improvement, iid)
    if not row or row["status"] not in ("pending", "approved"):
        return {"ok": False, "error": "no pending proposal with that id"}
    res = await apply(row["branch"], row["worktree"])
    if not res.get("ok"):
        await asyncio.to_thread(db.update_improvement, iid, status="failed",
                                note=res.get("error", "")[:500])
        await _emit(broadcast)
        return res
    push = settings()["push"]
    await asyncio.to_thread(
        db.update_improvement, iid, status="applied",
        stat={**row["stat"], "from": res["from"], "to": res["to"], "push": "pending" if push else "off"},
        note=f"{res['from']} -> {res['to']}" + ("; pushes to GitHub once the app is back up" if push else ""))
    await asyncio.to_thread(drop_worktree, row["branch"], row["worktree"])
    await _emit(broadcast)
    logger.info("restarting to pick up improvement %s", iid)
    await asyncio.to_thread(restart)
    return {**res, "note": "restarting; if the app does not come back the change is "
                           "rolled back automatically"}


def _name(orch, aid: str) -> str:
    return ((getattr(orch, "agents", {}) or {}).get(aid) or {}).get("name") or aid


def approve_by_agent(iid: int, aid: str) -> str:
    """An agent approving a teammate's proposal, where the owner lets them. It lands
    at the next check that finds nothing running, as an owner's approval would."""
    import db
    if not settings()["peer_approve"]:
        return ("refused: the owner approves proposals themselves. Agents approving each other's "
                "is a switch in Settings > Improve that is off")
    row = db.get_improvement(iid)
    if not row or row["status"] != "pending":
        return "refused: no proposal with that id is waiting for approval"
    if row["agent"] == aid:
        return "refused: a proposal needs someone other than its author to approve it"
    db.update_improvement(iid, status="approved", stat={**(row.get("stat") or {}), "approved_by": aid},
                          note=f"approved by {aid}; lands once nothing is running")
    logger.info("improvement %s approved by %s", iid, aid)
    return (f"approved [{iid}]. It lands at the next check that finds no task running, "
            "behind the rollback watchdog, and is pushed if the owner allows pushing")


def reject_by_agent(iid: int, aid: str, why: str) -> str:
    """Binning a proposal. Its author may always withdraw it; a teammate only where
    the owner lets agents decide on each other's work."""
    import db
    row = db.get_improvement(iid)
    if not row or row["status"] not in ("pending", "approved"):
        return "refused: no proposal with that id is waiting"
    if row["agent"] != aid and not settings()["peer_approve"]:
        return "refused: only its author or the owner can turn this proposal down"
    drop_worktree(row["branch"], row["worktree"])
    db.update_improvement(iid, status="rejected", note=f"turned down by {aid}: {why}"[:1500])
    return f"proposal [{iid}] turned down and its branch discarded"


async def _land_approved(orch, broadcast) -> str | None:
    """Land what an agent approved, once no task is running. None when nothing is."""
    import db
    rows = await asyncio.to_thread(db.list_improvements, "approved", 20)
    if not rows:
        return None
    busy = _busy(orch)
    if busy:
        return f"an approved change lands when {', '.join(busy)} finish"
    res = await land(rows[-1]["id"], broadcast, orch)
    return "landed an approved change, restarting" if res.get("ok") else \
        f"could not land an approved change: {res.get('error', '')[:200]}"


def push_master() -> tuple[bool, str]:
    """Fast-forward the remote to what is running. Never forced: if the remote
    has commits this checkout lacks, that is the owner's to sort out."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    code, upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    if code != 0 or "/" not in upstream:
        return False, "this branch has no upstream to push to"
    remote, branch = upstream.split("/", 1)
    code, out = _git("fetch", "-q", remote, timeout=60, env=env)
    if code != 0:
        return False, f"could not reach {remote}: {out[:200]}"
    code, _ = _git("merge-base", "--is-ancestor", upstream, "HEAD")
    if code != 0:
        return False, (f"{upstream} has commits this checkout does not, so it was not pushed; "
                       f"pull them in and push by hand")
    code, out = _git("push", remote, f"HEAD:{branch}", timeout=120, env=env)
    return code == 0, out[-300:] if code else f"pushed to {upstream}"


async def _answers() -> bool:
    """The watchdog's own test of health: the app answers HTTP at all."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.get(f"http://127.0.0.1:{cfg.port('app')}/api/state")
        return True
    except Exception:
        return False


async def after_restart(orch=None, broadcast=None):
    """Settle what the last process left half-done: a fix the restart cut
    short, and a landed change that is still to be pushed."""
    import db
    for r in await asyncio.to_thread(db.list_improvements, "fixing", 20):
        await asyncio.to_thread(drop_worktree, r["branch"], r["worktree"])
        await asyncio.to_thread(db.update_improvement, r["id"], status="discarded",
                                note="interrupted by a restart before it finished")
    for r in await asyncio.to_thread(db.list_improvements, "applied", 20):
        st = r["stat"] or {}
        if st.get("push") != "pending":
            continue
        _, head = await asyncio.to_thread(_git, "rev-parse", "HEAD")
        if not head.startswith(st.get("to") or "-"):
            await asyncio.to_thread(
                db.update_improvement, r["id"], status="rolled_back", stat={**st, "push": "off"},
                note="the app did not come back on this change, so the watchdog put the previous code back")
            await _tell_owner(orch, f"“{r['title']}” was rolled back: the app did not come back on it.")
            continue
        if not await _answers():
            # Running but not serving: the watchdog may still roll this back,
            # so it stays pending for the next start to look at.
            continue
        ok, out = await asyncio.to_thread(push_master)
        await asyncio.to_thread(db.update_improvement, r["id"], status="pushed" if ok else "applied",
                                stat={**st, "push": "done" if ok else "failed"},
                                note=f"{st.get('from')} -> {st.get('to')}; " + out)
        logger.info("improvement %s push: %s", r["id"], out)
        if r["agent"] == AUTO:
            await _tell_owner(orch, f"Self-repair landed “{r['title']}” ({st.get('to')})"
                                    + (", and pushed it." if ok else f". Not pushed: {out}"))
        elif st.get("approved_by"):
            await _tell_owner(orch, f"{_name(orch, r['agent'])}'s change “{r['title']}”, approved by "
                                    f"{_name(orch, st['approved_by'])}, landed ({st.get('to')})"
                                    + (", and was pushed." if ok else f". Not pushed: {out}"))
    await _emit(broadcast)


async def tick(orch, broadcast=None, now: float | None = None) -> str:
    """One pass: land a tested fix when the team is idle, or else start on the
    worst open problem. Returns what it did, for the log and the UI."""
    if _lock.locked():
        return "already running"
    async with _lock:
        result = await _tick(orch, broadcast, now)
    last_tick.update(ts=time.time(), result=result)
    return result


async def _tick(orch, broadcast, now) -> str:
    s = settings()
    if s["enabled"] and s["peer_approve"]:
        block = await asyncio.to_thread(readiness)
        landed = None if block else await _land_approved(orch, broadcast)
        if landed:
            return landed
    if not (s["enabled"] and s["autofix"]):
        return "off"
    block = await asyncio.to_thread(readiness)
    if block:
        return f"cannot run: {block}"
    rows = await asyncio.to_thread(_fix_rows)
    if any(r["status"] == "fixing" for r in rows):
        return "a fix is already being written"
    ready = [r for r in rows if r["status"] == "pending"]
    if ready:
        if not s["auto_apply"]:
            return "a tested fix is waiting on you"
        busy = _busy(orch)
        if busy:
            return f"a tested fix lands when {', '.join(busy)} finish"
        res = await land(ready[-1]["id"], broadcast, orch)
        return "landed, restarting" if res.get("ok") else f"could not land: {res.get('error', '')[:200]}"
    midnight = time.mktime(time.localtime(now or time.time())[:3] + (0, 0, 0, 0, 0, -1))
    if sum(1 for r in rows if r["ts"] >= midnight) >= FIXES_PER_DAY:
        return f"stopped for today after {FIXES_PER_DAY} fixes"
    agent_llm = getattr(orch, "agent_llm", None)
    found = await asyncio.to_thread(issues, agent_llm, now)
    todo = [i for i in found if i["state"] == "open"]
    for i in found:
        if i["state"] == "gave_up":
            last = next(r for r in rows if r["stat"]["issue"] == i["signature"])
            if not last["stat"].get("owner_told"):
                await asyncio.to_thread(db_update_stat, last, owner_told=True)
                await _tell_owner(orch, f"Self-repair tried {i['attempts']} fixes for “{i['title']}” "
                                        f"and it is still happening ({i['count']} times). It needs you; "
                                        f"the evidence is in Settings > Improve.")
    if not todo:
        return "nothing to fix" if not found else "nothing it can fix on its own"
    try:
        iid = await fix(orch, todo[0], broadcast)
    except RuntimeError as exc:      # no worktree, so nothing was started
        return f"could not start a fix: {str(exc)[:200]}"
    return f"worked on “{todo[0]['title']}” (#{iid})"


def db_update_stat(row: dict, **extra):
    import db
    db.update_improvement(row["id"], stat={**(row.get("stat") or {}), **extra})


async def loop(orch, broadcast=None):
    await asyncio.sleep(SETTLE)
    try:
        await after_restart(orch, broadcast)
    except Exception:
        logger.exception("self-repair could not settle the last restart")
    logger.info("self-repair loop started (tick %ss)", FIX_TICK)
    while True:
        await asyncio.sleep(FIX_TICK)
        try:
            result = await tick(orch, broadcast)
            if result not in ("off", "nothing to fix"):
                logger.info("self-repair: %s", result)
        except Exception:
            logger.exception("self-repair tick failed")
