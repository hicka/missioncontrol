# Local voice and hearing

Mission Control can use Kokoro-82M for speech and Whisper base.en for recognition.
Both run on the CPU, leaving the Apple GPU available for a local language model.
The default local voice is **Heart**, a warm American English voice at 0.95× speed.
Five English voices are selectable in Settings. Models occupy about 488 MB on disk.

```sh
./venv/bin/pip install -r requirements-voice.txt
./venv/bin/python scripts/setup_voice.py --activate
```

Restart the server after upgrading its code, then refresh the app. Open a conversation
and press the microphone. The browser asks for microphone permission. It listens until
about 600 ms of quiet, transcribes locally, sends the text to the configured agent,
and reads the reply. Stop cancels playback; Talk resumes. Close releases the microphone.
This is turn-based conversation with manual interruption, not full-duplex conversation.

In the native floating bar, hold **Option–Space** to record and release to send, or
click the microphone and pause to finish. The bar records locally and uses the same
Whisper endpoint; Apple Speech Recognition permission is no longer needed. Input
stops playback so the microphone does not transcribe the agent's voice. Escape cancels
capture, a pending transcript, or playback. Closing or hiding the bar also cancels
speech. Reply audio streams sentence by sentence, including authentication when the
app connects to a remote Mission Control server. Text-only reply bubbles stay silent.

The installer verifies the Kokoro model and voice bank against SHA-256 checksums and
pins the Whisper model revision. Model files live in `models/voice/` under the app's
data folder, are gitignored, and can be downloaded once for offline use. No voice data
or recordings are needed for setup. The first voice-mode entry warms up both models.

Selecting Kokoro or macOS speech disables cloud speech fallback. The separate local-only
checkbox also constrains Automatic mode. Failed local inference returns an error instead
of uploading audio or text for cloud speech processing. **The resulting transcript still
goes to the agent's configured language model**; choose a local language model as well if
the entire conversation must stay on the machine. Voice notes from external chat channels
still pass through those channels. Generated speech is cached under `uploads/tts/`.

## Latency and verification

```sh
HF_HUB_OFFLINE=1 ./venv/bin/python scripts/benchmark_voice.py
./venv/bin/python -m pytest tests/test_voice.py tests/test_control_api.py tests/test_whatsapp.py -q
node --test tests/voice_client.test.cjs  # Node 18 or later
```

The benchmark writes playable WAVs and a JSON report to `uploads/voice-benchmark/`.
On the development M2 Pro (10 cores, 16 GB), five clean synthetic samples measured:

| Measurement | Observed |
| --- | --- |
| Warm synthesis, median for 3.5–5 seconds of speech | 0.96 seconds |
| Warm first short sentence, two phrases tested three times | 0.37–0.39 seconds |
| Recognition, median including one model-load run | 0.37 seconds |
| Synthesis time / audio duration | 0.24–0.25 |

These are engine timings, not end-to-end conversation guarantees. Microphone quality,
background noise, the 600 ms endpointing pause, agent response time, network delivery,
and browser playback add latency. The clean round-trip transcripts recovered all words
(one time expression was normalized to digits); real microphone/noise accuracy and
subjective naturalness need listening evaluation. Short sentences are generated and
published progressively over `/api/tts/stream`; playback starts before the complete
reply is synthesized. The agent still supplies completed text bubbles, not token-streamed
speech. Existing voice notes use the complete cached WAV endpoint.

## What is trained

This installation uses **pretrained weights**. It does not train a new model or fine-tune
a speaker. Heart is a supplied voice, not an imitation of the user. This gets a measurable
local voice running without claiming that installing a model is training it.

If a distinctive voice or broader emotional control is needed, the next experiment is
Qwen3-TTS voice design/cloning or a speaker fine-tune. Select the target voice and a
recording dataset first, then reserve held-out text to compare pronunciation, consistency,
first-audio latency, and memory against this baseline. A heavier model must demonstrate
acceptable latency on the M2 Pro before replacing the conversation engine. Kokoro's
inference distribution does not include a turnkey speaker-training workflow.

References: [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M),
[Kokoro ONNX runtime](https://github.com/thewh1teagle/kokoro-onnx),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS).
