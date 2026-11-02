"""'Plug in a model and go': the one readiness question the app asks."""


def test_ready_needs_a_model_and_a_key_when_the_provider_wants_one(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "_resolved",
                        lambda conf: {"key_env": "ANTHROPIC_API_KEY", "key": ""})
    assert not llm.ready({"provider": "anthropic", "model": ""})
    assert not llm.ready({"provider": "anthropic", "model": "claude-sonnet-5"})
    monkeypatch.setattr(llm, "_resolved",
                        lambda conf: {"key_env": "ANTHROPIC_API_KEY", "key": "sk-x"})
    assert llm.ready({"provider": "anthropic", "model": "claude-sonnet-5"})


def test_local_models_are_ready_without_a_key(monkeypatch):
    import llm
    monkeypatch.setattr(llm, "_resolved", lambda conf: {"key_env": "", "key": ""})
    assert llm.ready({"provider": "ollama", "model": "llama3"})
