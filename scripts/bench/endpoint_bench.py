"""Amplitude endpointing vs streaming Silero VAD, judged against an independent reference.

The reference is faster-whisper's OFFLINE Silero VAD (``get_speech_timestamps``), which sees the whole file at
once and is therefore better informed than any streaming decision. Both endpointers are scored against it:

    endpoint latency = frames between the reference end of speech and the capture closing
    truncation       = the capture closing BEFORE the reference end of speech

Results are split by corpus. The owner's ORIGINAL recordings are the real microphone path and are what a shipping
decision should rest on; the augmented files are deliberately degraded training variants, useful only as a
robustness stress test. Silero's streaming thresholds were set from the measured distribution on the real
recordings (peak p50 0.984, p10 0.684), not from the library default.

Audio is read locally; nothing is copied or transmitted.

usage:
    python scripts/bench/endpoint_bench.py

Requires the owner's recordings under wakeword-training/data/ - they are read locally, never copied or sent
anywhere, and are excluded from git by .gitignore.
"""
import glob
import os
import random
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: E402
from openwakeword.vad import VAD  # noqa: E402

from void.voice.runtime import _rms_int16  # noqa: E402

FRAME = 480
FRAME_S = FRAME / 16000
ORIGINAL_DIR = os.path.normpath("wakeword-training/data/personal_positive")


def load(path):
    with wave.open(path) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            return None
        raw = w.readframes(w.getnframes())
    n = len(raw) // (FRAME * 2)
    return [raw[i * FRAME * 2:(i + 1) * FRAME * 2] for i in range(n)]


def reference_end_frame(frames):
    pcm = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    segs = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=100, speech_pad_ms=0))
    return int(segs[-1]["end"] / FRAME) if segs else None


class AmplitudeEndpointer:
    def __init__(self, silence_s, threshold, lead_grace_s=0.4):
        self.silence_s, self.threshold, self.lead_grace_s = silence_s, threshold, lead_grace_s
        self.elapsed = self.trailing = 0.0
        self.speech = False

    def __call__(self, frame):
        self.elapsed += FRAME_S
        if _rms_int16(frame) >= self.threshold:
            self.speech, self.trailing = True, 0.0
        elif self.speech:
            self.trailing += FRAME_S
        if self.elapsed < self.lead_grace_s:
            return None
        return "silence" if self.speech and self.trailing >= self.silence_s else None


class VadEndpointer:
    """Streaming Silero with hysteresis: a higher threshold to start speech, a lower one to keep it."""

    def __init__(self, silence_s, on=0.30, off=0.20, lead_grace_s=0.4, vad=None):
        self.silence_s, self.on, self.off = silence_s, on, off
        self.lead_grace_s = lead_grace_s
        self.vad = vad or VAD()
        self.vad.reset_states()
        self.elapsed = self.trailing = 0.0
        self.speech = self.in_speech = False

    def __call__(self, frame):
        self.elapsed += FRAME_S
        prob = float(self.vad.predict(np.frombuffer(frame, dtype=np.int16), frame_size=FRAME))
        self.in_speech = prob >= (self.off if self.in_speech else self.on)
        if self.in_speech:
            self.speech, self.trailing = True, 0.0
        elif self.speech:
            self.trailing += FRAME_S
        if self.elapsed < self.lead_grace_s:
            return None
        return "silence" if self.speech and self.trailing >= self.silence_s else None


def run(frames, endpointer, tail=200):
    silence = b"\x00\x00" * FRAME
    for i, f in enumerate(frames):
        if endpointer(f) == "silence":
            return i
    for j in range(tail):
        if endpointer(silence) == "silence":
            return len(frames) + j
    return None


random.seed(5)
files = []
for pat in ("wakeword-training/data/personal_positive/*.wav",
            "wakeword-training/data/personal_augmented/**/*.wav",
            "wakeword-training/data/personal_train_augmented/**/*.wav"):
    files.extend(sorted(glob.glob(pat, recursive=True)))
if len(files) > 400:
    files = random.sample(files, 400)

CLIPS = []
for f in files:
    fr = load(f)
    if not fr:
        continue
    ref = reference_end_frame(fr)
    if ref is None:
        continue
    CLIPS.append((fr, ref, os.path.normpath(os.path.dirname(f)) == ORIGINAL_DIR))

shared = VAD()
CONFIGS = [
    ("amplitude 0.6 s / gate 300  (SHIPPING)", lambda: AmplitudeEndpointer(0.6, 300.0)),
    ("amplitude 0.8 s / gate 500  (pre-V3)", lambda: AmplitudeEndpointer(0.8, 500.0)),
    ("VAD 0.6 s", lambda: VadEndpointer(0.6, vad=shared)),
    ("VAD 0.5 s", lambda: VadEndpointer(0.5, vad=shared)),
    ("VAD 0.4 s", lambda: VadEndpointer(0.4, vad=shared)),
    ("VAD 0.35 s", lambda: VadEndpointer(0.35, vad=shared)),
    ("VAD 0.3 s", lambda: VadEndpointer(0.3, vad=shared)),
    ("VAD 0.25 s", lambda: VadEndpointer(0.25, vad=shared)),
]

for corpus, only_real in (("REAL microphone recordings", True),
                          ("ALL, incl. deliberately degraded training variants", False)):
    subset = [c for c in CLIPS if c[2] or not only_real]
    print(f"=== {corpus} (n={len(subset)}) ===")
    print(f"{'configuration':40} {'latency p50':>12} {'p90':>7} {'max':>7} "
          f"{'truncated':>10} {'never fired':>12}")
    for label, make in CONFIGS:
        lats, truncated, never = [], 0, 0
        for frames, ref, _real in subset:
            fired = run(frames, make())
            if fired is None:
                never += 1
            elif fired < ref:
                truncated += 1
            else:
                lats.append((fired - ref) * FRAME_S)
        lats.sort()
        p = (lambda q: lats[min(len(lats) - 1, int(len(lats) * q))]) if lats else (lambda q: float("nan"))
        print(f"{label:40} {p(.5):11.2f}s {p(.9):6.2f}s {(max(lats) if lats else 0):6.2f}s "
              f"{truncated:>10} {never:>12}")
    print()
