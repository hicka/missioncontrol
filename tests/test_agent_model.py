"""A model per agent: the team default unless the agent says otherwise."""
import asyncio

import llm


def _orch():
    import orchestrator
    o = orchestrator.Orchestrator()
    o.queues = {aid: asyncio.Queue() for aid in o.agents}
    return o


def test_no_override_means_the_team_model(monkeypatch):
    monkeypatch.setattr(llm, "config", lambda: {"provider": "minimax", "model": "MiniMax-M3",
                                                "base_url": "", "max_tokens": 3000})
    assert llm.agent_config(None) is None
    assert llm.agent_config({}) is None
    assert llm.agent_config({"provider": "", "model": ""}) is None


def test_override_merges_over_the_team_default(monkeypatch):
    monkeypatch.setattr(llm, "config", lambda: {"provider": "minimax", "model": "MiniMax-M3",
                                                "base_url": "", "max_tokens": 3000})
    c = llm.agent_config({"provider": "anthropic", "model": "claude-haiku-4-5-20251001"})
    assert c["provider"] == "anthropic"
    assert c["model"] == "claude-haiku-4-5-20251001"
    assert c["max_tokens"] == 3000          # unstated fields still come from the team


def test_switching_provider_drops_the_team_base_url(monkeypatch):
    """A custom endpoint belongs to one provider; sending another provider's
    model there fails in a way that is hard to read."""
    monkeypatch.setattr(llm, "config", lambda: {"provider": "custom-openai",
                                                "model": "local-thing",
                                                "base_url": "http://box:1234/v1",
                                                "max_tokens": 3000})
    c = llm.agent_config({"provider": "anthropic", "model": "claude-opus-5"})
    assert c["base_url"] == ""
    assert llm._resolved(c)["base_url"] == "https://api.anthropic.com"
    # but an override that only swaps the model keeps the endpoint
    same = llm.agent_config({"model": "other-local-thing"})
    assert same["base_url"] == "http://box:1234/v1"


def test_agent_llm_reads_what_was_saved():
    o = _orch()
    aid = list(o.agents)[0]
    assert o.agent_llm(aid) is None
    o.update_agent(aid, {"llm": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}})
    conf = o.agent_llm(aid)
    assert conf["provider"] == "anthropic" and conf["model"] == "claude-haiku-4-5-20251001"


def test_an_empty_override_puts_the_agent_back_on_the_team_model():
    o = _orch()
    aid = list(o.agents)[0]
    o.update_agent(aid, {"llm": {"provider": "anthropic", "model": "claude-opus-5"}})
    assert o.agent_llm(aid) is not None
    o.update_agent(aid, {"llm": {}})
    assert o.agent_llm(aid) is None
    assert o.agents[aid]["llm"] == {}


def test_a_half_filled_override_is_not_half_applied():
    """Provider with no model would resolve to an empty model and fail the turn."""
    o = _orch()
    aid = list(o.agents)[0]
    o.update_agent(aid, {"llm": {"provider": "anthropic", "model": "   "}})
    assert o.agents[aid]["llm"] == {"provider": "anthropic"}


def test_the_agents_model_is_what_actually_gets_called(monkeypatch):
    o = _orch()
    aid = list(o.agents)[0]
    o.update_agent(aid, {"llm": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}})
    seen = {}

    async def fake_complete(system, messages, tools=None, agent=None, conf=None):
        seen["model"] = llm._resolved(conf)["model"]
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(llm, "complete", fake_complete)
    asyncio.run(llm.complete("s", [], None, agent=aid, conf=o.agent_llm(aid)))
    assert seen["model"] == "claude-haiku-4-5-20251001"


def test_usage_is_recorded_against_the_agents_own_model():
    """Per-agent models only pay off if the spend breakdown follows them."""
    import db
    db.add_usage("cheapbot", "anthropic", "claude-haiku-4-5-20251001", 100, 10)
    db.add_usage("bigbot", "anthropic", "claude-opus-5", 5000, 500)
    rows = {r["agent"]: r for r in db.usage_summary(1)["per_agent"]}
    assert rows["cheapbot"]["in_tokens"] == 100
    assert rows["bigbot"]["in_tokens"] == 5000


def test_an_agent_can_have_its_own_output_limit(tmp_path, monkeypatch):
    import orchestrator
    import llm
    monkeypatch.setattr(orchestrator, "LOCAL_PATH", tmp_path / "agents.local.yaml")
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    agent = o.update_agent(aid, {"llm": {"provider": "minimax", "model": "MiniMax-M3", "max_tokens": "8000"}})
    assert agent["llm"] == {"provider": "minimax", "model": "MiniMax-M3", "max_tokens": 8000}
    assert llm.agent_config(agent["llm"])["max_tokens"] == 8000
    assert o.update_agent(aid, {"llm": {"model": "MiniMax-M3", "max_tokens": 10**9}})["llm"]["max_tokens"] == 64000
    assert "max_tokens" not in o.update_agent(aid, {"llm": {"max_tokens": 8000}})["llm"], \
        "a limit alone is not an override: it needs the model it applies to"
