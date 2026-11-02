"""/model and /effort: the chat picks an agent's model and how hard it thinks."""
import asyncio

import pytest

import commands
import db
import llm


@pytest.fixture
def orch(tmp_path, monkeypatch):
    import orchestrator
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(orchestrator, "LOCAL_PATH", tmp_path / "agents.local.yaml")
    monkeypatch.setattr(llm, "config", lambda: {**llm.DEFAULT_CONFIG, "provider": "deepseek",
                                                "model": "deepseek-flash", "effort": "low"})
    monkeypatch.setattr(llm, "_fallback_configs", lambda: [])
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    yield o
    if db._conn:
        db._conn.close()


def run(orch, thread, text):
    return asyncio.run(commands.execute(orch, thread, text))


def test_model_shows_the_team_default(orch):
    aid = next(iter(orch.agents))
    res = run(orch, f"dm:{aid}", "/model")
    assert "deepseek/deepseek-flash" in res["reply"] and "default" in res["reply"]


def test_effort_sets_an_override_and_reset_clears_it(orch):
    aid = next(iter(orch.agents))
    res = run(orch, f"dm:{aid}", "/effort HIGH")
    assert "at high effort (own setting)" in res["reply"]
    assert orch.agents[aid]["llm"] == {"effort": "high"}
    assert llm._resolved(orch.agent_llm(aid))["effort"] == "high"
    res = run(orch, f"dm:{aid}", "/effort reset")
    assert "at low effort (default)" in res["reply"]
    assert orch.agents[aid]["llm"] == {}


def test_model_by_name_finds_its_provider(orch):
    aid = next(iter(orch.agents))
    res = run(orch, f"dm:{aid}", "/model deepseek-v4-pro")
    assert "deepseek/deepseek-v4-pro (own setting)" in res["reply"]
    assert orch.agents[aid]["llm"]["provider"] == "deepseek"
    # picking the team model again is the same as having no override
    run(orch, f"dm:{aid}", "/model deepseek deepseek-flash")
    assert "model" not in orch.agents[aid]["llm"]


def test_model_keeps_effort_and_warns_about_a_missing_key(orch, monkeypatch):
    aid = next(iter(orch.agents))
    monkeypatch.setattr(llm.cfg, "env_get", lambda name: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run(orch, f"dm:{aid}", "/effort max")
    res = run(orch, f"dm:{aid}", "/model openai gpt-5")
    assert orch.agents[aid]["llm"] == {"effort": "max", "provider": "openai", "model": "gpt-5"}
    assert "OPENAI_API_KEY" in res["reply"]


def test_bad_values_are_refused(orch):
    aid = next(iter(orch.agents))
    with pytest.raises(ValueError):
        run(orch, f"dm:{aid}", "/effort turbo")
    with pytest.raises(ValueError):
        run(orch, f"dm:{aid}", "/model nosuchmodel")
    with pytest.raises(ValueError):
        run(orch, f"dm:{aid}", "/model nosuchprovider some-model")


def test_group_thread_targets_the_mentioned_agent(orch):
    ids = list(orch.agents)
    res = run(orch, "group:team", f"/effort @{ids[1]} none")
    assert f"@{ids[1]} runs" in res["reply"] and "at none effort" in res["reply"]
