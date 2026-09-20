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


# --- D-01 (T0.3): recovery must survive failed restarts -----------------------
#
# V1 stopped supervising after ONE failed restart: broker.start() raising left
# broker.running False, and the health check returned on `not running` forever.
# The earlier test above stops after the first failure and never asserts that a
# SECOND attempt happens - which is how this went unnoticed. These assert
# liveness: the Nth attempt happens, backoff is capped, and shutdown ends it.

class FlakyStartBackend(FakeBackend):
    """start() raises on every call number in ``fail_on`` (1-indexed); records the
    (injected-clock) time of every start attempt."""

    def __init__(self, fail_on):
        super().__init__()
        self._fail_on = set(fail_on)
        self._call = 0
        self.clock = None
        self.attempt_times = []

    def start(self, on_frame):
        self._call += 1
        if self.clock is not None:
            self.attempt_times.append(self.clock())
        if self._call in self._fail_on:
            raise RuntimeError("device still unavailable")
        super().start(on_frame)


def _silent_rig(fail_on):
    backend = FlakyStartBackend(fail_on)
    ctrl, broker, backend, session, states, clock = _rig(backend=backend)
    backend.clock = clock
    broker.start()                       # call #1 succeeds
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    return ctrl, broker, backend, session, clock


def _tick(ctrl, backend, clock, seconds, *, emit=True, stop_when_healthy=False):
    for _ in range(int(seconds)):
        clock.advance(1.0)
        ctrl.poll_once()
        if emit:
            backend.emit()
        if stop_when_healthy and ctrl._mic_healthy:
            return True
    return ctrl._mic_healthy


def test_first_failed_restart_is_observable_in_the_health_snapshot():
    ctrl, broker, backend, _s, clock = _silent_rig(fail_on={2})
    ctrl.poll_once()                                  # restart attempt #1 raises
    snap = ctrl.mic_health_snapshot()
    assert backend._call == 2
    assert snap["supervised"] is True
    assert snap["healthy"] is False
    assert snap["broker_running"] is False            # the D-01 state...
    assert snap["broker_closed"] is False             # ...which is NOT "closed"
    assert snap["recovery_attempts"] == 1


def test_second_and_nth_failures_keep_retrying_until_success():
    ctrl, broker, backend, _s, clock = _silent_rig(fail_on={2, 3, 4, 5, 6})   # 5 failures, then success
    ctrl.poll_once()
    assert _tick(ctrl, backend, clock, 900, stop_when_healthy=True) is True
    assert backend._call >= 7                         # attempts 2..6 failed, #7 succeeded
    snap = ctrl.mic_health_snapshot()
    assert snap["healthy"] is True and snap["broker_running"] is True
    assert snap["recovery_attempts"] == 0             # reset after success


def test_backoff_is_capped_and_attempts_never_stop():
    ctrl, broker, backend, _s, clock = _silent_rig(fail_on=set(range(2, 10_000)))
    ctrl.poll_once()
    _tick(ctrl, backend, clock, 3600, emit=False)     # one simulated hour, device never returns
    times = backend.attempt_times[1:]                 # drop the initial start
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 40, f"only {len(times)} attempts in an hour"
    assert max(gaps) <= _MIC_RECOVERY_MAX_BACKOFF_S + 2.5, f"backoff exceeded the cap: {max(gaps)}"
    assert gaps[0] < gaps[-1]                         # it did back off first
    assert ctrl.mic_health_snapshot()["healthy"] is False


def test_recovery_is_not_attempted_during_an_active_capture_but_resumes_after():
    ctrl, broker, backend, session, clock = _silent_rig(fail_on={2, 3})
    ctrl.poll_once()                                  # attempt #1 (call 2) fails
    session.on_ptt_press()                            # owner starts talking
    assert session.state == VoiceState.LISTENING
    _tick(ctrl, backend, clock, 300, emit=False)
    assert backend._call == 2, "the stream was restarted out from under an active capture"
    session.on_ptt_release()                          # capture ends -> IDLE
    assert session.state == VoiceState.IDLE
    assert _tick(ctrl, backend, clock, 300, stop_when_healthy=True) is True
    assert backend._call >= 4


def test_owner_shutdown_ends_recovery_for_good():
    ctrl, broker, backend, _s, clock = _silent_rig(fail_on=set(range(2, 10_000)))
    ctrl.poll_once()
    assert backend._call == 2
    ctrl.shutdown()
    assert broker.closed is True
    _tick(ctrl, backend, clock, 600, emit=False)
    assert backend._call == 2, "the supervisor restarted the mic after the owner shut it down"
