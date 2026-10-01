"""Augmentation tests: WAV round-trip correctness and disabled-augmentation
passthrough. The audiomentations-backed pipeline itself is exercised by the
(marked-integration) end-to-end run, since it needs the real audiomentations
package; these unit tests focus on the deterministic, dependency-light parts
of this module."""
from __future__ import annotations

import numpy as np

from training.augment import _read_wav, _write_wav, augment_directory


def test_wav_round_trip_preserves_signal_shape_and_rate(tmp_path):
    sr = 16000
    samples = (np.sin(np.linspace(0, 20, sr)) * 0.5).astype(np.float32)
    path = tmp_path / "x.wav"
    _write_wav(str(path), samples, sr)

    read_back, read_sr = _read_wav(str(path))

    assert read_sr == sr
    assert read_back.shape == samples.shape
    # int16 quantization introduces small error - not bit-exact, but close.
    assert np.max(np.abs(read_back - samples)) < 0.01


def test_disabled_augmentation_copies_files_unchanged(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sr = 16000
    samples = np.zeros(sr, dtype=np.float32)
    _write_wav(str(src_dir / "clip.wav"), samples, sr)

    out_dir = tmp_path / "out"
    written = augment_directory(src_dir, out_dir, {"enabled": False}, seed=1)

    assert len(written) == 1
    read_back, _ = _read_wav(str(written[0]))
    assert np.allclose(read_back, samples)


def test_augment_directory_with_no_clips_produces_no_output(tmp_path):
    src_dir = tmp_path / "empty"
    src_dir.mkdir()
    out_dir = tmp_path / "out"
    written = augment_directory(src_dir, out_dir, {"enabled": False}, seed=1)
    assert written == []
    assert out_dir.exists()
