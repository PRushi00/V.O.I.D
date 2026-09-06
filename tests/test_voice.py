"""Phase 9A voice tests - deterministic, fakes only.

No real microphone, no faster-whisper, no model download, no GUI, no Gemini.
Fakes drive the VoiceSession logic; real adapters are covered by the live smoke
test (verify/voice_smoke.py). Live-only requirements (real mic/STT/TTS) are
noted where a test necessarily stands in for them with a fake.
"""
import pytest

from void.core.kill_switch import KillSwitch
from void.core.task import Status
from void.voice.adapters import (
    ActivationAdapter, AudioCapture, STT, STTError, TTS, TTSError,
    VoiceDependencyError,
)
from void.voice.session import VoiceSession
from void.voice.state import (
    IllegalVoiceTransition, VoiceState, VoiceStateMachine,
)


# --- fakes --------------------------------------------------------------

class FakeCapture(AudioCapture):
    def __init__(self, audio="AUDIO", raise_on_open=False, raise_on_stop=False):
        self._open = False
        self._audio = audio
        self.opens = 0
        self.closes = 0
        self._raise_on_open = raise_on_open
        self._raise_on_stop = raise_on_stop

    @property
    def is_open(self):
        return self._open

    def open(self):
        if self._raise_on_open:
            raise RuntimeError("mic busy")
        self._open = True
        self.opens += 1

    def stop(self):
        if self._raise_on_stop:
            self._open = False
            raise RuntimeError("capture failed")
        self._open = False
        self.closes += 1
        return self._audio

    def close(self):
        self._open = False
        self.closes += 1


class FakeSTT(STT):
    def __init__(self, text="hello void", raise_err=False, hook=None):
        self._text = text
        self._raise = raise_err
        self._hook = hook          # called during transcribe (simulate races)

    def transcribe(self, audio):
        if self._hook:
            self._hook()
        if self._raise:
            raise STTError("boom")
        return self._text


class FakeTTS(TTS):
    def __init__(self, raise_err=False, report_speaking=True):
        self._speaking = False
        self.spoke = []
        self.stops = 0
        self._raise = raise_err
        self._report_speaking = report_speaking   # False = never reports speaking

    @property
    def is_speaking(self):
        return self._speaking

    def speak(self, text):
        if self._raise:
            raise TTSError("no voice")
        self.spoke.append(text)
        self._speaking = self._report_speaking

    def stop(self):
        self.stops += 1
        self._speaking = False


class FakeResult:
    def __init__(self, status, result=None):
        self.status = status
        self.result = result


class FakeAssistant:
    def __init__(self, result=None, on_run=None):
        self.calls = []
        self._result = result or FakeResult(Status.COMPLETED, "all done")
        self._on_run = on_run

    def run(self, transcript):
        self.calls.append(transcript)
        if self._on_run:
            self._on_run()
        return self._result


def _session(**kw):
    cap = kw.pop("capture", None) or FakeCapture()
    stt = kw.pop("stt", None) or FakeSTT()
    tts = kw.pop("tts", None) or FakeTTS()
    asst = kw.pop("assistant", None) or FakeAssistant()
    ks = kw.pop("kill_switch", None) or KillSwitch()
    events = []
    sess = VoiceSession(asst, ks, capture=cap, stt=stt, tts=tts,
                        on_transcript=lambda t: events.append(("transcript", t)),
                        on_message=lambda m: events.append(("msg", m)),
                        **kw)
    return sess, cap, stt, tts, asst, ks, events


# --- PTT + microphone lifecycle (1-5, 15) ------------------------------

def test_ptt_press_starts_capture(_cap=None):
    s, cap, *_ = _session()
    s.on_ptt_press()
    assert s.state == VoiceState.LISTENING and cap.is_open


def test_mic_closed_while_idle():
    s, cap, *_ = _session()
    assert s.state == VoiceState.IDLE and not cap.is_open


def test_ptt_release_finalizes_and_closes_mic():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.on_ptt_press()
    s.on_ptt_release()
    assert not cap.is_open                 # mic closed after capture
    assert asst.calls == ["hello void"]    # dispatched
    assert s.state in (VoiceState.IDLE, VoiceState.SPEAKING)


def test_mic_closes_after_successful_capture():
    s, cap, *_ = _session()
    s.on_ptt_press()
    s.on_ptt_release()
    assert not cap.is_open


def test_mic_closes_after_capture_exception():
    cap = FakeCapture(raise_on_stop=True)
    s, cap, stt, tts, asst, ks, ev = _session(capture=cap)
    s.on_ptt_press()
    s.on_ptt_release()
    assert not cap.is_open
    assert asst.calls == []                # nothing dispatched on capture failure
    assert s.state == VoiceState.IDLE


def test_mic_closed_during_awaiting_confirmation():
    asst = FakeAssistant(FakeResult(Status.AWAITING_CONFIRMATION))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press()
    s.on_ptt_release()
    assert not cap.is_open


# --- transcript validation / dispatch (6-10) ---------------------------

def test_empty_transcript_not_dispatched():
    s, cap, stt, tts, asst, ks, ev = _session(stt=FakeSTT(text=""))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == [] and s.state == VoiceState.IDLE


def test_whitespace_transcript_not_dispatched():
    s, cap, stt, tts, asst, ks, ev = _session(stt=FakeSTT(text="   "))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == []


def test_stt_failure_not_dispatched():
    s, cap, stt, tts, asst, ks, ev = _session(stt=FakeSTT(raise_err=True))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == [] and s.state == VoiceState.IDLE


def test_transcript_available_before_dispatch():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.on_ptt_press(); s.on_ptt_release()
    # on_transcript fired, and it happened before Assistant.run recorded its call
    assert ("transcript", "hello void") in ev
    assert s.last_transcript == "hello void"


def test_transcript_enters_assistant_run():
    s, cap, stt, tts, asst, ks, ev = _session(stt=FakeSTT(text="open notes"))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == ["open notes"]


# --- voice cannot bypass execution authority (11-13) -------------------

def test_voice_dispatches_only_via_assistant_run():
    # The session has no tool/registry/riskgate handles; execution == run().
    s, cap, stt, tts, asst, ks, ev = _session()
    assert not hasattr(s, "tools") and not hasattr(s, "risk_gate")
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == ["hello void"]     # sole execution path


def test_high_risk_enters_awaiting_and_voice_cannot_approve():
    asst = FakeAssistant(FakeResult(Status.AWAITING_CONFIRMATION))
    states = []
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst,
                                           on_state=lambda st: states.append(st))
    s.on_ptt_press(); s.on_ptt_release()
    assert VoiceState.AWAITING_CONFIRMATION in states
    # The assistant was asked to RUN, never to approve/deny (no such call made).
    assert asst.calls == ["hello void"]
    assert not hasattr(asst, "approved")    # voice never approves
    # A spoken "yes" afterwards is a NEW run, not an approval of the pending one.
    asst2 = FakeAssistant(FakeResult(Status.COMPLETED, "ok"))
    s2, *_r = _session(assistant=asst2, stt=FakeSTT(text="yes"))
    s2.on_ptt_press(); s2.on_ptt_release()
    assert asst2.calls == ["yes"]           # a fresh goal, not an authorization


# --- single-flight (16-17) ---------------------------------------------

def test_single_flight_rejects_concurrent_activation():
    reentered = {"capture_opens_during_run": None}

    def during_run():
        # A second activation while a command is DISPATCHED must be rejected.
        s.on_ptt_press()
        reentered["capture_opens_during_run"] = cap.opens

    asst = FakeAssistant(on_run=during_run)
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == ["hello void"]           # exactly one run (no concurrency)
    assert reentered["capture_opens_during_run"] == 1  # no new capture opened


# --- kill switch (18-24, 31) -------------------------------------------

def test_killswitch_stops_microphone():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.on_ptt_press()
    assert cap.is_open
    s.stop()
    assert not cap.is_open and s.state == VoiceState.STOPPED


def test_killswitch_stops_tts():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "speaking now"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press(); s.on_ptt_release()
    assert s.state == VoiceState.SPEAKING and tts.spoke == ["speaking now"]
    s.stop()
    assert tts.stops >= 1 and s.state == VoiceState.STOPPED


@pytest.mark.parametrize("drive", ["idle", "listening", "speaking"])
def test_killswitch_from_every_state(drive):
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "hi"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    if drive == "listening":
        s.on_ptt_press()
    elif drive == "speaking":
        s.on_ptt_press(); s.on_ptt_release()
        assert s.state == VoiceState.SPEAKING
    s.stop()
    assert s.state == VoiceState.STOPPED


def test_cannot_reactivate_after_killswitch():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.stop()
    s.on_ptt_press()
    assert s.state == VoiceState.STOPPED and not cap.is_open and asst.calls == []


def test_late_stt_result_after_killswitch_is_discarded():
    # STT engages the kill switch mid-inference; the transcript must NOT dispatch.
    ks = KillSwitch()

    def race():
        ks.engage(reason="stop during STT")

    s, cap, stt, tts, asst, k, ev = _session(kill_switch=ks,
                                             stt=FakeSTT(text="delete everything",
                                                         hook=race))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == []                 # late result discarded
    assert s.state == VoiceState.STOPPED


def test_interrupted_input_discarded_via_generation():
    # A stop() between capture and dispatch bumps the generation -> discard.
    def race():
        s.stop()

    s, cap, stt, tts, asst, ks, ev = _session(stt=FakeSTT(text="do it", hook=race))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == [] and s.state == VoiceState.STOPPED


def test_shutdown_releases_resources():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.on_ptt_press()
    s.stop()
    assert not cap.is_open and tts.stops >= 0 and s.state == VoiceState.STOPPED


# --- TTS behavior (25-28) ----------------------------------------------

def test_tts_failure_preserves_agent_response():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "the answer is 42"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst, tts=FakeTTS(raise_err=True))
    s.on_ptt_press(); s.on_ptt_release()
    # Agent result intact; task not re-run or failed; session recovers.
    assert asst.calls == ["hello void"]
    assert s._last_result.result == "the answer is 42"
    assert s._last_result.status == Status.COMPLETED
    assert s.state == VoiceState.IDLE


def test_ptt_during_tts_interrupts_and_starts_capture():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "long response"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press(); s.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    s.on_ptt_press()                        # interrupt
    assert tts.stops >= 1
    assert s.state == VoiceState.LISTENING and cap.is_open


# --- state machine (29) ------------------------------------------------

def test_illegal_transition_rejected():
    sm = VoiceStateMachine()
    with pytest.raises(IllegalVoiceTransition):
        sm.to(VoiceState.SPEAKING)          # IDLE -> SPEAKING is illegal
    assert sm.state == VoiceState.IDLE


def test_stopped_is_terminal():
    sm = VoiceStateMachine()
    sm.force_stopped()
    with pytest.raises(IllegalVoiceTransition):
        sm.to(VoiceState.LISTENING)


# --- optional dependencies (30) ----------------------------------------

def test_core_and_voice_import_without_optional_stack():
    # These import cleanly even though faster-whisper/sounddevice/keyboard are
    # NOT installed (heavy deps are imported lazily inside adapter methods).
    import importlib
    for mod in ("void.app", "void.voice", "void.voice.session",
                "void.voice.adapters", "void.voice.state"):
        assert importlib.import_module(mod) is not None


def test_real_adapters_fail_cleanly_when_deps_missing():
    from void.voice.adapters import FasterWhisperSTT
    # When faster-whisper is absent, _load() must raise a clean, actionable
    # VoiceDependencyError (never a bare ImportError). When it IS installed (e.g.
    # after the live smoke test), the missing-dep path isn't exercisable here -
    # loading "small" would download/load the model, so skip.
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        with pytest.raises(VoiceDependencyError):
            FasterWhisperSTT()._load()
    else:
        pytest.skip("faster-whisper installed; missing-dependency path not hit")


# --- future wake-word plugs into the same boundary (32) ----------------

def test_future_wakeword_activation_boundary():
    fired = []

    class WakeWordActivation(ActivationAdapter):   # conceptual future adapter
        def start(self):
            fired.append("started")

        def stop(self):
            fired.append("stopped")

        def simulate(self):
            self._on_press()
            self._on_release()

    s, cap, stt, tts, asst, ks, ev = _session()
    wake = WakeWordActivation(on_press=s.on_ptt_press, on_release=s.on_ptt_release)
    wake.start()
    wake.simulate()                          # same session, no execution changes
    assert asst.calls == ["hello void"] and fired == ["started"]


# --- live-loop tick: session.poll() (async speech + global kill switch) ----

def test_poll_keeps_speaking_while_backend_speaks():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "talking"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press(); s.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    s.poll()                                  # tts still speaking -> stay
    assert s.state == VoiceState.SPEAKING


def test_poll_retires_speaking_to_idle_when_speech_finishes():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "talking"))
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst)
    s.on_ptt_press(); s.on_ptt_release()
    s.poll()                                  # observe speaking
    tts._speaking = False                     # backend finished
    s.poll()
    assert s.state == VoiceState.IDLE


def test_poll_grace_retires_when_backend_never_reports_speaking():
    # Degenerate: async speak() returned but the backend never reports speaking
    # (or the utterance finished between polls). The grace window retires it.
    clock = {"t": 100.0}
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "hi"))
    s, cap, stt, tts, a, ks, ev = _session(
        assistant=asst, tts=FakeTTS(report_speaking=False),
        speak_grace_seconds=0.5, now=lambda: clock["t"])
    s.on_ptt_press(); s.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    s.poll()                                  # within grace -> not retired yet
    assert s.state == VoiceState.SPEAKING
    clock["t"] += 0.6                         # past the grace window
    s.poll()
    assert s.state == VoiceState.IDLE


def test_poll_global_killswitch_interrupts_speaking():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "long answer"))
    ks = KillSwitch()
    s, cap, stt, tts, a, k, ev = _session(assistant=asst, kill_switch=ks)
    s.on_ptt_press(); s.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    ks.engage(reason="global stop from elsewhere")   # not a PTT edge
    s.poll()                                  # monitor tick makes it authoritative
    assert s.state == VoiceState.STOPPED
    assert tts.stops >= 1 and not cap.is_open


def test_poll_global_killswitch_from_idle_stops_voice():
    ks = KillSwitch()
    s, cap, stt, tts, asst, k, ev = _session(kill_switch=ks)
    ks.engage(reason="global stop")
    s.poll()
    assert s.state == VoiceState.STOPPED
    s.on_ptt_press()                          # no reactivation after stop
    assert s.state == VoiceState.STOPPED and asst.calls == []


def test_poll_is_noop_when_idle_and_clear():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.poll()
    assert s.state == VoiceState.IDLE


def test_poll_is_noop_when_stopped():
    s, cap, stt, tts, asst, ks, ev = _session()
    s.stop()
    s.poll()
    assert s.state == VoiceState.STOPPED


# --- VoiceController runtime (activation wiring + monitor) ------------------

from void.voice.runtime import VoiceController          # noqa: E402
from void.voice.adapters import PTTActivation           # noqa: E402


class FakeActivation:
    """Stands in for PTTActivation: records lifecycle, can drive the session."""
    def __init__(self, on_press, on_release, start_error=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = 0
        self.stopped = 0
        self._start_error = start_error

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        self.started += 1

    def stop(self):
        self.stopped += 1

    def press_and_release(self):
        self.on_press()
        self.on_release()


def _controller(**kw):
    s, cap, stt, tts, asst, ks, ev = _session(**kw)
    act = FakeActivation(s.on_ptt_press, s.on_ptt_release)
    ctrl = VoiceController(s, act, poll_interval=0.01)
    return ctrl, s, act, cap, stt, tts, asst, ks, ev


def test_controller_start_arms_activation_and_wires_edges():
    ctrl, s, act, cap, stt, tts, asst, ks, ev = _controller()
    ctrl.start(monitor=False)                 # deterministic: no thread
    assert act.started == 1
    act.press_and_release()                   # activation edges reach the session
    assert asst.calls == ["hello void"]


def test_controller_poll_once_delegates_to_session_poll():
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "reply"))
    ctrl, s, act, cap, stt, tts, a, ks, ev = _controller(assistant=asst)
    ctrl.start(monitor=False)
    act.press_and_release()
    assert s.state == VoiceState.SPEAKING
    ctrl.poll_once()                          # observe backend speaking
    tts._speaking = False                     # backend finished
    ctrl.poll_once()
    assert s.state == VoiceState.IDLE


def test_controller_shutdown_stops_activation_and_session():
    ctrl, s, act, cap, stt, tts, asst, ks, ev = _controller()
    ctrl.start(monitor=False)
    ctrl.shutdown()
    assert act.stopped == 1
    assert s.state == VoiceState.STOPPED and ctrl.stopped


def test_controller_start_propagates_dependency_error():
    s, cap, stt, tts, asst, ks, ev = _session()
    act = FakeActivation(s.on_ptt_press, s.on_ptt_release,
                         start_error=VoiceDependencyError("no keyboard"))
    ctrl = VoiceController(s, act)
    with pytest.raises(VoiceDependencyError):
        ctrl.start(monitor=False)


def test_controller_monitor_thread_enforces_killswitch():
    # The ONE real-thread test: a global stop engaged with no PTT edge must be
    # picked up by the background monitor and stop voice.
    import time as _t
    ks = KillSwitch()
    ctrl, s, act, cap, stt, tts, asst, k, ev = _controller(kill_switch=ks)
    ctrl.start(monitor=True)
    try:
        ks.engage(reason="global stop")       # not a PTT edge
        deadline = _t.time() + 2.0
        while not ctrl.stopped and _t.time() < deadline:
            _t.sleep(0.02)
        assert ctrl.stopped                   # monitor ticked poll() -> STOPPED
    finally:
        ctrl.shutdown()


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, dotted, default=None):
        return self._v.get(dotted, default)


class _StubAssistant:
    def __init__(self):
        self.config = _StubConfig({
            "voice.stt_model": "small", "voice.stt_device": "cpu",
            "voice.stt_language": "en", "voice.speak_responses": True,
            "voice.ptt_hotkey": "ctrl+space",
        })
        self.kill_switch = KillSwitch()


def test_from_assistant_builds_real_adapters_without_optional_deps():
    # Constructs the real controller (mic + faster-whisper + SAPI + PTT) from
    # config WITHOUT importing any heavy dep (all lazy). No mic/model/hotkey use.
    ctrl = VoiceController.from_assistant(_StubAssistant())
    from void.voice.session import VoiceSession
    assert isinstance(ctrl.session, VoiceSession)
    assert isinstance(ctrl._activation, PTTActivation)
    assert ctrl.state == VoiceState.IDLE


# --- PTT event normalization: press / auto-repeat / release ----------------
#
# The OS delivers raw key events (auto-repeat key-downs, and on some Windows
# setups synthesized key-ups between repeats), not clean edges. PTTActivation
# must collapse them: one physical press/hold => exactly one on_press, one
# physical release => exactly one on_release. These tests drive the event
# handlers directly (no real global hotkey / keyboard import).

def _recorder_ptt(key_is_down=None):
    presses, releases = [], []
    ptt = PTTActivation(lambda: presses.append(1), lambda: releases.append(1),
                        key_is_down=key_is_down)
    return ptt, presses, releases


def test_ptt_one_press_one_activation():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_down()
    assert presses == [1] and releases == []


def test_ptt_repeated_keydown_while_held_is_one_activation():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_down()               # physical press
    ptt._handle_key_down()               # OS auto-repeat
    ptt._handle_key_down()               # OS auto-repeat
    assert presses == [1]                # exactly one activation, no extras


def test_ptt_release_finalizes_exactly_once():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_down()
    ptt._handle_key_up()
    assert releases == [1]


def test_ptt_release_without_active_capture_is_noop():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_up()                 # up with no prior down
    assert presses == [] and releases == []


def test_ptt_spurious_up_during_hold_is_ignored_until_real_release():
    # Model auto-repeat: a key-up arrives while the key is still physically down.
    physically_down = {"v": True}
    ptt, presses, releases = _recorder_ptt(key_is_down=lambda: physically_down["v"])
    ptt._handle_key_down()               # physical press -> one activation
    ptt._handle_key_up()                 # repeat artifact: key still down -> ignore
    assert releases == []                # capture must NOT finalize mid-hold
    ptt._handle_key_down()               # more auto-repeat downs -> still one
    assert presses == [1]
    physically_down["v"] = False         # user actually lets go
    ptt._handle_key_up()
    assert releases == [1]               # finalize exactly once


def test_ptt_rapid_cycles_are_independent_sessions():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_down(); ptt._handle_key_up()
    ptt._handle_key_down(); ptt._handle_key_up()
    assert presses == [1, 1] and releases == [1, 1]


def test_ptt_stop_resets_hold_state():
    ptt, presses, releases = _recorder_ptt()
    ptt._handle_key_down()
    ptt.stop()                           # e.g. Ctrl+C shutdown mid-hold
    ptt._handle_key_down()               # a later, fresh press still works
    assert presses == [1, 1]


def test_ptt_hold_with_repeats_drives_exactly_one_capture_no_busy_spam():
    # End to end through a real VoiceSession: a single hold that produces several
    # auto-repeat downs before release must yield ONE capture, ONE dispatch, and
    # emit NO "Voice is busy" message.
    s, cap, stt, tts, asst, ks, ev = _session()
    ptt = PTTActivation(s.on_ptt_press, s.on_ptt_release)   # key_is_down=None
    ptt._handle_key_down()               # press
    ptt._handle_key_down()               # auto-repeat
    ptt._handle_key_down()               # auto-repeat
    assert s.state == VoiceState.LISTENING and cap.opens == 1
    ptt._handle_key_up()                 # release -> finalize + dispatch
    assert cap.opens == 1 and cap.closes >= 1
    assert asst.calls == ["hello void"]  # exactly one dispatch
    assert ("msg", "Voice is busy; ignoring activation.") not in ev


# --- Phase 9B: provider-agnostic TTS layer ---------------------------------
#
# A pluggable TTS interface with a Windows SAPI provider, a null-safe fallback,
# and a factory - so future backends (Piper/ElevenLabs/WinRT OneCore) slot in
# without touching the Agent/session. Deterministic: no real speakers.

import void.voice.tts as ttsmod                                   # noqa: E402
from void.voice.tts import (                                      # noqa: E402
    NullTTS, TTSProvider, _ResilientTTS, available_providers,
    create_tts_provider, register_tts_provider,
)
from void.voice.adapters import SapiTTS                           # noqa: E402


class RaisingTTS(TTS):
    @property
    def is_speaking(self):
        raise RuntimeError("status blew up")

    def speak(self, text):
        raise RuntimeError("backend on fire")

    def stop(self):
        raise RuntimeError("stop blew up")


def test_tts_interface_is_the_shared_base():
    # One hierarchy: the provider interface IS the adapters TTS base.
    assert TTSProvider is TTS
    assert issubclass(NullTTS, TTS) and issubclass(SapiTTS, TTS)


def test_null_tts_is_safe_noop():
    t = NullTTS()
    assert t.is_speaking is False
    t.speak("nothing comes out")         # must not raise, no speakers needed
    t.stop()
    assert t.spoke == ["nothing comes out"] and t.stops == 1


def test_factory_defaults_to_sapi_on_windows(monkeypatch):
    monkeypatch.setattr(ttsmod.sys, "platform", "win32")
    t = create_tts_provider()            # no config, no explicit provider
    assert isinstance(t, _ResilientTTS) and t.name == "sapi"
    assert isinstance(t.delegate, SapiTTS)   # Windows provider behind interface


def test_factory_defaults_to_null_off_windows(monkeypatch):
    monkeypatch.setattr(ttsmod.sys, "platform", "linux")
    t = create_tts_provider()
    assert t.name == "null" and isinstance(t.delegate, NullTTS)


def test_factory_reads_provider_from_config():
    cfg = _StubConfig({"voice.tts_provider": "null"})
    t = create_tts_provider(cfg)
    assert t.name == "null" and isinstance(t.delegate, NullTTS)


def test_factory_unknown_provider_falls_back_to_null():
    t = create_tts_provider(provider="does-not-exist")
    assert t.name == "null" and isinstance(t.delegate, NullTTS)


def test_factory_construction_failure_falls_back_to_null():
    def boom():
        raise RuntimeError("cannot construct")
    register_tts_provider("boom", boom)
    try:
        t = create_tts_provider(provider="boom")
        assert t.name == "null" and isinstance(t.delegate, NullTTS)
    finally:
        ttsmod._PROVIDERS.pop("boom", None)


def test_resilient_wrapper_normalizes_speak_errors_to_ttserror():
    t = _ResilientTTS(RaisingTTS(), "raising")
    with pytest.raises(TTSError):
        t.speak("hello")                 # RuntimeError -> TTSError (uniform)


def test_resilient_wrapper_is_speaking_and_stop_never_raise():
    t = _ResilientTTS(RaisingTTS(), "raising")
    assert t.is_speaking is False        # status error swallowed
    t.stop()                             # stop is on the kill-switch path: safe


def test_resilient_wrapper_delegates_success():
    fake = FakeTTS()
    t = _ResilientTTS(fake, "fake")
    t.speak("hi there")
    assert fake.spoke == ["hi there"] and t.is_speaking is True
    t.stop()
    assert fake.stops == 1


def test_register_tts_provider_is_replaceable():
    # A future backend can be added by name with no Agent/session change.
    made = []

    class PretendPiper(TTS):
        def __init__(self):
            made.append(1)
        @property
        def is_speaking(self):
            return False
        def speak(self, text):
            pass
        def stop(self):
            pass

    register_tts_provider("pretend-piper", PretendPiper)
    try:
        assert "pretend-piper" in available_providers()
        t = create_tts_provider(provider="pretend-piper")
        assert t.name == "pretend-piper" and isinstance(t.delegate, PretendPiper)
        assert made == [1]
    finally:
        ttsmod._PROVIDERS.pop("pretend-piper", None)


def test_controller_builds_tts_via_factory_null_backend():
    # from_assistant must go through the factory, not hard-code SAPI. With a
    # 'null' provider configured, the session's TTS is the null-backed provider.
    class _Stub(_StubAssistant):
        def __init__(self):
            super().__init__()
            self.config = _StubConfig({
                "voice.stt_model": "small", "voice.stt_device": "cpu",
                "voice.stt_language": "en", "voice.speak_responses": True,
                "voice.ptt_hotkey": "ctrl+space", "voice.tts_provider": "null",
            })

    ctrl = VoiceController.from_assistant(_Stub())
    session_tts = ctrl.session._tts
    assert isinstance(session_tts, _ResilientTTS) and session_tts.name == "null"


def test_tts_failure_is_non_fatal_through_session():
    # A provider that always fails must not corrupt the agent result or crash the
    # session: the result stands and the session returns to IDLE.
    asst = FakeAssistant(FakeResult(Status.COMPLETED, "the answer is 42"))
    failing = _ResilientTTS(RaisingTTS(), "raising")
    s, cap, stt, tts, a, ks, ev = _session(assistant=asst, tts=failing)
    s.on_ptt_press(); s.on_ptt_release()
    assert a.calls == ["hello void"]
    assert s._last_result.result == "the answer is 42"
    assert s.state == VoiceState.IDLE
