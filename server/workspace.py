"""Where a Worker's commands run, so that two tasks do not share one working tree.

Several profiles point at the same repository, and a checkout has one current branch.
One Worker switching branch changed what another was reading, and a commit could land
on someone else's branch. So one Worker at a time has the shared checkout, with its
caches and build products, and anyone who needs that repository meanwhile works in a
copy of their own: copy-on-write, so 4.5 GB takes seconds and almost no disk, and a
real clone of the state, so its dependencies and untracked config are there and a
branch can be checked out in two places. Commands that name the shared path are pointed
at the copy, because skills and prompts name that path.

A copy goes when its task ends, unless it holds work that was never pushed.
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from apphome import ROOT as DATA_HOME
import kernel_store as store

logger = logging.getLogger("missioncontrol.workspace")

# One folder per data home: a test run or a preview on this machine has its own sessions,
# and must never take the live app's copies for leftovers of its own.
ROOT = Path.home() / ".mc-workspaces" / hashlib.sha256(str(DATA_HOME).encode()).hexdigest()[:10]
SELF_REPO = str(Path(__file__).resolve().parent.parent)
COPY_TIMEOUT = 300
KEPT_AFTER_ENDING = 30 * 86_400

_toplevels: dict[str, str | None] = {}


def toplevel(path: str) -> str | None:
    """The repository a folder is in. A home directory under git is not a project."""
    real = os.path.realpath(os.path.expanduser(path or "~"))
    if real not in _toplevels:
        try:
            done = subprocess.run(["git", "-C", real, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=10)
            top = os.path.realpath(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            top = None
        _toplevels[real] = None if top == os.path.realpath(Path.home()) else top
    return _toplevels[real]


def _git(where: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", where, *args], capture_output=True, text=True, timeout=60)


_remotes: dict[str, bool] = {}


def _has_remote(repo: str) -> bool:
    """A copy only gets anywhere by being pushed. In a folder with no remote, the folder is
    the work: what a job made there has to be there, and a copy would be left to be deleted."""
    if repo not in _remotes:
        try:
            _remotes[repo] = bool(_git(repo, "remote").stdout.strip())
        except (OSError, subprocess.SubprocessError):
            _remotes[repo] = False
    return _remotes[repo]


def _spellings(path: str) -> list[str]:
    home = str(Path.home())
    if not path.startswith(home + os.sep):
        return [path]
    rest = path[len(home) + 1:]
    return [path, f"~/{rest}", f"$HOME/{rest}", f"${{HOME}}/{rest}"]


def _named(path: str) -> re.Pattern:
    """A path written in a command, as a whole name: acmeshop is not acmeshop-old."""
    return re.compile(r"(?<![\w./~-])(?:" + "|".join(re.escape(s) for s in _spellings(path)) + r")(?![\w.-])")


def _default_branch(where: str) -> str:
    head = _git(where, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0:
        return head.stdout.strip().removeprefix("origin/")
    # Nobody ran `remote set-head`, which is usual for a repository that started locally.
    for name in ("main", "master"):
        if _git(where, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return ""


def make_copy(repo: str, dest: Path) -> bool:
    """A clean copy of a repository on its default branch. What another Worker has
    half done in the checkout must not come along, so tracked changes and new files
    go; what git ignores (dependencies, .env) stays, which is the point of copying."""
    # Anything already there is a copy that was cut short, and copying onto it would nest.
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = (["cp", "-cR", repo, str(dest)] if sys.platform == "darwin"
               else ["cp", "-a", "--reflink=auto", repo, str(dest)] if sys.platform.startswith("linux") else None)
    try:
        if command and subprocess.run(command, capture_output=True, timeout=COPY_TIMEOUT).returncode == 0:
            # Whoever is at work in the original may be mid-command, and that copies too.
            for lock in (dest / ".git").glob("*.lock"):
                lock.unlink(missing_ok=True)
            shutil.rmtree(dest / ".git" / "worktrees", ignore_errors=True)
            if _clean_up_copy(str(dest)):
                return True
            # A copy that is not clean is someone else's half-done work with a new name.
            logger.warning("could not clean the copy of %s", repo)
            shutil.rmtree(dest, ignore_errors=True)
            return False
        # Not a filesystem that clones. A worktree has no dependencies, but it is separate.
        shutil.rmtree(dest, ignore_errors=True)
        return _git(repo, "worktree", "add", "--detach", str(dest),
                    _default_branch(repo) or "HEAD").returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not copy %s: %s", repo, exc)
        shutil.rmtree(dest, ignore_errors=True)
        return False


def _clean_up_copy(where: str) -> bool:
    """On the default branch as GitHub has it, with nothing of anyone else's: not their
    uncommitted changes, and not their local branches, which would look like unpushed work
    of its own and keep the copy from ever being removed."""
    branch = _default_branch(where)
    if branch and _git(where, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").returncode == 0:
        moved = _git(where, "checkout", "-f", "-B", branch, f"origin/{branch}")
    else:
        moved = _git(where, "checkout", "-f", branch) if branch else subprocess.CompletedProcess((), 0)
    if moved.returncode or _git(where, "reset", "--hard").returncode or _git(where, "clean", "-fd").returncode:
        return False
    others = [b for b in _git(where, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split() if b != branch]
    if others:
        _git(where, "branch", "-D", *others)
    _git(where, "stash", "clear")
    return True


def unsaved(path: str) -> bool:
    """Whether deleting this copy would lose something: changes, or commits nowhere else."""
    if not os.path.isdir(path):
        return False
    try:
        if _git(path, "status", "--porcelain").stdout.strip():
            return True
        return bool(_git(path, "log", "--branches", "--not", "--remotes", "--oneline", "-1").stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True


def is_dirty(path: str) -> bool:
    try:
        return bool(_git(path, "status", "--porcelain").stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True


def release(row: dict):
    """The task ended. Its copies go, unless that would lose work."""
    for repo, path in ((row.get("meta") or {}).get("copies") or {}).items():
        if unsaved(path):
            logger.warning("%s ended with unpushed work in %s; keeping it", row["id"], path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        _git(repo, "worktree", "prune")
        try:
            Path(path).parent.rmdir()
        except OSError:
            pass      # another of its copies is still there


def sweep(now: float | None = None):
    """Copies kept for unpushed work do not stay forever."""
    now = now or time.time()
    if not ROOT.is_dir():
        return
    for folder in ROOT.iterdir():
        row = store.session_get(folder.name)
        # A folder with no session is not known to be anyone's to delete.
        if row and row["status"] == "ended" and now - row["updated"] > KEPT_AFTER_ENDING:
            shutil.rmtree(folder, ignore_errors=True)


class Workspaces:
    def __init__(self, orch):
        self.orch = orch
        self.locks: dict[str, asyncio.Lock] = {}

    def checkouts(self) -> list[str]:
        """The shared checkouts: every repository a profile works in. Changes to
        Mission Control itself go through the improve tool, which has its own worktree."""
        out = []
        for agent in self.orch.agents.values():
            top = toplevel(agent.get("cwd") or "~")
            if top and top != SELF_REPO and top not in out and _has_remote(top):
                out.append(top)
        return out

    def cwd(self, row: dict, base: str) -> str:
        copies = (row.get("meta") or {}).get("copies") or {}
        if not copies:
            return base
        top = toplevel(base)
        where = copies.get(top) if top else None
        if not where or not os.path.isdir(where):
            return base
        inside = os.path.relpath(os.path.realpath(os.path.expanduser(base)), top)
        return where if inside == "." else os.path.join(where, inside)

    async def _home_for(self, row: dict, repo: str) -> tuple[str, str]:
        """Where this session works on a repository, and anything it has to be told."""
        async with self.locks.setdefault(row["id"], asyncio.Lock()):
            row = await asyncio.to_thread(store.session_get, row["id"]) or row
            meta = row.get("meta") or {}
            copies = meta.get("copies") or {}
            have = copies.get(repo)
            if have and os.path.isdir(have):
                return have, ""
            # Changes nobody has committed are somebody's: the owner's, or a Worker's that
            # stopped or failed part way. Taking the checkout over would be working on top of them.
            if await asyncio.to_thread(store.checkout_holder, repo) == row["id"] or (
                    not await asyncio.to_thread(is_dirty, repo)
                    and await asyncio.to_thread(store.checkout_claim, repo, row["id"])):
                return repo, ""
            # With the path in the name, two repositories called the same do not collide.
            dest = ROOT / row["id"] / f"{Path(repo).name}-{hashlib.sha256(repo.encode()).hexdigest()[:6]}"
            if not await asyncio.to_thread(make_copy, repo, dest):
                return repo, (f"[Another task is using {repo} and a copy could not be made, so you are sharing it. "
                              "Check the branch before you commit, and do not switch it.]")
            await asyncio.to_thread(store.session_set, row["id"], meta={**meta, "copies": {**copies, repo: str(dest)}})
            logger.info("%s works in its own copy of %s", row["id"], repo)
            return str(dest), (f"[Another task is using {repo}, so you have your own copy at {dest}, clean, on the "
                               "default branch. Commands naming that path run in your copy. Run git fetch before "
                               "you branch. Only what you push leaves it.]")

    async def place(self, row: dict, name: str, args: dict, base: str) -> tuple[dict, str]:
        """Points one tool call at this session's own copy of what it touches."""
        if not isinstance(args, dict) or name not in ("terminal", "claude_code", "watch"):
            return args, ""
        if name == "watch" and args.get("action") != "start":
            return args, ""
        checkouts = await asyncio.to_thread(self.checkouts)
        if not checkouts:
            return args, ""
        keys = {"terminal": ("command", "cwd"), "claude_code": ("repo", "task"), "watch": ("poll",)}[name]
        said = "\n".join(str(args.get(k) or "") for k in keys)
        where = args.get("cwd") or args.get("repo") or base
        here = await asyncio.to_thread(toplevel, where)
        notes = []
        for repo in checkouts:
            named = _named(repo)
            if repo != here and not named.search(said):
                continue
            home, note = await self._home_for(row, repo)
            if note:
                notes.append(note)
            if home != repo:
                args = {k: named.sub(lambda _: home, v) if k in keys and isinstance(v, str) else v
                        for k, v in args.items()}
        return args, "\n".join(notes)
