"""REPRODUCE (scratch only, not part of the repo): does the mic-health supervisor
keep retrying after ONE failed restart?  Uses the repo's own test doubles.

Hypothesis from code reading: AudioCaptureBroker.start() sets _running=False when
backend.start() raises, and VoiceController._check_mic_health() early-returns on
`not broker.running`, so recovery is abandoned forever after the first failed
restart.  Expected on V1 baseline: BOTH tests below FAIL.
"""
import sys
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")

from tests.test_voice_mic_recovery import (
    FailingStartBackend, _rig, _MIC_SILENCE_TIMEOUT_S, _MIC_RECOVERY_MAX_BACKOFF_S,
)


def _go_silent_then_fail_first_restart():
    # start #1 = normal start, start #2 = the recovery attempt (raises),
    # start #3+ = succeeds (the device came back).
    backend = FailingStartBackend(fail_on_call=2)
    ctrl, broker, backend, _session, states, clock = _rig(backend=backend)
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                       # recovery attempt #1 raises
    return ctrl, broker, backend, states, clock


def test_supervisor_keeps_retrying_after_a_failed_restart():
    ctrl, broker, backend, states, clock = _go_silent_then_fail_first_restart()
    assert backend._call == 2              # the failing restart happened
    # The device is now available again. Give the supervisor 10 minutes of
    # monitor ticks (100 ms cadence is irrelevant; the clock is injected).
    for _ in range(600):
        clock.advance(1.0)
        ctrl.poll_once()
    assert backend._call >= 3, (
        f"supervisor never retried: backend.start() called {backend._call} time(s); "
        f"broker.running={broker.running}")


def test_mic_eventually_recovers_when_device_returns():
    ctrl, broker, backend, states, clock = _go_silent_then_fail_first_restart()
    recovered = False
    for _ in range(600):
        clock.advance(1.0)
        ctrl.poll_once()
        backend.emit()                      # frames flow if the stream is open
        if ctrl._mic_healthy:
            recovered = True
            break
    assert recovered, ("mic never recovered although the device is available again; "
                       f"broker.running={broker.running} starts={backend._call}")
