"""Personal-positive training data: real human recordings of the wake phrase,
kept as a separate, additive path alongside the existing synthetic positive
set - never mixed into it, never touching validation.

Two generations of this module coexist right now, deliberately:

1. The ORIGINAL, naive path (augment_personal_positive /
   extract_personal_positive_features, unchanged below): augments ALL 9
   personal recordings together into data/personal_augmented/ and
   data/features/positive_personal.npy. Still present and untouched by this
   change - not deleted until the holdout-aware replacement below is
   reviewed and approved.

2. The NEW, holdout-aware split path (split_personal_recordings /
   augment_personal_training_split / extract_personal_training_split_features
   / extract_personal_holdout_features): splits the 9 ORIGINAL recordings
   deterministically into 7 for training and 2 for holdout BEFORE any
   augmentation happens, so no augmented derivative of a holdout recording
   can ever reach the training features. This lets a future evaluation check
   whether the trained model recognizes genuinely unseen instances of the
   user's own voice, rather than only memorized augmented copies of
   recordings it was already trained on. Writes to NEW, separate paths
   (data/personal_train_augmented/, data/features/positive_personal_train.npy,
   data/features/positive_personal_holdout.npy) - it does not touch
   data/personal_augmented/ or data/features/positive_personal.npy at all.

Design notes (apply to both generations):
  * This module does not modify training/train.py, training/augment.py,
    training/extract_features.py, or train_void.py - it only calls their
    existing public (and, for _read_wav/_write_wav, already-tested-directly-
    by-this-project's-own-test-suite) functions/helpers, unchanged.
  * None of these feature-array names are among train_void.py's
    _raw_categories() keys, so today's --augment/--features/--train/
    --evaluate CLI stages never touch any of them automatically (neither for
    training nor validation) - wiring any of them into an actual training
    run is a separate, later decision, not made here.
  * Every name starts with "positive", so training.train.build_dataset()
    (which labels any feature-array key starting with "positive" as class 1)
    would treat any of them as a positive example if a future step
    explicitly includes it in the features dict passed to train_model().
  * None of these names end with "_val", so even if later merged into the
    same features dict train_void.py loads, stage_evaluate()'s
    "k.endswith('_val')" filter would never select any of them - they can
    only ever be training data or a manually-invoked holdout check, never
    silently become part of the existing validation dataset.
"""
from __future__ import annotations

import random
from pathlib import Path

PERSONAL_POSITIVE_DIRNAME = "personal_positive"
PERSONAL_AUGMENTED_DIRNAME = "personal_augmented"
PERSONAL_FEATURE_NAME = "positive_personal"

# --- new, holdout-aware split path --------------------------------------
PERSONAL_TRAIN_AUGMENTED_DIRNAME = "personal_train_augmented"
PERSONAL_TRAIN_FEATURE_NAME = "positive_personal_train"
PERSONAL_HOLDOUT_FEATURE_NAME = "positive_personal_holdout"
DEFAULT_N_HOLDOUT = 5  # of 23 originals as of the 9+14 consolidation - keeps
                       # roughly the same ~22% holdout fraction as the earlier
                       # 2-of-9 split (2/9 ~= 0.22, 5/23 ~= 0.22)

# Approved Strategy B (see the design-analysis turn preceding this change):
# moderate, opt-in oversampling applied ONLY to positive_personal_train,
# applied ONLY inside train_void.py's stage_train_personalized() - never to
# positive_train/adversarial_train/noise_train/speech_train, and irrelevant
# to positive_personal_holdout (which never reaches training regardless).
# This is a pure data-preprocessing multiplier: training.train.py's sampling
# algorithm (rng.integers(...) uniform draw with replacement) is completely
# unmodified - it simply receives a larger input array.
PERSONAL_OVERSAMPLE_FACTOR = 5


def discover_personal_positive_wavs(data_dir: str | Path) -> list[Path]:
    """Lists the existing personal-recording WAV files. Does not create,
    move, or modify anything - a pure read of what's already on disk. This
    is the UNFILTERED listing (includes excluded/curated-out recordings,
    see EXCLUDED_PERSONAL_RECORDINGS below) - kept as-is so it still reports
    the true physical contents of the directory. The holdout-aware
    personalized-training pipeline uses discover_approved_personal_positive_
    wavs() instead; the legacy, non-holdout-aware augment_personal_positive()/
    extract_personal_positive_features() pair below intentionally continues
    to use this unfiltered listing, since curation was scoped to the
    personalized training pipeline only."""
    src = Path(data_dir) / PERSONAL_POSITIVE_DIRNAME
    if not src.exists():
        return []
    return sorted(src.glob("*.wav"))


# Manually reviewed, human-approved exclusion list (2026-09-12 curation pass
# over the 23-recording consolidated dataset). This is a deterministic
# ALLOWLIST-by-exclusion, not an acoustic-threshold filter applied at
# training time - the reasons below reflect a human listening review, not a
# recomputed measurement:
#   - "Recording (10).wav": more background noise and very low audible voice.
#   - "Recording (4).wav": only "Hey voi" is understandable - wake phrase is
#     incomplete/unclear.
#   - "personal_voice__Recording (4).wav": same issue as Recording (4).wav -
#     wake phrase not sufficiently clear.
# Deliberately KEPT despite being acoustically unusual (reviewed and judged
# useful pronunciation/acoustic variation, not defects):
#   - "Recording (2).wav": audible, starts immediately with the wake phrase,
#     somewhat fast.
#   - "personal_voice__Recording (2).wav": similar variation to the above.
# The three excluded files are NEVER deleted/moved/modified - they remain
# physically present in data/personal_positive/ at all times; only their
# participation in the personalized train/holdout pipeline is removed.
EXCLUDED_PERSONAL_RECORDINGS = frozenset({
    "Recording (10).wav",
    "Recording (4).wav",
    "personal_voice__Recording (4).wav",
})


def discover_approved_personal_positive_wavs(data_dir: str | Path) -> list[Path]:
    """The curated listing used by the personalized train/holdout pipeline:
    every physically-present personal recording (discover_personal_positive_
    wavs(), unmodified) MINUS the human-reviewed EXCLUDED_PERSONAL_RECORDINGS.
    Matching is by filename only - the excluded files are never touched on
    disk, only omitted from this list. Deterministic: depends only on which
    files currently exist and the fixed EXCLUDED_PERSONAL_RECORDINGS set,
    never on any acoustic measurement.
    """
    all_wavs = discover_personal_positive_wavs(data_dir)
    return [p for p in all_wavs if p.name not in EXCLUDED_PERSONAL_RECORDINGS]


def augment_personal_positive(config: dict, data_dir: str | Path) -> Path:
    """Augments data/personal_positive/*.wav into data/personal_augmented/
    using the existing augment_directory() implementation, unmodified. The
    9 original source WAVs are only ever read here, never moved or deleted -
    augment_directory() already writes an untouched '_orig' copy of each
    plus 'variants_per_clip' augmented copies into the OUTPUT directory,
    leaving the source directory exactly as it was.
    """
    from training.augment import augment_directory

    data_dir = Path(data_dir)
    src = data_dir / PERSONAL_POSITIVE_DIRNAME
    wavs = discover_personal_positive_wavs(data_dir)
    if not wavs:
        raise FileNotFoundError(f"no personal positive WAV files found at {src}")

    dst = data_dir / PERSONAL_AUGMENTED_DIRNAME
    augment_directory(src, dst, config["augmentation"], config["seed"])
    return dst


def extract_personal_positive_features(config: dict, data_dir: str | Path,
                                       audio_features=None):
    """Extracts features for data/personal_augmented/*.wav using the existing
    extract_features implementation, unmodified, and writes
    data/features/positive_personal.npy (never touching any other .npy file
    in that directory). Returns the (N, 16, 96) array. Requires
    augment_personal_positive() to have been run first."""
    import numpy as np
    from training.extract_features import (
        build_audio_features, extract_features_for_wavs, resolve_clip_seconds,
    )

    data_dir = Path(data_dir)
    augmented_dir = data_dir / PERSONAL_AUGMENTED_DIRNAME
    wavs = sorted(str(p) for p in augmented_dir.glob("*.wav"))
    if not wavs:
        raise FileNotFoundError(
            f"no augmented personal clips at {augmented_dir} - run "
            f"augment_personal_positive() first")

    audio_features = audio_features or build_audio_features()
    sr = config["features"]["sample_rate"]
    clip_seconds = resolve_clip_seconds(
        audio_features, target_frames=16,
        configured_seconds=config["features"]["clip_seconds"])

    features = extract_features_for_wavs(wavs, audio_features, clip_seconds, sr)

    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    out_path = features_dir / f"{PERSONAL_FEATURE_NAME}.npy"
    np.save(out_path, features)
    return features


# =========================================================================
# NEW: holdout-aware split path (source-leakage-safe)
# =========================================================================

def split_personal_recordings(wavs: list[Path], seed: int,
                              n_holdout: int = DEFAULT_N_HOLDOUT
                              ) -> tuple[list[Path], list[Path]]:
    """Deterministically splits ORIGINAL recordings - never their augmented
    derivatives - into (train, holdout) sets. Splitting happens at this level,
    before any augmentation, specifically so no augmented variant of a
    holdout recording can ever leak into a training set: the two groups are
    never fed to augmentation together.

    Deterministic: the same `wavs` list and `seed` always produce the same
    split (a local random.Random(seed) instance, not the global random
    module - reproducible independent of call order or other code's RNG use).
    `wavs` is sorted first so the result also does not depend on filesystem
    iteration order.
    """
    if n_holdout >= len(wavs):
        raise ValueError(
            f"n_holdout ({n_holdout}) must be smaller than the number of "
            f"recordings ({len(wavs)})")
    ordered = sorted(wavs, key=lambda p: Path(p).name)
    rng = random.Random(seed)
    holdout = sorted(rng.sample(ordered, n_holdout), key=lambda p: Path(p).name)
    holdout_names = {Path(p).name for p in holdout}
    train = [w for w in ordered if Path(w).name not in holdout_names]
    return train, holdout


def augment_specific_files(wav_paths: list[Path], output_dir: str | Path,
                           aug_cfg: dict, seed: int) -> list[Path]:
    """Identical per-file behavior to training.augment.augment_directory (an
    untouched '_orig' copy plus 'variants_per_clip' augmented '_aug{v}'
    copies per source clip, via the SAME unchanged build_augmentation_pipeline
    / _read_wav / _write_wav helpers) but restricted to an explicit list of
    source files rather than globbing an entire directory. This lets the
    training-split recordings be augmented without ever creating a directory
    that also contains the holdout recordings - a hard structural guarantee
    against source leakage, not just a filtering step applied after the fact.
    """
    from training.augment import _read_wav, _write_wav, build_augmentation_pipeline

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    ordered = sorted(wav_paths, key=lambda p: Path(p).name)

    if not aug_cfg.get("enabled", True):
        for src in ordered:
            samples, sr = _read_wav(str(src))
            dst = output_dir / Path(src).name
            _write_wav(str(dst), samples, sr)
            written.append(dst)
        return written

    pipeline = build_augmentation_pipeline(aug_cfg, seed)
    variants = int(aug_cfg["variants_per_clip"])

    for src in ordered:
        samples, sr = _read_wav(str(src))
        stem = Path(src).stem

        original_dst = output_dir / f"{stem}_orig.wav"
        _write_wav(str(original_dst), samples, sr)
        written.append(original_dst)

        for v in range(variants):
            augmented = pipeline(samples=samples, sample_rate=sr)
            dst = output_dir / f"{stem}_aug{v}.wav"
            _write_wav(str(dst), augmented, sr)
            written.append(dst)

    return written


def augment_personal_training_split(config: dict, data_dir: str | Path,
                                    n_holdout: int = DEFAULT_N_HOLDOUT
                                    ) -> tuple[Path, list[Path], list[Path]]:
    """Splits the 9 original recordings (7 train / 2 holdout by default) and
    augments ONLY the training split into data/personal_train_augmented/.
    The holdout recordings are never read by augment_specific_files() at
    all - not filtered out afterward, never given to the augmentation
    pipeline in the first place. Returns (augmented_dir, train_wavs,
    holdout_wavs) so callers (and tests) can verify the split without
    re-deriving it.
    """
    data_dir = Path(data_dir)
    src = data_dir / PERSONAL_POSITIVE_DIRNAME
    wavs = discover_approved_personal_positive_wavs(data_dir)
    if not wavs:
        raise FileNotFoundError(
            f"no approved personal positive WAV files found at {src} "
            f"(after excluding {sorted(EXCLUDED_PERSONAL_RECORDINGS)})")

    train_wavs, holdout_wavs = split_personal_recordings(
        wavs, config["seed"], n_holdout)

    dst = data_dir / PERSONAL_TRAIN_AUGMENTED_DIRNAME
    augment_specific_files(train_wavs, dst, config["augmentation"], config["seed"])
    return dst, train_wavs, holdout_wavs


def extract_personal_training_split_features(config: dict, data_dir: str | Path,
                                             audio_features=None):
    """Extracts features for data/personal_train_augmented/*.wav (the
    train-split augmented clips only) and writes
    data/features/positive_personal_train.npy. Requires
    augment_personal_training_split() to have been run first."""
    import numpy as np
    from training.extract_features import (
        build_audio_features, extract_features_for_wavs, resolve_clip_seconds,
    )

    data_dir = Path(data_dir)
    augmented_dir = data_dir / PERSONAL_TRAIN_AUGMENTED_DIRNAME
    wavs = sorted(str(p) for p in augmented_dir.glob("*.wav"))
    if not wavs:
        raise FileNotFoundError(
            f"no augmented personal training clips at {augmented_dir} - run "
            f"augment_personal_training_split() first")

    audio_features = audio_features or build_audio_features()
    sr = config["features"]["sample_rate"]
    clip_seconds = resolve_clip_seconds(
        audio_features, target_frames=16,
        configured_seconds=config["features"]["clip_seconds"])

    features = extract_features_for_wavs(wavs, audio_features, clip_seconds, sr)

    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    np.save(features_dir / f"{PERSONAL_TRAIN_FEATURE_NAME}.npy", features)
    return features


def extract_personal_holdout_features(config: dict, data_dir: str | Path,
                                      n_holdout: int = DEFAULT_N_HOLDOUT,
                                      audio_features=None):
    """Extracts features DIRECTLY from the raw (unaugmented) holdout
    recordings - never from any augmented derivative - and writes
    data/features/positive_personal_holdout.npy. Evaluating on the raw
    holdout recordings (rather than augmented copies of them) is deliberate:
    the question this is meant to answer is whether the model recognizes a
    genuinely unseen recording of the user's own voice, not whether it is
    robust to synthetic signal perturbations (a separate, already-covered
    question on the training side).
    """
    import numpy as np
    from training.extract_features import (
        build_audio_features, extract_features_for_wavs, resolve_clip_seconds,
    )

    data_dir = Path(data_dir)
    wavs = discover_approved_personal_positive_wavs(data_dir)
    if not wavs:
        raise FileNotFoundError(
            f"no approved personal positive WAV files found at "
            f"{data_dir / PERSONAL_POSITIVE_DIRNAME} "
            f"(after excluding {sorted(EXCLUDED_PERSONAL_RECORDINGS)})")
    _train_wavs, holdout_wavs = split_personal_recordings(
        wavs, config["seed"], n_holdout)

    audio_features = audio_features or build_audio_features()
    sr = config["features"]["sample_rate"]
    clip_seconds = resolve_clip_seconds(
        audio_features, target_frames=16,
        configured_seconds=config["features"]["clip_seconds"])

    features = extract_features_for_wavs(
        [str(p) for p in holdout_wavs], audio_features, clip_seconds, sr)

    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    np.save(features_dir / f"{PERSONAL_HOLDOUT_FEATURE_NAME}.npy", features)
    return features


# =========================================================================
# NEW: minimal personalized-training integration helpers
# =========================================================================
#
# These are deliberately small and additive: they let train_void.py's
# EXISTING stage_train()/stage_evaluate() be reused completely unchanged
# (personalization is expressed only via which `features` dict and which
# `config["model_name"]` are passed in, both already-supported parameters),
# plus one genuinely new function (evaluate_personal_holdout) for the
# separate holdout-only evaluation stage_evaluate() has no equivalent of.

def load_personal_train_features(data_dir: str | Path):
    """Loads data/features/positive_personal_train.npy (the augmented
    train-split clips - 54 as of the 23-recording/18-train consolidation).
    Raises FileNotFoundError with an actionable message if the
    personal-positive data-preparation step hasn't been run yet - never
    silently substitutes an empty array. Returns the raw (unoversampled)
    array; see oversample_personal_train_features() for Strategy B."""
    import numpy as np

    path = Path(data_dir) / "features" / f"{PERSONAL_TRAIN_FEATURE_NAME}.npy"
    if not path.exists():
        raise FileNotFoundError(
            f"no personal training features at {path} - run "
            f"augment_personal_training_split() and "
            f"extract_personal_training_split_features() first")
    return np.load(path)


def oversample_personal_train_features(features, factor: int = PERSONAL_OVERSAMPLE_FACTOR):
    """Strategy B: physically duplicates the given (N, 16, 96) personal
    training feature array `factor` times along axis 0, producing an
    effective (N * factor, 16, 96) array - e.g. 54 -> 270 at the approved
    factor of 5. This is a pure data-preprocessing step: it changes what
    array is handed to training.train.train_model(), not how that function
    samples from it. training.train.py's sampling loop
    (`rng.integers(0, n, size=batch_size)`, uniform with replacement) is
    never modified - it simply operates on a larger input.

    Applied ONLY to positive_personal_train by its one caller (train_void.py's
    stage_train_personalized()) - never to positive_train/adversarial_train/
    noise_train/speech_train, which are passed through unchanged.
    """
    import numpy as np

    factor = int(factor)
    if factor < 1:
        raise ValueError(f"oversample factor must be >= 1, got {factor}")
    if factor == 1:
        return features
    return np.concatenate([features] * factor, axis=0)


def load_personal_holdout_features(data_dir: str | Path):
    """Loads data/features/positive_personal_holdout.npy (the raw holdout
    clips - 5 as of the 23-recording consolidation). Raises
    FileNotFoundError with an actionable message if the personal-positive
    data-preparation step hasn't been run yet."""
    import numpy as np

    path = Path(data_dir) / "features" / f"{PERSONAL_HOLDOUT_FEATURE_NAME}.npy"
    if not path.exists():
        raise FileNotFoundError(
            f"no personal holdout features at {path} - run "
            f"extract_personal_holdout_features() first")
    return np.load(path)


def evaluate_personal_holdout(net, holdout_features, threshold: float,
                              extra_thresholds: "list[float] | None" = None) -> dict:
    """Scores the personal holdout set (all label=1, by construction - it is
    never mixed with any negative category) against an already-trained
    network, using the SAME scoring helper training.evaluate._scores() uses
    for the normal validation set, unchanged. Deliberately separate from
    training.evaluate.evaluate_model(): that function assumes both a
    positive AND a negative population are present (to compute FP/FN/
    accuracy); an all-positive holdout set only supports recall-style
    metrics, so this is a distinct, purpose-built function rather than a
    forced reuse of evaluate_model() with a fake empty negative set.
    """
    from training.evaluate import _scores

    scores = _scores(net, holdout_features.astype("float32"))
    n = len(scores)
    tp = int((scores >= threshold).sum()) if n else 0

    result = {
        "n_holdout": n,
        "threshold": threshold,
        "true_positives_at_threshold": tp,
        "false_negatives_at_threshold": n - tp,
        "recall_at_threshold": (tp / n) if n else None,
        "scores": [float(s) for s in scores],
        "min_score": float(scores.min()) if n else None,
        "max_score": float(scores.max()) if n else None,
        "mean_score": float(scores.mean()) if n else None,
    }
    if extra_thresholds:
        result["threshold_sweep"] = [
            {"threshold": t,
             "true_positives": int((scores >= t).sum()) if n else 0,
             "false_negatives": int((scores < t).sum()) if n else 0,
             "recall": (float((scores >= t).sum()) / n) if n else None}
            for t in extra_thresholds
        ]
    return result
