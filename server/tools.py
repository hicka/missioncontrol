"""Real tool executors: terminal and Claude Code delegation."""
import asyncio
import json
import os
import sys
import re
from pathlib import Path
import processes

TERMINAL_TIMEOUT = 180
JOB_TIMEOUT = 1800   # a command that moved to the background is not holding anyone up
CLAUDE_TIMEOUT = 1800
OUTPUT_CAP = 8000

BLOCKED = [
    r"\bsudo\b",
    r"\brm\s+(-\w*\s+)*[/~]\s*($|;|&)",
    r"\brm\s+-\w*rf?\w*\s+[/~]",
    r"\bmkfs\b",
    r"\bdiskutil\s+erase",
    r"\blaunchctl\s+(bootout|unload|disable)",
    r">\s*/dev/sd",
    r"\bshutdown\b|\breboot\b",
]


def _blocked(cmd: str) -> str | None:
    for pat in BLOCKED:
        if re.search(pat, cmd):
            return f"blocked by policy: matches `{pat}`"
    return _own_controls(cmd)


def _own_controls(cmd: str) -> str | None:
    """The owner's own controls, reachable from any shell here because local requests are
    trusted: their voice, and the answer to a held command. A tripwire like the rest, for
    the obvious way in. It is not what stops a script set on getting round it."""
    try:
        import settings
        port = settings.port("app")
    except Exception:
        port = 8899
    if re.search(r"(?:^|[\s;&|(/])bin/mc\b", cmd) or re.search(
            rf"(?:127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):{port}\b[^\s]*/api/"
            r"(?:send|approvals|improvements|lessons|settings|cron|runtime|kernel/(?:pause|resume|policies|sessions))\b", cmd):
        return "blocked by policy: that is the owner's own control of this app, which an agent does not use."
    return None


# The list above is the floor, not the ceiling: a shell can always destroy
# something in a way a regex will not catch, and the patterns only name the
# handful of shapes somebody thought of. This second gate reads what the regex
# let through and asks whether the owner would want to have been asked.
#
# It can only add a refusal, never lift one. A pattern match is still final, so
# the deterministic answer is never overridden by a probabilistic one.
#
# Threshold measured over 24 commands. Asking whether a command is
# "irreversible" does not work — mv and chmod are reversible, and they scored
# below `rm -rf ./dist`. Asking whether an unsupervised assistant should get the
# owner's approval separates cleanly: things that should be refused came in at
# 0.77-0.97, ordinary read-only work at 0.12-0.28, and routine cleanup that must
# not be refused (node_modules, dist, caches, log truncation) at 0.38-0.64. 0.70
# sits between the last two with roughly equal margin on each side. It is a
# narrower band than the other gates here, so it is worth rechecking against
# runtime_store.decisions(kind="terminal_guard") before trusting it further.
GUARD_P = 0.70
_GUARD_QUESTION = (
    "An assistant is about to run this in the owner's shell unsupervised. Should "
    "it stop and get the owner's explicit approval first? owner_instructions holds "
    "what the owner actually asked this assistant to do, in their own words.")
# Judging a command with no idea whether it was asked for is how a merge the
# owner requested nine seconds earlier ends up held. The instructions come from
# the owner's own stored messages, never from the agent, so an agent cannot
# claim permission it was not given.
_GUARD_CRITERIA = {
    "true": "The owner would be upset to discover this ran without being asked. "
            "A vague or open-ended instruction ('do whatever is needed', 'sort it "
            "out', 'tidy up') is NOT a request for a specific destructive act and "
            "does not excuse one.",
    "false": "Routine development work, or the owner named this specific action and "
             "the command plainly carries out that named request."}
# Agents repeat themselves constantly (the same test command, the same status
# check), and the same command in the same directory cannot warrant a different
# answer. Cache the verdict so the latency is paid once, not every turn.
_verdicts: dict[tuple, bool] = {}
_VERDICT_CAP = 512


def _guard_on() -> bool:
    try:
        import settings
        return bool((settings.load().get("decide") or {}).get("guard_terminal", True))
    except Exception:
        return True


async def wants_approval(cmd: str, workdir: str, intent: str = "") -> bool:
    """The judgement alone: would the owner want to be asked before this ran? False
    when it cannot be reached, because unreachable is not a refusal; the regex stood."""
    import decide
    import runtime_store
    if not _guard_on():
        return False
    # Checked before the rest of the config so a repeat command costs a dict
    # lookup rather than re-reading settings and .env off disk every turn. The
    # instructions are part of the key: the same command is a different question
    # once the owner has asked for it.
    key = (cmd.strip(), workdir, (intent or "").strip()[:400])
    if key not in _verdicts:
        if not decide.configured():
            return False
        probability = await decide.noul(
            {"command": cmd, "working_directory": workdir,
             "owner_instructions": (intent or "").strip()[:400] or "(none recently)"},
            _GUARD_QUESTION, _GUARD_CRITERIA)
        if probability is None:
            return False
        await asyncio.to_thread(
            runtime_store.decision_record, "terminal_guard", "",
            {"command": cmd[:200], "probability": probability,
             "threshold": GUARD_P}, probability >= GUARD_P)
        if len(_verdicts) >= _VERDICT_CAP:
            _verdicts.clear()
        _verdicts[key] = probability >= GUARD_P
    return _verdicts[key]


async def _judged(cmd: str, workdir: str, agent: str = "",
                  intent: str = "") -> str | None:
    """Refuse what the patterns missed. None when allowed, or undecidable."""
    import runtime_store
    if not await wants_approval(cmd, workdir, intent):
        return None
    # An agent that has just been told to stop often edits the command and tries
    # again before the owner has answered. Two requests for one intention is how
    # a merge gets approved twice, so point at the one already waiting.
    waiting = await asyncio.to_thread(
        runtime_store.approval_pending_like, agent or "", cmd)
    if waiting:
        return (f"still held for approval [{waiting['id']}]: you already have a request "
                "waiting with the owner for essentially this command. Editing it and "
                "sending it again makes two requests for one intention. Wait to be woken "
                "with the result, and end your turn now.")
    request = await asyncio.to_thread(
        runtime_store.approval_create, agent or "", cmd, workdir,
        "flagged as needing the owner's approval")
    return (f"held for approval [{request['id']}]: this looks like it needs the "
            "owner's say-so before it runs unsupervised. The exact command has been "
            "put in front of them; if they approve it, it runs as written and you "
            "will be woken with the output. Say what you were trying to achieve and "
            "then end your turn — do not try to work around this, and do not "
            "re-send it.")


def _resolve_cwd(cwd: str | None, fallback: str) -> str:
    path = Path(os.path.expanduser(cwd or fallback))
    return str(path) if path.is_dir() else str(Path.home())


def _env() -> dict:
    return processes.environment()


def _cap(out: str) -> str:
    out = out.strip()
    if len(out) > OUTPUT_CAP:
        out = out[:4000] + f"\n... [{len(out)} chars, middle truncated] ...\n" + out[-3000:]
    return out or "(no output)"


# The GitHub bot reviews diffs; it must never check out or run a
# contributor's PR code (arbitrary code on the owner's machine), merge,
# or touch repo settings.
BOT_BLOCKED = [
    r"\bgh\s+pr\s+checkout\b",
    r"\bgh\s+pr\s+merge\b",
    r"\bgh\s+api\b[^|;&]*/merges?\b",
    r"\bgh\s+api\s+graphql\b[^|;&]*\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|updateRef|deleteRef|createRef)\b",
    r"\bgh\s+repo\s+(delete|rename|archive|edit|sync)\b",
    r"\bgh\s+secret\b",
    r"\bgh\s+variable\b",
    r"\bgh\s+release\b",
    r"\bgh\s+run\s+(rerun|cancel|delete)\b",
    r"\bgh\s+workflow\s+(disable|enable|run)\b",
    r"\bgh\s+api\b[^|;&]*(-X\s*=?\s*|--method[\s=]+)[\"']?(DELETE|PUT)\b",
    r"\bgit\s+push\b[^|;&]*\b(main|master)\b",
    r"\bgit\s+(checkout|switch|worktree)\b[^|;&]*\bpull/\d",
]


# Mission Control's own checkout. Agents may read it freely — diagnosing a
# problem in the app means reading its code — but changing it goes through the
# improve tool, which isolates the work in a worktree, refuses the files that
# hold their limits, runs the tests, and asks the owner. Without this the guard
# is opt-in: the first agent to try simply ran `git checkout -b` in the live
# repo and committed, and a restart would have booted code nobody reviewed.
#
# This is a strong default, not a sandbox. A shell can always write a file some
# way a regex will not catch. What it buys is that the improve tool is the path
# of least resistance and casual edits to the running app stop being possible.
SELF_REPO = str(Path(__file__).resolve().parent.parent)

# `git -C <path> commit` and `git commit` both have to match, so allow flags
# between the verb and git itself, bounded so it cannot leap a command separator.
_GIT_WRITE = re.compile(r"""(?ix)
    \bgit\b[^|;&]{0,90}?\s
    (commit|checkout|switch|branch|merge|rebase|reset|push|pull|cherry-pick|
     revert|stash|apply|clean|worktree|tag|remote)\b
""")
_FILE_WRITE = re.compile(r"(?ix) \bsed\s+-i | \btee\b | \bchmod\b | \bchown\b |"
                         r"\b(rm|mv|cp|mkdir|touch|ln|truncate)\b")
_REDIRECT = re.compile(r">>?\s*(?P<target>[^\s|;&]+)")


def _self_mention() -> re.Pattern:
    """This checkout however a command names it, and nothing that merely shares its name:
    ~/missioncontrol-mobile, a clone at ~/code/missioncontrol and github.com/you/missioncontrol
    are other repositories, and a team working on them is doing ordinary work."""
    home = str(Path.home())
    spelled = [re.escape(SELF_REPO)]
    if SELF_REPO.startswith(home + os.sep):
        rel = re.escape(SELF_REPO[len(home) + 1:])
        # as typed from home, where a bare name is this checkout and not some other one
        spelled += [r"~/" + rel, r"\$\{?HOME\}?/" + rel, r"(?<![\w./~$-])" + rel]
    return re.compile(rf"(?:{'|'.join(spelled)})(?![\w.-])")


def _mentions_self_repo(text: str) -> bool:
    return bool(_self_mention().search(text))


def _inside_self_repo(workdir: str) -> bool:
    real = os.path.realpath(workdir)
    return real == SELF_REPO or real.startswith(SELF_REPO + os.sep)


def _targets_self_repo(cmd: str, workdir: str) -> bool:
    return _inside_self_repo(workdir) or _mentions_self_repo(cmd)


def _self_edit_blocked(cmd: str, workdir: str) -> str | None:
    if not _targets_self_repo(cmd, workdir):
        return None
    inside = _inside_self_repo(workdir)
    hit = bool(_GIT_WRITE.search(cmd)) or bool(_FILE_WRITE.search(cmd))
    if not hit:
        # a redirection only counts when it lands in the repo: writing a grep
        # result to /tmp from inside the checkout is not self-modification
        for m in _REDIRECT.finditer(cmd):
            t = m.group("target")
            if _mentions_self_repo(t) or (inside and not t.startswith(("/", "~"))):
                hit = True
                break
    if not hit:
        return None      # reading Mission Control's own code is always fine
    return ("blocked: this changes the checkout Mission Control is running from. Read it "
            "all you like, but change it somewhere else: in a clone of its repository, "
            "through a pull request, or with the improve tool, which writes the change on "
            "its own branch, runs the tests, and puts the diff in front of the owner. "
            "Editing the running app directly means a restart boots code nobody "
            "reviewed.")


def _bot_blocked(cmd: str) -> str | None:
    import ledger
    plain = ledger._as_run(cmd)
    for pat in BOT_BLOCKED:
        if re.search(pat, plain):
            return ("blocked for the bot: reviewing means reading diffs, never running "
                    "PR code, merging, or changing settings. Comment your verdict instead.")
    return None


async def run_terminal(command: str, cwd: str | None, agent_cwd: str,
                       env_extra: dict | None = None, as_bot: bool = False,
                       agent: str = "", judge: bool = True, intent: str = "",
                       timeout: int = TERMINAL_TIMEOUT, keys: bool = True) -> str:
    block = _blocked(command) or (_bot_blocked(command) if as_bot else None)
    if block:
        return block
    workdir = _resolve_cwd(cwd, agent_cwd)
    block = _self_edit_blocked(command, workdir)
    if block:
        return block
    if judge:
        block = await _judged(command, workdir, agent, intent)
        if block:
            return block
    env = _env()
    if not keys:
        # Work started by an outside event runs without the owner's credentials,
        # so it cannot leak what it never had.
        import settings as cfg
        for name in cfg.env_all():
            env.pop(name, None)
    if env_extra:
        env.update(env_extra)
    try:
        proc = await processes.create_subprocess_shell(
            command, cwd=workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **processes.spawn_options(),
        )
        text = await processes.capture(proc, timeout, OUTPUT_CAP)
        out = f"exit {proc.returncode}\n{_cap(text)}"
        # A command that hands itself to the background has returned as far as anyone
        # can tell. Say what is still going, because nothing will wake anyone when it ends.
        if processes.still_running(proc.pid):
            out += ("\n[Something this command started is still running in the background. It is not a job: "
                    "nothing tells you when it ends, so check on it, and it is killed if this task is stopped.]")
        return out
    except asyncio.TimeoutError:
        return f"timed out after {timeout}s"
    except Exception as exc:
        return f"failed to run: {exc}"


MAX_PROGRESS = 80


def _tool_line(name: str, inp: dict) -> str:
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
    elif name == "Bash":
        target = inp.get("command") or ""
    elif name in ("Grep", "Glob"):
        target = (inp.get("pattern") or "") + (" in " + inp["path"] if inp.get("path") else "")
    elif name in ("Agent", "Task"):
        target = inp.get("description") or inp.get("prompt") or ""
    elif name in ("WebFetch", "WebSearch"):
        target = inp.get("url") or inp.get("query") or ""
    else:
        target = json.dumps(inp)[:120] if inp else ""
    return f"claude \u203a {name} {' '.join(str(target).split())}".strip()[:240]


def describe_event(ev: dict) -> list[str]:
    """One human line per thing Claude Code just did, from a stream-json event."""
    out = []
    t = ev.get("type")
    if t == "assistant":
        for block in ((ev.get("message") or {}).get("content") or []):
            if block.get("type") == "text" and (block.get("text") or "").strip():
                out.append("claude: " + " ".join(block["text"].split())[:240])
            elif block.get("type") == "tool_use":
                out.append(_tool_line(block.get("name") or "tool", block.get("input") or {}))
    elif t == "result":
        bits = []
        if ev.get("duration_ms"):
            bits.append(f"{round(ev['duration_ms'] / 1000)}s")
        if ev.get("total_cost_usd"):
            bits.append(f"${ev['total_cost_usd']:.2f}")
        if ev.get("num_turns"):
            bits.append(f"{ev['num_turns']} turns")
        out.append(("claude failed" if ev.get("is_error") else "claude finished")
                   + (f" ({', '.join(bits)})" if bits else ""))
    return out


_repo_locks: dict[str, asyncio.Lock] = {}


# Only the reviewer posts on GitHub and only the owner merges, and that holds in here
# too. It is the one way to the shell that does not pass through the terminal tool.
CLAUDE_DISALLOWED = ("gh pr comment", "gh pr review", "gh pr close", "gh issue comment", "gh issue create", "gh pr merge")
# A job the owner set up uses Claude Code to prepare things. What goes out still goes past them.
CLAUDE_DISALLOWED_STANDING = ("git push", "gh pr create", "gh release", "gh repo", "gh secret", "gh api", "eas submit",
                              "eas update", "npm publish", "curl", "wget", "envoy", "bin/envoyer", "forge deploy")


async def claude_code(task: str, repo: str | None, agent_cwd: str, progress=None, env_extra: dict | None = None,
                      standing: bool = False) -> str:
    """Run Claude Code headless. With `progress` (an async callable taking a
    line), every step it takes is reported as it happens: files read, edits,
    commands, and its own narration. One run per repo at a time: two agents
    editing the same tree at once would trample each other."""
    workdir = _resolve_cwd(repo, agent_cwd)
    lock = _repo_locks.setdefault(os.path.realpath(workdir), asyncio.Lock())
    if lock.locked() and progress:
        await progress("claude: waiting for another run in this repo to finish")
    rules = (*CLAUDE_DISALLOWED, *(CLAUDE_DISALLOWED_STANDING if standing else ()))
    async with lock:
        return await _claude_code_locked(task, workdir, progress, env_extra, ",".join(f"Bash({r}:*)" for r in rules))


async def _claude_code_locked(task: str, workdir: str, progress=None, env_extra: dict | None = None,
                              disallowed: str = "") -> str:
    try:
        proc = await processes.create_subprocess_exec(
            "claude", "-p", task, "--output-format", "stream-json", "--verbose",
            "--disallowedTools", disallowed or ",".join(f"Bash({r}:*)" for r in CLAUDE_DISALLOWED),
            cwd=workdir, env={**_env(), **(env_extra or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **processes.spawn_options(),
        )
    except FileNotFoundError:
        return "claude CLI not found on PATH"
    except Exception as exc:
        return f"failed to run claude: {exc}"

    final = None
    steps = 0
    err_chunks = []

    async def drain_err():
        # chunked, not line-based: a single stderr line can exceed asyncio's
        # 64KB line limit and would otherwise raise LimitOverrunError
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            err_chunks.append(chunk.decode("utf-8", errors="replace"))
            if len(err_chunks) > 4:
                del err_chunks[:-4]

    async def handle_line(line: str):
        nonlocal final, steps
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        for note in describe_event(ev):
            steps += 1
            if progress and steps <= MAX_PROGRESS:
                try:
                    await progress(note)
                except Exception:
                    pass
        if ev.get("type") == "result":
            final = ev.get("result") or ""
            if ev.get("is_error"):
                final = "[error] " + final

    async def pump():
        # stream-json emits one JSON object per line, but a line (a big file
        # read, a long message) can be many MB. Read in chunks and split on
        # newlines ourselves so there is no line-length limit to overflow.
        buf = ""
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            if len(buf) > 8_000_000:
                raise ValueError("Claude stream event exceeds 8 MB")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                await handle_line(line)
        if buf:
            await handle_line(buf)

    err_task = asyncio.create_task(drain_err())
    try:
        await asyncio.wait_for(pump(), timeout=CLAUDE_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        await processes.terminate(proc)
        return f"claude timed out after {CLAUDE_TIMEOUT}s"
    except BaseException:
        await processes.terminate(proc)
        raise
    finally:
        if not err_task.done():
            err_task.cancel()
        await asyncio.gather(err_task, return_exceptions=True)
    if final is None:
        final = "".join(err_chunks)[-1500:] or "(claude produced no result)"
    return f"claude exit {proc.returncode}\n{_cap(final)}"


async def synth_speech(text: str) -> tuple[bytes, str] | None:
    """Text to speech with the team's configured voice (see voice.py)."""
    import voice
    return await voice.synth(text)


async def run_approved(request: dict) -> str:
    """Run a command the owner approved, with the pattern list still in force.

    The owner can lift the judge's refusal; they cannot lift policy. `sudo` and
    the rest are refused here exactly as they were at the tool, and so is the
    self-checkout guard, so an approval can never become a way around either.
    """
    if request.get("kind", "command") != "command":
        return "refused: this approval is not a shell command"
    command, workdir = request["command"], request["cwd"]
    block = _blocked(command) or _self_edit_blocked(command, workdir)
    if block:
        return block
    import db
    import github
    import ledger
    # On the ledger like anything else an agent runs, or a comment or close it asked for
    # would read as the owner's own doing when GitHub tells us about it.
    action = None
    if ledger.is_outward(command):
        action = await asyncio.to_thread(db.add_action, request.get("agent") or "", ledger.normalize(command),
                                         command, "running")
    out = await run_terminal(command, workdir, workdir, judge=False, env_extra=github.agent_env(command))
    if action is not None:
        await asyncio.to_thread(db.finish_action, action, out)
    return out
