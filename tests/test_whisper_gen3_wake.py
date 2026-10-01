"""Deterministic tests for the Gen3 (Whisper-encoder) wake detector's
runtime logic: non-blocking buffering, background-thread scoring, and
edge-triggered single-wake emission. Uses injected fakes for the encoder
and ONNX session, so no faster-whisper / real model is needed and the
score is fully controllable. The Whisper encode and ONNX classifier
themselves are validated separately against real audio (see
wakeword-training/docs/gen3.md)."""
from __future__ import annotations

import sys
import time
import types
from unittest import mock

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


def test_feed_audio_emits_periodic_frame_received_health_marker(caplog, monkeypatch):
    # Post-reboot diagnosability: prove frames from the broker are actually
    # reaching this detector, without logging any audio content - just a
    # periodic count. Shrink the cadence so the test doesn't need 5 real
    # seconds of frames.
    import logging

    import void.voice.whisper_gen3_wake as mod
    monkeypatch.setattr(mod, "_HEALTH_LOG_PERIOD_S", 0.05)
    det = _make(logit=-5.0)   # score below threshold: isolates the frame marker
    det.start()
    with caplog.at_level(logging.INFO, logger="void.voice.whisper_gen3_wake"):
        try:
            for f in _frames(n_seconds=2.0):
                det.feed_audio(f)
                time.sleep(0.01)
        finally:
            det.close()
    markers = [r for r in caplog.records if "WAKEWORD_FRAMES_RECEIVED" in r.message]
    assert markers, "expected at least one WAKEWORD_FRAMES_RECEIVED health marker"
    for r in markers:
        assert "count=" in r.message and "over=" in r.message


def test_wake_trigger_logs_the_exact_triggering_score(caplog):
    # False-wake investigation: there was previously no way to see the EXACT
    # score that triggered a given wake - only a throttled periodic sample
    # that could be showing a stale/unrelated inference. This must fire
    # every time a wake is emitted (not throttled like the health markers),
    # since it shares the same rising-edge condition as the wake event itself.
    import logging

    det = _make(logit=5.0)   # sigmoid(5) = 0.993 >= 0.34
    with caplog.at_level(logging.INFO, logger="void.voice.whisper_gen3_wake"):
        det.start()
        try:
            for f in _frames():
                det.feed_audio(f)
            assert _wait(lambda: any(
                "WAKE_TRIGGER_SCORE" in r.message for r in caplog.records))
        finally:
            det.close()
    markers = [r for r in caplog.records if "WAKE_TRIGGER_SCORE" in r.message]
    assert len(markers) == 1                 # exactly one, matching the one wake
    assert "score=0.993" in markers[0].message
    assert "threshold=0.34" in markers[0].message


def test_infer_loop_emits_periodic_processing_active_health_marker(caplog, monkeypatch):
    # Proves the background encode+classify loop is actually running
    # end-to-end (not just armed), independent of whether wake ever fires.
    import logging

    import void.voice.whisper_gen3_wake as mod
    monkeypatch.setattr(mod, "_HEALTH_LOG_PERIOD_S", 0.05)
    det = _make(logit=-5.0, hop=0.01)
    with caplog.at_level(logging.INFO, logger="void.voice.whisper_gen3_wake"):
        det.start()
        try:
            for f in _frames(n_seconds=1.5):
                det.feed_audio(f)
            assert _wait(lambda: any(
                "WAKEWORD_PROCESSING_ACTIVE" in r.message for r in caplog.records))
        finally:
            det.close()
    markers = [r for r in caplog.records if "WAKEWORD_PROCESSING_ACTIVE" in r.message]
    assert markers
    for r in markers:
        assert "last_score=" in r.message and "threshold=" in r.message


# --- CPU budget of the always-on encoder ---------------------------------------------------------
#
# The detector encodes a 2 s window every 200 ms forever. CTranslate2's default is to use every core, which measured
# SLOWER and far more erratic than one thread (p50 168 ms / p95 403 ms vs 88 / 92 on 24 idle cores) while saturating
# the machine and starving the STT decode of the command that follows the wake. See docs/VOICE_LATENCY_2026-09-23.md.

def test_the_encoder_is_built_with_a_bounded_cpu_thread_count():
    import void.voice.whisper_gen3_wake as mod
    seen = {}

    class _FakeWhisperModel:
        def __init__(self, name, device=None, compute_type=None, cpu_threads=None):
            seen.update(name=name, device=device, compute_type=compute_type, cpu_threads=cpu_threads)

    sys_mod = types.ModuleType("faster_whisper")
    sys_mod.WhisperModel = _FakeWhisperModel
    with mock.patch.dict(sys.modules, {"faster_whisper": sys_mod}):
        det = mod.WhisperGen3WakeDetector(classifier_path="x.onnx", cpu_threads=2,
                                          _session_factory=lambda: _FakeSession(0.0))
        det._load()
    assert seen["cpu_threads"] == 2 and seen["device"] == "cpu"


def test_the_default_cpu_thread_count_is_one_and_bad_values_fall_back_safely():
    import void.voice.whisper_gen3_wake as mod
    assert mod._DEFAULT_CPU_THREADS == 1
    mk = lambda t: mod.WhisperGen3WakeDetector(classifier_path="x.onnx", cpu_threads=t)   # noqa: E731
    assert mk(None)._cpu_threads == 1 and mk("nonsense")._cpu_threads == 1
    assert mk(0)._cpu_threads == 1 and mk(-4)._cpu_threads == 1      # never 0/negative (= "all cores" in ct2)
    assert mk(3)._cpu_threads == 3
    assert mod.WhisperGen3WakeDetector(classifier_path="x.onnx")._cpu_threads == 1


def test_the_config_key_reaches_the_detector():
    from void.config import Config
    from void.voice.wake import create_wake_detector
    d = create_wake_detector(Config({"voice": {"wake_provider": "whisper_gen3",
                                               "wake_model_path": "x.onnx", "wake_cpu_threads": 4}}))
    assert d._cpu_threads == 4
    shipped = create_wake_detector(Config.load())      # the shipped default stays bounded
    assert shipped._cpu_threads == 1


def test_the_onnx_classifier_session_pool_is_bounded_too():
    """ORT sizes its intra-op pool to every core and spin-waits between runs: 20.6 cores of continuous idle load vs
    0.30 when bounded, at identical per-inference time (docs/VOICE_LATENCY_2026-09-23.md)."""
    import void.voice.whisper_gen3_wake as mod
    made = {}

    class _Opts:
        intra_op_num_threads = None
        inter_op_num_threads = None

    def _session(path, sess_options=None, providers=None):
        made.update(path=path, opts=sess_options, providers=providers)
        return _FakeSession(0.0)

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.SessionOptions = _Opts
    fake_ort.InferenceSession = _session
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = lambda *a, **k: object()
    with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort, "faster_whisper": fw}):
        det = mod.WhisperGen3WakeDetector(classifier_path=__file__, cpu_threads=2)
        det._load()
    assert made["opts"].intra_op_num_threads == 2 and made["opts"].inter_op_num_threads == 2
    assert made["providers"] == ["CPUExecutionProvider"]
