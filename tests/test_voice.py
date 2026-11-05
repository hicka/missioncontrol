"""The team voice: settings, provider choice, and cached synthesis."""
import asyncio
import json
import pytest


def test_settings_defaults_and_roundtrip():
    import settings as cfg
    import voice
    s = voice.settings()
    assert s["provider"] == "auto" and s["voice"] and 0.5 <= s["speed"] <= 2
    cfg.update("voice", {"voice": "English_CaptivatingStoryteller", "speed": "1.1",
                         "speak_replies": True})
    s = voice.settings()
    assert s["voice"] == "English_CaptivatingStoryteller"
    assert s["speed"] == 1.1 and s["speak_replies"] is True


def test_auto_picks_first_available_provider(monkeypatch):
    import voice
    monkeypatch.setattr(voice, "providers",
                        lambda: {"minimax": False, "openai": True, "local": True})
    assert voice._pick("auto") == "openai"
    assert voice._pick("minimax") is None
    assert voice._pick("local") == "local"


def test_tts_file_synthesises_once_then_serves_cache(tmp_path, monkeypatch):
    import voice
    calls = []

    async def fake_synth(text, v=None):
        calls.append(text)
        return b"ID3fake-mp3", "audio/mpeg"

    monkeypatch.setattr(voice, "TTS_DIR", tmp_path)
    monkeypatch.setattr(voice, "synth", fake_synth)

    first = asyncio.run(voice.tts_file("Mission Control online."))
    assert first["url"].startswith("/uploads/tts/") and first["url"].endswith(".mp3")
    assert first["cached"] is False
    assert (tmp_path / first["url"].rsplit("/", 1)[1]).read_bytes() == b"ID3fake-mp3"

    again = asyncio.run(voice.tts_file("Mission Control online."))
    assert again["url"] == first["url"] and again["cached"] is True
    assert len(calls) == 1


def test_split_reply_marks_text_only_bubbles():
    import orchestrator
    parts = orchestrator.split_reply(
        "Short spoken bit.\n\n[text]\nlong details\nmore lines\n\nAnother spoken line.")
    assert parts[0] == ("Short spoken bit.", None)
    assert parts[1][0].startswith("long details") and parts[1][1] == {"silent": True}
    assert parts[2] == ("Another spoken line.", None)
    assert orchestrator.split_reply("[text]\n\n") == []
    # a report that ends with the silence marker keeps the report, drops the marker
    assert orchestrator.split_reply("Nothing on fire.\n\nNO_REPLY") == [("Nothing on fire.", None)]
    assert orchestrator.split_reply("no_reply.") == []


def test_transcribe_without_any_provider_returns_none(monkeypatch):
    import llm
    import voice
    monkeypatch.setattr(voice, "stt_available", lambda: False)

    async def nothing(data, name):
        return None

    monkeypatch.setattr(llm, "transcribe_audio", nothing)
    assert asyncio.run(voice.transcribe(b"\x00", "x.webm")) is None


def test_spoken_message_carries_flag_and_voice_note(tmp_path, monkeypatch):
    import db
    import kernel_store
    import orchestrator
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "voice.db")
    monkeypatch.setattr(db, "_conn", None)
    o = orchestrator.Orchestrator()

    def next_batch():
        items = kernel_store.inbox_pending("router")
        o.router.on_drained(items)
        text = o.router.render(items)
        kernel_store.inbox_take("router", [i["id"] for i in items], "user", text)
        return text

    asyncio.run(o.user_message(f"dm:{o.lead}", "how is the deploy going", voice=True))
    assert db.last_message(f"dm:{o.lead}")["meta"].get("voice") is True
    prompt = next_batch()
    assert "VOICE MODE" in prompt and "[text]" in prompt
    asyncio.run(o.user_message(f"dm:{o.lead}", "typed this one"))
    assert "VOICE MODE" not in next_batch()
    db._conn.close()


@pytest.mark.parametrize("provider", ["kokoro", "local", "auto"])
def test_offline_never_falls_back_to_cloud(provider, monkeypatch):
    import voice
    import llm
    monkeypatch.setattr(voice, "settings", lambda: {**voice.DEFAULTS, "provider": provider, "local_only": True})
    monkeypatch.setattr(voice, "providers", lambda: {"kokoro": False, "local": False, "minimax": True, "openai": True})
    monkeypatch.setattr(voice, "stt_available", lambda: True)
    def fail(*args):
        raise RuntimeError("local model failed")
    async def cloud(*args):
        pytest.fail("Offline mode contacted cloud speech")
    monkeypatch.setattr(voice, "_local_transcribe", fail)
    monkeypatch.setattr(voice, "_minimax", cloud)
    monkeypatch.setattr(voice, "_openai", cloud)
    monkeypatch.setattr(llm, "transcribe_audio", cloud)
    assert asyncio.run(voice.synth("Stay here.")) is None
    assert asyncio.run(voice.transcribe(b"audio", "test.wav")) is None


def test_kokoro_wav_cache_and_offline_cache_separation(tmp_path, monkeypatch):
    import voice
    configuration = {**voice.DEFAULTS, "provider": "kokoro"}
    monkeypatch.setattr(voice, "settings", lambda: configuration)
    monkeypatch.setattr(voice, "TTS_DIR", tmp_path)
    async def synth(*args):
        return b"RIFFtest", "audio/wav"
    monkeypatch.setattr(voice, "synth", synth)
    first = asyncio.run(voice.tts_file("Hello."))
    assert first["mime"] == "audio/wav" and first["url"].endswith(".wav")
    assert asyncio.run(voice.tts_file("Hello."))["cached"]
    configuration.update(provider="auto", local_only=False)
    cloud_allowed = asyncio.run(voice.tts_file("Hello."))
    configuration["local_only"] = True
    assert asyncio.run(voice.tts_file("Hello."))["url"] != cloud_allowed["url"]


def test_chunks_preserve_words_and_bound_first_audio():
    import voice
    text = "Hello there. " + "Let's work through this together, one step at a time. " * 12
    parts = voice.speech_chunks(text)
    assert " ".join(parts) == text.strip()
    assert len(parts) > 1 and all(0 < len(p) <= 140 for p in parts)
    assert voice.speech_chunks("  ") == []
    assert all(len(p) <= 140 for p in voice.speech_chunks("x" * 400))


def test_stream_endpoint_yields_playable_chunks_and_reports_failure(monkeypatch):
    import main
    import voice
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_api_token", lambda: "voice-test")
    calls = []
    async def synth(text, selected=None):
        calls.append(text)
        return {"url": "/uploads/tts/test.wav", "mime": "audio/wav", "cached": False} if len(calls) == 1 else None
    monkeypatch.setattr(voice, "tts_file", synth)
    client = TestClient(main.app, headers={"Authorization": "Bearer voice-test"})
    response = client.post("/api/tts/stream", json={"text": "A helpful spoken sentence. " * 15})
    records = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert records[0]["url"].endswith(".wav") and records[0]["ok"]
    assert not records[1]["ok"] and len(calls) == 2
