"""Check local speech in a running app using a fake microphone; never send a chat.

Run setup_voice.py --activate and benchmark_voice.py first, then restart the app.
Usage: ./venv/bin/python scripts/check_voice_ui.py --url http://127.0.0.1:8899
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:8899")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sample = root / "uploads/voice-benchmark/heart-1.wav"
out = root / "build/review"
out.mkdir(parents=True, exist_ok=True)
assert sample.exists(), "Run scripts/benchmark_voice.py first"

with sync_playwright() as p:
    browser = p.chromium.launch(args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={sample}", "--autoplay-policy=no-user-gesture-required",
    ])
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    errors, messages = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    def intercept_send(route):
        messages.append(route.request.post_data_json)
        route.fulfill(json={"ok": True})
    page.route("**/api/send", intercept_send)
    page.goto(args.url, wait_until="networkidle")
    page.evaluate("openSettings('llm')")
    page.wait_for_function("document.querySelector('#voice-id').value === 'af_heart'")
    assert page.locator("#voice-provider").input_value() == "kokoro"
    assert page.locator("#voice-id").input_value() == "af_heart"
    assert page.locator("#voice-local-only").is_checked()
    page.locator("#voice-card").screenshot(path=str(out / "local-voice-settings.png"))
    # Exercise real browser capture, VAD, upload and local Whisper; intercept only the chat send.
    page.evaluate("document.querySelector('#voice-handsfree').checked = false; voiceOpen()")
    page.wait_for_function("V.phase === 'thinking'", timeout=30000)
    page.wait_for_timeout(1200)
    assert messages and messages[0]["voice"] is True, messages
    assert "here" in messages[0]["text"].lower(), messages
    page.evaluate("voiceStopAll(); V.err = ''; voiceSpeak('Hey, I heard you. The local voice is ready.');")
    page.wait_for_function("V.pending === 0 && V.phase !== 'speaking'", timeout=20000)
    assert page.evaluate("V.err") == "", page.evaluate("V.err")
    page.evaluate("voiceClose()")
    assert page.evaluate("V.stream === null && V.ctx === null && !V.on")
    assert not errors, errors
    print(json.dumps({"ok": True, "synthetic_microphone_transcript": messages[0]["text"],
                      "chat_intercepted": True, "streamed_playback": "completed", "page_errors": errors}))
    browser.close()
