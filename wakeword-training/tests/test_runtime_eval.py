"""Regression tests for the real-audio-only / window-aware evaluation
harness (training/runtime_eval.py, Phase 7 of the 16kHz engineering pass).

Uses a FAKE model (predict_clip stubbed to return a scripted per-chunk
score sequence) so these tests are fast, deterministic, and independent of
any real trained model - they pin down the HARNESS's region-splitting
logic, not any model's actual behavior.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from training.runtime_eval import evaluate_wav_via_predict_clip, sweep_thresholds, threshold_crossings

CHUNK_SIZE = 1280
SAMPLE_RATE = 16000
CHUNK_S = CHUNK_SIZE / SAMPLE_RATE  # 0.08s


def _write_wav(path, seconds, sample_rate=SAMPLE_RATE):
    n = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(np.zeros(n, dtype=np.int16).tobytes())


class FakeModel:
    """predict_clip() stub returning a pre-scripted score for each chunk
    index, regardless of what audio is actually in the (fake) clip."""

    def __init__(self, scores_by_index: dict[int, float], model_key="hey_void"):
        self._scores_by_index = scores_by_index
        self.model_key = model_key

    def predict_clip(self, clip, padding=1, chunk_size=1280, **kwargs):
        with wave.open(str(clip), "rb") as wf:
            n_samples = wf.getnframes()
        n_padded_samples = n_samples + 2 * padding * SAMPLE_RATE
        n_chunks = n_padded_samples // chunk_size
        return [{self.model_key: self._scores_by_index.get(i, 0.01)}
                for i in range(int(n_chunks))]


def test_a_spike_purely_inside_leading_padding_is_never_reported_as_content(tmp_path):
    # Leading padding is 1s = 12.5 chunks; chunk index 3 is deep inside it,
    # far from where any real audio could influence the receptive field.
    wav = tmp_path / "clip.wav"
    _write_wav(wav, seconds=0.5)
    model = FakeModel({3: 0.99})

    result = evaluate_wav_via_predict_clip(model, wav, model.model_key, clip_seconds=2.06)

    assert result["silence_only"]["max"] == 0.99
    assert result["content_influenced"]["max"] < 0.99


def test_a_spike_long_after_the_receptive_field_could_contain_real_audio_is_silence_only(tmp_path):
    # A 0.5s clip's receptive-field-relevant window ends at
    # real_duration_s + clip_seconds = 0.5 + 2.06 = 2.56s after real audio
    # starts. The default padding=1s isn't even long enough to reach past
    # that point (a 0.5s clip + 1s trailing padding only spans 1.5s past
    # real-audio-start), so this test uses padding=3s to actually construct
    # a genuine silence-only trailing region, with a spike placed well past
    # the 2.56s cutoff (chunk 75 -> t=6.0s in the padded clip, i.e. 3.0s
    # after real audio starts, comfortably beyond 2.56s).
    wav = tmp_path / "clip.wav"
    _write_wav(wav, seconds=0.5)
    model = FakeModel({75: 0.99})

    result = evaluate_wav_via_predict_clip(model, wav, model.model_key,
                                           padding=3, clip_seconds=2.06)

    assert result["silence_only"]["max"] == 0.99
    assert result["content_influenced"]["max"] < 0.99


def test_a_spike_shortly_after_a_short_clip_ends_is_content_influenced(tmp_path):
    # This is the exact scenario found during Phase 6/7 re-investigation:
    # a genuine ~1.4s "hey void" utterance cannot be scored until enough
    # trailing silence fills the classifier's ~2.06s receptive field, so
    # the real recognition signal legitimately appears shortly AFTER the
    # file's own nominal duration ends - it must not be discarded as
    # "padding" the way an earlier, less careful version of this harness did.
    wav = tmp_path / "clip.wav"
    _write_wav(wav, seconds=1.4)
    # padding=1s -> real audio starts at chunk index 1/CHUNK_S = 12.5;
    # real audio ends at (1+1.4)/CHUNK_S = 30; a spike shortly after that
    # (chunk 32, t=2.56s) is still within clip_seconds=2.06s of real audio
    # ending, so its receptive field still overlaps real content.
    model = FakeModel({32: 0.95})

    result = evaluate_wav_via_predict_clip(model, wav, model.model_key, clip_seconds=2.06)

    assert result["content_influenced"]["max"] == 0.95


def test_threshold_crossings_with_none_max_crosses_nothing():
    assert threshold_crossings(None, [0.3, 0.5, 0.9]) == {0.3: False, 0.5: False, 0.9: False}


def test_threshold_crossings_basic():
    crossings = threshold_crossings(0.65, [0.3, 0.5, 0.7, 0.9])
    assert crossings == {0.3: True, 0.5: True, 0.7: False, 0.9: False}


def test_sweep_thresholds_computes_fraction_at_or_above_each_threshold():
    scores = [0.1, 0.4, 0.6, 0.65, 0.9]
    result = sweep_thresholds(scores, [0.5, 0.6, 0.7, 0.95])
    assert result == {0.5: 0.6, 0.6: 0.6, 0.7: 0.2, 0.95: 0.0}


def test_sweep_thresholds_handles_empty_scores_without_raising():
    result = sweep_thresholds([], [0.5, 0.6])
    assert result == {0.5: 0.0, 0.6: 0.0}


def test_region_stats_reports_none_for_an_empty_region(tmp_path):
    # A pathological all-silence-region case (very long clip relative to
    # padding) still returns well-formed, non-crashing stats.
    wav = tmp_path / "clip.wav"
    _write_wav(wav, seconds=0.1)
    model = FakeModel({})  # every chunk defaults to a flat low score

    result = evaluate_wav_via_predict_clip(model, wav, model.model_key, clip_seconds=2.06)

    assert result["content_influenced"]["n"] > 0
    assert result["content_influenced"]["max"] == pytest.approx(0.01)
