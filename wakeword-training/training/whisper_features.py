"""Gen 3 feature extraction: a frozen Whisper-small encoder (via faster-whisper
/ CTranslate2) replaces openWakeWord's tiny embedding as the acoustic
representation.

WHY: an extensive Gen 2 investigation (see docs/generalization_fix.md)
found a reproducible ~35-40% adversarial false-positive ceiling that
persisted across position-jitter, hard-negative mining, longer training,
validation-based checkpoint selection, larger classifier capacity, a
specialist Stage-2 verifier, and an AND-cascade - all of which used
openWakeWord's own frozen, 96-dim embedding as their feature input. The
convergent conclusion was that the bottleneck is the REPRESENTATION, not
the classifier head trained on top of it.

Whisper's encoder is trained on a vastly larger, more phonetically diverse
speech corpus than openWakeWord's embedding model, and is already a
trusted, locally-available dependency of this exact project (V.O.I.D's own
STT backend, `void/voice/adapters.py`'s `faster_whisper.WhisperModel` -
see requirements-voice.txt). Using its encoder as a frozen feature
extractor is a natural, low-risk transfer-learning choice: no new
untrusted model source, no network download needed (the "small" checkpoint
is already cached locally from STT use), and empirically fast enough for
continuous use (~23ms/2s-window on this machine with int8 quantization,
far below what a background listening process needs to stay responsive).

This module never modifies the Whisper weights (frozen, inference-only,
loaded read-only via ctranslate2) and never touches V.O.I.D's own STT
adapter code - it only reads the SAME model files from the SAME local
cache, exactly the way faster-whisper is designed to be used by any
number of independent processes.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

WHISPER_MODEL_SIZE = "small"
WHISPER_SAMPLE_RATE = 16000  # native rate - matches this project's audio contract exactly
WHISPER_N_MELS = 80
ENCODER_DIM = 768  # whisper-small's d_model


class WhisperEncoderError(RuntimeError):
    """The frozen Whisper encoder could not be loaded or run."""


_cached_model = None


def build_whisper_encoder(compute_type: str = "int8"):
    """Loads the frozen faster-whisper "small" model (CPU, int8 by default
    for speed - see the module docstring's latency measurement). Cached at
    module level so repeated calls within one process don't reload it.
    Raises WhisperEncoderError with an actionable message if the
    'faster-whisper' package or its cached model files aren't available -
    never silently falls back to a different representation."""
    global _cached_model
    if _cached_model is not None:
        return _cached_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise WhisperEncoderError(
            "Gen 3 features require the 'faster-whisper' package "
            "(pip install faster-whisper) - it is already a dependency of "
            "V.O.I.D's own STT backend (requirements-voice.txt), so its "
            "model weights are very likely already cached locally.") from exc
    try:
        model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type=compute_type)
    except Exception as exc:
        raise WhisperEncoderError(
            f"could not load the faster-whisper {WHISPER_MODEL_SIZE!r} model: {exc}") from exc
    _cached_model = model
    return model


def _read_wav_int16(path: str | Path, expected_sample_rate: int = WHISPER_SAMPLE_RATE) -> np.ndarray:
    from training.audio_validation import validate_wav_format
    validate_wav_format(path, expected_sample_rate=expected_sample_rate)
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def _fit_to_length(samples: np.ndarray, n_samples: int,
                   rng: "np.random.Generator | None" = None) -> np.ndarray:
    """Same jittered-placement contract as training/extract_features.py's
    function of the same name (kept as an independent copy rather than a
    shared import so Gen 2 and Gen 3 feature pipelines stay fully
    decoupled - Gen 2 remains an untouched research baseline). `rng=None`
    preserves left-aligned placement (used only for validation data, never
    for training, to structurally close off any repeat of the Gen 2
    position-sensitivity bug)."""
    if len(samples) >= n_samples:
        if rng is None:
            return samples[:n_samples]
        max_start = len(samples) - n_samples
        start = int(rng.integers(0, max_start + 1))
        return samples[start:start + n_samples]
    pad_total = n_samples - len(samples)
    if rng is None:
        return np.concatenate([samples, np.zeros(pad_total, dtype=samples.dtype)])
    leading = int(rng.integers(0, pad_total + 1))
    trailing = pad_total - leading
    return np.concatenate([np.zeros(leading, dtype=samples.dtype), samples,
                           np.zeros(trailing, dtype=samples.dtype)])


def int16_to_float32(samples: np.ndarray) -> np.ndarray:
    return (samples.astype(np.float32) / 32768.0)


def encode_waveform(model, waveform_float32: np.ndarray) -> np.ndarray:
    """Runs the frozen Whisper encoder on one waveform (float32, mono,
    16kHz, arbitrary length up to the model's 30s context) and returns its
    encoder hidden-state sequence, shape (T, 768). T is roughly
    len(waveform)/320 (mel hop 160 samples, encoder downsamples by 2x)."""
    import ctranslate2

    mel = model.feature_extractor(waveform_float32)  # (80, n_mel_frames)
    features = ctranslate2.StorageView.from_array(mel[np.newaxis].astype(np.float32))
    encoded = model.model.encode(features, to_cpu=True)
    return np.array(encoded)[0]  # (T, 768)


def extract_fixed_window_features(wav_paths: list[str], model, clip_seconds: float,
                                  sample_rate: int = WHISPER_SAMPLE_RATE,
                                  rng: "np.random.Generator | None" = None) -> np.ndarray:
    """Training/offline-validation feature extraction: fits every clip to
    exactly `clip_seconds` (jittered placement if `rng` given, else
    left-aligned - see `_fit_to_length`), then runs the frozen encoder.
    Returns (N, T, 768) - T is fixed since every input waveform has the
    same fixed sample count. Empty input returns (0, 0, ENCODER_DIM)."""
    if not wav_paths:
        return np.zeros((0, 0, ENCODER_DIM), dtype=np.float32)
    n_samples = int(clip_seconds * sample_rate)
    feats = []
    for p in wav_paths:
        samples = _fit_to_length(_read_wav_int16(p, sample_rate), n_samples, rng=rng)
        waveform = int16_to_float32(samples)
        feats.append(encode_waveform(model, waveform))
    return np.stack(feats).astype(np.float32)
