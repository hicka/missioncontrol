"""Offline neural speech. Models stay resident; inference never downloads files."""
import importlib.util
import io
import threading
import wave

from apphome import ROOT

MODEL_DIR = ROOT / "models" / "voice"
MODEL_VERSION = "kokoro-v1.0"
VOICES = {
    "af_heart": "Heart · warm, conversational American English",
    "af_bella": "Bella · bright American English",
    "am_michael": "Michael · relaxed American English",
    "bf_emma": "Emma · warm British English",
    "bm_george": "George · calm British English",
}
_model = None
_lock = threading.Lock()


def available() -> bool:
    return (importlib.util.find_spec("kokoro_onnx") is not None
            and (MODEL_DIR / f"{MODEL_VERSION}.onnx").is_file()
            and (MODEL_DIR / "voices-v1.0.bin").is_file())


def _load():
    global _model
    if _model is None:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(MODEL_DIR / f"{MODEL_VERSION}.onnx"),
                                       sess_options=options, providers=["CPUExecutionProvider"])
        _model = Kokoro.from_session(session, str(MODEL_DIR / "voices-v1.0.bin"))
    return _model


def synth(text: str, voice: str, speed: float) -> tuple[bytes, str]:
    import numpy as np
    with _lock:  # eSpeak and the shared model are not reentrant.
        model = _load()
        name = voice if voice in VOICES else "af_heart"
        samples, rate = model.create(text, voice=name, speed=speed,
                                     lang="en-gb" if name.startswith("b") else "en-us")
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue(), "audio/wav"
