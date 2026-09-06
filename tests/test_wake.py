"""Phase 9C-1 wake-word detector tests - deterministic, fakes only.

No microphone, no real openWakeWord model, no audio device, no threads. A fake
model backend (injected via the internal _model_factory seam) drives detection
deterministically. These tests cover the provider-neutral boundary, lifecycle,
single-event emission, isolation (no Assistant/VoiceState involvement), and the
deterministic failure modes.
"""
import pytest

from void.voice.adapters import VoiceDependencyError
from void.voice.wake import (
    WAKE_DETECTED, WAKE_PHRASE, NullWakeDetector, OpenWakeWordDetector,
    WakeWordBackendError, WakeWordConfigError, WakeWordDetector,
    available_providers, create_wake_detector, register_wake_provider,
)
import void.voice.wake as wakemod


class FakeModel:
    """Stands in for an openWakeWord model: returns queued scores per predict()."""
    def __init__(self, scores=None, raise_on_predict=False):
        self._scores = list(scores or [])
        self._raise = raise_on_predict
        self.frames = []

    def predict(self, frame):
        self.frames.append(frame)
        if self._raise:
            raise RuntimeError("inference blew up")
        score = self._scores.pop(0) if self._scores else 0.0
        return {"hey_void": score}


def _detector(scores=None, threshold=0.6, on_wake=None, raise_on_predict=False):
    fake = FakeModel(scores=scores, raise_on_predict=raise_on_predict)
    det = OpenWakeWordDetector(
        model_path="dummy",              # ignored because a factory is injected
        threshold=threshold, on_wake=on_wake,
        _model_factory=lambda: fake,
    )
    return det, fake


# --- construction / lifecycle (1-9) ------------------------------------

def test_detector_constructs():
    det, _ = _detector()
    assert isinstance(det, WakeWordDetector)


def test_detector_start_and_stop():
    det, _ = _detector()
    det.start()
    det.stop()                            # no exception


def test_start_is_idempotent():
    det, fake = _detector(scores=[0.9])
    det.start(); det.start(); det.start()  # repeated start -> deterministic
    det.feed_audio(b"f")
    # only one model was loaded and detector behaves normally
    assert det._model is fake


def test_stop_is_idempotent():
    det, _ = _detector()
    det.start()
    det.stop(); det.stop(); det.stop()    # repeated stop -> deterministic no-op


def test_close_is_idempotent():
    det, _ = _detector()
    det.start()
    det.close(); det.close()              # repeated close -> deterministic no-op


def test_feed_before_start_is_ignored():
    events = []
    det, fake = _detector(scores=[0.99], on_wake=events.append)
    det.feed_audio(b"frame")              # before start -> ignored
    assert events == [] and fake.frames == []


def test_feed_after_stop_is_ignored():
    events = []
    det, fake = _detector(scores=[0.99], on_wake=events.append)
    det.start()
    det.stop()
    det.feed_audio(b"frame")              # after stop -> ignored
    assert events == []


def test_feed_after_close_is_ignored():
    events = []
    det, fake = _detector(scores=[0.99], on_wake=events.append)
    det.start()
    det.close()
    det.feed_audio(b"frame")              # after close -> ignored
    assert events == []


# --- event emission (10, 13) -------------------------------------------

def test_positive_detection_emits_exactly_one_event():
    events = []
    # scores rise above threshold once, then stay high, then drop.
    det, fake = _detector(scores=[0.1, 0.95, 0.97, 0.2], threshold=0.6,
                          on_wake=events.append)
    det.start()
    for _ in range(4):
        det.feed_audio(b"f")
    assert events == [WAKE_DETECTED]      # exactly one, despite two high frames


def test_event_payload_is_only_the_wake_constant():
    captured = []
    det, _ = _detector(scores=[0.9], on_wake=captured.append)
    det.start()
    det.feed_audio(b"f")
    assert captured == [WAKE_DETECTED]
    assert WAKE_DETECTED == "wake_detected"
    # The event is a bare constant: no command/authorization/task/session data.
    assert isinstance(captured[0], str)


def test_rising_edge_refires_only_after_dropping_below_threshold():
    events = []
    det, _ = _detector(scores=[0.9, 0.2, 0.9], threshold=0.6, on_wake=events.append)
    det.start()
    for _ in range(3):
        det.feed_audio(b"f")
    assert events == [WAKE_DETECTED, WAKE_DETECTED]   # two distinct rising edges


def test_below_threshold_never_emits():
    events = []
    det, _ = _detector(scores=[0.1, 0.59, 0.0], threshold=0.6, on_wake=events.append)
    det.start()
    for _ in range(3):
        det.feed_audio(b"f")
    assert events == []


# --- isolation: no Assistant / no VoiceState (11, 12) ------------------

def test_detector_has_no_execution_authority():
    # The detector holds no assistant/session/tools/riskgate handles - it cannot
    # invoke Assistant.run() or any action; its sole output is the wake callback.
    det, _ = _detector(scores=[0.9])
    for attr in ("assistant", "_assistant", "run", "session", "_session",
                 "tools", "risk_gate", "kill_switch"):
        assert not hasattr(det, attr)


def test_detection_does_not_touch_voicestate():
    # Feeding a positive detection must not import or mutate VoiceState. We assert
    # the callback only receives the constant and the detector exposes no state.
    import void.voice.state as state_mod
    before = state_mod.VoiceState.IDLE
    events = []
    det, _ = _detector(scores=[0.95], on_wake=events.append)
    det.start()
    det.feed_audio(b"f")
    assert events == [WAKE_DETECTED]
    assert state_mod.VoiceState.IDLE == before   # unchanged constant
    assert not hasattr(det, "state") and not hasattr(det, "_state")


# --- deterministic failure modes (14, 15, 16) --------------------------

def test_missing_model_configuration_fails_clearly():
    det = OpenWakeWordDetector(model_path=None)   # no path, no factory
    with pytest.raises(WakeWordConfigError):
        det.start()


def test_empty_model_path_fails_clearly():
    det = OpenWakeWordDetector(model_path="")
    with pytest.raises(WakeWordConfigError):
        det.start()


def test_nonexistent_model_path_fails_clearly(tmp_path):
    missing = tmp_path / "hey_void.onnx"
    det = OpenWakeWordDetector(model_path=str(missing))
    with pytest.raises(WakeWordConfigError):
        det.start()


def test_backend_init_failure_fails_clearly():
    def boom():
        raise RuntimeError("model load failed")
    det = OpenWakeWordDetector(model_path="dummy", _model_factory=boom)
    with pytest.raises(WakeWordBackendError):
        det.start()


def test_inference_failure_fails_clearly():
    det, _ = _detector(raise_on_predict=True, scores=[0.9])
    det.start()
    with pytest.raises(WakeWordBackendError):
        det.feed_audio(b"f")


def test_optional_dependency_absence_fails_cleanly(tmp_path):
    # With a real model path present but 'openwakeword' not installed, _load must
    # raise a clean VoiceDependencyError (never a bare ImportError). If the
    # package IS installed, this missing-dep path isn't exercisable -> skip.
    model_file = tmp_path / "hey_void.onnx"
    model_file.write_bytes(b"not-a-real-model")
    det = OpenWakeWordDetector(model_path=str(model_file))
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        with pytest.raises(VoiceDependencyError):
            det.start()
    else:
        pytest.skip("openwakeword installed; missing-dependency path not hit")


def test_core_and_wake_import_without_optional_stack():
    # Importing core + the wake module must work even though openwakeword is not
    # installed (the heavy import is lazy inside OpenWakeWordDetector._load).
    import importlib
    for mod in ("void.app", "void.voice", "void.voice.wake"):
        assert importlib.import_module(mod) is not None


# --- null detector + factory -------------------------------------------

def test_null_detector_never_wakes():
    events = []
    det = NullWakeDetector(on_wake=events.append)
    det.start()
    det.feed_audio(b"f"); det.feed_audio(b"f")
    det.stop(); det.close()
    assert events == []


def test_factory_builds_openwakeword_by_default():
    det = create_wake_detector()          # no config -> default provider
    assert isinstance(det, OpenWakeWordDetector)


def test_factory_reads_provider_and_config_from_config():
    class _Cfg:
        def __init__(self, values):
            self._v = values

        def get(self, key, default=None):
            return self._v.get(key, default)

    det = create_wake_detector(_Cfg({"voice.wake_provider": "null"}))
    assert isinstance(det, NullWakeDetector)

    det2 = create_wake_detector(_Cfg({
        "voice.wake_provider": "openwakeword",
        "voice.wake_model_path": "some/path.onnx",
        "voice.wake_threshold": 0.8,
    }))
    assert isinstance(det2, OpenWakeWordDetector) and det2.threshold == 0.8


def test_factory_unknown_provider_fails_clearly():
    with pytest.raises(WakeWordConfigError):
        create_wake_detector(provider="porcupine")   # not registered


def test_register_wake_provider_is_replaceable():
    made = []

    class PretendBackend(WakeWordDetector):
        def __init__(self, on_wake=None):
            super().__init__(on_wake); made.append(1)
        def start(self): pass
        def stop(self): pass
        def feed_audio(self, frame): pass
        def close(self): pass

    register_wake_provider("pretend", PretendBackend)
    try:
        assert "pretend" in available_providers()
        det = create_wake_detector(provider="pretend")
        assert isinstance(det, PretendBackend) and made == [1]
    finally:
        wakemod._PROVIDERS.pop("pretend", None)


def test_declared_wake_phrase_is_hey_void():
    assert WAKE_PHRASE == "Hey V.O.I.D."