"""Regression tests for the audio-contract validation gate (Phase 2/15 of
the 16kHz sample-rate engineering pass). These tests exist specifically to
prevent a silent regression back to the bug that was found and fixed this
pass: SAPI-generated WAVs written at 44.1kHz while every downstream stage
assumed 16kHz, silently truncating/distorting audio because sample counts
were reinterpreted at the wrong rate.
"""
from __future__ import annotations

import wave

import numpy as np
import pytest

from training.audio_validation import (
    AudioFormatError,
    read_wav_format,
    scan_directory_audio_format,
    validate_wav_format,
)


def _write_wav(path, *, sample_rate=16000, channels=1, sample_width=2, seconds=1.0):
    n = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(np.zeros(n * channels, dtype=np.int16).tobytes())


def test_validate_wav_format_accepts_genuine_16khz_mono_pcm16(tmp_path):
    path = tmp_path / "good.wav"
    _write_wav(path, sample_rate=16000, channels=1, sample_width=2)
    validate_wav_format(path)  # must not raise


def test_validate_wav_format_rejects_44100hz(tmp_path):
    # This is EXACTLY the bug that was found and fixed: SAPI's
    # SpeechAudioFormatType enum value 34 (SAFT44kHz16BitMono) was being
    # used where 18 (SAFT16kHz16BitMono) was required. A 44.1kHz WAV must
    # never be silently accepted as 16kHz audio.
    path = tmp_path / "wrong_rate.wav"
    _write_wav(path, sample_rate=44100, channels=1, sample_width=2)
    with pytest.raises(AudioFormatError, match="44100"):
        validate_wav_format(path)


def test_validate_wav_format_rejects_stereo(tmp_path):
    path = tmp_path / "stereo.wav"
    _write_wav(path, sample_rate=16000, channels=2, sample_width=2)
    with pytest.raises(AudioFormatError, match="channels"):
        validate_wav_format(path)


def test_validate_wav_format_rejects_wrong_sample_width(tmp_path):
    path = tmp_path / "8bit.wav"
    _write_wav(path, sample_rate=16000, channels=1, sample_width=1)
    with pytest.raises(AudioFormatError, match="sample_width"):
        validate_wav_format(path)


def test_read_wav_format_returns_ground_truth_header_values(tmp_path):
    path = tmp_path / "clip.wav"
    _write_wav(path, sample_rate=16000, seconds=0.5)
    sr, ch, sw, n = read_wav_format(path)
    assert (sr, ch, sw) == (16000, 1, 2)
    assert n == 8000


def test_scan_directory_audio_format_reports_all_valid(tmp_path):
    d = tmp_path / "wavs"
    d.mkdir()
    for i in range(3):
        _write_wav(d / f"clip_{i}.wav", sample_rate=16000, seconds=0.3 + i * 0.1)
    report = scan_directory_audio_format(d)
    assert report["all_valid"] is True
    assert report["invalid_files"] == []
    assert report["sample_rate_distribution"] == {16000: 3}
    assert report["channel_distribution"] == {1: 3}
    assert report["sample_width_distribution"] == {2: 3}


def test_scan_directory_audio_format_flags_a_mismatched_file_without_raising(tmp_path):
    d = tmp_path / "wavs"
    d.mkdir()
    _write_wav(d / "good.wav", sample_rate=16000)
    _write_wav(d / "bad.wav", sample_rate=44100)
    report = scan_directory_audio_format(d)  # must not raise
    assert report["all_valid"] is False
    assert len(report["invalid_files"]) == 1
    assert "bad.wav" in report["invalid_files"][0]["path"]
    assert report["invalid_files"][0]["sample_rate"] == 44100
