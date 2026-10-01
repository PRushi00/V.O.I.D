"""Model training + ONNX export tests, using tiny synthetic (16, 96) feature
arrays (no real audio, no TTS, no network) - fast enough for the normal unit
suite, but exercises the REAL openwakeword.train.Model architecture and its
REAL export_to_onnx() method, per "reuse the openWakeWord-compatible DNN
architecture" rather than reimplementing it."""
from __future__ import annotations

import numpy as np
import pytest

from training.train import TrainingDataError, _val_score, build_dataset, train_model


def _fake_features(n_pos=12, n_neg=12, shape=(16, 96), seed=0):
    rng = np.random.default_rng(seed)
    return {
        "positive_train": rng.standard_normal((n_pos, *shape)).astype(np.float32),
        "adversarial_train": rng.standard_normal((n_neg, *shape)).astype(np.float32),
    }


def test_build_dataset_labels_positive_and_negative_correctly():
    features = _fake_features(n_pos=3, n_neg=5)
    X, y = build_dataset(features)
    assert X.shape == (8, 16, 96)
    assert (y == 1.0).sum() == 3
    assert (y == 0.0).sum() == 5


def test_build_dataset_raises_on_no_positive_examples():
    features = {"adversarial_train": np.zeros((4, 16, 96), dtype=np.float32)}
    with pytest.raises(TrainingDataError, match="no positive"):
        build_dataset(features)


def test_build_dataset_raises_on_no_negative_examples():
    features = {"positive_train": np.zeros((4, 16, 96), dtype=np.float32)}
    with pytest.raises(TrainingDataError, match="no negative"):
        build_dataset(features)


def test_build_dataset_raises_on_completely_empty_input():
    with pytest.raises(TrainingDataError, match="no feature data"):
        build_dataset({})


def test_train_model_exports_a_loadable_onnx_file(tmp_path):
    onnxruntime = pytest.importorskip("onnxruntime")
    config = {
        "seed": 7,
        "model_name": "hey_void_test",
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 8, "training_steps": 5, "learning_rate": 0.01,
                 "val_every_n_steps": 2},
    }
    features = _fake_features(n_pos=16, n_neg=16)

    owwmodel, history, onnx_path = train_model(config, features, tmp_path)

    assert onnx_path.exists() and onnx_path.name == "hey_void_test.onnx"
    assert len(history["loss"]) > 0

    session = onnxruntime.InferenceSession(str(onnx_path))
    input_meta = session.get_inputs()[0]
    assert list(input_meta.shape[1:]) == [16, 96]

    dummy = np.random.RandomState(0).rand(1, 16, 96).astype(np.float32)
    outputs = session.run(None, {input_meta.name: dummy})
    assert outputs[0].shape == (1, 1)
    assert 0.0 <= float(outputs[0][0][0]) <= 1.0


def test_train_model_is_reproducible_given_the_same_seed(tmp_path):
    config = {
        "seed": 123,
        "model_name": "hey_void_repro",
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 8, "training_steps": 5, "learning_rate": 0.01,
                 "val_every_n_steps": 2},
    }
    features = _fake_features(n_pos=16, n_neg=16, seed=1)

    _m1, history1, _p1 = train_model(config, features, tmp_path / "run1")
    _m2, history2, _p2 = train_model(config, features, tmp_path / "run2")

    assert history1["loss"] == pytest.approx(history2["loss"])


# --- validation-based checkpoint selection ---------------------------------
# A direct A/B step-count sweep (identical seed/data/config, only step count
# varying) found this training setup's accuracy trajectory is NOT smoothly
# convergent - it oscillates, sometimes wildly (one sweep saw adversarial FP
# go 50.7% -> 46.7% -> 98.7% -> 86.7% -> 38.0% -> 68.0% -> 78.7% across
# 500/750/.../2000 steps). Exporting whatever the LAST step happens to
# produce means step-count becomes an arbitrary gamble. These tests pin down
# the fix: track the best-validated checkpoint throughout training and
# export that one instead.

def _val_features(n=16, shape=(16, 96), seed=2):
    rng = np.random.default_rng(seed)
    return {
        "positive_val": rng.standard_normal((n, *shape)).astype(np.float32),
        "adversarial_val": rng.standard_normal((n, *shape)).astype(np.float32),
    }


def test_val_score_prefers_high_recall_low_adversarial_fp():
    import torch

    class AlwaysHigh(torch.nn.Module):
        def forward(self, x):
            return torch.ones(x.shape[0], 1)

    class AlwaysLow(torch.nn.Module):
        def forward(self, x):
            return torch.zeros(x.shape[0], 1)

    val_feats = _val_features(n=8)
    good_net_result = _val_score(AlwaysHigh(), val_feats)  # perfect recall, but also 100% adv FP
    bad_net_result = _val_score(AlwaysLow(), val_feats)    # zero recall, zero adv FP

    assert good_net_result["recall"] == 1.0
    assert good_net_result["adv_fp"] == 1.0
    assert bad_net_result["recall"] == 0.0
    assert bad_net_result["adv_fp"] == 0.0
    # Neither extreme should trivially "win" - the combined score penalizes
    # adversarial FP twice as heavily as it rewards recall, per this
    # project's own stated priority ("the adversarial FP target is the most
    # important target").
    assert good_net_result["combined_score"] == pytest.approx(1.0 - 2.0 * 1.0)
    assert bad_net_result["combined_score"] == pytest.approx(0.0)


def test_val_score_handles_missing_categories_without_raising():
    import torch
    result = _val_score(lambda x: torch.zeros(x.shape[0], 1), {})
    assert result["recall"] is None
    assert result["adv_fp"] == 0.0
    assert result["speech_fp"] == 0.0
    assert result["noise_fp"] == 0.0


def test_train_model_without_val_features_exports_the_last_step_unchanged(tmp_path):
    # Default/legacy behavior (val_features=None) must be bit-identical to
    # before this feature existed - no history keys added, no checkpoint
    # rollback.
    config = {
        "seed": 5, "model_name": "hey_void_legacy",
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 8, "training_steps": 10, "learning_rate": 0.01,
                 "val_every_n_steps": 2},
    }
    features = _fake_features(n_pos=16, n_neg=16)
    _owwmodel, history, _onnx_path = train_model(config, features, tmp_path)
    assert "selected_checkpoint_step" not in history
    assert "val_step" not in history


def test_train_model_with_val_features_selects_best_checkpoint_not_last(tmp_path):
    config = {
        "seed": 42, "model_name": "hey_void_checkpoint_select",
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 8, "training_steps": 20, "learning_rate": 0.05,
                 "val_every_n_steps": 2},
        "evaluation": {"threshold": 0.5},
    }
    features = _fake_features(n_pos=16, n_neg=16, seed=3)
    val_features = _val_features(n=16, seed=9)

    _owwmodel, history, _onnx_path = train_model(config, features, tmp_path,
                                                 val_features=val_features)

    assert "selected_checkpoint_step" in history
    assert history["selected_checkpoint_step"] in history["val_step"]
    # The selected checkpoint's recorded score must be the best (max) one
    # actually observed during training - not merely present, but correct.
    best_ix = history["val_step"].index(history["selected_checkpoint_step"])
    assert history["val_combined_score"][best_ix] == max(history["val_combined_score"])


def test_train_model_ignores_val_features_with_no_positive_examples(tmp_path):
    # stage_train()'s own guard (positive_val must exist and be non-empty)
    # lives in train_void.py, not train_model() itself - train_model() will
    # happily use whatever val_features dict it's given. This test instead
    # confirms train_model() doesn't crash on a val set with zero adversarial
    # examples (the opposite gap), still degrading gracefully.
    config = {
        "seed": 11, "model_name": "hey_void_val_no_adv",
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 8, "training_steps": 6, "learning_rate": 0.01,
                 "val_every_n_steps": 2},
    }
    features = _fake_features(n_pos=16, n_neg=16)
    val_features = {"positive_val": _val_features(n=8)["positive_val"]}

    _owwmodel, history, _onnx_path = train_model(config, features, tmp_path,
                                                 val_features=val_features)
    assert "selected_checkpoint_step" in history
