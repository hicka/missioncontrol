"""The team voice: settings, provider choice, and cached synthesis."""
import asyncio


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
