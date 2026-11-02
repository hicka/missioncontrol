import asyncio
import json

import llm


def test_openai_message_adapter_roundtrip():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"cmd": "ls"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}]},
    ]
    oa = llm._messages_to_openai("sys", msgs)
    assert oa[0]["role"] == "system"
    assert oa[-1]["role"] == "tool" and oa[-1]["tool_call_id"] == "t1"
    resp = llm._response_from_openai({"choices": [{"message": {
        "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": '{"a":1}'}}]}}]})
    assert llm.text_of(resp) == "ok"
    assert llm.tool_uses(resp)[0]["input"] == {"a": 1}


def test_fallback_chain(monkeypatch):
    calls = []

    async def fake_complete_with(r, system, messages, tools, agent, on_text=None):
        calls.append(r["model"])
        if r["model"] == "primary-model":
            raise RuntimeError("LLM HTTP 429: rate limited")
        return {"content": [{"type": "text", "text": "from fallback"}]}

    monkeypatch.setattr(llm, "_complete_with", fake_complete_with)
    monkeypatch.setattr(llm, "config", lambda: {
        **llm.DEFAULT_CONFIG, "model": "primary-model",
        "fallbacks": [{"provider": "ollama", "model": "backup-model"}]})
    resp = asyncio.run(llm.complete("s", [{"role": "user", "content": "x"}]))
    assert llm.text_of(resp) == "from fallback"
    assert calls == ["primary-model", "backup-model"]


def test_non_retryable_raises(monkeypatch):
    async def fake_complete_with(r, system, messages, tools, agent, on_text=None):
        raise RuntimeError("LLM HTTP 401: bad key")

    monkeypatch.setattr(llm, "_complete_with", fake_complete_with)
    monkeypatch.setattr(llm, "config", lambda: {
        **llm.DEFAULT_CONFIG, "model": "m",
        "fallbacks": [{"provider": "ollama", "model": "b"}]})
    try:
        asyncio.run(llm.complete("s", [{"role": "user", "content": "x"}]))
        raise AssertionError("should have raised")
    except RuntimeError as e:
        assert "401" in str(e)


def test_browser_images_reach_openai_as_images_after_all_tool_results():
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'test-png'}}
    messages = [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'screen', 'content': [
            {'type': 'text', 'text': 'Browser viewport'}, image]},
        {'type': 'tool_result', 'tool_use_id': 'other', 'content': 'Other result'}]}]
    adapted = llm._messages_to_openai('system', messages)
    assert [row['role'] for row in adapted] == ['system', 'tool', 'tool', 'user']
    assert adapted[1]['content'] == 'Browser viewport'
    assert adapted[-1]['content'][-1] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,test-png'}}
    assert 'test-png' not in adapted[1]['content']


def test_browser_images_reach_anthropic_unchanged(monkeypatch):
    captured = []
    async def post(url, payload, headers):
        captured.append(payload)
        return {'content': [{'type': 'text', 'text': 'Seen'}]}
    monkeypatch.setattr(llm, '_post', post)
    blocks = [{'type': 'text', 'text': 'Page'}, {'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': 'test-png'}}]
    messages = [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'screen', 'content': blocks}]}]
    asyncio.run(llm._complete_with({'wire': 'anthropic', 'model': 'test', 'max_tokens': 20,
        'key': '', 'base_url': 'https://test.invalid'}, 'system', messages, None, 'test'))
    assert captured[0]['messages'][0]['content'][0]['content'] == blocks


# ── fallback cooldown and exhausted plans ───────────────────────────────

import httpx
import pytest


@pytest.fixture(autouse=True)
def _fresh_cooldowns(monkeypatch):
    monkeypatch.setattr(llm, "_down", {})


def test_deepseek_preset_resolves():
    r = llm._resolved({**llm.DEFAULT_CONFIG, "provider": "deepseek", "model": "deepseek-v4-pro"})
    assert (r["wire"], r["base_url"], r["key_env"]) == (
        "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY")


def test_exhausted_plan_is_not_retried(monkeypatch):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(429, json={"type": "error", "error": {
            "type": "rate_limit_error",
            "message": "Token Plan usage limit reached: Upgrade your Token Plan"}})

    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="plan exhausted"):
        asyncio.run(llm._post("https://x/v1/messages", {"max_tokens": 10}, {}))
    assert len(hits) == 1


def test_transient_429_is_still_retried(monkeypatch):
    hits = []

    def handler(request):
        hits.append(1)
        if len(hits) < 2:
            return httpx.Response(429, json={"error": {"message": "Too many requests"}})
        return httpx.Response(200, json={"content": []})

    async def no_sleep(_):
        pass

    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    assert asyncio.run(llm._post("https://x/v1/messages", {"max_tokens": 10}, {})) == {"content": []}
    assert len(hits) == 2


def _fake_primary_down(calls):
    async def fake(r, system, messages, tools, agent, on_text=None):
        calls.append(r["model"])
        if r["model"] == "primary-model":
            raise RuntimeError("LLM HTTP 429 (plan exhausted): used up")
        return {"content": [{"type": "text", "text": "ok"}]}
    return fake


def test_primary_skipped_while_cooling_down(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_complete_with", _fake_primary_down(calls))
    monkeypatch.setattr(llm, "config", lambda: {
        **llm.DEFAULT_CONFIG, "model": "primary-model",
        "fallbacks": [{"provider": "ollama", "model": "backup-model"}]})
    msgs = [{"role": "user", "content": "x"}]
    asyncio.run(llm.complete("s", msgs))
    asyncio.run(llm.complete("s", msgs))
    assert calls == ["primary-model", "backup-model", "backup-model"]


def test_primary_tried_again_after_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_complete_with", _fake_primary_down(calls))
    monkeypatch.setattr(llm, "config", lambda: {
        **llm.DEFAULT_CONFIG, "model": "primary-model",
        "fallbacks": [{"provider": "ollama", "model": "backup-model"}]})
    msgs = [{"role": "user", "content": "x"}]
    asyncio.run(llm.complete("s", msgs))
    for slot in list(llm._down):
        llm._down[slot] = 0
    asyncio.run(llm.complete("s", msgs))
    assert calls == ["primary-model", "backup-model", "primary-model", "backup-model"]


def test_without_fallback_primary_is_tried_every_time(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_complete_with", _fake_primary_down(calls))
    monkeypatch.setattr(llm, "config", lambda: {**llm.DEFAULT_CONFIG, "model": "primary-model"})
    msgs = [{"role": "user", "content": "x"}]
    for _ in range(2):
        with pytest.raises(RuntimeError, match="plan exhausted"):
            asyncio.run(llm.complete("s", msgs))
    assert calls == ["primary-model", "primary-model"]


# ── effort mapping and streaming ────────────────────────────────────────

def _resolved_for(provider, model, effort, base_url=""):
    return llm._resolved({**llm.DEFAULT_CONFIG, "provider": provider, "model": model,
                          "effort": effort, "base_url": base_url})


def test_effort_maps_to_each_providers_own_field():
    assert llm._thinking_params(_resolved_for("deepseek", "deepseek-flash", "low")) == {"reasoning_effort": "low"}
    assert llm._thinking_params(_resolved_for("deepseek", "deepseek-flash", "medium")) == {"reasoning_effort": "high"}
    assert llm._thinking_params(_resolved_for("deepseek", "deepseek-flash", "none")) == {"thinking": {"type": "disabled"}}
    assert llm._thinking_params(_resolved_for("anthropic", "claude-opus-5-5", "high")) == {"output_config": {"effort": "high"}}
    assert llm._thinking_params(_resolved_for("anthropic", "claude-opus-5-5", "none")) == {"output_config": {"effort": "low"}}
    assert llm._thinking_params(_resolved_for("minimax", "MiniMax-M3", "low")) == {}
    assert llm._thinking_params(_resolved_for("minimax", "MiniMax-M3", "high")) == {"thinking": {"type": "adaptive"}}
    assert llm._thinking_params(_resolved_for("minimax", "MiniMax-M3.1-Flash", "low")) == {"output_config": {"effort": "low"}}
    assert llm._thinking_params(_resolved_for("minimax", "MiniMax-M3.1-Flash", "none")) == {"output_config": {"effort": "low"}}
    assert llm._thinking_params(_resolved_for("minimax", "MiniMax-M3.1-Flash", "max")) == {"output_config": {"effort": "max"}}
    assert llm._thinking_params(_resolved_for("ollama", "llama3", "max")) == {}
    assert llm._thinking_params(_resolved_for("custom-openai", "x", "max", "https://host/v1")) == {}


def test_unknown_effort_falls_back_to_default():
    assert _resolved_for("deepseek", "deepseek-flash", "turbo")["effort"] == llm.DEFAULT_EFFORT
    merged = llm.agent_config({"model": "m", "effort": "max"})
    assert merged["effort"] == "max"
    assert llm.agent_config({"model": "m", "effort": "bogus"})["effort"] == llm.DEFAULT_EFFORT


def _sse(events, done=False):
    body = "".join(f"event: {e.get('type', 'message')}\ndata: {json.dumps(e)}\n\n" for e in events)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def test_anthropic_stream_assembles_text_and_tool_use(monkeypatch):
    seen = []

    async def on_text(delta):
        seen.append(delta)

    events = [
        {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 12}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "terminal", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "ls'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' -la"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})))
    data = asyncio.run(llm._post_stream("https://x/v1/messages", {"stream": True}, {}, "anthropic", on_text))
    assert seen == ["Hel", "lo"]
    assert data["content"][0] == {"type": "text", "text": "Hello"}
    assert data["content"][1] == {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"cmd": "ls -la"}}
    assert data["stop_reason"] == "tool_use"
    assert data["usage"] == {"input_tokens": 12, "output_tokens": 7}


def test_openai_stream_assembles_tool_calls_and_usage(monkeypatch):
    seen = []

    async def on_text(delta):
        seen.append(delta)

    chunks = [
        {"model": "deepseek-flash", "choices": [{"delta": {"role": "assistant", "content": "Sure"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": ", one sec"}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                "function": {"name": "terminal", "arguments": ""}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":'}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "pwd"}'}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
    ]
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, content=_sse(chunks, done=True), headers={"content-type": "text/event-stream"})))
    data = asyncio.run(llm._post_stream("https://x/v1/chat/completions", {"stream": True}, {}, "openai", on_text))
    resp = llm._response_from_openai(data)
    assert seen == ["Sure", ", one sec"]
    assert llm.text_of(resp) == "Sure, one sec"
    assert llm.tool_uses(resp)[0]["input"] == {"cmd": "pwd"}
    assert resp["stop_reason"] == "tool_use"
    assert data["usage"] == {"prompt_tokens": 30, "completion_tokens": 9}


def test_stream_refusal_before_bytes_is_reported(monkeypatch):
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(401, json={"error": {"message": "bad key"}})))

    async def on_text(delta):
        pass

    with pytest.raises(RuntimeError, match="HTTP 401"):
        asyncio.run(llm._post_stream("https://x/v1/messages", {}, {}, "anthropic", on_text))


def test_complete_resets_listener_on_each_attempt(monkeypatch):
    seen = []

    async def fake(r, system, messages, tools, agent, on_text=None):
        await on_text(None)
        if r["model"] == "primary-model":
            await on_text("partial")
            raise RuntimeError("LLM HTTP 503: down")
        await on_text("ok")
        return {"content": [{"type": "text", "text": "ok"}]}

    async def on_text(delta):
        seen.append(delta)

    monkeypatch.setattr(llm, "_complete_with", fake)
    monkeypatch.setattr(llm, "config", lambda: {
        **llm.DEFAULT_CONFIG, "model": "primary-model",
        "fallbacks": [{"provider": "ollama", "model": "backup-model"}]})
    asyncio.run(llm.complete("s", [{"role": "user", "content": "x"}], on_text=on_text))
    assert seen == [None, "partial", None, "ok"]


def test_thinking_streams_on_both_wires(monkeypatch):
    seen = []

    async def on_text(delta, kind="text"):
        seen.append((kind, delta))

    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Yes."}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
    ]
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})))
    data = asyncio.run(llm._post_stream("https://x/v1/messages", {}, {}, "anthropic", on_text))
    assert seen == [("thinking", "hmm "), ("text", "Yes.")]
    assert data["content"][0] == {"type": "thinking", "thinking": "hmm ", "signature": "sig"}

    seen.clear()
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "let me see"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, content=_sse(chunks, done=True), headers={"content-type": "text/event-stream"})))
    data = asyncio.run(llm._post_stream("https://x/v1/chat/completions", {}, {}, "openai", on_text))
    assert seen == [("thinking", "let me see"), ("text", "Done")]
    assert llm.text_of(llm._response_from_openai(data)) == "Done"


def test_a_thinking_model_gets_room_to_answer_after_it_thinks(monkeypatch):
    """Thinking counts against max_tokens: MiniMax M3.1 Flash at medium effort
    spent the whole 3000-token default and cut webdev's replies off mid-call."""
    def resolved(**c):
        monkeypatch.setattr(llm, "config", lambda: {**llm.DEFAULT_CONFIG, "max_tokens": 3000, **c})
        return llm._resolved()["max_tokens"]

    assert resolved(provider="minimax", model="MiniMax-M3.1-Flash") == llm.THINKING_MAX_TOKENS
    assert resolved(provider="minimax", model="MiniMax-M2.5") == llm.THINKING_MAX_TOKENS
    assert resolved(provider="minimax", model="MiniMax-M3", effort="high") == llm.THINKING_MAX_TOKENS
    assert resolved(provider="deepseek", model="deepseek-flash") == llm.THINKING_MAX_TOKENS
    # no thinking, no reason to change what the owner set
    assert resolved(provider="minimax", model="MiniMax-M3", effort="low") == 3000
    assert resolved(provider="deepseek", model="deepseek-flash", effort="none") == 3000
    assert resolved(provider="ollama", model="llama") == 3000
    # a larger limit the owner set is kept
    assert resolved(provider="minimax", model="MiniMax-M3.1-Flash", max_tokens=32000) == 32000
