"""The GitHub steward: review events route to it, and it can't run PR code."""
import asyncio
import os
import stat


def test_code_events_route_to_the_review_agent(monkeypatch):
    import github
    import settings as cfg
    monkeypatch.setattr(cfg, "load", lambda: {"github": {
        "route_ci": "devops", "route_code": "cto", "review_agent": "steward"}})
    agents = {"cto": {}, "devops": {}, "steward": {}}
    assert github.route_agent({"kind": "code"}, agents) == "steward"    # issues/PRs/comments
    assert github.route_agent({"kind": "ci"}, agents) == "devops"       # CI still to devops
    # no bot configured: falls back to route_code
    monkeypatch.setattr(cfg, "load", lambda: {"github": {
        "route_ci": "devops", "route_code": "cto", "review_agent": ""}})
    assert github.route_agent({"kind": "code"}, agents) == "cto"


def test_bot_terminal_refuses_to_run_pr_code():
    import tools
    for cmd in ["gh pr checkout 12", "gh pr merge 12 --squash", "gh repo delete hicka/x",
                "gh secret set FOO", "gh api -X DELETE repos/x/y/labels/bug",
                "git push origin HEAD:main", "git checkout pull/12/head"]:
        assert tools._bot_blocked(cmd), cmd
    for cmd in ["gh pr diff 12 -R hicka/x", "gh pr review 12 --comment -b hi",
                "gh issue comment 3 -b hi", "gh issue edit 3 --add-label bug",
                "git push origin readme-badges", "gh pr create --fill"]:
        assert tools._bot_blocked(cmd) is None, cmd


def test_run_terminal_applies_bot_block_and_extra_env(tmp_path, monkeypatch):
    import tools
    monkeypatch.setattr(tools, "TERMINAL_TIMEOUT", 10)
    blocked = asyncio.run(tools.run_terminal("gh pr checkout 5", None, str(tmp_path), as_bot=True))
    assert blocked.startswith("blocked for the bot")
    # the same command is allowed for a normal agent (they may legitimately check out)
    ok = asyncio.run(tools.run_terminal("echo normal", None, str(tmp_path), as_bot=False))
    assert "normal" in ok
    # env_extra reaches the process (this is how the bot token becomes gh's identity)
    out = asyncio.run(tools.run_terminal("echo $GH_TOKEN", None, str(tmp_path),
                                         env_extra={"GH_TOKEN": "tok123"}))
    assert "tok123" in out


def test_ledger_covers_the_bots_outward_gh_verbs():
    import ledger
    for cmd in ["gh pr review 12 --request-changes -b x", "gh issue comment 3 -b x",
                "gh issue edit 3 --add-label bug", "gh label create bug",
                "gh api -X POST repos/x/y/labels -f name=bug"]:
        assert ledger.is_outward(cmd), cmd
    for cmd in ["gh pr diff 12", "gh issue view 3", "gh api repos/x/y/labels"]:
        assert not ledger.is_outward(cmd), cmd
