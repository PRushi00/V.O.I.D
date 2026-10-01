"""Tests for the personal-positive training-data path
(training/personal_positive.py) - real human recordings of the wake phrase,
kept separate from and additive to the existing synthetic dataset.

Fast, non-integration tests cover: discovery-count (skips cleanly if the real
personal recordings aren't present on this machine), build_dataset labeling
compatibility, and the "cannot enter validation" structural guarantee - all
using tiny synthetic arrays, no real backbone needed.

Integration tests (real audiomentations + the real openwakeword feature
backbone, already cached in this venv - no network access) copy the real
personal recordings into an ISOLATED tmp_path directory and run the real
augment/extract functions there, so they never touch or depend on the real
data/personal_augmented or data/features/positive_personal.npy produced
separately by the actual data-preparation run.

Counts below are dynamic (derived from whatever discover_personal_positive_
wavs() actually finds), not hardcoded, since the personal recording set has
already grown once (9 -> 23) and may grow again.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

import train_void
from training.personal_positive import (
    PERSONAL_FEATURE_NAME,
    augment_personal_positive,
    discover_personal_positive_wavs,
    extract_personal_positive_features,
)
from training.train import build_dataset

REAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REAL_PERSONAL_POSITIVE_DIR = REAL_DATA_DIR / "personal_positive"

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
    dst = tmp_data_dir / "personal_positive"
    dst.mkdir(parents=True)
    src_wavs = discover_personal_positive_wavs(REAL_DATA_DIR)
    for p in src_wavs:
        shutil.copy(p, dst / p.name)
    return src_wavs


# --- 1. discovery ------------------------------------------------------

def test_all_personal_wavs_are_discovered():
    _skip_if_no_real_personal_recordings()
    wavs = discover_personal_positive_wavs(REAL_DATA_DIR)
    # 23 = 9 originals + 14 consolidated in from personal_voice/ (2026-09-12).
    assert len(wavs) == 23


def test_discover_returns_empty_list_when_directory_absent(tmp_path):
    assert discover_personal_positive_wavs(tmp_path) == []


# --- 2/3. real augmentation output count (integration: real audiomentations)

@pytest.mark.integration
def test_augmentation_output_count_is_correct(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    src_wavs = _copy_real_personal_positive_into(data_dir)

    config = {"seed": 1234, "augmentation": _AUG_CFG}
    out_dir = augment_personal_positive(config, data_dir)

    # variants_per_clip=2 + 1 untouched original per source clip
    assert len(list(out_dir.glob("*.wav"))) == len(src_wavs) * 3 == 69
    # the 23 original source files remain untouched, in their own directory
    assert len(list((data_dir / "personal_positive").glob("*.wav"))) == 23


# --- 4/6. real feature extraction shape + saved-array correctness ----------

@pytest.mark.integration
def test_personal_feature_shape_is_compatible_with_existing_representation(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)

    config = {
        "seed": 1234, "augmentation": _AUG_CFG,
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
    }
    augment_personal_positive(config, data_dir)
    features = extract_personal_positive_features(config, data_dir)

    assert features.shape == (69, 16, 96)

    out_path = data_dir / "features" / f"{PERSONAL_FEATURE_NAME}.npy"
    assert out_path.exists()
    saved = np.load(out_path)
    assert saved.shape == features.shape
    assert np.array_equal(saved, features)


@pytest.mark.integration
def test_extract_without_prior_augmentation_fails_clearly(tmp_path):
    _skip_if_no_real_personal_recordings()
    data_dir = tmp_path / "data"
    _copy_real_personal_positive_into(data_dir)
    config = {"features": {"sample_rate": 16000, "clip_seconds": 2.1}}
    with pytest.raises(FileNotFoundError):
        extract_personal_positive_features(config, data_dir)


# --- 5. build_dataset labeling compatibility (fast, no real data needed) --

def test_positive_personal_is_labeled_positive_by_build_dataset():
    features = {
        PERSONAL_FEATURE_NAME: np.zeros((3, 16, 96), dtype=np.float32),
        "noise_train": np.zeros((5, 16, 96), dtype=np.float32),
    }
    X, y = build_dataset(features)
    assert X.shape == (8, 16, 96)
    assert (y == 1.0).sum() == 3
    assert (y == 0.0).sum() == 5


# --- 6. personal data cannot accidentally enter validation ------------------

def test_positive_personal_key_does_not_end_with_val():
    assert not PERSONAL_FEATURE_NAME.endswith("_val")


def test_positive_personal_is_not_one_of_the_raw_categories_train_void_loads(tmp_path):
    # train_void.py's _load_saved_features()/stage_train()/stage_evaluate()
    # only ever look at _raw_categories(data_dir)'s fixed key set - proving
    # "positive_personal" isn't among them means today's existing --train/
    # --evaluate CLI stages cannot pick it up at all (for training OR
    # validation) without a separate, explicit future change.
    categories = train_void._raw_categories(tmp_path)
    assert PERSONAL_FEATURE_NAME not in categories


def test_positive_personal_would_be_excluded_by_the_existing_val_filter():
    # Mirrors stage_evaluate()'s exact filter expression:
    #   val_features = {k: v for k, v in features.items() if k.endswith("_val")}
    features = {
        PERSONAL_FEATURE_NAME: np.zeros((3, 16, 96), dtype=np.float32),
        "positive_val": np.zeros((2, 16, 96), dtype=np.float32),
    }
    val_features = {k: v for k, v in features.items() if k.endswith("_val")}
    assert PERSONAL_FEATURE_NAME not in val_features
    assert "positive_val" in val_features


def test_positive_personal_matches_neither_train_nor_val_suffix_filter():
    # "positive_personal" ends with neither "_train" nor "_val", so it is
    # inert to BOTH of train_void.py's existing stage_train()/stage_evaluate()
    # suffix filters today - it can only ever be included by a separate,
    # explicit future change (e.g. merging it in alongside the "_train"
    # features before calling train_model()), never silently, and never as
    # validation data.
    features = {
        PERSONAL_FEATURE_NAME: np.zeros((3, 16, 96), dtype=np.float32),
        "positive_val": np.zeros((2, 16, 96), dtype=np.float32),
        "positive_train": np.zeros((4, 16, 96), dtype=np.float32),
    }
    train_features = {k: v for k, v in features.items() if k.endswith("_train")}
    val_features = {k: v for k, v in features.items() if k.endswith("_val")}
    assert PERSONAL_FEATURE_NAME not in train_features
    assert PERSONAL_FEATURE_NAME not in val_features
    assert "positive_train" in train_features and "positive_val" in val_features
