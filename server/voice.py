"""Text to speech for the team.

One configured voice speaks everywhere: the phone's realtime voice mode,
spoken replies in chat, and voice notes on iMessage or Telegram.
Providers in order of preference: MiniMax (licensed studio voices, the
same key the model uses), OpenAI TTS, then the macOS voice so it always
works. Audio is cached under uploads/tts so a bubble is synthesised once.
"""
import asyncio
import processes
import hashlib
import os
import sys
import time

import settings as cfg
from apphome import ROOT

TTS_DIR = ROOT / "uploads" / "tts"
MAX_CHARS = 1500

DEFAULTS = {
    "provider": "auto",           # auto | minimax | openai | local
    "voice": "English_Deep-VoicedGentleman",
    "speed": 0.95,
    "speak_replies": False,       # phone speaks new agent replies in chat
    "stt_model": "base.en",       # local Whisper size: tiny.en, base.en, small.en
}

MINIMAX_HOST = "https://api.minimax.io"
OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]

_voice_cache: dict = {"at": 0.0, "voices": []}


def settings() -> dict:
    s = cfg.load().get("voice") or {}
    out = {**DEFAULTS, **{k: v for k, v in s.items() if v is not None}}
    try:
        out["speed"] = float(out["speed"])
    except (TypeError, ValueError):
        out["speed"] = DEFAULTS["speed"]
    return out


def providers() -> dict:
    """Which providers can actually speak (and listen) right now."""
    return {
        "minimax": bool(cfg.env_get("MINIMAX_API_KEY")),
        "openai": bool(cfg.env_get("OPENAI_API_KEY")),
        "local": sys.platform == "darwin",
        "whisper": stt_available(),
    }


def stt_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


_stt: dict = {"model": None, "name": None}


def _stt_model():
    name = settings()["stt_model"]
    if _stt["model"] is None or _stt["name"] != name:
        from faster_whisper import WhisperModel
        _stt["model"] = WhisperModel(name, device="cpu", compute_type="int8")
        _stt["name"] = name
    return _stt["model"]


def _local_transcribe(data: bytes, filename: str) -> str:
    import tempfile
    suffix = os.path.splitext(filename)[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        segments, _ = _stt_model().transcribe(f.name, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data: bytes, filename: str) -> str | None:
    """Speech to text: Whisper on this machine when installed, else OpenAI
    when a key is set, else nothing."""
    if stt_available():
        try:
            return await asyncio.to_thread(_local_transcribe, data, filename)
        except Exception:
            pass
    import llm
    return await llm.transcribe_audio(data, filename)


def _pick(provider: str) -> str | None:
    avail = providers()
    if provider != "auto":
        return provider if avail.get(provider) else None
    for name in ("minimax", "openai", "local"):
        if avail[name]:
            return name
    return None


async def list_voices() -> list[dict]:
    """Voices for the active provider; MiniMax's list is fetched and cached."""
    prov = _pick(settings()["provider"])
    if prov == "openai":
        return [{"id": v, "name": v} for v in OPENAI_VOICES]
    if prov == "local":
        return [{"id": "", "name": "system voice"}]
    if prov != "minimax":
        return []
    if _voice_cache["voices"] and time.time() - _voice_cache["at"] < 600:
        return _voice_cache["voices"]
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(f"{MINIMAX_HOST}/v1/get_voice",
                                  headers={"Authorization": f"Bearer {cfg.env_get('MINIMAX_API_KEY')}"},
                                  json={"voice_type": "system"})
            voices = [{"id": v["voice_id"],
                       "name": v["voice_id"].split("_", 1)[-1].replace("_", " ").replace("-", " ")}
                      for v in (r.json().get("system_voice") or [])
                      if str(v.get("voice_id", "")).startswith("English")]
    except Exception:
        voices = []
    if voices:
        _voice_cache.update(at=time.time(), voices=voices)
    return voices or _voice_cache["voices"]


async def _minimax(text: str, voice: str, speed: float) -> tuple[bytes, str] | None:
    import httpx
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            f"{MINIMAX_HOST}/v1/t2a_v2",
            headers={"Authorization": f"Bearer {cfg.env_get('MINIMAX_API_KEY')}"},
            json={"model": "speech-02-hd", "text": text, "stream": False,
                  "voice_setting": {"voice_id": voice or DEFAULTS["voice"],
                                    "speed": speed, "vol": 1, "pitch": 0},
                  "audio_setting": {"format": "mp3", "sample_rate": 32000}})
        hexa = ((r.json().get("data") or {}).get("audio")) if r.status_code == 200 else None
        return (bytes.fromhex(hexa), "audio/mpeg") if hexa else None


async def _openai(text: str, voice: str, speed: float) -> tuple[bytes, str] | None:
    import httpx
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {cfg.env_get('OPENAI_API_KEY')}"},
            json={"model": "tts-1", "voice": voice if voice in OPENAI_VOICES else "onyx",
                  "input": text, "speed": speed})
        return (r.content, "audio/mpeg") if r.status_code == 200 else None


async def _local(text: str) -> tuple[bytes, str] | None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        aiff = os.path.join(td, "v.aiff")
        m4a = os.path.join(td, "v.m4a")
        p1 = await processes.create_subprocess_exec("say", "-o", aiff, text)
        await p1.wait()
        p2 = await processes.create_subprocess_exec(
            "afconvert", "-f", "m4af", "-d", "aac", aiff, m4a,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p2.wait()
        if os.path.exists(m4a):
            with open(m4a, "rb") as f:
                return f.read(), "audio/mp4"
    return None


async def synth(text: str, voice: str | None = None) -> tuple[bytes, str] | None:
    """Speak text with the configured voice, falling through providers
    so something always comes out."""
    text = " ".join(text.split())[:MAX_CHARS]
    if not text:
        return None
    s = settings()
    voice = voice or s["voice"]
    first = _pick(s["provider"])
    order = [first] + [p for p in ("minimax", "openai", "local") if p != first and providers()[p]]
    for prov in [p for p in order if p]:
        try:
            if prov == "minimax":
                out = await _minimax(text, voice, s["speed"])
            elif prov == "openai":
                out = await _openai(text, voice, s["speed"])
            else:
                out = await _local(text)
            if out:
                return out
        except Exception:
            continue
    return None


async def tts_file(text: str, voice: str | None = None) -> dict | None:
    """Synthesise once per (text, voice, speed) and serve it from uploads/tts."""
    s = settings()
    voice = voice or s["voice"]
    key = hashlib.sha1(f"{s['provider']}|{voice}|{s['speed']}|{text.strip()}".encode()).hexdigest()[:20]
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    for ext, mime in (("mp3", "audio/mpeg"), ("m4a", "audio/mp4")):
        path = TTS_DIR / f"{key}.{ext}"
        if path.exists():
            return {"url": f"/uploads/tts/{path.name}", "mime": mime, "cached": True}
    out = await synth(text, voice)
    if not out:
        return None
    data, mime = out
    ext = "m4a" if mime == "audio/mp4" else "mp3"
    path = TTS_DIR / f"{key}.{ext}"
    await asyncio.to_thread(path.write_bytes, data)
    return {"url": f"/uploads/tts/{path.name}", "mime": mime, "cached": False}
