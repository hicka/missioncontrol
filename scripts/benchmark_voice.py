"""Offline, uncached speech timings and intelligibility samples on this machine."""
import asyncio
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave

os.environ["HF_HUB_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import local_voice
import voice

TEXTS = [
    "Hey, I'm here. Tell me what you're working on, and we'll figure it out together.",
    "I've checked the build. Everything looks good, and the tests are passing.",
    "That didn't work quite as expected. Give me a moment, I'll try another approach.",
    "Would you like the quick answer, or should I walk you through the details?",
    "The meeting is at three thirty. I'll have your notes ready before then.",
]


async def main():
    out = local_voice.MODEL_DIR.parent.parent / "uploads" / "voice-benchmark"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    start = time.perf_counter()
    local_voice.synth("Ready.", "af_heart", 0.95)
    cold = time.perf_counter() - start
    for index, text in enumerate(TEXTS):
        start = time.perf_counter()
        data, _ = local_voice.synth(text, "af_heart", 0.95)
        elapsed = time.perf_counter() - start
        path = out / f"heart-{index + 1}.wav"
        path.write_bytes(data)
        with wave.open(io.BytesIO(data)) as wav:
            duration = wav.getnframes() / wav.getframerate()
        start = time.perf_counter()
        transcript = await voice.transcribe(data, path.name)
        row = {"text": text, "transcript": transcript, "synthesis_seconds": round(elapsed, 3),
               "audio_seconds": round(duration, 3), "real_time_factor": round(elapsed / duration, 3),
               "transcription_seconds": round(time.perf_counter() - start, 3), "sample": str(path)}
        print(json.dumps(row), flush=True)
        rows.append(row)
    report = {"voice": "af_heart", "speed": 0.95, "cold_warmup_seconds": round(cold, 3),
              "median_synthesis_seconds": statistics.median(r["synthesis_seconds"] for r in rows),
              "median_transcription_seconds": statistics.median(r["transcription_seconds"] for r in rows),
              "samples": rows, "note": "Synthetic clean speech; excludes microphone, network, endpointing and LLM latency."}
    (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {out / 'results.json'}")


if __name__ == "__main__":
    asyncio.run(main())
