"""Discard captures that clearly contain no usable speech, BEFORE speech-to-text.

Whisper invents words when it is handed a sliver of speech surrounded by silence: a clipped wake-word
tail or a keyboard tap decodes as "Thank you.", "Wait." or "Please subscribe to the channel." and, without
this guard, that text is dispatched to the agent as if the owner had said it (one wasted cloud call and a
spoken reply to nothing). Measured on synthesised clips: 100-200 ms of speech + silence -> invented text,
while every genuine short command ("Stop.", "Yes.", "Pause.") spans at least ~450 ms.

The check is deliberately blunt and local: find the 30 ms frames that are speech-like and require the
speech to span a minimum time (and to be more than a couple of stray frames). "Speech-like" is relative to
the capture's own loudest frame with a small absolute floor, so a quiet speaker is not penalised while digital
silence and a lone click are. Not a classifier, no model, no new dependency. Anything it cannot measure (not a
numpy array) is passed through untouched - it fails open.
"""
from __future__ import annotations

FRAME_MS = 30
SAMPLE_RATE = 16000
DEFAULT_MIN_SPEECH_MS = 250
_ABS_FLOOR_RMS = 100.0          # int16 units (~ -50 dBFS, far below the 500 wake gate): silence whatever the peak
_REL_TO_PEAK = 0.10             # a frame is speech-like if it is at least this fraction of the loudest frame


def speech_ms(audio) -> tuple[float, float] | None:
    """``(span_ms, voiced_ms)`` of speech-like audio in a mono 16 kHz capture (float32 in [-1, 1] or int16):
    the time from the first to the last speech-like frame, and the time in speech-like frames. ``None`` when
    ``audio`` is not something this can measure."""
    try:
        import numpy as np
    except ImportError:
        return None
    if not isinstance(audio, np.ndarray) or audio.ndim != 1:
        return None
    if audio.dtype.kind == "f":
        samples = np.nan_to_num(audio.astype(np.float64)) * 32768.0
    elif audio.dtype == np.int16:
        samples = audio.astype(np.float64)
    else:
        return None
    per_frame = SAMPLE_RATE * FRAME_MS // 1000
    frames = len(samples) // per_frame
    if frames == 0:
        return 0.0, 0.0
    rms = np.sqrt((samples[: frames * per_frame].reshape(frames, per_frame) ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak < _ABS_FLOOR_RMS:
        return 0.0, 0.0
    voiced = np.flatnonzero(rms >= max(_ABS_FLOOR_RMS, _REL_TO_PEAK * peak))
    return float((voiced[-1] - voiced[0] + 1) * FRAME_MS), float(len(voiced) * FRAME_MS)


def has_enough_speech(audio, min_speech_ms: float = DEFAULT_MIN_SPEECH_MS) -> bool:
    """False only when the capture is measurably too brief or too quiet to be a command. ``min_speech_ms <= 0``
    disables the guard; a capture that cannot be measured passes."""
    if min_speech_ms <= 0:
        return True
    m = speech_ms(audio)
    if m is None:
        return True
    span, voiced = m
    return span >= min_speech_ms and voiced >= min_speech_ms / 2
