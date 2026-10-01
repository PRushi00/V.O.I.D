"""Feature extraction: reuses openwakeword's OWN AudioFeatures pipeline
(melspectrogram -> the shared, frozen Google speech-embedding model) - this
is the exact feature representation openwakeword.train.Model (and V.O.I.D's
inference-side openwakeword.model.Model) expect. Nothing here reinvents a
different representation.

Clip length is chosen so a WHOLE clip's embedding sequence has exactly
`input_shape[0]` frames (16, matching the model architecture) - verified
explicitly against the installed openwakeword version via
AudioFeatures.get_embedding_shape(), never assumed from a formula. One clip
= one training example, with no separate windowing step needed.
"""
from __future__ import annotations

import wave
import zlib
from pathlib import Path

import numpy as np


class FeatureShapeError(RuntimeError):
    """The configured clip duration does not produce the number of embedding
    frames the model architecture expects, and no nearby duration was found
    that does - never silently truncated/padded to force a mismatched fit."""


def build_audio_features():
    """Constructs the real openwakeword.utils.AudioFeatures object,
    downloading the shared melspectrogram/embedding backbone models on first
    use if not already present. These are openwakeword's own small, official
    shared assets - the same ones V.O.I.D's inference path will also need
    once a real wake model is configured."""
    import openwakeword
    from openwakeword.utils import AudioFeatures

    openwakeword.utils.download_models()
    return AudioFeatures()


def resolve_clip_seconds(audio_features, target_frames: int, configured_seconds: float,
                         search_range_s: float = 1.0, step_s: float = 0.02) -> float:
    """Finds a clip duration (seconds) whose whole-clip embedding shape is
    exactly (target_frames, N), starting at configured_seconds and searching
    nearby if needed. Raises FeatureShapeError rather than silently
    truncating/padding features to force an ill-fitting match."""
    shape = audio_features.get_embedding_shape(configured_seconds)
    if shape[0] == target_frames:
        return configured_seconds

    n_steps = int(search_range_s / step_s)
    for i in range(1, n_steps + 1):
        for candidate in (configured_seconds + i * step_s, configured_seconds - i * step_s):
            if candidate <= 0:
                continue
            if audio_features.get_embedding_shape(candidate)[0] == target_frames:
                return candidate

    raise FeatureShapeError(
        f"could not find a clip duration near {configured_seconds}s that "
        f"produces exactly {target_frames} embedding frames (the model's "
        f"input_shape[0]); checked +/-{search_range_s}s in {step_s}s steps. "
        f"Adjust features.clip_seconds in the config.")


def _read_wav_int16(path: str, expected_sample_rate: int | None = None) -> np.ndarray:
    """Reads raw int16 PCM samples from a WAV file.

    If `expected_sample_rate` is given, the file's ACTUAL header sample rate
    (and channel/width) is checked against it before the samples are
    returned - this is the critical checkpoint that was previously missing:
    every downstream computation (_fit_to_length, clip_seconds*sample_rate)
    treats sample COUNT as if it were always at the configured rate, which
    silently mis-truncates/distorts audio whose real rate differs (this is
    exactly how the 44.1kHz-vs-16kHz SAPI defect went undetected). Raises
    AudioFormatError rather than silently proceeding - no WAV may be
    silently interpreted at the wrong sample rate.
    """
    with wave.open(path, "rb") as wf:
        sr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if expected_sample_rate is not None:
        from training.audio_validation import validate_wav_format
        validate_wav_format(path, expected_sample_rate=expected_sample_rate)
    return np.frombuffer(raw, dtype=np.int16)


def _fit_to_length(samples: np.ndarray, n_samples: int,
                   rng: "np.random.Generator | None" = None) -> np.ndarray:
    """Every clip fed to the embedding model must be exactly the resolved
    clip length, since a fixed frame-count output assumes a fixed-length
    input. Short clips are zero-padded; long ones truncated.

    `rng`, when given, randomizes WHERE the real audio lands within the
    fixed-length window (for a short clip: a random split of the padding
    into leading/trailing silence, instead of always all-trailing; for a
    long clip: a random n_samples-length window instead of always the
    first n_samples) rather than the default always-left-aligned
    placement. `rng=None` (the default) preserves the exact prior
    behavior bit-for-bit - existing callers/tests are unaffected.

    This exists because a direct diagnostic found the trained classifier is
    strongly POSITION-SENSITIVE, not just content-sensitive: the identical
    "hey void" audio scored 0.91 when left-aligned at offset 0 (matching
    this function's original always-left-align convention, which every
    training example - positive AND negative - was built with) but
    collapsed to ~0.01 once shifted just 200ms later in the same
    fixed-length window. Every training example sharing the same
    left-aligned convention means the classifier could learn "audio energy
    positioned at the START of the window, trailing off into silence at a
    specific point" as its decision cue instead of genuine, position-
    invariant phonetic content - plausibly why short negative phrases
    sharing that same "content-then-silence-at-this-exact-offset" shape
    (e.g. any short "-oid/-oyd" word, which is ALSO left-aligned the same
    way) get confused with the target phrase. Randomizing placement during
    TRAINING feature extraction forces the classifier to learn the phrase
    regardless of where it falls in the window - closer to how a phrase
    actually appears at an arbitrary offset in real streaming audio.
    """
    if len(samples) >= n_samples:
        if rng is None:
            return samples[:n_samples]
        max_start = len(samples) - n_samples
        start = int(rng.integers(0, max_start + 1))
        return samples[start:start + n_samples]
    pad_total = n_samples - len(samples)
    if rng is None:
        pad = np.zeros(pad_total, dtype=samples.dtype)
        return np.concatenate([samples, pad])
    leading = int(rng.integers(0, pad_total + 1))
    trailing = pad_total - leading
    return np.concatenate([np.zeros(leading, dtype=samples.dtype), samples,
                           np.zeros(trailing, dtype=samples.dtype)])


def extract_features_for_wavs(wav_paths: list[str], audio_features, clip_seconds: float,
                              sample_rate: int, batch_size: int = 32,
                              rng: "np.random.Generator | None" = None) -> np.ndarray:
    """Returns an array of shape (len(wav_paths), input_frames, 96). Empty
    input returns an empty (0, 0, 0) array rather than raising, so callers
    can handle an empty category (e.g. no clips yet) uniformly.

    `rng`, when given, is forwarded to `_fit_to_length` so every clip in
    this batch gets an independently randomized (but, given a seeded `rng`,
    reproducible) placement within the fixed-length window instead of the
    default always-left-aligned one. See `_fit_to_length` for why."""
    if not wav_paths:
        return np.zeros((0, 0, 0), dtype=np.float32)
    n_samples = int(clip_seconds * sample_rate)
    clips = np.stack([_fit_to_length(_read_wav_int16(p, expected_sample_rate=sample_rate),
                                     n_samples, rng=rng)
                     for p in wav_paths])
    return audio_features.embed_clips(clips, batch_size=min(batch_size, len(wav_paths)))


def extract_all_features(config: dict, augmented_dirs: dict[str, Path],
                         audio_features=None,
                         jitter_seed: int | None = None) -> dict[str, np.ndarray]:
    """Extracts features for every category directory produced by the
    augmentation stage. Returns {category_name: (N, 16, 96) array}.

    `jitter_seed`, when given, enables randomized within-window placement
    (see `_fit_to_length`) for every "*_train" category only - "*_val"
    categories always keep the original deterministic left-aligned
    placement, since the authoritative evaluation is the real streaming
    runtime path (training/runtime_eval.py), which does not use this
    function's placement convention at all; val's offline placement only
    needs to stay stable/comparable across runs, not position-diverse.
    Each training category gets its own independently-seeded generator
    (derived from `jitter_seed` + a stable hash of the category name) so
    results do not depend on dict iteration order and are reproducible
    given the same `jitter_seed`.
    """
    audio_features = audio_features or build_audio_features()
    sr = config["features"]["sample_rate"]
    clip_seconds = resolve_clip_seconds(
        audio_features, target_frames=16,
        configured_seconds=config["features"]["clip_seconds"])

    out: dict[str, np.ndarray] = {}
    for name, d in augmented_dirs.items():
        wavs = sorted(str(p) for p in Path(d).glob("*.wav"))
        rng = None
        if jitter_seed is not None and name.endswith("_train"):
            category_seed = (jitter_seed + zlib.crc32(name.encode("utf-8"))) % (2**32)
            rng = np.random.default_rng(category_seed)
        out[name] = extract_features_for_wavs(wavs, audio_features, clip_seconds, sr, rng=rng)
    return out
