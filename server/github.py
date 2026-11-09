"""GitHub events bridge: tunnel + auto-registered webhooks, with a polling safety net.

Each event carries what it takes to route it without reading it: whether anyone but
a Worker already on that thread could care (followers_only), whether it can wait for
one that is not in the middle of something (quiet), whether it needs a decision even
if someone is on it (needs_decision), and whether the thread is over (closes).
"""
import asyncio
import datetime
import time
import db
import ledger
import processes
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

import settings as cfg  # settings live together in settings.yaml

logger = logging.getLogger("missioncontrol.github")

from apphome import ROOT

STATE_PATH = ROOT / "github_state.json"

WEBHOOK_PATH = "/github-webhook"

DEFAULTS = {
    "enabled": True,
    "repos": [],
    "route_ci": "devops",
    "route_code": "cto",
    "review_agent": "",   # issues, PRs and comments route here (the bot); "" keeps route_code
    "poll_seconds": 300,
    "secret": "",
}

GH_ENV_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
RUNS_REMEMBERED = 200   # finished workflow runs per repository; a poll reads the newest 30


def settings() -> dict:
    s = cfg.load()
    gh = {**DEFAULTS, **s.get("github", {})}
    if not gh["secret"]:
        gh["secret"] = uuid.uuid4().hex + uuid.uuid4().hex
        s["github"] = gh
        cfg.save(s)
    return gh


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as exc:
            logger.warning("github state file unreadable, starting empty: %s", exc)
    return {}


def _save_state(state: dict):
    # Whole or not at all: a reader in the middle of a plain write sees an empty file,
    # takes it for a first run and forgets every PR it knew.
    cfg.write_whole(STATE_PATH, json.dumps(state, indent=1))


async def _gh(*args: str) -> tuple[int, str]:
    proc = await processes.create_subprocess_exec(
        "gh", *args,
        env={"PATH": GH_ENV_PATH, "HOME": str(Path.home())},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    # In this order: returncode is None until the process has been waited for.
    out = await _communicate(proc)
    return proc.returncode, out


async def _communicate(proc, data: bytes | None = None) -> str:
    """Timing out does not end a process. Giving up on a hung gh without killing it
    left five of them running for days, one for each time it happened."""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout=60)
    except asyncio.TimeoutError:
        await processes.terminate(proc)
        # A bare TimeoutError prints as nothing, which is how "poll failed for x: " got
        # into the log 27 times and would have reached the owner as a blank.
        raise asyncio.TimeoutError("gh did not answer within 60 seconds") from None
    return out.decode("utf-8", errors="replace")


# ---------- event model ----------

# How much of what someone wrote an event carries. The rest stays on GitHub, and a cut
# says so: a review truncated without a mark reads as a review that stopped there.
MAX_BODY = 6000


def _body(text) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_BODY:
        return text
    return f"{text[:MAX_BODY]}\n[{len(text) - MAX_BODY} more characters on GitHub]"


def _titled(line: str, body) -> str:
    body = _body(body)
    return f"{line}\n\n{body}" if body else line


def _when(stamp) -> float | None:
    """A GitHub timestamp as epoch seconds."""
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _evt(kind: str, repo: str, text: str, store: bool = False, number=None, actor: str = "",
         branch: str = "", key: str = "", comment_id=None, memory: str = "", followers_only: bool = False,
         quiet: bool = False, needs_decision: bool = False, closes: bool = False, review_worthy: bool = False,
         shared_branch: bool = False, at: float | None = None) -> dict:
    # store=True marks durable repo-state changes worth writing to team memory
    # at is when it happened, which a poll can be reading about a good while later
    return {"kind": kind, "repo": repo, "text": text, "store": store, "number": number,
            "actor": actor or "", "branch": branch or "", "key": key or "", "comment_id": comment_id,
            "memory": memory, "followers_only": followers_only, "quiet": quiet, "needs_decision": needs_decision,
            "closes": closes, "review_worthy": review_worthy, "shared_branch": shared_branch, "at": at}


_signed_in_as: str | None = None
AGENT_POST_WINDOW = 2000    # how far back a command could have started and still be causing this
AGENT_POST_NEAR = 120       # how long after one of ours something on that same PR looks like its doing
AGENT_POST_WAIT = 6.0       # the webhook can beat the command that caused it
# How a Claude Code run is written on the ledger, with the repository it runs in.
CLAUDE_ACTION = "claude_code"


async def owner_login() -> str:
    """The owner's GitHub account: settings, or who gh is signed in as here."""
    global _signed_in_as
    configured = settings().get("owner_login")
    if configured:
        return str(configured)
    if _signed_in_as is None:
        try:
            code, out = await _gh("api", "user", "--jq", ".login")
        except Exception:
            return ""
        if code != 0:
            return ""
        _signed_in_as = out.strip()
    return _signed_in_as


_bot_login: str | None = None


async def bot_login() -> str:
    """The account agents use on GitHub: settings, or who its token says it is."""
    global _bot_login
    configured = settings().get("bot_login")
    if configured:
        return str(configured)
    token = bot_token()
    if not token:
        return ""
    if _bot_login is None:
        try:
            proc = await processes.create_subprocess_exec(
                "gh", "api", "user", "--jq", ".login",
                env={"PATH": GH_ENV_PATH, "HOME": str(Path.home()), "GH_TOKEN": token, "GH_HOST": "github.com"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out = await _communicate(proc)
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        _bot_login = out.strip()
    return _bot_login


def agent_env(command: str) -> dict | None:
    """What makes a command act on GitHub as the agents' account and not as the owner.
    Everything does, except what only the owner's login may do. gh is the only thing
    that reads this: git pushes through its own credentials either way."""
    token = bot_token()
    if not token or ledger.needs_owner_login(command):
        return None
    return {"GH_TOKEN": token, "GH_HOST": "github.com"}


# ---------- repositories a profile runs on its own ----------
#
# The owner can give a profile repositories of its own (own_repos on the profile). In
# them, work nobody asked for this minute, such as a scheduled round and what it hands
# on, carries its own changes all the way: a branch, a pull request, a review, a merge,
# and talk on issues. What stays the owner's even there: rewriting a shared branch, a
# force push, tags and releases, settings, secrets, and the raw API. Anything this
# cannot read for certain counts as not covered, and is held as before.

_OWN_GH = {("pr", verb) for verb in ("create", "merge", "review", "comment", "close", "ready", "edit", "reopen")} | {
    ("issue", verb) for verb in ("create", "comment", "edit", "close", "reopen")} | {
    ("label", "create"), ("label", "edit"), ("run", "rerun"), ("run", "cancel"), ("workflow", "run")}
_PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "--porcelain", "-v", "--verbose"}
_GITHUB_REPO = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_REDIRECT = re.compile(r"(?<!\S)\d*>>?(?:&\d+|\s*\S+)")
_SEPARATOR = re.compile(r"&&|\|\||[;|&\n]")


def _repo_of_url(url: str) -> str | None:
    found = _GITHUB_REPO.search((url or "").strip())
    return f"{found[1]}/{found[2]}".lower() if found else None


def _git_out(workdir: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _remote_repo(workdir: str, remote: str | None) -> str | None:
    """The GitHub repository a remote of this checkout points at. With none named, only
    a checkout with a single remote says which one is meant."""
    if remote is None:
        names = (_git_out(workdir, "remote") or "").split()
        if len(names) != 1:
            return None
        remote = names[0]
    if "/" in remote or ":" in remote:
        return _repo_of_url(remote)
    return _repo_of_url(_git_out(workdir, "remote", "get-url", "--push", remote) or "")


def _push_repo(args: list[str], workdir: str) -> str | None:
    import router
    positional = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in _PUSH_FLAGS:
                return None
            continue
        positional.append(arg)
    remote, specs = (positional[0], positional[1:]) if positional else (None, [])
    current = _git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD") or ""
    for spec in specs or [current]:
        src, _, dst = spec.partition(":")
        target = (dst or src).removeprefix("refs/heads/")
        if target == "HEAD":
            target = current
        tag = _git_out(workdir, "rev-parse", "--verify", "--quiet", f"refs/tags/{src}") is not None
        if (not src or spec.startswith("+") or tag or not target or target.startswith("refs/")
                or target in router.SHARED_BRANCHES):
            return None
    return _remote_repo(workdir, remote)


def _gh_repo(args: list[str], workdir: str) -> str | None:
    named, rest, i = None, [], 0
    while i < len(args):
        arg = args[i]
        if arg in ("-R", "--repo") and i + 1 < len(args):
            named, i = args[i + 1], i + 2
            continue
        if arg.startswith("--repo="):
            named = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    if tuple(rest[:2]) not in _OWN_GH or "--admin" in rest:
        return None
    if named:
        return named.lower() if re.fullmatch(r"[\w.-]+/[\w.-]+", named) else _repo_of_url(named)
    return _remote_repo(workdir, None)


def own_repo_work(command: str, workdir: str, repos) -> bool:
    """Whether everything outward in this command is the kind of work a profile does on
    its own, in a repository the owner gave it."""
    owned = {r.lower() for r in repos or ()}
    run = ledger._without_text(command)
    if not owned or ledger._RUNS_TEXT.search(run):
        return False
    here, touched = os.path.expanduser(workdir), set()
    for segment in _SEPARATOR.split(_REDIRECT.sub(" ", run)):
        words = [w.strip("'\"") for w in segment.split()]
        if not words:
            continue
        if words[0] == "cd":
            here = os.path.join(here, os.path.expanduser(words[1] if len(words) > 1 else "~"))
            continue
        if not ledger.is_outward(segment):
            continue
        repo = None
        if words[0] == "git":
            where, i = here, 1
            while i + 1 < len(words) and words[i] == "-C":
                where, i = os.path.join(here, os.path.expanduser(words[i + 1])), i + 2
            if words[i:i + 1] == ["push"]:
                repo = _push_repo(words[i + 1:], where)
        elif words[0] == "gh":
            repo = _gh_repo(words[1:], here)
        if repo not in owned:
            return False
        touched.add(repo)
    return bool(touched)


def own_repo_intent(repos) -> str:
    """What the owner said by giving a profile repositories, for the judge of commands."""
    return (f"I let this agent run {', '.join(repos)} on its own: it pushes branches, opens, reviews and "
            "merges pull requests, and comments on issues there without asking me.")


def _agent_posted(evt: dict, actions: list[dict]) -> bool:
    """Whether a command of ours caused this event, going by the ledger. With a bot token
    agents speak as the bot, so a comment under the owner's name is theirs unless its exact
    id came back from a command. Guessing from timing would take the owner's reply to the
    reviewer, moments after the reviewer posted, for the reviewer's own."""
    cid = str(evt.get("comment_id") or "")
    number = str(evt.get("number") or "")
    repo = evt.get("repo") or ""
    has_own_account = bool(bot_token())
    at = evt.get("at") or time.time()
    for action in actions:
        outcome, command = action["outcome"], action["command"]
        if cid and cid in outcome:
            return True
        if command.startswith(CLAUDE_ACTION):
            # Its commands are not on the ledger one by one. While it runs in this
            # repository, anything happening there under the owner's name may be it.
            if not has_own_account and f" in {repo.rsplit('/', 1)[-1]}:" in command and action["ts"] <= at and (
                    outcome == "running" or at <= (action.get("finished") or at)):
                return True
            if not cid and number and f"{repo}/pull/{number}" in outcome:
                return True
            continue
        if cid and has_own_account:
            continue
        # Output sent elsewhere leaves no id to match. An agent acting on this same PR
        # moments ago is then the likely source, so it does not count as the owner.
        near = abs(at - action["ts"]) < AGENT_POST_NEAR
        named = bool(number) and re.search(rf"\b{re.escape(number)}\b", command)
        if cid:
            if near and "issuecomment-" not in outcome and named and ("comment" in command or "review" in command):
                return True
            continue
        if number and f"{repo}/pull/{number}" in outcome:
            return True
        # An agent commenting on a PR moments before the owner merges it did not merge it.
        if near and named and re.search(r"\b(merge|close|reopen)\b", command):
            return True
        if near and evt.get("branch") and "git push" in command:
            return True
    return False


async def _agent_acted(evt: dict) -> bool:
    deadline = time.monotonic() + AGENT_POST_WAIT
    at = evt.get("at") or time.time()
    while True:
        actions = await asyncio.to_thread(db.actions_since, at - AGENT_POST_WINDOW)
        if _agent_posted(evt, actions):
            return True
        unfinished = [a for a in actions if a["outcome"] == "running" and not a["command"].startswith(CLAUDE_ACTION)]
        # Only the command still running could be about to name this exact comment, and
        # with its own account no command of ours would be posting under this login at all.
        if not unfinished or time.monotonic() > deadline or (evt.get("comment_id") and bot_token()):
            return False
        await asyncio.sleep(0.5)


async def said_by_owner(evt: dict) -> bool:
    """Whether a person, the owner, wrote this comment on GitHub.

    Agents now use their own account, so a comment under the owner's name is almost
    always them. The login alone still cannot be the whole test: an approved command
    and anything run before the bot token was set went through the owner's login, and
    if those counted, an agent could give itself orders by writing one. What separates
    them is the ledger: every comment an agent posts comes back from gh with its id, and
    the owner's never appears there. Anything other than a comment or review is left out,
    because only those say what the owner wants.
    """
    if not evt.get("comment_id") or not evt.get("actor") or evt["actor"] != await owner_login():
        return False
    return not await _agent_acted(evt)


async def classify(evt: dict) -> dict:
    """Who this event really came from. from_owner is the owner speaking. echo is us
    hearing ourselves, or a bot's boilerplate, which is only of interest to a Worker
    already on that thread: 6 of 9 GitHub events the Router read were this, at 26,000
    tokens each, and each ended in it deciding to do nothing. ours is the team's own
    hands: the agents' account, or an agent acting through the owner's, as against a
    stranger's or another bot's."""
    actor, out = evt.get("actor") or "", {"from_owner": False, "echo": False, "under_owner": False, "ours": False}
    if not actor:
        return out
    bot = await bot_login()
    # A failed run is news whoever pushed the commit.
    if evt.get("kind") == "ci":
        out["ours"] = bool(bot) and actor == bot
        return out
    if actor.endswith("[bot]") or actor == bot:
        out["echo"] = True
        out["ours"] = bool(bot) and actor == bot
    elif actor == await owner_login():
        if evt.get("comment_id"):
            out["from_owner"] = await said_by_owner(evt)
            out["echo"] = out["under_owner"] = not out["from_owner"]
        else:
            out["echo"] = out["under_owner"] = await _agent_acted(evt)
        out["ours"] = out["under_owner"]
    return out


def routing(evt: dict, who: dict) -> dict:
    """What the event's own nature and its source add up to, for Intake. A PR opened by
    an agent is still a PR that needs reviewing: #504 reached production without one."""
    echo = who["echo"] and not evt.get("review_worthy")
    flags = {"followers_only": (evt.get("followers_only") or echo) and not who["from_owner"],
             "quiet": evt.get("quiet"), "needs_decision": evt.get("needs_decision") and not echo,
             "closes": evt.get("closes"), "ours": who.get("ours") and not who["from_owner"]}
    return {k: True for k, v in flags.items() if v}


def event_thread(evt: dict) -> str:
    """One PR or issue is one stream of events, whatever path each one came by. CI
    has no number of its own, so a run belongs to the PR if it has one, else its branch."""
    if evt.get("number"):
        return f"github:{evt['repo']}#{evt['number']}"
    return f"github:{evt['repo']}:{evt.get('branch') or 'repo'}"


def route_agent(evt: dict, agents: dict) -> str | None:
    gh = settings()
    if evt["kind"] == "ci":
        aid = gh["route_ci"]
    else:
        aid = gh.get("review_agent") or gh["route_code"]
    if aid in agents:
        return aid
    return next(iter(agents), None)


def review_agent() -> str:
    return settings().get("review_agent") or ""


def bot_token() -> str | None:
    import settings as cfg
    return cfg.env_get("GITHUB_BOT_TOKEN")


# ---------- webhook intake ----------

def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], digest)


def _run_event(repo: str, run: dict, default_branch: str = "") -> dict | None:
    """One finished workflow run. Dependabot's own update jobs fail for reasons nobody
    here can act on, and they were 19 of 26 "CI failures" in a month. A success changes
    nothing for anyone not already waiting on it. A re-run is a new attempt at a result,
    so it can fail again and be news again."""
    if run.get("event") == "dynamic":
        return None
    branch = run.get("head_branch") or ""
    attempt = run.get("run_attempt") or 1
    key = f"run:{repo}:{run.get('id')}" + (f":{attempt}" if attempt > 1 else "")
    common = dict(number=next((p.get("number") for p in run.get("pull_requests") or []), None),
                  actor=(run.get("actor") or {}).get("login", ""), branch=branch,
                  shared_branch=bool(default_branch) and branch == default_branch)
    if run.get("conclusion") in ("failure", "timed_out", "startup_failure"):
        return _evt("ci", repo, f"CI failure in {repo}: workflow \"{run.get('name')}\" "
                                f"{run.get('conclusion')} on branch `{branch}` ({run.get('html_url')})",
                    key=key, **common)
    if run.get("conclusion") == "success":
        return _evt("ci", repo, f"CI passed in {repo}: workflow \"{run.get('name')}\" on branch `{branch}`",
                    key=f"{key}:passed", followers_only=True, quiet=True, **common)
    return None


def _pr_evt(repo: str, pr: dict, action: str, sender: str = "") -> dict:
    num = pr.get("number")
    author = (pr.get("user") or {}).get("login", "")
    line = (f"PR #{num} {action} in {repo}: \"{pr.get('title')}\" by {author} "
            f"(`{(pr.get('head') or {}).get('ref')}` -> `{(pr.get('base') or {}).get('ref')}`)")
    return _evt("code", repo, _titled(line, pr.get("body")), store=True, memory=line, number=num,
                actor=(sender or author) if action == "reopened" else author,
                key=f"pr:{repo}:{num}:opened" if action == "opened" else f"pr:{repo}:{num}:{action}:{pr.get('updated_at')}",
                needs_decision=True, quiet=True, review_worthy=action in ("opened", "ready_for_review"))


def _pr_closed_evt(repo: str, pr: dict, sender: str = "") -> dict:
    num = pr.get("number")
    what = "merged" if pr.get("merged") or pr.get("merged_at") else "closed without merge"
    by = f" by {sender}" if sender else ""
    return _evt("code", repo, f"PR #{num} {what}{by} in {repo}: \"{pr.get('title')}\"", store=True, number=num,
                actor=sender, key=f"pr:{repo}:{num}:closed:{pr.get('closed_at')}", followers_only=True, closes=True)


def _issue_opened_evt(repo: str, issue: dict) -> dict:
    author = (issue.get("user") or {}).get("login", "")
    line = f"Issue #{issue.get('number')} opened in {repo}: \"{issue.get('title')}\" by {author}"
    return _evt("code", repo, _titled(line, issue.get("body")), store=True, memory=line, number=issue.get("number"),
                actor=author, key=f"issue:{repo}:{issue.get('number')}")


def _comment_evt(repo: str, number, comment: dict) -> dict:
    who = (comment.get("user") or {}).get("login", "")
    return _evt("code", repo, f"Comment on #{number} in {repo} by {who}: {_body(comment.get('body'))}",
                number=number, actor=who, key=f"comment:{repo}:{comment.get('id')}", comment_id=comment.get("id"),
                at=_when(comment.get("created_at")))


def _inline_evt(repo: str, number, comment: dict) -> dict:
    who = (comment.get("user") or {}).get("login", "")
    where = f"{comment.get('path')}:{comment.get('line') or comment.get('original_line') or '?'}"
    return _evt("code", repo, f"Review comment on #{number} in {repo} by {who} at {where}: {_body(comment.get('body'))}",
                number=number, actor=who, key=f"review_comment:{repo}:{comment.get('id')}", comment_id=comment.get("id"),
                at=_when(comment.get("created_at")))


def _review_evt(repo: str, number, review: dict) -> dict:
    who = (review.get("user") or {}).get("login", "")
    state = (review.get("state") or "").lower().replace("_", " ")
    body = _body(review.get("body"))
    # An approval or an empty note asks nothing of whoever opened the PR. Changes requested does.
    calm = (review.get("state") or "").upper() != "CHANGES_REQUESTED" and not body
    return _evt("code", repo, f"Review on #{number} in {repo} by {who}: {state}" + (f"\n\n{body}" if body else ""),
                number=number, actor=who, key=f"review:{repo}:{review.get('id')}", comment_id=review.get("id"), quiet=calm,
                at=_when(review.get("submitted_at")))


def parse_webhook(event_name: str, payload: dict) -> dict | None:
    repo = (payload.get("repository") or {}).get("full_name", "?")
    sender = (payload.get("sender") or {}).get("login", "")
    action = payload.get("action")

    if event_name == "workflow_run":
        if action != "completed":
            return None
        return _run_event(repo, payload.get("workflow_run") or {},
                          (payload.get("repository") or {}).get("default_branch") or "")

    if event_name == "pull_request":
        pr = payload.get("pull_request") or {}
        num = pr.get("number")
        if action in ("opened", "ready_for_review", "reopened"):
            _remember_pr(repo, num, "open")
            return _pr_evt(repo, pr, action, sender)
        if action == "closed":
            _remember_pr(repo, num, "closed")
            return _pr_closed_evt(repo, pr, sender)
        if action == "review_requested":
            who = (payload.get("requested_reviewer") or {}).get("login") or (payload.get("requested_team") or {}).get("name")
            return _evt("code", repo, f"Review of PR #{num} in {repo} requested from {who} by {sender}: \"{pr.get('title')}\"",
                        number=num, actor=sender, needs_decision=True, quiet=True,
                        key=f"pr:{repo}:{num}:review_requested:{who}:{pr.get('updated_at')}")
        if action == "synchronize":
            head = (payload.get("after") or "")[:8]
            return _evt("code", repo, f"New commits on PR #{num} in {repo} by {sender}, now at {head}",
                        number=num, actor=sender, branch=(pr.get("head") or {}).get("ref") or "", quiet=True,
                        followers_only=True, key=f"pr:{repo}:{num}:pushed:{payload.get('after')}")
        if action == "edited":
            changed = ", ".join(sorted((payload.get("changes") or {}).keys())) or "details"
            return _evt("code", repo, f"PR #{num} in {repo} edited by {sender} ({changed}): \"{pr.get('title')}\"",
                        number=num, actor=sender, quiet=True, followers_only=True,
                        key=f"pr:{repo}:{num}:edited:{pr.get('updated_at')}")
        return None

    if event_name == "issues":
        issue = payload.get("issue") or {}
        num = issue.get("number")
        if action == "opened":
            # The poll's cursor stays the poll's. Moved from here, a delivery for #12 would
            # mean a missed #11 is never read. The event's key takes care of the overlap.
            return _issue_opened_evt(repo, issue)
        if action in ("closed", "reopened"):
            return _evt("code", repo, f"Issue #{num} {action} by {sender} in {repo}: \"{issue.get('title')}\"",
                        number=num, actor=sender, followers_only=True, closes=action == "closed", quiet=action != "closed",
                        key=f"issue:{repo}:{num}:{action}:{issue.get('updated_at')}")
        if action == "edited":
            return _evt("code", repo, f"Issue #{num} in {repo} edited by {sender}: \"{issue.get('title')}\"",
                        number=num, actor=sender, quiet=True, followers_only=True,
                        key=f"issue:{repo}:{num}:edited:{issue.get('updated_at')}")
        return None

    if event_name == "issue_comment":
        number = (payload.get("issue") or {}).get("number")
        comment = payload.get("comment") or {}
        if action == "created":
            return _comment_evt(repo, number, comment)
        if action == "edited":
            # Not an instruction, whoever edited it: an edit does not say what they want now.
            who = (comment.get("user") or {}).get("login", "")
            return _evt("code", repo, f"Comment on #{number} in {repo} edited by {who}: {_body(comment.get('body'))}",
                        number=number, actor=who, quiet=True, followers_only=True,
                        key=f"comment:{repo}:{comment.get('id')}:edited:{comment.get('updated_at')}")
        return None

    if event_name == "pull_request_review" and action == "submitted":
        return _review_evt(repo, (payload.get("pull_request") or {}).get("number"), payload.get("review") or {})

    if event_name == "pull_request_review_comment" and action == "created":
        return _inline_evt(repo, (payload.get("pull_request") or {}).get("number"), payload.get("comment") or {})

    if event_name == "push":
        ref = payload.get("ref") or ""
        if not ref.startswith("refs/heads/") or payload.get("deleted"):
            return None
        branch = ref[len("refs/heads/"):]
        count = len(payload.get("commits") or [])
        headline = ((payload.get("head_commit") or {}).get("message") or "").splitlines()[:1]
        return _evt("code", repo, f"{count} commit(s) pushed to `{branch}` in {repo} by {sender}"
                    + (f": {headline[0][:200]}" if headline else ""),
                    actor=sender, branch=branch, quiet=True, followers_only=True,
                    key=f"push:{repo}:{payload.get('after')}")

    return None


def _remember_pr(repo: str, number, state_name: str):
    state = _load_state()
    state.setdefault(repo, {}).setdefault("prs", {})[str(number)] = state_name
    _save_state(state)


# ---------- webhook auto-registration ----------

async def _gh_json(method: str, path: str, body: dict) -> tuple[int, str]:
    proc = await processes.create_subprocess_exec(
        "gh", "api", "-X", method, path, "--input", "-",
        env={"PATH": GH_ENV_PATH, "HOME": str(Path.home())},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out = await _communicate(proc, json.dumps(body).encode())
    return proc.returncode, out


async def register_hooks(public_url: str) -> list[str]:
    """Create or update our webhook on each repo. Returns status lines."""
    gh = settings()
    hook_url = public_url.rstrip("/") + WEBHOOK_PATH
    events = ["workflow_run", "pull_request", "pull_request_review", "pull_request_review_comment",
              "issues", "issue_comment", "push"]
    results = []
    for repo in gh["repos"]:
        try:
            code, out = await _gh("api", f"repos/{repo}/hooks")
            hooks = json.loads(out) if code == 0 else []
            ours = [h for h in hooks
                    if WEBHOOK_PATH in ((h.get("config") or {}).get("url") or "")]
            payload = {"active": True, "events": events,
                       "config": {"url": hook_url, "content_type": "json",
                                  "secret": gh["secret"], "insecure_ssl": "0"}}
            if ours:
                code, out = await _gh_json("PATCH", f"repos/{repo}/hooks/{ours[0]['id']}",
                                           payload)
                results.append(f"{repo}: updated hook {ours[0]['id']} -> {hook_url}"
                               if code == 0 else f"{repo}: update FAILED: {out[:200]}")
                # Only one of ours is ever kept current, so any other is pointing at a
                # tunnel that is gone. It would fail every delivery from now on.
                for extra in ours[1:] if code == 0 else []:
                    gone, _ = await _gh("api", "-X", "DELETE", f"repos/{repo}/hooks/{extra['id']}")
                    results.append(f"{repo}: removed duplicate hook {extra['id']}"
                                   if gone == 0 else f"{repo}: could not remove duplicate hook {extra['id']}")
            else:
                payload["name"] = "web"
                code, out = await _gh_json("POST", f"repos/{repo}/hooks", payload)
                results.append(f"{repo}: created hook -> {hook_url}"
                               if code == 0 else f"{repo}: create FAILED: {out[:200]}")
        except Exception as exc:
            results.append(f"{repo}: error {exc}")
    for r in results:
        logger.info("github hooks: %s", r)
    return results


# ---------- cloudflared tunnel ----------

class Tunnel:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.drain_task = None

    async def start(self) -> str | None:
        try:
            self.proc = await processes.create_subprocess_exec(
                "cloudflared", "tunnel", "--url", f"http://127.0.0.1:{cfg.port('public')}",
                "--no-autoupdate",
                env={"PATH": GH_ENV_PATH, "HOME": str(Path.home())},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.warning("cloudflared not found; webhook mode unavailable")
            return None
        pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        try:
            async with asyncio.timeout(40):
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        break
                    m = pattern.search(line.decode("utf-8", errors="replace"))
                    if m:
                        self.url = m.group(0)
                        import runtime
                        self.drain_task = runtime.spawn(self._drain(), "tunnel", "Tunnel output")
                        logger.info("tunnel up: %s", self.url)
                        return self.url
        except TimeoutError:
            logger.warning("tunnel URL not found within 40s")
        await self.stop()
        return None

    async def _drain(self):
        try:
            while self.proc and self.proc.stdout and not self.proc.stdout.at_eof():
                await self.proc.stdout.readline()
        except Exception as exc:
            logger.warning("tunnel drain ended: %s", exc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self):
        if self.drain_task:
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
            self.drain_task = None
        if self.proc and self.proc.returncode is None:
            await processes.terminate(self.proc)
        self.proc = None
        self.url = None


# ---------- poller (safety net; shares state with webhook path) ----------

# Webhooks are the fast path and nothing more. GitHub does not retry a delivery that
# failed, and the tunnel's address changes when it restarts, so this reads what they
# might have missed from a cursor. Every event has a stable key, so reading one twice is
# harmless, and nothing advances until what was read has been passed on.
POLL_ALERT_AFTER = 6        # polls in a row, which at 5 minutes is half an hour
_poll_failures: dict[str, dict] = {}


class PollError(RuntimeError):
    pass


async def _read(path: str):
    """A failed read has to look like a failure. Treated as an empty list, an outage
    looked exactly like a quiet repository."""
    code, out = await _gh("api", path)
    if code != 0:
        raise PollError(f"{path}: {out[:200]}")
    return json.loads(out)


def _stamp() -> str:
    # A little behind the clock, because GitHub's clock and this one differ. The overlap
    # is read twice and deduplicated by key.
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _poll_repo(repo: str, rs: dict) -> list[dict]:
    events: list[dict] = []
    # The first read only learns what is there. A state file from an older version knows
    # far fewer PRs than this now reads, and all the others would look newly opened.
    first_run = not rs.get("seeded")
    started = _stamp()
    since = rs.get("comments_since") or started
    seeded_at = rs.setdefault("seeded_at", started)

    prs = await _read(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=20")
    known = rs.setdefault("prs", {})
    for pr in prs:
        num, st = str(pr["number"]), pr["state"]
        prev = known.get(num)
        # An old PR coming back into view is not a new one.
        if prev is None and st == "open" and not first_run and (pr.get("created_at") or seeded_at) >= seeded_at:
            events.append(_pr_evt(repo, pr, "opened"))
        elif prev == "open" and st == "closed":
            events.append(_pr_closed_evt(repo, pr))
        known[num] = st

    issues = [i for i in await _read(f"repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=10")
              if "pull_request" not in i]
    last_issue = rs.get("last_issue", 0)
    for issue in issues:
        if issue["number"] > last_issue and not first_run:
            events.append(_issue_opened_evt(repo, issue))
    if issues:
        rs["last_issue"] = max(last_issue, max(i["number"] for i in issues))

    runs = (await _read(f"repos/{repo}/actions/runs?per_page=30")).get("workflow_runs", [])
    # Not a cursor on the newest id: that marked runs still in progress as read, and a
    # failure in one of them was never reported. Anything past the floor is looked at
    # again until it has finished.
    if "run_floor" not in rs:
        rs["run_floor"] = rs.get("last_run_id") or max((r["id"] for r in runs), default=0)
    # The floor never moves, so what has been passed on is remembered by key. Without that
    # every finished run on the page was passed on again each poll: 30,000 log lines in a
    # week, and only the events table stood between a restart and hearing them all twice.
    done = list(rs.get("runs_done") or [])
    for run in reversed(runs):
        if run["id"] > rs["run_floor"] and run.get("status") == "completed":
            evt = _run_event(repo, run)
            if evt and evt["key"] not in done:
                events.append(evt)
                done.append(evt["key"])
    rs["runs_done"] = done[-RUNS_REMEMBERED:]

    if not first_run and rs.get("comments_since"):
        comments = await _read(f"repos/{repo}/issues/comments?since={since}&sort=updated&direction=asc&per_page=100")
        for comment in comments:
            if (comment.get("created_at") or "") >= since:
                number = int((comment.get("issue_url") or "0").rsplit("/", 1)[-1])
                events.append(_comment_evt(repo, number, comment))
        inline = await _read(f"repos/{repo}/pulls/comments?since={since}&sort=updated&direction=asc&per_page=100")
        for comment in inline:
            if (comment.get("created_at") or "") >= since:
                number = int((comment.get("pull_request_url") or "0").rsplit("/", 1)[-1])
                events.append(_inline_evt(repo, number, comment))
        for pr in prs:
            if pr["state"] == "open" and (pr.get("updated_at") or "") >= since:
                for review in await _read(f"repos/{repo}/pulls/{pr['number']}/reviews?per_page=100"):
                    if (review.get("submitted_at") or "") >= since and review.get("state") != "PENDING":
                        events.append(_review_evt(repo, pr["number"], review))
        # A full page means there may be more: carry on from where it stopped.
        full = [rows for rows in (comments, inline) if len(rows) >= 100]
        started = min((rows[-1].get("updated_at") or started for rows in full), default=started)
    rs["comments_since"] = started
    rs["seeded"] = True
    return events


def _merge_state(repo: str, rs: dict):
    """A webhook may have written to this file while the poll was waiting on GitHub."""
    state = _load_state()
    current = state.setdefault(repo, {})
    known = {**current.get("prs", {}), **rs.get("prs", {})}
    last_issue = max(current.get("last_issue", 0), rs.get("last_issue", 0))
    current.update(rs)
    current["prs"], current["last_issue"] = known, last_issue
    _save_state(state)


async def poll_once(dispatch=None) -> list[dict]:
    """Reads every repository. With dispatch, each event is passed on before the cursor
    moves past it, so a process dying in between reads it again rather than losing it."""
    gh = settings()
    state = _load_state()
    events: list[dict] = []
    for repo in gh["repos"]:
        rs = dict(state.get(repo) or {})
        try:
            found = await _poll_repo(repo, rs)
        except Exception as exc:
            logger.warning("poll failed for %s: %s", repo, exc)
            trouble = _poll_failures.setdefault(repo, {"count": 0, "told": False})
            trouble["count"] += 1
            trouble["error"] = str(exc)[:200]
            continue
        _poll_failures.pop(repo, None)
        if dispatch:
            for evt in found:
                await dispatch(evt)
        _merge_state(repo, rs)
        events.extend(found)
    return events


def poll_trouble() -> list[tuple[str, str]]:
    """Repositories that have stopped answering, each said once per outage."""
    out = []
    for repo, trouble in _poll_failures.items():
        if trouble["count"] >= POLL_ALERT_AFTER and not trouble["told"]:
            trouble["told"] = True
            out.append((repo, trouble.get("error") or ""))
    return out
