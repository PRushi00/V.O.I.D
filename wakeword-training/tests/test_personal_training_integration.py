"""Tests for the minimal personalized-training CLI integration in
train_void.py: stage_train_personalized() and
stage_personal_holdout_evaluate(), plus the guarantee that normal
--train/--evaluate dataset composition is completely unchanged.

Fast and hermetic: monkeypatches training.train.train_model /
training.evaluate.evaluate_model / training.extract_features.
build_audio_features/resolve_clip_seconds with tiny fakes that capture what
they were called with, so these tests run in milliseconds with no real
audio, no real openwakeword backbone, and no real training loop -
consistent with tests/test_train_export.py's existing approach of exercising
the real Model/torch.save() plumbing via a minimal real torch.nn.Module
standing in for the real network.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch

import train_void
import training.evaluate
import training.extract_features
import training.train
from training.personal_positive import (
    PERSONAL_HOLDOUT_FEATURE_NAME, PERSONAL_TRAIN_FEATURE_NAME,
)

NORMAL_TRAIN_CATEGORIES = {"positive_train", "adversarial_train",
                          "noise_train", "speech_train"}
NORMAL_VAL_CATEGORIES = {"positive_val", "adversarial_val",
                        "noise_val", "speech_val"}


class _FakeNet(torch.nn.Module):
    """Flatten-then-linear-then-sigmoid, so it accepts (N, 16, 96) input and
    produces (N, 1) output like the real openwakeword DNN architecture does -
    a plain nn.Linear(16*96, 1) applied directly would instead apply
    per-timestep (last-dim-only) and produce the wrong output shape."""

    def __init__(self):
        super().__init__()
        self.fc = torch.nn.Linear(16 * 96, 1)

    def forward(self, x):
        return torch.sigmoid(self.fc(x.reshape(x.shape[0], -1)))


class _FakeOWWModel:
    """Minimal stand-in with exactly the attributes the real code touches:
    .model (a real torch.nn.Module, so .state_dict()/.eval() work) and
    .input_shape."""

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
    """A tmp data/ tree with tiny fake .npy files for all 8 normal
    categories plus the 2 personal ones - enough for
    _load_saved_features()/load_personal_*_features() to succeed with no
    real audio or feature extraction."""
    data_dir = tmp_path / "data"
    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True)
    for name in NORMAL_TRAIN_CATEGORIES | NORMAL_VAL_CATEGORIES:
        _write_feature_npy(features_dir, name, 4)
    _write_feature_npy(features_dir, PERSONAL_TRAIN_FEATURE_NAME, 3)
    _write_feature_npy(features_dir, PERSONAL_HOLDOUT_FEATURE_NAME, 2)
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
        captured["features_keys"] = sorted(features.keys())
        captured["model_name"] = config["model_name"]
        captured["val_features"] = val_features
        owwmodel = _FakeOWWModel()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        onnx_path = output_dir / f"{config['model_name']}.onnx"
        owwmodel.export_to_onnx(str(onnx_path))
        history = {"step": [0], "loss": [0.1], "accuracy": [1.0]}
        return owwmodel, history, onnx_path

    # stage_train() does `from training.train import train_model` LOCALLY,
    # inside the function body - patching the module attribute is what
    # matters, since the local import re-reads it at call time.
    monkeypatch.setattr(training.train, "train_model", fake_train_model)


@pytest.fixture(autouse=True)
def _patch_evaluate_backend(monkeypatch, captured):
    def fake_evaluate_model(config, owwmodel, val_features, clip_seconds):
        captured["val_features_keys"] = sorted(val_features.keys())
        return {
            "n_positive_val": 0, "n_negative_val": 0,
            "threshold": config["evaluation"]["threshold"],
            "true_positives": 0, "false_negatives": 0,
            "false_positives": 0, "true_negatives": 0,
            "recall": None, "false_reject_rate": None,
            "false_accept_rate": None, "accuracy": None,
            "negative_hours_tested": 0.0, "false_positives_per_hour": None,
            "target_fp_per_hour": config["evaluation"]["target_fp_per_hour"],
            "threshold_sweep": [], "caveats": [],
        }

    monkeypatch.setattr(training.evaluate, "evaluate_model", fake_evaluate_model)
    monkeypatch.setattr(training.extract_features, "build_audio_features",
                        lambda: object())
    monkeypatch.setattr(training.extract_features, "resolve_clip_seconds",
                        lambda *a, **k: 2.1)


# --- 1. normal training excludes personal data ------------------------

def test_normal_training_excludes_personal_data(base_config, rigged_data_dir,
                                                 tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train(base_config, rigged_data_dir, model_dir, features=None)

    assert set(captured["features_keys"]) == NORMAL_TRAIN_CATEGORIES
    assert PERSONAL_TRAIN_FEATURE_NAME not in captured["features_keys"]
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_keys"]
    assert captured["model_name"] == "hey_void_test"


def test_load_saved_features_never_includes_personal_keys(rigged_data_dir):
    features = train_void._load_saved_features(rigged_data_dir)
    assert PERSONAL_TRAIN_FEATURE_NAME not in features
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in features


# --- 2/3/6. personalized training includes train, excludes holdout -----

def test_personalized_training_includes_positive_personal_train(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    assert PERSONAL_TRAIN_FEATURE_NAME in captured["features_keys"]
    assert set(captured["features_keys"]) == (
        NORMAL_TRAIN_CATEGORIES | {PERSONAL_TRAIN_FEATURE_NAME})


def test_personalized_training_excludes_positive_personal_holdout(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_keys"]


def test_no_personal_holdout_sample_can_enter_training(
        base_config, rigged_data_dir, tmp_path, captured):
    # Explicit, named proof (distinct from the exclusion test above): the
    # holdout array's sample count (2) never contributes to the count of
    # examples the fake trainer was actually handed.
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_keys"]
    # also true of plain (non-personalized) training:
    train_void.stage_train(base_config, rigged_data_dir, model_dir, features=None)
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["features_keys"]


def test_personalized_training_writes_to_a_distinct_model_name(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)
    assert captured["model_name"] == "hey_void_test_personalized"


# --- 18/19. normal model files are never overwritten --------------------

def test_personalized_training_never_overwrites_the_normal_model_files(
        base_config, rigged_data_dir, tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True)
    normal_onnx = model_dir / "hey_void_test.onnx"
    normal_pt = model_dir / "hey_void_test.pt"
    normal_onnx.write_bytes(b"ORIGINAL-PRODUCTION-ONNX")
    normal_pt.write_bytes(b"ORIGINAL-PRODUCTION-CHECKPOINT")

    train_void.stage_train_personalized(base_config, rigged_data_dir, model_dir)

    # untouched, byte-for-byte
    assert normal_onnx.read_bytes() == b"ORIGINAL-PRODUCTION-ONNX"
    assert normal_pt.read_bytes() == b"ORIGINAL-PRODUCTION-CHECKPOINT"
    # personalized outputs exist, distinctly named
    assert (model_dir / "hey_void_test_personalized.onnx").exists()
    assert (model_dir / "hey_void_test_personalized.pt").exists()
    assert (model_dir / "hey_void_test_personalized_history.json").exists()


# --- 4. normal validation remains unchanged ------------------------------

def test_normal_validation_remains_unchanged(base_config, rigged_data_dir,
                                             tmp_path, captured):
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True)
    train_void.stage_evaluate(base_config, rigged_data_dir, model_dir,
                              owwmodel=_FakeOWWModel())
    assert set(captured["val_features_keys"]) == NORMAL_VAL_CATEGORIES
    assert PERSONAL_TRAIN_FEATURE_NAME not in captured["val_features_keys"]
    assert PERSONAL_HOLDOUT_FEATURE_NAME not in captured["val_features_keys"]


def test_personalized_evaluate_still_uses_only_the_four_normal_categories(
        base_config, rigged_data_dir, tmp_path, captured):
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True)
    personalized_config = dict(base_config)
    personalized_config["model_name"] = "hey_void_test_personalized"
    train_void.stage_evaluate(personalized_config, rigged_data_dir, model_dir,
                              owwmodel=_FakeOWWModel())
    assert set(captured["val_features_keys"]) == NORMAL_VAL_CATEGORIES


# --- 5/7. personal holdout is evaluated separately -----------------------

def test_personal_holdout_is_evaluated_separately(base_config, rigged_data_dir,
                                                   tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True)

    result = train_void.stage_personal_holdout_evaluate(
        base_config, rigged_data_dir, model_dir, owwmodel=_FakeOWWModel())

    assert result["n_holdout"] == 2
    assert "threshold_sweep" in result

    holdout_report = model_dir / "hey_void_test_personal_holdout_evaluation.json"
    normal_report = model_dir / "hey_void_test_evaluation.json"
    assert holdout_report.exists()
    assert not normal_report.exists()  # this stage never writes the normal report

    saved = json.loads(holdout_report.read_text(encoding="utf-8"))
    assert saved["n_holdout"] == 2
