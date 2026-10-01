"""Shared fixtures for the wake-word training pipeline's test suite.

Unit tests here use tiny synthetic fixtures (a handful of short WAV clips,
random feature arrays) - never a real dataset, real TTS engine, or a real
multi-minute training run. Tests that genuinely need the real openwakeword
feature-extraction backbone (a one-time download) or V.O.I.D's own venv are
marked `@pytest.mark.integration` and are skipped by default; run them
explicitly with `pytest -m integration`.
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Environment-specific workaround (see training/_sklearn_import_shim.py):
# this machine's Windows Application Control policy blocks a scikit-learn
# native DLL, and openwakeword/__init__.py unconditionally imports a
# sklearn-dependent module this project never uses. Must run before any
# `import openwakeword` anywhere in the test suite.
from training._sklearn_import_shim import install_sklearn_import_shim  # noqa: E402
install_sklearn_import_shim()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs real openwakeword feature models "
        "and/or V.O.I.D's own venv (network/download on first use); "
        "excluded by default, run with `pytest -m integration`")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(
        reason="integration test - run explicitly with `pytest -m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def write_silence_wav(path: str | Path, seconds: float = 1.0, sr: int = 16000) -> None:
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(np.zeros(n, dtype=np.int16).tobytes())


@pytest.fixture
def tiny_wav_dir(tmp_path):
    """A directory with a handful of tiny (silent) WAV clips."""
    d = tmp_path / "wavs"
    d.mkdir()
    for i in range(3):
        write_silence_wav(d / f"clip_{i}.wav", seconds=0.5)
    return d


@pytest.fixture
def minimal_config(tmp_path):
    """A minimal, valid in-memory config dict matching training/config.py's
    required-field schema, rooted at a temp directory."""
    return {
        "model_name": "hey_void_test",
        "target_phrase": "hey void",
        "output_dir": str(tmp_path / "models"),
        "data_dir": str(tmp_path / "data"),
        "seed": 42,
        "positive": {"tts_backend": "sapi", "n_samples": 4, "n_samples_val": 2,
                    "voices": [], "piper_sample_generator_path": "", "piper_voices": []},
        "negative": {"tts_backend": "sapi", "n_adversarial_phrases": 3,
                    "n_adversarial_samples": 4, "n_noise_samples": 4,
                    "n_neutral_speech_samples": 2, "val_fraction": 0.5,
                    "custom_negative_phrases": ["hey voice"],
                    "neutral_sentences": ["what time is it"]},
        "augmentation": {"enabled": True, "variants_per_clip": 1,
                        "gain_db_range": [-3, 3], "pitch_semitone_range": [-1, 1],
                        "add_noise_probability": 0.5, "noise_snr_db_range": [5, 15],
                        "bandstop_probability": 0.2, "distortion_probability": 0.2,
                        "reverb_probability": 0.2},
        "features": {"sample_rate": 16000, "clip_seconds": 2.1},
        "model": {"model_type": "dnn", "layer_dim": 8, "n_blocks": 1,
                 "batch_size": 4, "training_steps": 5, "learning_rate": 0.01,
                 "val_every_n_steps": 2},
        "evaluation": {"threshold": 0.5, "target_fp_per_hour": 0.5},
    }
