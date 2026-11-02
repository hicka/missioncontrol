"""Outward actions happen once; a turn that runs out of budget resumes, not restarts."""
import asyncio
import time


def test_outward_detection_is_about_the_world_not_the_disk():
    import ledger
    for cmd in ["eas submit --platform ios --latest", "git push origin main",
                "gh pr create --fill", "curl -s -X POST https://api.x/y -d '{}'",
                "npm publish", "kubectl rollout restart deploy/api", "ssh box 'forge deploy'"]:
        assert ledger.is_outward(cmd), cmd
    for cmd in ["ls -la", "git status", "git log --oneline -3", "curl -s https://api.x/y",
                "cat deploy.log", "eas build:view abc --json", "npm test"]:
        assert not ledger.is_outward(cmd), cmd
    assert ledger.normalize("  eas   submit\n --platform ios ") == "eas submit --platform ios"


def test_recent_action_window():
    import db
    key = "eas submit --platform ios --latest"
    db.add_action("devops", key, key, "submission 39bc80f1 queued")
    prior = db.recent_action(key, 3600)
    assert prior and prior["agent"] == "devops" and "39bc80f1" in prior["outcome"]
    assert db.recent_action(key, 3600, now=time.time() + 4000) is None   # window passed
    assert db.recent_action("something else", 3600) is None


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_terminal_refuses_to_repeat_an_outward_command(monkeypatch):
    import tools
    o = _orch()
    aid = list(o.agents)[0]
    runs = []

    async def fake_run(cmd, cwd, agent_cwd, **kw):
        runs.append(cmd)
        return "ok: submission queued"

    monkeypatch.setattr(tools, "run_terminal", fake_run)
    cmd = "eas submit --platform ios --profile production"
    call = lambda args: asyncio.run(o._exec_tool(aid, "~", "terminal", args))

    assert call({"command": cmd}).startswith("ok")
    second = call({"command": cmd})
    assert second.startswith("NOT RUN") and "again=true" in second
    assert call({"command": "git status"}) == "ok: submission queued"   # harmless commands run freely
    assert call({"command": cmd, "again": True}).startswith("ok")        # explicit override
    assert runs.count(cmd) == 2
    assert o._ctx(aid).actions.count("eas submit --platform ios --profile production") == 2
