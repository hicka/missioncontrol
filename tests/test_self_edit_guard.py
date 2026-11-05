"""The improve tool's guardrails are worthless if terminal walks around them.

This is not hypothetical: asked to fix a bug in the app, the first agent to try
ran `git checkout -b` in the live checkout and committed there. A restart would
have booted code nobody reviewed.
"""
from pathlib import Path

import tools

HOME = str(Path.home())
REPO = tools.SELF_REPO
# The checkout as a person types it. Wherever the suite runs from: the improve
# tool runs it in a worktree under /tmp, and a fixed ~/missioncontrol failed every
# proposal there, since that worktree is not the checkout it names.
TYPED = "~" + REPO[len(HOME):] if REPO.startswith(HOME + "/") else REPO


def _blocked(cmd, wd=REPO):
    return tools._self_edit_blocked(cmd, wd)


def test_the_exact_bypass_that_happened():
    assert _blocked(f"cd {REPO} && git checkout -b fix/heartbeat-drift")
    assert _blocked(f"cd {REPO} && git add server/orchestrator.py && git commit -m x")


def test_git_writes_are_caught_however_the_path_is_given():
    """`git -C <path> commit` puts flags between git and the verb."""
    for cmd in (f"git -C {TYPED} commit -am wip",
                f"git -C {REPO} push origin master",
                f"cd {REPO} && git reset --hard HEAD~1",
                f"cd {REPO} && git rebase master",
                f"cd {REPO} && git worktree add /tmp/x"):
        assert _blocked(cmd, HOME), cmd


def test_the_leash_cannot_be_edited_through_the_shell():
    """server/tools.py is protected from the improve tool; it has to be
    protected from the terminal too, or the protection is decorative."""
    for cmd in (f"sed -i '' 's/BLOCKED/ALLOWED/' {REPO}/server/tools.py",
                f"echo pass >> {REPO}/server/budget.py",
                f"rm {REPO}/server/selfimprove.py",
                f"cp /tmp/evil.py {REPO}/server/ledger.py",
                f"cat > {REPO}/server/tools.py <<'X'\nX"):
        assert _blocked(cmd, HOME), cmd


def test_reading_the_app_stays_free():
    """Diagnosing a problem in Mission Control means reading Mission Control."""
    for cmd in (f"cd {REPO} && git status --short",
                f"cd {REPO} && git log --oneline -5",
                f"cd {REPO} && git diff master..HEAD",
                "git -C ~/missioncontrol show HEAD",
                f"grep -rn heartbeat {REPO}/server/",
                f"sed -n '1,40p' {REPO}/server/orchestrator.py",
                f"cd {REPO} && venv/bin/python -m pytest -q",
                f"wc -l {REPO}/server/*.py"):
        assert _blocked(cmd, REPO) is None, cmd


def test_writing_output_somewhere_else_is_not_self_modification():
    """A grep from inside the checkout piped to /tmp is not an edit."""
    assert _blocked(f"cd {REPO} && grep -rn x server/ > /tmp/out.txt") is None
    # but writing back into the repo is
    assert _blocked(f"cd {REPO} && grep -rn x server/ > server/notes.txt")


def test_other_repos_are_none_of_this_guard_s_business():
    for cmd, wd in (("cd ~/Documents/acme && git commit -am release", HOME + "/Documents/acme"),
                    ("cd ~/Sites/acmeshop && git checkout -b feature", HOME + "/Sites/acmeshop"),
                    ("git -C ~/Sites/acmeshop push", HOME)):
        assert _blocked(cmd, wd) is None, cmd


def test_repositories_that_only_share_its_name_are_ordinary_work(tmp_path):
    """A team keeping the phone app at ~/missioncontrol-mobile, or a clone of this
    repository somewhere else, was refused as if it were editing the running app."""
    name = Path(REPO).name
    for cmd, wd in ((f"git -C {REPO}-mobile commit -am fix", HOME),
                    (f"cd {TYPED}-mobile && git checkout -b crew/fix", HOME),
                    ("git commit -am fix", REPO + "-mobile"),
                    (f"git -C {tmp_path}/{name} push -u origin crew/fix", HOME),
                    (f"cd {tmp_path}/{name} && touch notes.md", HOME),
                    (f"git clone https://github.com/acme/{name} {tmp_path}/copy", HOME)):
        assert _blocked(cmd, wd) is None, cmd


def test_its_bare_name_from_home_is_still_the_checkout():
    if not REPO.startswith(HOME + "/"):
        return
    rel = REPO[len(HOME) + 1:]
    assert _blocked(f"cd {rel} && git commit -am x", HOME)
    assert _blocked(f"cd $HOME/{rel} && git commit -am x", HOME)


def test_the_refusal_points_at_the_supported_path():
    msg = _blocked(f"cd {REPO} && git commit -am x")
    assert "improve tool" in msg
    assert "reviewed" in msg


def test_the_guard_runs_before_the_command_does():
    import inspect
    src = inspect.getsource(tools.run_terminal)
    assert "_self_edit_blocked" in src
    assert src.index("_self_edit_blocked") < src.index("create_subprocess")
