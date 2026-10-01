"""Is a speculative transcript actually reusable?

Speculation only pays if the transcript decoded from a MID-CAPTURE snapshot equals the one decoded from the full
capture. If it differs, the speculative result can never be substituted for the authoritative one - and verifying
equality would require the final decode, which is the very cost speculation is meant to avoid.

The snapshot point is where a real implementation would fire: once the endpointer has seen enough trailing silence
to be fairly confident, but before the full timeout. Both the shipping 0.6 s timeout and a 0.3 s snapshot are
simulated by truncating the audio, which is exactly what the capture buffer would have contained at that instant.

usage:
    python scripts/bench/speculative_stt_probe.py

Reads the owner's recordings locally; nothing is copied or transmitted, and .gitignore excludes them.
"""
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from void.voice import cuda as _cuda  # noqa: E402
_cuda.register()   # the CUDA DLLs must be on PATH before faster_whisper loads

from void.config import Config  # noqa: E402
from void.voice.adapters import FasterWhisperSTT  # noqa: E402
from void.voice.runtime import _rms_int16  # noqa: E402

FRAME = 480
cfg = Config.load()
GATE = float(cfg.get("voice.wake_energy_threshold", 300))
TIMEOUT = float(cfg.get("voice.wake_silence_timeout", 0.6))

stt = FasterWhisperSTT(model_name=cfg.get("voice.stt_model", "small"),
                       device=cfg.get("voice.stt_device", "cpu"),
                       compute_type=cfg.get("voice.stt_compute_type", "int8"),
                       beam_size=int(cfg.get("voice.stt_beam_size", 1)),
                       vad_filter=bool(cfg.get("voice.stt_vad_filter", True)))
stt.warmup()
print(f"STT device={stt.device}  gate={GATE}  silence_timeout={TIMEOUT}s\n")


def frames_of(path):
    with wave.open(path) as w:
        if w.getframerate() != 16000 or w.getsampwidth() != 2 or w.getnchannels() != 1:
            return None
        raw = w.readframes(w.getnframes())
    return [raw[i * FRAME * 2:(i + 1) * FRAME * 2] for i in range(len(raw) // (FRAME * 2))]


def to_audio(frames):
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(b"".join(frames), dtype="<i2").astype(np.float32) / 32768.0


def endpoint_frame(frames, silence_s):
    """The frame at which the production endpointer would close the capture (its 'silence' rule)."""
    trailing, speech, elapsed = 0.0, False, 0.0
    for i, f in enumerate(frames):
        elapsed += 0.03
        if _rms_int16(f) >= GATE:
            speech, trailing = True, 0.0
        elif speech:
            trailing += 0.03
        if elapsed >= 0.4 and speech and trailing >= silence_s:
            return i
    return None


CLIPS = sorted(Path("wakeword-training/data/personal_positive").glob("*.wav"))

print(f"{'clip':28} {'snapshot @0.30s':30} {'final @0.60s':30} {'same?':6} {'spec ms':>8} {'final ms':>9}")
same = differ = skipped = 0
for path in CLIPS:
    frames = frames_of(str(path))
    if not frames:
        skipped += 1
        continue
    spec_at = endpoint_frame(frames, 0.30)
    final_at = endpoint_frame(frames, TIMEOUT)
    if spec_at is None or final_at is None:
        skipped += 1
        continue
    t = time.perf_counter()
    spec = stt.transcribe(to_audio(frames[:spec_at + 1])).strip()
    spec_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    final = stt.transcribe(to_audio(frames[:final_at + 1])).strip()
    final_ms = (time.perf_counter() - t) * 1000
    match = spec.casefold().rstrip(".!?,") == final.casefold().rstrip(".!?,")
    same += match
    differ += not match
    flag = "yes" if match else "NO"
    print(f"{path.stem[:26]:28} {spec[:28]!r:30} {final[:28]!r:30} {flag:6} "
          f"{spec_ms:7.0f} {final_ms:8.0f}")

total = same + differ
print(f"\nidentical: {same}/{total}   different: {differ}/{total}   unusable clips skipped: {skipped}")
if differ:
    print("A speculative transcript that differs cannot be substituted for the authoritative one.")
