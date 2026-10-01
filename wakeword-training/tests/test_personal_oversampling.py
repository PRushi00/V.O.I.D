"""Tests for Strategy B: moderate personal oversampling
(training/personal_positive.py's oversample_personal_train_features() and
train_void.py's stage_train_personalized() oversample_factor wiring).

Fast and hermetic: same monkeypatching approach as
tests/test_personal_training_integration.py (a tiny real torch.nn.Module
stands in for the real openwakeword network, and training.train.train_model
is replaced with a fake that captures what it was actually called with) - no
real audio, no real backbone, no real training loop.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import torch

import train_void
import training.train
from training.personal_positive import (
    PERSONAL_HOLDOUT_FEATURE_NAME,
    PERSONAL_OVERSAMPLE_FACTOR,
    PERSONAL_TRAIN_FEATURE_NAME,
    oversample_personal_train_features,
)

NORMAL_TRAIN_CATEGORIES = {"positive_train", "adversarial_train",
                          "noise_train", "speech_train"}


class _FakeNet(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc = torch.nn.Linear(16 * 96, 1)

    def forward(self, x):
        return torch.sigmoid(self.fc(x.reshape(x.shape[0], -1)))


class _FakeOWWModel:
    def __init__(self):
        self.model = _FakeNet()
        self.input_shape = (16, 96)

    def export_to_onnx(self, path, class_mapping=None):
        Path(path).write_bytes(b"fake-onnx")


def _write_feature_npy(features_dir: Path, name: str, n: int) -> None:
    arr = np.zeros((n, 16, 96), dtype=np.float32)
    np.save(features_dir / f"{name}.npy", arr)


@pytest.fixture
def rigged_data_dir(tmp_path):
    """8 normal categories (4 train + 4 val) at their real proportions,
    plus 54 personal-train and 5 personal-holdout - mirroring the actual
    current dataset shape (2154 total before oversampling)."""
    data_dir = tmp_path / "data"
    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True)
    counts = {"positive_train": 600, "positive_val": 150,
             "adversarial_train": 600, "adversarial_val": 150,
             "noise_train": 600, "noise_val": 150,
             "speech_train": 300, "speech_val": 75}
    for name, n in counts.items():
        _write_feature_npy(features_dir, name, n)
    _write_feature_npy(features_dir, PERSONAL_TRAIN_FEATURE_NAME, 54)
    _write_feature_npy(features_dir, PERSONAL_HOLDOUT_FEATURE_NAME, 5)
    return data_dir


@pytest.fixture
def base_config():
    return {
        "model_name": "hey_void_test",
        "seed": 1,
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 4, "training_steps": 2, "learning_rate": 0.01,
                 "val_every_n_steps": 1},
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
        "evaluation": {"threshold": 0.5, "target_fp_per_hour": 0.5},
    }


@pytest.fixture
def captured():
    return {}


@pytest.fixture(autouse=True)
def _patch_train_model(monkeypatch, captured):
    def fake_train_model(config, features, output_dir, val_features=None):
        captured["features_shapes"] = {k: v.shape[0] for k, v in features.items()}
        captured["model_name"] = config["model_name"]
        captured["val_features"] = val_features
        owwmodel = _FakeOWWModel()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        onnx_path = output_dir / f"{config['model_name']}.onnx"
        owwmodel.export_to_onnx(str(onnx_path))
        history = {"step": [0], "loss": [0.1], "accuracy": [1.0]}
        return owwmodel, history, onnx_path

    monkeypatch.setattr(training.train, "train_model", fake_train_model)


# --- 1. oversample_personal_train_features() produces the expected count --

def test_k5_produces_expected_effective_personal_sample_count():
    raw = np.zeros((54, 16, 96), dtype=np.float32)
    out = oversample_personal_train_features(raw, factor=5)
    assert out.shape[0] == 270


def test_default_factor_matches_the_approved_strategy_b_constant():
    assert PERSONAL_OVERSAMPLE_FACTOR == 5
    raw = np.zeros((54, 16, 96), dtype=np.float32)
    out = oversample_personal_train_features(raw)  # uses the default
    assert out.shape[0] == 270


def test_oversample_factor_one_is_a_no_op():
    raw = np.zeros((54, 16, 96), dtype=np.float32)
    out = oversample_personal_train_features(raw, factor=1)
    assert out.shape[0] == 54


def test_oversample_rejects_factor_below_one():
    raw = np.zeros((54, 16, 96), dtype=np.float32)
    with pytest.raises(ValueError):
        oversample_personal_train_features(raw, factor=0)


def test_oversampled_content_is_exact_repetition_not_new_data():
    raw = np.arange(54 * 16 * 96, dtype=np.float32).reshape(54, 16, 96)
    out = oversample_personal_train_features(raw, factor=5)
    assert out.shape[0] == 270
    # every one of the 5 copies is byte-identical to the original 54 rows
    for copy_idx in range(5):
        chunk = out[copy_idx * 54:(copy_idx + 1) * 54]
        assert np.array_equal(chunk, raw)


# --- 2/3. normal categories are unchanged by oversampling -----------------

def test_normal_categories_are_not_oversampled(base_config, rigged_data_dir,
                                               tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    shapes = captured["features_shapes"]
    assert shapes["positive_train"] == 600
    assert shapes["adversarial_train"] == 600
    assert shapes["noise_train"] == 600
    assert shapes["speech_train"] == 300


def test_normal_training_path_is_completely_unaffected(base_config,
                                                        rigged_data_dir,
                                                        tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train(base_config, rigged_data_dir, model_dir, features=None)

    shapes = captured["features_shapes"]
    assert set(shapes) == NORMAL_TRAIN_CATEGORIES
    assert PERSONAL_TRAIN_FEATURE_NAME not in shapes
    assert shapes["positive_train"] == 600
    assert shapes["adversarial_train"] == 600
    assert shapes["noise_train"] == 600
    assert shapes["speech_train"] == 300


# --- 5. personalized training uses the OVERSAMPLED personal data ----------

def test_personalized_training_uses_oversampled_personal_data(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    shapes = captured["features_shapes"]
    assert shapes[PERSONAL_TRAIN_FEATURE_NAME] == 270  # 54 * 5, not 54

    total = sum(shapes.values())
    assert total == 2370  # 600+600+600+300+270


def test_explicit_oversample_factor_overrides_the_default(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir,
                                        oversample_factor=3)
    assert captured["features_shapes"][PERSONAL_TRAIN_FEATURE_NAME] == 162  # 54*3


# --- 4/6. holdout features are never included ------------------------------

def test_holdout_features_are_never_included_in_personalized_training(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_shapes"]


def test_holdout_features_are_never_included_in_normal_training(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train(base_config, rigged_data_dir, model_dir, features=None)
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_shapes"]


# --- 7. production model paths remain unchanged ----------------------------

def test_personalized_training_still_writes_to_the_personalized_model_name(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True)
    normal_onnx = model_dir / "hey_void_test.onnx"
    normal_pt = model_dir / "hey_void_test.pt"
    normal_onnx.write_bytes(b"ORIGINAL-PRODUCTION-ONNX")
    normal_pt.write_bytes(b"ORIGINAL-PRODUCTION-CHECKPOINT")

    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    assert captured["model_name"] == "hey_void_test_personalized"
    assert normal_onnx.read_bytes() == b"ORIGINAL-PRODUCTION-ONNX"
    assert normal_pt.read_bytes() == b"ORIGINAL-PRODUCTION-CHECKPOINT"
    assert (model_dir / "hey_void_test_personalized.onnx").exists()
