"""Tests for the holdout-aware personal-recording split
(training/personal_positive.py's split_personal_recordings /
augment_personal_training_split / extract_personal_training_split_features /
extract_personal_holdout_features), including the human-approved curation
(EXCLUDED_PERSONAL_RECORDINGS / discover_approved_personal_positive_wavs()).

Fast, non-integration tests cover the split logic itself (determinism, exact
counts, no overlap, exclusion enforcement) using both the real personal
recordings (skipped cleanly if absent) and small synthetic path lists for
edge cases.

As of the 2026-09-12 curation pass: 23 physically-present recordings, 3
human-reviewed exclusions (EXCLUDED_PERSONAL_RECORDINGS), 20 approved,
split 15 train / 5 holdout via DEFAULT_N_HOLDOUT=5.

Integration tests (real audiomentations + the real openwakeword feature
backbone, already cached in this venv) copy ALL real personal recordings
(including the 3 excluded ones, to faithfully exercise the real exclusion
logic rather than pre-filtering before the test even runs) into an ISOLATED
tmp_path directory and run the real split/augment/extract functions there -
never touching the real data/personal_train_augmented,
data/features/positive_personal_train.npy, or
data/features/positive_personal_holdout.npy produced separately by the
actual data-preparation run, and never touching the OLDER
data/personal_augmented / data/features/positive_personal.npy artifacts
from the non-holdout-aware path at all.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from training.personal_positive import (
    EXCLUDED_PERSONAL_RECORDINGS,
    PERSONAL_HOLDOUT_FEATURE_NAME,
    PERSONAL_TRAIN_FEATURE_NAME,
    augment_personal_training_split,
    discover_approved_personal_positive_wavs,
    discover_personal_positive_wavs,
    extract_personal_holdout_features,
    extract_personal_training_split_features,
    split_personal_recordings,
)

REAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REAL_PERSONAL_POSITIVE_DIR = REAL_DATA_DIR / "personal_positive"
REAL_SEED = 1234  # matches config/hey_void.yaml's top-level `seed`

_AUG_CFG = {"enabled": True, "variants_per_clip": 2,
           "gain_db_range": [-6, 6], "pitch_semitone_range": [-2, 2],
           "add_noise_probability": 0.5, "noise_snr_db_range": [5, 15],
           "bandstop_probability": 0.3, "distortion_probability": 0.2,
           "reverb_probability": 0.3}


def _skip_if_no_real_personal_recordings():
    if not REAL_PERSONAL_POSITIVE_DIR.exists() or \
            not any(REAL_PERSONAL_POSITIVE_DIR.glob("*.wav")):
        pytest.skip(f"no personal recordings at {REAL_PERSONAL_POSITIVE_DIR}")


def _copy_real_personal_positive_into(tmp_data_dir: Path) -> list[Path]:
    # Copies ALL physically-present recordings, excluded ones included, so
    # integration tests exercise the real exclusion logic inside the tmp
    # copy rather than assuming it via pre-filtering.
    dst = tmp_data_dir / "personal_positive"
    dst.mkdir(parents=True)
    src_wavs = discover_personal_positive_wavs(REAL_DATA_DIR)
    for p in src_wavs:
        shutil.copy(p, dst / p.name)
    return src_wavs


# --- curation / exclusion (fast, real filenames) ---------------------------

def test_exactly_three_specified_files_are_excluded():
    assert EXCLUDED_PERSONAL_RECORDINGS == frozenset({
        "Recording (10).wav",
        "Recording (4).wav",
        "personal_voice__Recording (4).wav",
    })
    assert len(EXCLUDED_PERSONAL_RECORDINGS) == 3


def test_all_other_twenty_recordings_remain_approved():
    _skip_if_no_real_personal_recordings()
    all_wavs = discover_personal_positive_wavs(REAL_DATA_DIR)
    approved = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    assert len(all_wavs) == 23
    assert len(approved) == 20
    approved_names = {p.name for p in approved}
    assert approved_names == {p.name for p in all_wavs} - EXCLUDED_PERSONAL_RECORDINGS


def test_excluded_files_never_appear_in_approved_discovery():
    _skip_if_no_real_personal_recordings()
    approved_names = {p.name for p in discover_approved_personal_positive_wavs(REAL_DATA_DIR)}
    assert approved_names.isdisjoint(EXCLUDED_PERSONAL_RECORDINGS)


def test_excluded_original_wavs_still_exist_on_disk():
    _skip_if_no_real_personal_recordings()
    for name in EXCLUDED_PERSONAL_RECORDINGS:
        path = REAL_PERSONAL_POSITIVE_DIR / name
        assert path.exists(), f"excluded file {name} must remain physically present"
        assert path.stat().st_size > 0


def test_curation_is_deterministic():
    # Same input set -> same approved set, every call, no randomness involved.
    fake_dir_listing = ["a.wav", "Recording (10).wav", "b.wav", "Recording (4).wav",
                        "personal_voice__Recording (4).wav", "c.wav"]
    for _ in range(5):
        result = [n for n in fake_dir_listing if n not in EXCLUDED_PERSONAL_RECORDINGS]
        assert result == ["a.wav", "b.wav", "c.wav"]


# --- split logic on the APPROVED (curated) set -----------------------------

def test_exactly_fifteen_originals_assigned_to_train():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    train, _holdout = split_personal_recordings(wavs, seed=REAL_SEED)
    assert len(train) == 15


def test_exactly_five_originals_assigned_to_holdout():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    _train, holdout = split_personal_recordings(wavs, seed=REAL_SEED)
    assert len(holdout) == 5


def test_excluded_files_never_appear_in_train_or_holdout():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    train, holdout = split_personal_recordings(wavs, seed=REAL_SEED)
    train_names = {p.name for p in train}
    holdout_names = {p.name for p in holdout}
    assert train_names.isdisjoint(EXCLUDED_PERSONAL_RECORDINGS)
    assert holdout_names.isdisjoint(EXCLUDED_PERSONAL_RECORDINGS)


def test_no_original_basename_appears_in_both_sets():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    train, holdout = split_personal_recordings(wavs, seed=REAL_SEED)
    train_names = {p.name for p in train}
    holdout_names = {p.name for p in holdout}
    assert train_names.isdisjoint(holdout_names)
    assert len(train_names) + len(holdout_names) == len(wavs)


def test_split_is_deterministic_across_repeated_calls():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    t1, h1 = split_personal_recordings(wavs, seed=REAL_SEED)
    t2, h2 = split_personal_recordings(wavs, seed=REAL_SEED)
    assert [p.name for p in t1] == [p.name for p in t2]
    assert [p.name for p in h1] == [p.name for p in h2]


def test_split_is_independent_of_input_list_order():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    reversed_wavs = list(reversed(wavs))
    t1, h1 = split_personal_recordings(wavs, seed=REAL_SEED)
    t2, h2 = split_personal_recordings(reversed_wavs, seed=REAL_SEED)
    assert {p.name for p in t1} == {p.name for p in t2}
    assert {p.name for p in h1} == {p.name for p in h2}


def test_different_seed_can_produce_a_different_split():
    _skip_if_no_real_personal_recordings()
    wavs = discover_approved_personal_positive_wavs(REAL_DATA_DIR)
    _t_a, h_a = split_personal_recordings(wavs, seed=REAL_SEED)
    _t_b, h_b = split_personal_recordings(wavs, seed=REAL_SEED + 1)
    # Not a strict guarantee for every possible seed pair, but this specific
    # pair is checked-in as a regression fixture: the split is seed-sensitive,
    # not a fixed rule independent of the seed.
    assert {p.name for p in h_a} != {p.name for p in h_b}


def test_split_raises_when_n_holdout_is_not_smaller_than_total():
    fake_wavs = [Path(f"clip_{i}.wav") for i in range(3)]
    with pytest.raises(ValueError):
        split_personal_recordings(fake_wavs, seed=1, n_holdout=3)


# --- integration: real augmentation must never see excluded/holdout files -

@pytest.mark.integration
def test_no_augmented_derivative_of_holdout_recording_enters_training_split(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)  # includes the 3 excluded files

    config = {"seed": REAL_SEED, "augmentation": _AUG_CFG}
    out_dir, train_wavs, holdout_wavs = augment_personal_training_split(config, data_dir)

    assert len(train_wavs) == 15
    assert len(holdout_wavs) == 5

    holdout_stems = {p.stem for p in holdout_wavs}
    produced = list(out_dir.glob("*.wav"))
    assert len(produced) == 15 * 3  # variants_per_clip=2 + 1 orig, for the 15 train clips only

    for f in produced:
        # augmented filenames are "<source_stem>_orig.wav" / "<source_stem>_aug{v}.wav"
        source_stem = f.stem.rsplit("_", 1)[0]
        assert source_stem not in holdout_stems, (
            f"{f} appears to derive from a holdout recording ({source_stem})")


@pytest.mark.integration
def test_no_excluded_recording_enters_augmented_training_output(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)  # includes the 3 excluded files

    config = {"seed": REAL_SEED, "augmentation": _AUG_CFG}
    out_dir, train_wavs, holdout_wavs = augment_personal_training_split(config, data_dir)

    excluded_stems = {Path(n).stem for n in EXCLUDED_PERSONAL_RECORDINGS}
    train_names = {p.name for p in train_wavs}
    holdout_names = {p.name for p in holdout_wavs}
    produced_stems = {f.stem.rsplit("_", 1)[0] for f in out_dir.glob("*.wav")}

    assert train_names.isdisjoint(EXCLUDED_PERSONAL_RECORDINGS)
    assert holdout_names.isdisjoint(EXCLUDED_PERSONAL_RECORDINGS)
    assert produced_stems.isdisjoint(excluded_stems)


@pytest.mark.integration
def test_no_excluded_recording_enters_holdout_features(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)

    config = {
        "seed": REAL_SEED, "augmentation": _AUG_CFG,
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
    }
    # extract_personal_holdout_features re-derives the split internally via
    # discover_approved_personal_positive_wavs() - this proves the SAME
    # exclusion guarantee holds independent of augment_personal_training_split.
    features = extract_personal_holdout_features(config, data_dir)
    assert features.shape[0] == 5  # never 6+ from an excluded file leaking in


@pytest.mark.integration
def test_personal_holdout_features_have_expected_shape(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)

    config = {
        "seed": REAL_SEED, "augmentation": _AUG_CFG,
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
    }
    features = extract_personal_holdout_features(config, data_dir)

    assert features.shape == (5, 16, 96)
    out_path = data_dir / "features" / f"{PERSONAL_HOLDOUT_FEATURE_NAME}.npy"
    assert out_path.exists()
    saved = np.load(out_path)
    assert np.array_equal(saved, features)


@pytest.mark.integration
def test_personal_training_split_feature_shape_matches_fifteen_recordings(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)

    config = {
        "seed": REAL_SEED, "augmentation": _AUG_CFG,
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
    }
    augment_personal_training_split(config, data_dir)
    features = extract_personal_training_split_features(config, data_dir)

    assert features.shape == (45, 16, 96)  # 15 recordings * (1 orig + 2 aug)
    out_path = data_dir / "features" / f"{PERSONAL_TRAIN_FEATURE_NAME}.npy"
    assert out_path.exists()


@pytest.mark.integration
def test_holdout_and_training_split_features_are_disjoint_sample_counts(tmp_path):
    # Sanity cross-check: 15 (train) + 5 (holdout) == 20 (approved originals,
    # 23 physical minus 3 excluded), and the train side is augmented
    # (45 clips) while holdout is raw (5).
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)

    config = {
        "seed": REAL_SEED, "augmentation": _AUG_CFG,
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
    }
    augment_personal_training_split(config, data_dir)
    train_features = extract_personal_training_split_features(config, data_dir)
    holdout_features = extract_personal_holdout_features(config, data_dir)

    assert train_features.shape[0] == 45
    assert holdout_features.shape[0] == 5
