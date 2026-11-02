"""Provider-agnostic LLM client.

The orchestrator speaks the Anthropic messages shape internally (content
blocks, tool_use / tool_result). Providers with an Anthropic-compatible
endpoint pass straight through; OpenAI-compatible providers (OpenAI,
Ollama, LM Studio, or any custom base_url) go through a wire adapter in
both directions, so the rest of the app never cares which one is active.

Configured in settings.yaml:

llm:
  provider: minimax            # a PRESETS key
  model: MiniMax-M3
  base_url: ""                 # override, required for custom-* providers
  max_tokens: 3000
  effort: low                  # none | low | medium | high | max: how hard the model thinks
  fallbacks:                   # tried in order while the model above is down
    - {provider: deepseek, model: deepseek-v4-pro}

The API key lives in .env under the preset's key_env name; a fallback's key
under its own preset's key_env.
"""
import asyncio
import contextvars
import json
import logging
import os
import re
import time
import uuid

import httpx

import settings as cfg

MAX_TOKENS = 3000

# Every model call made inside an agent's turn (its own, and a research tool's)
# adds to that turn's tally, so a run can be judged on what it cost as well as
# how it went. Set by the turn's own task, so nothing outside it is counted.
TURN_USAGE: contextvars.ContextVar = contextvars.ContextVar("turn_usage", default=None)

PRESETS = {
    "minimax": {"wire": "anthropic", "base_url": "https://api.minimax.io/anthropic",
                "key_env": "MINIMAX_API_KEY", "label": "MiniMax",
                "models": ["MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.5"]},
    "anthropic": {"wire": "anthropic", "base_url": "https://api.anthropic.com",
                  "key_env": "ANTHROPIC_API_KEY", "label": "Anthropic Claude",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                             "claude-fable-5-1"]},
    "openai": {"wire": "openai", "base_url": "https://api.openai.com/v1",
               "key_env": "OPENAI_API_KEY", "label": "OpenAI", "models": []},
    "deepseek": {"wire": "openai", "base_url": "https://api.deepseek.com/v1",
                 "key_env": "DEEPSEEK_API_KEY", "label": "DeepSeek",
                 "models": ["deepseek-v4-pro", "deepseek-flash"]},
    "ollama": {"wire": "openai", "base_url": "http://localhost:11434/v1",
               "key_env": "", "label": "Ollama (local)", "models": []},
    "lmstudio": {"wire": "openai", "base_url": "http://localhost:1234/v1",
                 "key_env": "", "label": "LM Studio (local)", "models": []},
    "missioncontrol-cloud": {"wire": "anthropic", "base_url": "",
                             "key_env": "MC_CLOUD_TOKEN",
                             "label": "Mission Control Cloud",
                             "models": ["MiniMax-M3", "MiniMax-M2.5"]},
    "custom-anthropic": {"wire": "anthropic", "base_url": "",
                         "key_env": "CUSTOM_LLM_API_KEY",
                         "label": "Custom (Anthropic-compatible)", "models": []},
    "custom-openai": {"wire": "openai", "base_url": "",
                      "key_env": "CUSTOM_LLM_API_KEY",
                      "label": "Custom (OpenAI-compatible)", "models": []},
}

# How hard the model thinks before it answers. Lower is faster and cheaper;
# "none" turns thinking off where the provider allows it. Each provider has
# its own knob for this, mapped in _thinking_params.
EFFORTS = ("none", "low", "medium", "high", "max")
DEFAULT_EFFORT = "low"

DEFAULT_CONFIG = {"provider": "minimax", "model": "MiniMax-M3",
                  "base_url": "", "max_tokens": MAX_TOKENS, "effort": DEFAULT_EFFORT}


def config() -> dict:
    c = dict(DEFAULT_CONFIG)
    c.update(cfg.load().get("llm") or {})
    return c


def agent_config(override: dict | None) -> dict | None:
    """A per-agent model override merged over the team default, or None when the
    agent has none and should just use the team's model.

    Switching provider drops the team's base_url: a custom endpoint that belongs
    to one provider would otherwise be sent to another and fail confusingly.
    """
    if not override:
        return None
    provider = (override.get("provider") or "").strip()
    model = (override.get("model") or "").strip()
    effort = override.get("effort") if override.get("effort") in EFFORTS else ""
    if not (provider or model or effort):
        return None
    merged = dict(config())
    if provider and provider != merged.get("provider"):
        merged["base_url"] = ""
    if provider:
        merged["provider"] = provider
    if model:
        merged["model"] = model
    if override.get("base_url"):
        merged["base_url"] = override["base_url"]
    if override.get("max_tokens"):
        merged["max_tokens"] = override["max_tokens"]
    if override.get("effort") in EFFORTS:
        merged["effort"] = override["effort"]
    return merged


# A model's thinking counts against max_tokens, so on a model that thinks the
# app's 3000-token default can be spent before a word of the answer: MiniMax
# M3.1 Flash at medium effort cut off webdev's replies at exactly 3000 again
# and again. Anthropic's own guidance for a non-streaming request is ~16000.
THINKING_MAX_TOKENS = 16000


def _thinks(r: dict) -> bool:
    """True when the request _thinking_params builds for r makes the model think."""
    effort = r.get("effort") or DEFAULT_EFFORT
    base = r["base_url"]
    if r["wire"] == "anthropic":
        if "anthropic.com" in base:
            return True
        if "minimax" in base:
            model = (r.get("model") or "").lower()
            if "flash" in model or "m3.1" in model or "m2" in model:
                return True
            return effort in ("medium", "high", "max")
        return False
    return "deepseek" in base and effort != "none"


def history_is_bound(c: dict | None = None) -> bool:
    """True when the provider ties each thinking block to the exact history
    that produced it (Anthropic's own API, for accounts created from September
    2026): earlier messages in a turn must then be sent back byte for byte, so
    nothing may trim them."""
    r = _resolved(c)
    return r["wire"] == "anthropic" and "anthropic.com" in r["base_url"]


def _resolved(c: dict | None = None) -> dict:
    c = c or config()
    preset = PRESETS.get(c.get("provider") or "", PRESETS["custom-openai"])
    base = (c.get("base_url") or "").rstrip("/") or preset["base_url"].rstrip("/")
    key = ""
    if preset["key_env"]:
        key = os.getenv(preset["key_env"]) or cfg.env_get(preset["key_env"]) or ""
    effort = c.get("effort") if c.get("effort") in EFFORTS else DEFAULT_EFFORT
    r = {"wire": preset["wire"], "base_url": base, "key": key,
         "key_env": preset["key_env"], "model": c.get("model") or "",
         "max_tokens": int(c.get("max_tokens") or MAX_TOKENS),
         "provider": c.get("provider"), "effort": effort}
    if _thinks(r):
        r["max_tokens"] = max(r["max_tokens"], THINKING_MAX_TOKENS)
    return r


def _thinking_params(r: dict) -> dict:
    """The provider's own request fields for the effort level. Providers whose
    knob is unknown get nothing extra, so a custom endpoint never sees a field
    it would reject."""
    effort = r.get("effort") or DEFAULT_EFFORT
    base = r["base_url"]
    if r["wire"] == "anthropic":
        if "anthropic.com" in base:
            # Claude thinks on every request; effort sets the depth.
            return {"output_config": {"effort": "low" if effort == "none" else effort}}
        if "minimax" in base:
            model = (r.get("model") or "").lower()
            if "flash" in model or "m3.1" in model:
                # M3.1 Flash always thinks and defaults to max; effort sets the depth.
                return {"output_config": {"effort": "low" if effort == "none" else effort}}
            # MiniMax-M3 thinks only when asked; M2.x always does and takes no knob.
            return {"thinking": {"type": "adaptive"}} if effort in ("medium", "high", "max") else {}
        return {}
    if "deepseek" in base:
        if effort == "none":
            return {"thinking": {"type": "disabled"}}
        return {"reasoning_effort": {"low": "low", "medium": "high", "high": "high", "max": "max"}[effort]}
    return {}


# Tests swap in an httpx.MockTransport; None means the real network.
_transport = None

# A refusal that says the plan or balance is used up will not clear in a few
# seconds of backoff, so it goes to the fallback (or fails) at once instead of
# costing every call the full retry wait.
_EXHAUSTED = ("usage limit", "quota", "insufficient", "credit", "balance", "billing")


def _exhausted(status: int, text: str) -> bool:
    low = text.lower()
    return status in (400, 402, 429) and any(w in low for w in _EXHAUSTED)


# What backing off can fix: the provider is busy or a hop in front of it gave up.
_RETRY_STATUS = (408, 429, 500, 502, 503, 504, 529)


async def _post(url: str, payload: dict, headers: dict) -> dict:
    delay = 2.0
    # A non-streaming reply arrives all at once, so the read timeout has to
    # cover the whole generation: a larger output allowance needs longer.
    read = min(600.0, max(180.0, int(payload.get("max_tokens") or 0) / 40))
    async with httpx.AsyncClient(timeout=httpx.Timeout(read, connect=15.0),
                                 transport=_transport) as client:
        for attempt in range(4):
            try:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    return r.json()
                if _exhausted(r.status_code, r.text):
                    raise RuntimeError(f"LLM HTTP {r.status_code} (plan exhausted): {r.text[:400]}")
                if r.status_code in _RETRY_STATUS and attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"LLM connection failed: {exc}") from exc
    raise RuntimeError("LLM: retries exhausted")


async def _post_stream(url: str, payload: dict, headers: dict, wire: str, on_text) -> dict:
    """The streaming twin of _post: the same request with stream=true, the
    model's text handed to on_text as it arrives, and the finished reply
    assembled into the shape the non-streaming endpoint would have returned.
    A refusal before any bytes flow is retried like _post; a drop mid-stream
    is not, since part of the reply has already been shown."""
    delay = 2.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0),
                                 transport=_transport) as client:
        for attempt in range(4):
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode("utf-8", "replace")
                        if _exhausted(r.status_code, text):
                            raise RuntimeError(f"LLM HTTP {r.status_code} (plan exhausted): {text[:400]}")
                        if r.status_code in _RETRY_STATUS and attempt < 3:
                            await asyncio.sleep(delay)
                            delay *= 2
                            continue
                        raise RuntimeError(f"LLM HTTP {r.status_code}: {text[:400]}")
                    events = _sse_events(r.aiter_lines())
                    if wire == "anthropic":
                        return await _assemble_anthropic(events, on_text)
                    return await _assemble_openai(events, on_text)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < 3 and not getattr(exc, "_mid_stream", False):
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"LLM connection failed: {exc}") from exc
    raise RuntimeError("LLM: retries exhausted")


async def _sse_events(lines):
    """Server-sent events as parsed JSON, one per yielded item."""
    data: list[str] = []
    try:
        async for line in lines:
            if line == "":
                if data:
                    raw = "\n".join(data)
                    data = []
                    if raw.strip() == "[DONE]":
                        return
                    try:
                        yield json.loads(raw)
                    except ValueError:
                        continue
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        exc._mid_stream = True
        raise
    if data:
        raw = "\n".join(data)
        if raw.strip() != "[DONE]":
            try:
                yield json.loads(raw)
            except ValueError:
                pass


def _merge_usage(usage: dict, later: dict):
    """The opening event has the input count and the closing one the output count.
    Some providers repeat fields in the closing event with a zero or a part of the
    total, which must not replace the real number."""
    for key, value in later.items():
        known = usage.get(key)
        usage[key] = max(known, value) if isinstance(known, (int, float)) and isinstance(value, (int, float)) else value


async def _assemble_anthropic(events, on_text) -> dict:
    blocks: list[dict] = []
    partial: dict[int, str] = {}
    out: dict = {"content": blocks}
    usage: dict = {}
    async for ev in events:
        kind = ev.get("type")
        if kind == "message_start":
            usage.update((ev.get("message") or {}).get("usage") or {})
            out["model"] = (ev.get("message") or {}).get("model")
        elif kind == "content_block_start":
            block = dict(ev.get("content_block") or {})
            if block.get("type") == "tool_use":
                partial[ev["index"]] = ""
            while len(blocks) <= ev["index"]:
                blocks.append({"type": "text", "text": ""})
            blocks[ev["index"]] = block
        elif kind == "content_block_delta":
            idx = ev.get("index", len(blocks) - 1)
            if idx >= len(blocks):
                continue
            delta = ev.get("delta") or {}
            block = blocks[idx]
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
                if on_text and delta.get("text"):
                    await on_text(delta["text"])
            elif delta.get("type") == "input_json_delta":
                partial[idx] = partial.get(idx, "") + delta.get("partial_json", "")
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                if on_text and delta.get("thinking"):
                    await on_text(delta["thinking"], "thinking")
            elif delta.get("type") == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
        elif kind == "content_block_stop":
            idx = ev.get("index")
            if idx in partial:
                raw = partial.pop(idx)
                try:
                    blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    blocks[idx]["input"] = raw   # the harness rejects malformed input
        elif kind == "message_delta":
            out.update({k: v for k, v in (ev.get("delta") or {}).items() if v is not None})
            _merge_usage(usage, ev.get("usage") or {})
        elif kind == "error":
            err = ev.get("error") or {}
            raise RuntimeError(f"LLM stream error: {err.get('type', '')} {err.get('message', '')}"[:400])
    out["usage"] = usage
    return out


async def _assemble_openai(events, on_text) -> dict:
    text = ""
    calls: dict[int, dict] = {}
    finish = None
    usage: dict = {}
    model = None
    async for ev in events:
        if ev.get("error"):
            err = ev["error"]
            raise RuntimeError(f"LLM stream error: {err.get('message', err) if isinstance(err, dict) else err}"[:400])
        model = ev.get("model") or model
        if ev.get("usage"):
            usage = ev["usage"]
        for choice in ev.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content") and on_text:
                await on_text(delta["reasoning_content"], "thinking")
            if delta.get("content"):
                text += delta["content"]
                if on_text:
                    await on_text(delta["content"])
            for call in delta.get("tool_calls") or []:
                idx = call.get("index", len(calls))
                slot = calls.setdefault(idx, {"id": "", "type": "function",
                                              "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message: dict = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"model": model, "usage": usage,
            "choices": [{"message": message, "finish_reason": finish}]}


# ── OpenAI wire adapters ────────────────────────────────────────────────

def _tools_to_openai(tools: list) -> list:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object"})}}
            for t in tools]


def _messages_to_openai(system: str, messages: list) -> list:
    out = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text, calls = "", []
            for b in content:
                if b.get("type") == "text":
                    text += b.get("text", "")
                elif b.get("type") == "tool_use":
                    calls.append({"id": b["id"], "type": "function",
                                  "function": {"name": b["name"],
                                               "arguments": json.dumps(b.get("input") or {})}})
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user turn: tool results and/or text/image blocks
            visual = []
            for b in content:
                if b.get("type") == "tool_result":
                    res = b.get("content", "")
                    if isinstance(res, list):
                        parts = []
                        for block in res:
                            if block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif block.get("type") == "image":
                                visual.append({"type": "text", "text": f"Image from tool call {b['tool_use_id']}:"})
                                visual.append(_image_to_openai(block))
                        res = "\n".join(parts)
                    elif not isinstance(res, str):
                        res = json.dumps(res)
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": res})
                elif b.get("type") == "text":
                    visual.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "image":
                    visual.append(_image_to_openai(b))
            # Finish ALL tool replies before the user image message. OpenAI
            # rejects intervening user messages when tool calls are unresolved.
            if visual:
                if len(visual) == 1 and visual[0]['type'] == 'text':
                    out.append({"role": "user", "content": visual[0]['text']})
                else:
                    out.append({"role": "user", "content": visual})
    return out


def _image_to_openai(block: dict) -> dict:
    source = block['source']
    url = (f"data:{source['media_type']};base64,{source['data']}"
           if source['type'] == 'base64' else source['url'])
    return {"type": "image_url", "image_url": {"url": url}}


def _response_from_openai(data: dict) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            # Preserve the malformed payload for the harness to reject. An
            # empty object could accidentally run a tool with default effects.
            args = fn.get("arguments")
        blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                       "name": fn.get("name", ""), "input": args})
    stop = {"length": "max_tokens", "tool_calls": "tool_use", "stop": "end_turn"}.get(choice.get("finish_reason"))
    return {"content": blocks, **({"stop_reason": stop} if stop else {})}


# ── Public interface (unchanged for the orchestrator) ───────────────────

def _fallback_configs() -> list[dict]:
    """settings llm.fallbacks: [{provider, model, base_url?}] tried in order."""
    out = []
    for f in (config().get("fallbacks") or []):
        if isinstance(f, dict) and f.get("provider") and f.get("model"):
            out.append({**DEFAULT_CONFIG, **f})
    return out


_RETRYABLE = ("HTTP 408", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "HTTP 529",
              "plan exhausted", "connection failed", "retries exhausted")


def refused(exc: BaseException) -> bool:
    """The provider read this request and will not take it, so sending it again gets the
    same answer. Unlike being down, this is about what is in the transcript."""
    text = str(exc)
    return "plan exhausted" not in text and bool(re.search(r"LLM HTTP (400|413|422)\b", text))

# A model that just failed in one of those ways is skipped for this long when
# a fallback can take over, so the later calls of a turn do not each wait out
# the primary's retries before landing on the fallback. Without a fallback it
# is tried every time, as before.
COOLDOWN = 120.0
_down: dict[str, float] = {}


def _slot(r: dict) -> str:
    return f"{r.get('provider')}|{r['model']}|{r['base_url']}"


def _usage_counts(usage: dict, wire: str) -> tuple[int, int, int]:
    """(input charged in full, output, input read from the provider's cache). Cache
    writes cost at least the full rate, so they count as input."""
    if wire == "anthropic":
        return (int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
                int(usage.get("output_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0))
    details = usage.get("prompt_tokens_details") or {}
    cached = int(usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0)
    return max(0, int(usage.get("prompt_tokens") or 0) - cached), int(usage.get("completion_tokens") or 0), cached


def _record_usage(agent: str | None, r: dict, usage: dict | None, wire: str):
    if not usage:
        return
    try:
        import db
        tin, tout, cached = _usage_counts(usage, wire)
        tally = TURN_USAGE.get()
        if isinstance(tally, dict):
            tally["in"] = tally.get("in", 0) + tin
            tally["out"] = tally.get("out", 0) + tout
        db.add_usage(agent or "", r.get("provider") or "", r["model"], tin, tout, cached)
    except Exception:
        pass


# Most of every call is what the last call already sent: the prompt, the tools, the
# transcript so far. Providers that take a breakpoint read that prefix back at a tenth
# of the price. Unknown hosts get nothing, as with effort, so a custom endpoint never
# sees a field it would reject. DeepSeek caches on its own and needs no field.
def _caches(r: dict) -> bool:
    return r["wire"] == "anthropic" and ("anthropic.com" in r["base_url"] or "minimax" in r["base_url"])


CACHE_MARK = {"type": "ephemeral"}
_THINKING = ("thinking", "redacted_thinking")


def _own_thinking(messages: list, slot: str) -> list:
    """Providers reject thinking written by anyone else, so after /model or a fallback
    what the last model wrote stays behind. Unmarked blocks are from before this was
    recorded; they go out as they are, and Session strips them if they get refused."""
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list) or not any(isinstance(b, dict) and "_by" in b for b in content):
            out.append(message)
            continue
        kept = [{k: v for k, v in b.items() if k != "_by"} for b in content
                if not (isinstance(b, dict) and b.get("_by") not in (None, slot))]
        out.append({**message, "content": kept or [{"type": "text", "text": "(no text)"}]})
    return out


def _with_breakpoint(messages: list) -> list:
    """Marks the end of the newest message, so the next call reads all of this back. A
    copy: the mark belongs to this one request, and the transcript is what gets stored."""
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = list(content)
    else:
        return messages
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        # Thinking cannot carry a mark and an empty text block is rejected with one.
        if isinstance(block, dict) and block.get("type") not in _THINKING and (
                block.get("type") != "text" or (block.get("text") or "").strip()):
            blocks[i] = {**block, "cache_control": CACHE_MARK}
            return [*messages[:-1], {**last, "content": blocks}]
    return messages


def _anthropic_payload(r: dict, system: str, messages: list, tools: list | None) -> dict:
    outgoing = _own_thinking(messages, _slot(r))
    payload = {"model": r["model"], "max_tokens": r["max_tokens"], "system": system,
               "messages": outgoing, **_thinking_params(r)}
    if tools:
        payload["tools"] = tools
    if _caches(r):
        # Tools come before the system prompt in the prefix, so this one mark covers both.
        payload["system"] = [{"type": "text", "text": system, "cache_control": CACHE_MARK}]
        payload["messages"] = _with_breakpoint(outgoing)
    return payload


async def _complete_with(r: dict, system: str, messages: list,
                         tools: list | None, agent: str | None, on_text=None) -> dict:
    """One request. With on_text the reply streams: every text delta is awaited
    through it as on_text(delta), the model's thinking as on_text(delta,
    "thinking"), and None is sent first so a listener can drop anything an
    earlier attempt showed."""
    if on_text:
        await on_text(None)
    if r["wire"] == "anthropic":
        payload = _anthropic_payload(r, system, messages, tools)
        headers = {"x-api-key": r["key"], "anthropic-version": "2023-06-01"}
        url = f"{r['base_url']}/v1/messages"
        if on_text:
            data = await _post_stream(url, {**payload, "stream": True}, headers, "anthropic", on_text)
        else:
            data = await _post(url, payload, headers)
        _record_usage(agent, r, data.get("usage"), "anthropic")
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") in _THINKING:
                block["_by"] = _slot(r)
        return data
    payload = {"model": r["model"], "max_tokens": r["max_tokens"],
               "messages": _messages_to_openai(system, messages), **_thinking_params(r)}
    if tools:
        payload["tools"] = _tools_to_openai(tools)
    headers = {"Authorization": f"Bearer {r['key'] or 'local'}"}
    url = f"{r['base_url']}/chat/completions"
    if on_text:
        data = await _post_stream(url, {**payload, "stream": True,
                                        "stream_options": {"include_usage": True}},
                                  headers, "openai", on_text)
    else:
        data = await _post(url, payload, headers)
    _record_usage(agent, r, data.get("usage"), "openai")
    return _response_from_openai(data)


async def complete(system: str, messages: list, tools: list | None = None,
                   agent: str | None = None, conf: dict | None = None, on_text=None) -> dict:
    """conf is a per-agent model override; without one the team default is used.
    Fallbacks stay team-wide either way, so an agent on a flaky model still has
    somewhere to land. on_text, when given, receives the reply's text as it is
    generated (and None whenever a fresh attempt starts)."""
    r = _resolved(conf)
    if not r["model"]:
        raise RuntimeError("No model configured. Open Settings and pick an LLM.")
    log = logging.getLogger("missioncontrol.llm")
    fallbacks = []
    for fb in _fallback_configs():
        fr = _resolved(fb)
        if fr["key_env"] and not fr["key"]:
            continue
        fallbacks.append((fb, fr))
    primary_error = None
    if fallbacks and _down.get(_slot(r), 0) > time.time():
        log.info("%s/%s is cooling down after a failure; using the fallback",
                 r["provider"], r["model"])
    else:
        try:
            data = await _complete_with(r, system, messages, tools, agent, on_text)
            _down.pop(_slot(r), None)
            return data
        except RuntimeError as exc:
            if not any(t in str(exc) for t in _RETRYABLE):
                raise
            primary_error = exc
            _down[_slot(r)] = time.time() + COOLDOWN
            if fallbacks:
                log.warning("primary model failed (%s); falling back to %s/%s",
                            str(exc)[:120], fallbacks[0][0]["provider"], fallbacks[0][0]["model"])
    for fb, fr in fallbacks:
        try:
            return await _complete_with(fr, system, messages, tools, agent, on_text)
        except RuntimeError as exc:
            log.warning("fallback %s/%s failed: %s", fb["provider"], fb["model"], str(exc)[:120])
            primary_error = exc
            continue
    raise primary_error


def truncated(response: dict) -> bool:
    """The reply stopped at the output limit, so whatever it was writing is unfinished."""
    return response.get("stop_reason") == "max_tokens"


def text_of(response: dict) -> str:
    return "\n".join(
        b.get("text", "") for b in response.get("content", []) if b.get("type") == "text"
    ).strip()


def tool_uses(response: dict) -> list:
    uses = []
    for b in response.get("content", []) or []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        if not isinstance(b.get("name"), str):
            b["name"] = ""
        if not isinstance(b.get("id"), str) or not b["id"]:
            b["id"] = f"call_{uuid.uuid4().hex[:12]}"
        uses.append(b)
    return uses


# ── Settings-UI helpers ─────────────────────────────────────────────────

async def list_models(provider: str, base_url: str = "") -> list[str]:
    """Best-effort model list for the picker; static for anthropic-wire clouds."""
    preset = PRESETS.get(provider) or {}
    if preset.get("models"):
        return preset["models"]
    base = (base_url or preset.get("base_url") or "").rstrip("/")
    if not base:
        return []
    key = cfg.env_get(preset.get("key_env") or "") or "local"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "ollama":
                r = await client.get(base.removesuffix("/v1") + "/api/tags")
                return sorted(m["name"] for m in r.json().get("models", []))
            r = await client.get(f"{base}/models",
                                 headers={"Authorization": f"Bearer {key}"})
            return sorted(m["id"] for m in r.json().get("data", []))
    except Exception:
        return []


async def test_connection(conf: dict) -> dict:
    """One tiny completion with the given config; returns ok/error."""
    try:
        r = _resolved({**DEFAULT_CONFIG, **conf})
        if r["wire"] == "anthropic":
            payload = {"model": r["model"], "max_tokens": 20, "system": "Reply with OK.",
                       "messages": [{"role": "user", "content": "ping"}], **_thinking_params(r)}
            headers = {"x-api-key": r["key"], "anthropic-version": "2023-06-01"}
            await _post(f"{r['base_url']}/v1/messages", payload, headers)
        else:
            payload = {"model": r["model"], "max_tokens": 20,
                       "messages": [{"role": "system", "content": "Reply with OK."},
                                    {"role": "user", "content": "ping"}], **_thinking_params(r)}
            headers = {"Authorization": f"Bearer {r['key'] or 'local'}"}
            await _post(f"{r['base_url']}/chat/completions", payload, headers)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


# ── media helpers ───────────────────────────────────────────────────────

async def quick(system: str, user_text: str, max_tokens: int = 700,
                agent: str | None = None, conf: dict | None = None) -> str:
    """One-shot completion for internal jobs (summaries, syntheses)."""
    resp = await complete(system, [{"role": "user", "content": user_text}],
                          None, agent=agent, conf={**(conf or config()), "max_tokens": max_tokens})
    return text_of(resp)


async def describe_image(data_b64: str, mime: str, agent: str | None = None) -> str:
    """Describe an image with the active model when it supports vision."""
    r = _resolved()
    try:
        if r["wire"] == "anthropic":
            payload = {"model": r["model"], "max_tokens": max(400, r["max_tokens"]) if "anthropic.com" in r["base_url"] else 400,
                       "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64",
                            "media_type": mime, "data": data_b64}},
                           {"type": "text", "text":
                            "Describe this image factually and read out any text in it."}]}]}
            headers = {"x-api-key": r["key"], "anthropic-version": "2023-06-01"}
            data = await _post(f"{r['base_url']}/v1/messages", payload, headers)
            _record_usage(agent, r, data.get("usage"), "anthropic")
            return text_of(data)
        payload = {"model": r["model"], "max_tokens": 400,
                   "messages": [{"role": "user", "content": [
                       {"type": "image_url", "image_url":
                        {"url": f"data:{mime};base64,{data_b64}"}},
                       {"type": "text", "text":
                        "Describe this image factually and read out any text in it."}]}]}
        headers = {"Authorization": f"Bearer {r['key'] or 'local'}"}
        data = await _post(f"{r['base_url']}/chat/completions", payload, headers)
        _record_usage(agent, r, data.get("usage"), "openai")
        return _response_from_openai(data).get("content", [{}])[0].get("text", "")
    except Exception as exc:
        return f"(photo attached; the active model could not read it: {str(exc)[:80]})"


async def transcribe_audio(data: bytes, filename: str) -> str | None:
    """Voice note -> text via OpenAI Whisper when a key is configured."""
    key = cfg.env_get("OPENAI_API_KEY")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, data)},
                data={"model": "whisper-1"})
            if r.status_code == 200:
                return r.json().get("text", "").strip()
    except Exception:
        pass
    return None


def ready(conf: dict | None = None) -> bool:
    """True when a model is chosen and its key (if the provider needs one)
    is present: the only thing the app needs before it is usable."""
    conf = conf or config()
    if not conf.get("model"):
        return False
    r = _resolved(conf)
    return not r["key_env"] or bool(r["key"])
