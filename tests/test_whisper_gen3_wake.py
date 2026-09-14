"""Deterministic tests for the Gen3 (Whisper-encoder) wake detector's
runtime logic: non-blocking buffering, background-thread scoring, and
edge-triggered single-wake emission. Uses injected fakes for the encoder
and ONNX session, so no faster-whisper / real model is needed and the
score is fully controllable. The Whisper encode and ONNX classifier
themselves are validated separately against real audio (see
wakeword-training/docs/gen3.md)."""
from __future__ import annotations

import time

import numpy as np
import pytest

from void.voice.whisper_gen3_wake import WhisperGen3WakeDetector
from void.voice.wake import WAKE_DETECTED


class _FakeModel:
    def encode(self, feats, to_cpu=True):
        # shape (1, T, 768) — content is irrelevant; the fake session decides.
        return np.zeros((1, 101, 768), dtype=np.float32)


class _FakeEncoder:
    def __init__(self):
        self.model = _FakeModel()

    def feature_extractor(self, waveform):
        return np.zeros((80, 201), dtype=np.float32)


class _FakeSession:
    """Returns a fixed logit so score = sigmoid(logit) is deterministic."""
    def __init__(self, logit: float):
        self._logit = logit

    def get_inputs(self):
        class _I:
            name = "encoder_features"
        return [_I()]

    def run(self, _outs, _feed):
        return [np.array([[self._logit]], dtype=np.float32)]


def _make(logit, on_wake=None, hop=0.02, **kw):
    return WhisperGen3WakeDetector(
        classifier_path="unused",
        threshold=0.34,
        hop_seconds=hop,
        on_wake=on_wake,
        _encoder_factory=lambda: _FakeEncoder(),
        _session_factory=lambda: _FakeSession(logit),
        **kw,
    )


def _frames(n_seconds=1.5, frame=480):
    total = int(n_seconds * 16000)
    data = (np.random.RandomState(0).randint(-1000, 1000, total)).astype("<i2")
    return [data[i:i + frame].tobytes() for i in range(0, total, frame)]


def _wait(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_high_score_emits_exactly_one_wake_per_rising_edge():
    events = []
    det = _make(logit=5.0, on_wake=events.append)   # sigmoid(5)=0.993 >= 0.34
    det.start()
    try:
        for f in _frames():
            det.feed_audio(f)
        assert _wait(lambda: len(events) >= 1)
        # stays latched: no repeated events while the score remains high
        time.sleep(0.15)
        assert events == [WAKE_DETECTED]
    finally:
        det.close()


def test_low_score_never_wakes():
    events = []
    det = _make(logit=-5.0, on_wake=events.append)  # sigmoid(-5)=0.0067 < 0.34
    det.start()
    try:
        for f in _frames():
            det.feed_audio(f)
        time.sleep(0.3)
        assert events == []
    finally:
        det.close()


def test_feed_audio_before_start_and_after_stop_is_ignored():
    events = []
    det = _make(logit=5.0, on_wake=events.append)
    det.feed_audio(b"\x00\x00" * 480)      # before start -> ignored
    det.start()
    det.stop()                              # background thread halted
    for f in _frames(n_seconds=0.5):
        det.feed_audio(f)                   # after stop -> ignored
    time.sleep(0.2)
    assert events == []
    det.close()


def test_feed_audio_is_nonblocking_fast():
    # feed_audio must not run inference on the caller (broker pump) thread:
    # even with a deliberately slow encoder, buffering a frame stays fast.
    class _SlowEncoder(_FakeEncoder):
        def feature_extractor(self, waveform):
            time.sleep(0.5)
            return super().feature_extractor(waveform)

    det = WhisperGen3WakeDetector(
        classifier_path="unused", threshold=0.34, hop_seconds=0.01,
        _encoder_factory=lambda: _SlowEncoder(), _session_factory=lambda: _FakeSession(5.0))
    det.start()
    try:
        t0 = time.perf_counter()
        for _ in range(20):
            det.feed_audio(b"\x00\x00" * 480)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.1, f"feed_audio blocked ({elapsed:.3f}s) - inference leaked onto caller thread"
    finally:
        det.close()


def test_stop_then_start_rearms():
    events = []
    det = _make(logit=5.0, on_wake=events.append)
    det.start(); det.stop()
    events.clear()
    det.start()
    try:
        for f in _frames():
            det.feed_audio(f)
        assert _wait(lambda: len(events) >= 1)
    finally:
        det.close()


def test_missing_classifier_path_raises_config_error():
    from void.voice.wake import WakeWordConfigError
    det = WhisperGen3WakeDetector(classifier_path=None)
    with pytest.raises(WakeWordConfigError):
        det.start()


def test_inference_error_is_logged_once_not_silent(caplog):
    # Observability: a persistent inference failure must NOT leave V.O.I.D
    # silently dead - it is logged exactly once (not spammed), wake goes
    # inactive, and no false wake is emitted.
    import logging

    class _BadEncoder(_FakeEncoder):
        def feature_extractor(self, waveform):
            raise RuntimeError("boom")

    events = []
    det = WhisperGen3WakeDetector(
        classifier_path="unused", threshold=0.34, hop_seconds=0.01,
        on_wake=events.append,
        _encoder_factory=lambda: _BadEncoder(),
        _session_factory=lambda: _FakeSession(5.0))
    with caplog.at_level(logging.WARNING, logger="void.voice.whisper_gen3_wake"):
        det.start()
        try:
            for f in _frames():
                det.feed_audio(f)
            assert _wait(lambda: any("wake inference failed" in r.message
                                     for r in caplog.records))
            time.sleep(0.1)
        finally:
            det.close()
    assert events == []                      # never a false wake on failure
    n = sum("wake inference failed" in r.message for r in caplog.records)
    assert n == 1                            # logged once, not on every hop
