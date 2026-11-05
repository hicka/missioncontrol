"""Download and verify local speech models; optionally activate them.

Usage: ./venv/bin/python scripts/setup_voice.py --activate
Install requirements-voice.txt first. Network is used only during setup.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from local_voice import MODEL_DIR

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
WHISPER_REVISION = "3d3d5dee26484f91867d81cb899cfcf72b96be6c"


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        path = MODEL_DIR / name
        if not path.exists() or digest(path) != expected:
            partial = path.with_suffix(path.suffix + ".download")
            print(f"Downloading {name}…", flush=True)
            try:
                with urllib.request.urlopen(f"{RELEASE}/{name}", timeout=60) as response, partial.open("wb") as out:
                    shutil.copyfileobj(response, out)
                if digest(partial) != expected:
                    raise RuntimeError(f"Checksum mismatch: {name}")
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)
        print(f"Verified {name}", flush=True)
    from huggingface_hub import snapshot_download
    # Reuse an existing cache read-only, otherwise download into the app data folder.
    try:
        cached = snapshot_download("Systran/faster-whisper-base.en", revision=WHISPER_REVISION,
                                   local_files_only=True,
                                   allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])
        shutil.copytree(cached, MODEL_DIR / "whisper-base.en", dirs_exist_ok=True)
    except FileNotFoundError:
        snapshot_download("Systran/faster-whisper-base.en", revision=WHISPER_REVISION,
                          local_dir=MODEL_DIR / "whisper-base.en",
                          allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])
    # Load both before switching the app to them.
    import local_voice
    local_voice.synth("Ready.", "af_heart", 0.95)
    from faster_whisper import WhisperModel
    WhisperModel(str(MODEL_DIR / "whisper-base.en"), device="cpu", compute_type="int8",
                 cpu_threads=4, local_files_only=True)
    if args.activate:
        import settings
        settings.update("voice", {"provider": "kokoro", "voice": "af_heart", "speed": 0.95,
                                  "stt_model": "base.en", "local_only": True})
        print("Activated warm conversational local speech. Restart the server after code upgrades.")
    print(f"Models ready in {MODEL_DIR}")


if __name__ == "__main__":
    main()
