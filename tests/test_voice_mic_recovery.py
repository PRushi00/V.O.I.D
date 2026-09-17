"""Regression tests for the mic-health supervisor in VoiceController.

Root cause under test: the physical microphone stream can go silent (a
Windows microphone-privacy toggle revoking then restoring access while a
stream is open, a device that disappears and reappears, a transient
driver/native fault) while the tray/Qt event loop and the session state
machine - sitting quietly in IDLE waiting for a wake word - keep running and
look completely normal. Nothing watched for "frames stopped arriving"
before this; the wake detector and PTT capture only ever react to frames
that DO arrive.

Deterministic and hardware-free: a FakeBackend drives a REAL
AudioCaptureBroker, and an injectable clock (the controller's ``now``
parameter) drives the health-check's timing without any real sleeping.
"""
import time

from void.core.kill_switch import KillSwitch
from void.core.task import Status
from void.voice.adapters import STT, TTS, BrokerCapture
from void.voice.capture_broker import AudioCaptureBroker, CaptureBackend
from void.voice.runtime import (
    _MIC_RECOVERY_CONFIRM_S, _MIC_RECOVERY_GRACE_S,
    _MIC_RECOVERY_INITIAL_BACKOFF_S, _MIC_RECOVERY_MAX_BACKOFF_S,
    _MIC_SILENCE_TIMEOUT_S, VoiceController,
)
from void.voice.session import VoiceSession
from void.voice.state import VoiceState


class FakeBackend(CaptureBackend):
    """Deterministic capture source: emits frames on demand, counts
    start/stop/close so a test can prove recovery reuses THIS SAME backend
    rather than ever constructing a second one."""

    def __init__(self):
        self.starts = self.stops = self.closes = 0
        self.running = False
        self._on_frame = None

    def start(self, on_frame):
        self.starts += 1
        self._on_frame = on_frame
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def close(self):
        self.closes += 1
        self.running = False

    def emit(self, frame=b"\x00\x00"):
        if self.running and self._on_frame is not None:
            self._on_frame(frame)


class FailingStartBackend(FakeBackend):
    """A backend whose start() raises the Nth time it is called (1-indexed) -
    simulates a device still unavailable when recovery tries to reopen it."""

    def __init__(self, fail_on_call: int):
        super().__init__()
        self._fail_on_call = fail_on_call
        self._call = 0

    def start(self, on_frame):
        self._call += 1
        if self._call == self._fail_on_call:
            raise RuntimeError("device still unavailable")
        super().start(on_frame)


class NullSTT(STT):
    def transcribe(self, audio) -> str:
        return ""


class NullTTS(TTS):
    @property
    def is_speaking(self) -> bool:
        return False

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass


class FakeResult:
    def __init__(self):
        self.status = Status.COMPLETED
        self.result = ""


class FakeAssistant:
    def run(self, transcript):
        return FakeResult()


class Clock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _rig(*, backend=None):
    backend = backend or FakeBackend()
    clock = Clock()
    # The broker and the controller MUST share one clock: the controller
    # judges silence duration against the broker's own frame timestamps, so
    # two independently-advancing clocks would never agree on "how long".
    broker = AudioCaptureBroker(backend=backend, now=clock)
    session = VoiceSession(FakeAssistant(), KillSwitch(),
                           capture=BrokerCapture(broker), stt=NullSTT(),
                           tts=NullTTS())
    states = []
    ctrl = VoiceController(session, None, broker=broker,
                          on_state=states.append, now=clock)
    return ctrl, broker, backend, session, states, clock


# --- quiescent / no-op cases -------------------------------------------

def test_noop_when_no_broker_configured():
    session = VoiceSession(FakeAssistant(), KillSwitch(), capture=None,
                           stt=NullSTT(), tts=NullTTS())
    ctrl = VoiceController(session, None)   # broker=None
    ctrl.poll_once()                        # must not raise


def test_noop_before_broker_is_started():
    ctrl, broker, backend, *_ = _rig()
    ctrl.poll_once()                        # broker never start()ed
    assert backend.starts == 0


def test_noop_before_any_frame_has_ever_arrived():
    ctrl, broker, backend, _session, states, clock = _rig()
    broker.start()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 100)
    ctrl.poll_once()
    # No frame ever arrived, so there is nothing to judge yet (this is
    # "just started", not "went silent") - never mistaken for a failure.
    assert backend.starts == 1
    assert "mic_unavailable" not in states
    broker.close()


# --- healthy operation ---------------------------------------------------

def test_stays_healthy_while_frames_keep_arriving():
    ctrl, broker, backend, _session, states, clock = _rig()
    broker.start()
    for _ in range(5):
        backend.emit()
        broker.drain()
        clock.advance(1.0)
        ctrl.poll_once()
    assert backend.starts == 1   # never restarted
    assert "mic_unavailable" not in states
    broker.close()


# --- detection + recovery --------------------------------------------

def test_detects_silence_and_flags_unavailable():
    ctrl, broker, backend, _session, states, clock = _rig()
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()
    assert "mic_unavailable" in states


def test_recovery_restarts_the_same_backend_not_a_second_one():
    ctrl, broker, backend, _session, states, clock = _rig()
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # detects + immediately retries once
    assert backend.stops == 1
    assert backend.starts == 2             # the SAME backend object, reopened
    assert "mic_recovering" in states


def test_recovery_succeeds_once_a_fresh_frame_arrives():
    ctrl, broker, backend, session, states, clock = _rig()
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # unhealthy -> restart attempted
    assert backend.starts == 2

    clock.advance(0.5)
    backend.emit()                         # the stream is actually working again
    broker.drain()
    ctrl.poll_once()                       # should recognize recovery
    assert states[-1] == VoiceState.IDLE   # tray snapped back to the real state
    assert backend.starts == 2             # no further restart needed
    broker.close()


def test_recovery_failure_backs_off_before_retrying_again():
    backend = FakeBackend()
    ctrl, broker, backend, _session, states, clock = _rig(backend=backend)
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # restart attempt #1
    assert backend.starts == 2

    # Grace period elapses with no fresh frame -> attempt judged failed.
    # Backoff DOUBLES on this first failure (initial 2.0s -> 4.0s).
    clock.advance(_MIC_RECOVERY_GRACE_S + 0.1)
    ctrl.poll_once()
    assert backend.starts == 2             # no immediate retry - backoff applies
    doubled_backoff = _MIC_RECOVERY_INITIAL_BACKOFF_S * 2
    assert ctrl._mic_backoff == doubled_backoff

    # Ticking through less than the (doubled) backoff window: still no retry.
    clock.advance(doubled_backoff - 0.5)
    ctrl.poll_once()
    assert backend.starts == 2

    # Once the backoff elapses, a second restart attempt happens.
    clock.advance(1.0)
    ctrl.poll_once()
    assert backend.starts == 3


def test_backoff_never_exceeds_the_cap_no_busy_loop():
    ctrl, broker, backend, _session, states, clock = _rig(
        backend=FakeBackend())
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # attempt #1

    for _ in range(8):                     # repeatedly fail recovery
        clock.advance(_MIC_RECOVERY_GRACE_S + 0.1)
        ctrl.poll_once()                   # judged failed -> backoff scheduled
        assert ctrl._mic_backoff <= _MIC_RECOVERY_MAX_BACKOFF_S
        clock.advance(ctrl._mic_backoff + 0.1)
        ctrl.poll_once()                   # next attempt fires
    assert ctrl._mic_backoff == _MIC_RECOVERY_MAX_BACKOFF_S


def test_recovery_never_interrupts_an_active_capture():
    ctrl, broker, backend, session, states, clock = _rig()
    broker.start()
    backend.emit()
    broker.drain()
    session.on_ptt_press()                 # IDLE -> LISTENING
    assert session.state == VoiceState.LISTENING

    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()
    # Flagged unavailable, but must NOT restart the stream mid-capture.
    assert "mic_unavailable" in states
    assert backend.starts == 1

    session.on_ptt_release()               # back to IDLE (empty transcript)
    assert session.state == VoiceState.IDLE
    ctrl.poll_once()                        # now recovery may proceed
    assert backend.starts == 2
    broker.close()


def test_recovery_attempt_that_raises_is_treated_as_a_failure():
    backend = FailingStartBackend(fail_on_call=2)   # 2nd start() = the recovery attempt
    ctrl, broker, backend, _session, states, clock = _rig(backend=backend)
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # restart attempt raises internally
    assert "mic_recovering" in states
    assert ctrl._mic_healthy is False
    assert ctrl._mic_backoff > _MIC_RECOVERY_INITIAL_BACKOFF_S   # already backed off
