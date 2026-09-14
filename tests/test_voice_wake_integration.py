"""Phase 9C-3: wake activation + AudioCaptureBroker + STT integration.

Deterministic, no real microphone. A FakeCaptureBackend drives the REAL
AudioCaptureBroker; a ScriptedWake stands in for the detector; broker.drain()
synchronises the pump so assertions never race a timer. Covers the single
physical-owner invariant, wake->capture through the existing VoiceSession
reducer + generation token, deterministic endpointing, the no-pre-wake-audio
privacy rule, TTS self-trigger avoidance, PTT compatibility, and KillSwitch /
STOPPED / CLOSED behaviour.
"""
import inspect
import struct
import sys

import pytest

from void.core.kill_switch import KillSwitch
from void.core.task import Status
from void.voice.adapters import STT, STTError, TTS, BrokerCapture
import void.voice.adapters as adapters_mod
from void.voice.capture_broker import AudioCaptureBroker, CaptureBackend
from void.voice.session import VoiceSession
from void.voice.state import VoiceEvent, VoiceState
import void.voice.runtime as runtime_mod
from void.voice.runtime import (
    VoiceController, _WakeEndpointer, _WakePolicy, _rms_int16,
)
from void.voice.wake import WAKE_DETECTED


FRAME_SAMPLES = 480                       # 30 ms @ 16 kHz -> 0.03 s / frame
SILENCE = b"\x00\x00" * FRAME_SAMPLES


def tone(amp: int) -> bytes:
    return struct.pack("<%dh" % FRAME_SAMPLES, *([amp, -amp] * (FRAME_SAMPLES // 2)))


LOUD = tone(8000)


# --- fakes ---------------------------------------------------------------

class FakeBackend(CaptureBackend):
    """Deterministic capture source: emits frames on demand, counts lifecycle."""

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

    def emit(self, frame):
        if self.running and self._on_frame is not None:
            self._on_frame(frame)

    def emit_many(self, frame, n):
        for _ in range(n):
            self.emit(frame)


class ScriptedWake:
    """Fake WakeWordDetector: lifecycle counters, feed_audio counter, fire()."""

    def __init__(self, start_error=None):
        self.on_wake = None
        self.fed = 0
        self.started = self.stopped = self.closed = 0
        self._start_error = start_error

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        self.started += 1

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1

    def feed_audio(self, frame):
        self.fed += 1

    def fire(self):
        if self.on_wake is not None:
            self.on_wake(WAKE_DETECTED)


class RecordingSTT(STT):
    def __init__(self, text="do the thing", raise_err=False, hook=None):
        self.audios = []
        self._text = text
        self._raise = raise_err
        self._hook = hook

    def transcribe(self, audio):
        self.audios.append(audio)
        if self._hook:
            self._hook()
        if self._raise:
            raise STTError("boom")
        # empty (all-zero) audio -> empty transcript, like a real STT would
        try:
            if audio is not None and len(audio) and not any(audio):
                return ""
        except TypeError:
            pass
        return self._text


class FakeTTS(TTS):
    def __init__(self):
        self._speaking = False
        self.spoke = []
        self.stops = 0

    @property
    def is_speaking(self):
        return self._speaking

    def speak(self, text):
        self.spoke.append(text)
        self._speaking = True

    def stop(self):
        self.stops += 1
        self._speaking = False


class FakeResult:
    def __init__(self, result="ok"):
        self.status = Status.COMPLETED
        self.result = result


class FakeAssistant:
    def __init__(self, on_run=None):
        self.calls = []
        self._on_run = on_run

    def run(self, transcript):
        self.calls.append(transcript)
        if self._on_run:
            self._on_run()
        return FakeResult("ok")


class FakeActivation:
    def __init__(self, on_press, on_release):
        self.on_press, self.on_release = on_press, on_release
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def press_and_release(self):
        self.on_press()
        self.on_release()


class ManualWorker:
    def __init__(self):
        self.jobs = []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def submit(self, fn):
        if not self.stopped:
            self.jobs.append(fn)

    def run_all(self):
        while self.jobs:
            self.jobs.pop(0)()

    def stop(self, timeout=2.0):
        self.stopped = True


def _fast_policy(**kw):
    base = dict(no_speech_s=0.3, silence_s=0.09, max_capture_s=1.0,
                rearm_delay_ms=0, energy_threshold=1000.0, lead_grace_s=0.0)
    base.update(kw)
    return _WakePolicy(**base)


def _rig(*, wake=None, policy=None, worker=None, stt=None, assistant=None,
         kill_switch=None, poll_interval=0.01, speak_response=True):
    backend = FakeBackend()
    broker = AudioCaptureBroker(backend=backend)
    stt = stt or RecordingSTT()
    tts = FakeTTS()
    asst = assistant or FakeAssistant()
    ks = kill_switch or KillSwitch()
    cap = BrokerCapture(broker)
    session = VoiceSession(asst, ks, capture=cap, stt=stt, tts=tts,
                           speak_response=speak_response)
    wk = wake or ScriptedWake()
    ctrl = VoiceController(session, None, poll_interval=poll_interval,
                           worker=worker, broker=broker, wake=wk,
                           wake_policy=policy or _fast_policy())
    ctrl._activation = FakeActivation(ctrl.on_ptt_press, ctrl.on_ptt_release)
    return ctrl, session, broker, backend, wk, stt, tts, asst, ks


def _arm(ctrl):
    """Bring the controller up and let the reconciler arm the detector."""
    ctrl.start(monitor=False)
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    assert ctrl._wake_armed


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, dotted, default=None):
        return self._v.get(dotted, default)


class _StubAssistant:
    def __init__(self, extra=None):
        vals = {"voice.stt_model": "small", "voice.stt_device": "cpu",
                "voice.stt_language": "en", "voice.speak_responses": True,
                "voice.ptt_hotkey": "ctrl+space", "voice.wake_provider": "null"}
        vals.update(extra or {})
        self.config = _StubConfig(vals)
        self.kill_switch = KillSwitch()


# --- 1 / 2 / 27: single physical microphone owner --------------------

def test_from_assistant_builds_one_broker_and_broker_capture():
    ctrl = VoiceController.from_assistant(_StubAssistant())
    assert isinstance(ctrl._broker, AudioCaptureBroker)
    assert isinstance(ctrl.session._capture, BrokerCapture)
    assert ctrl.session._capture._broker is ctrl._broker      # the SAME broker
    assert ctrl._wake is not None                             # null detector wired
    assert "sounddevice" not in sys.modules                   # nothing opened


def test_wake_and_stt_integration_opens_no_second_backend():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    assert backend.starts == 1
    wake.fire()                                               # wake capture
    backend.emit_many(LOUD, 6); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()             # -> endpoint -> STT
    ctrl.on_ptt_press(); backend.emit_many(LOUD, 3); broker.drain()  # a PTT too
    ctrl.on_ptt_release()
    assert backend.starts == 1 and backend.stops == 0         # still ONE stream
    ctrl.shutdown()
    assert backend.closes == 1


def test_shutdown_closes_the_physical_backend():
    ctrl, s, broker, backend, *_ = _rig()
    ctrl.start(monitor=False)
    assert backend.starts == 1 and backend.running
    ctrl.shutdown()
    assert backend.closes == 1 and not backend.running
    assert s.state == VoiceState.CLOSED


# --- 3: the detector consumes broker frames -------------------------

def test_wake_detector_receives_broker_frames_only_while_armed():
    ctrl, s, broker, backend, wake, *_ = _rig()
    backend  # noqa
    ctrl.start(monitor=False)
    backend.emit_many(SILENCE, 5); broker.drain()
    assert wake.fed == 0                                      # not armed yet
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    backend.emit_many(SILENCE, 7); broker.drain()
    assert wake.fed >= 7                                      # armed -> fed


# --- 4-11: wake only activates from IDLE; it bumps the generation --

def test_wake_while_idle_starts_listening_and_bumps_generation():
    ctrl, s, broker, backend, wake, *_ = _rig()
    _arm(ctrl)
    gen0 = s.generation
    wake.fire()
    assert s.state == VoiceState.LISTENING
    assert s._capture.is_open
    assert s.generation == gen0 + 1                           # NEW_GENERATION


@pytest.mark.parametrize("drive", [
    "listening", "transcribing", "dispatched", "speaking", "stopped", "closed",
])
def test_wake_is_ignored_outside_idle(drive):
    holder = {}

    def maybe_fire():
        if drive in ("transcribing", "dispatched"):
            holder["wake"].fire()

    if drive == "transcribing":
        stt = RecordingSTT(hook=maybe_fire)
        asst = FakeAssistant()
    elif drive == "dispatched":
        stt = RecordingSTT()
        asst = FakeAssistant(on_run=maybe_fire)
    else:
        stt, asst = RecordingSTT(), FakeAssistant()

    ctrl, s, broker, backend, wake, _stt, tts, _asst, ks = _rig(
        stt=stt, assistant=asst)
    holder["wake"] = wake
    _arm(ctrl)
    gen_before = None

    if drive == "listening":
        ctrl.on_ptt_press()
        assert s.state == VoiceState.LISTENING
        gen_before = s.generation
        wake.fire()
    elif drive in ("transcribing", "dispatched"):
        gen_before = s.generation + 1                         # the PTT press bumps once
        ctrl.on_ptt_press()
        backend.emit_many(LOUD, 3); broker.drain()
        ctrl.on_ptt_release()                                 # inline: hook fires wake mid-chain
    elif drive == "speaking":
        ctrl.on_ptt_press(); backend.emit_many(LOUD, 3); broker.drain()
        ctrl.on_ptt_release()
        assert s.state == VoiceState.SPEAKING
        gen_before = s.generation
        wake.fire()
    elif drive == "stopped":
        s.stop()
        ctrl.poll_once()
        gen_before = s.generation
        wake.fire()
    elif drive == "closed":
        ctrl.shutdown()
        gen_before = s.generation
        wake.fire()

    assert not ctrl._wake_capture_active                      # wake created nothing
    assert s.generation == gen_before                         # no extra generation
    # at most the single PTT-driven command ever ran
    assert len(_asst.calls) <= 1
    if drive in ("stopped",):
        assert s.state == VoiceState.STOPPED
    if drive in ("closed",):
        assert s.state == VoiceState.CLOSED


# --- 12: stale wake callback cannot affect a newer generation -----

def test_stale_wake_finalize_is_dropped_after_generation_change():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING and ctrl._wake_capture_active
    # generation moves on underneath the in-flight capture (e.g. KillSwitch)
    s._apply(VoiceEvent.KILLSWITCH)
    assert s.state == VoiceState.STOPPED
    # the endpointer still fires (speech then silence) -> finalize sees the
    # generation changed and must NOT drive a release
    backend.emit_many(LOUD, 4); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()
    assert asst.calls == []                                   # nothing dispatched
    assert s.state == VoiceState.STOPPED                      # latch intact
    assert not ctrl._wake_capture_active                      # capture flag released
    # a lingering endpointer (if the killswitch had raced ahead of a fire) is
    # also cleaned by the reconciler on the next tick
    ctrl.poll_once()
    assert ctrl._endpointer is None and not ctrl._wake_armed


def test_wake_finalize_generation_guard_direct():
    ctrl, s, *_ , asst, ks = _rig()
    ctrl.start(monitor=False)
    ctrl._wake_capture_active = True
    ctrl._wake_gen = 999                                      # deliberately stale
    ctrl._endpointer = _WakeEndpointer(_fast_policy(), lambda _r: None)
    ctrl._wake_finalize("silence")
    assert asst.calls == [] and s.state == VoiceState.IDLE


# --- 13 / 14: no pre-wake audio reaches the command / STT ---------

def test_pre_wake_audio_never_reaches_stt():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    # a large burst of PRE-WAKE audio (the detector sees it; the command must not)
    backend.emit_many(LOUD, 25); broker.drain()
    assert wake.fed >= 25
    wake.fire()                                               # begins capture here
    assert s.state == VoiceState.LISTENING
    backend.emit_many(LOUD, 6); broker.drain()                # the actual command
    backend.emit_many(SILENCE, 4); broker.drain()             # trailing silence -> endpoint
    assert len(stt.audios) == 1
    got = len(stt.audios[0])
    assert got >= 6 * FRAME_SAMPLES                           # the command is present
    assert got < 20 * FRAME_SAMPLES                           # the 25 pre-wake frames are NOT


def test_pre_wake_audio_dropped_even_without_an_explicit_drain():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    backend.emit_many(LOUD, 20)                               # NOT drained before wake
    wake.fire()                                               # open() drains internally
    backend.emit_many(LOUD, 5); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()
    assert len(stt.audios[0]) < 15 * FRAME_SAMPLES


# --- 15 / 16 / 17: deterministic endpointing --------------------

def _finalize_spy(ctrl):
    seen = []
    orig = ctrl._wake_finalize
    ctrl._wake_finalize = lambda r: (seen.append(r), orig(r)) and None
    return seen


def test_silence_endpoint_finalizes_after_sustained_silence():
    ctrl, s, broker, backend, wake, stt, *_ = _rig()
    _arm(ctrl)
    seen = _finalize_spy(ctrl)
    wake.fire()
    backend.emit_many(LOUD, 5); broker.drain()                # speech
    backend.emit_many(SILENCE, 4); broker.drain()             # >= silence_s (0.09 = 3 frames)
    assert seen == ["silence"]
    assert s.state in (VoiceState.SPEAKING, VoiceState.IDLE)  # release chain ran


def test_no_speech_timeout_finalizes_when_nobody_speaks():
    ctrl, s, broker, backend, wake, stt, *_ = _rig()
    _arm(ctrl)
    seen = _finalize_spy(ctrl)
    wake.fire()
    backend.emit_many(SILENCE, 12); broker.drain()            # 0.3 s no-speech budget
    assert seen == ["no_speech"]
    assert not ctrl._wake_capture_active


def test_hard_max_capture_duration_finalizes():
    ctrl, s, broker, backend, wake, stt, *_ = _rig(
        policy=_fast_policy(silence_s=99.0, no_speech_s=99.0, max_capture_s=0.45))
    _arm(ctrl)
    seen = _finalize_spy(ctrl)
    wake.fire()
    backend.emit_many(LOUD, 20); broker.drain()               # never silent -> hits max
    assert seen == ["max_duration"]


def test_endpointer_fires_exactly_once():
    fired = []
    ep = _WakeEndpointer(_fast_policy(), fired.append)
    for _ in range(40):
        ep(SILENCE)
    assert fired == ["no_speech"]                             # one reason, once


def test_endpointer_holds_no_audio_buffer():
    ep = _WakeEndpointer(_fast_policy(), lambda _r: None)
    ep(LOUD)
    for name in vars(ep):
        assert not isinstance(getattr(ep, name), (bytes, bytearray, list))


def test_lead_grace_prevents_premature_endpoint_after_wake():
    # Regression for "wake fires, Blackhole activates, then nothing": the wake
    # tail plus the natural pause before the command must NOT be finalized as a
    # complete-then-silent utterance. During lead_grace_s, silence cannot end
    # the capture; after it, normal endpointing resumes.
    fired = []
    ep = _WakeEndpointer(
        _fast_policy(lead_grace_s=0.3, silence_s=0.09, no_speech_s=5.0,
                     max_capture_s=99.0), fired.append)
    ep(LOUD); ep(LOUD)                       # 0.06 s "speech" (stand-in for wake tail)
    for _ in range(3):                       # 0.09 s trailing silence >= silence_s
        ep(SILENCE)
    assert fired == []                       # ... but inside grace -> suppressed
    for _ in range(8):                       # cross the 0.3 s grace boundary
        ep(SILENCE)
    assert fired == ["silence"]              # after grace, endpointing resumes


def test_lead_grace_zero_is_legacy_endpointing():
    fired = []
    ep = _WakeEndpointer(_fast_policy(lead_grace_s=0.0, silence_s=0.09),
                         fired.append)
    ep(LOUD)
    for _ in range(4):
        ep(SILENCE)
    assert fired == ["silence"]              # no grace -> immediate silence endpoint


# --- 18 / 19: PTT still works; PTT + wake never overlap ----------

def test_ptt_path_still_flows_through_the_broker():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    ctrl.on_ptt_press()
    assert s.state == VoiceState.LISTENING and s._capture.is_open
    backend.emit_many(LOUD, 8); broker.drain()
    ctrl.on_ptt_release()                                     # inline (no worker)
    assert asst.calls == ["do the thing"]
    assert s.state == VoiceState.SPEAKING
    assert len(stt.audios[0]) == 8 * FRAME_SAMPLES            # exactly what was spoken after press


def test_ptt_then_wake_cannot_start_a_second_command():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    ctrl.on_ptt_press()                                       # PTT owns the session
    wake.fire()                                               # state != IDLE -> ignored
    assert not ctrl._wake_capture_active
    backend.emit_many(LOUD, 3); broker.drain()
    ctrl.on_ptt_release()
    assert asst.calls == ["do the thing"]                     # exactly one command


def test_wake_then_ptt_down_is_ignored_by_the_reducer():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING
    g = s.generation
    ctrl.on_ptt_press()                                       # PTT_DOWN in LISTENING -> ignored
    assert s.generation == g and s.state == VoiceState.LISTENING


# --- 20 / 21: TTS output must not self-trigger a wake -----------

def test_tts_speaking_disarms_wake_and_echo_is_not_fed():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    ctrl.on_ptt_press(); backend.emit_many(LOUD, 3); broker.drain()
    ctrl.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    ctrl.poll_once()                                          # reconcile -> disarm wake
    assert not ctrl._wake_armed
    fed_before = wake.fed
    backend.emit_many(LOUD, 20); broker.drain()               # simulated TTS echo
    assert wake.fed == fed_before                             # detector not fed during TTS
    wake.fire()                                               # even a spurious fire is inert
    assert not ctrl._wake_capture_active and s.state == VoiceState.SPEAKING


def test_residual_audio_after_tts_is_drained_before_rearm():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig(
        policy=_fast_policy(rearm_delay_ms=60), poll_interval=0.03)
    assert ctrl._rearm_ticks == 2
    _arm(ctrl)
    ctrl.on_ptt_press(); backend.emit_many(LOUD, 3); broker.drain()
    ctrl.on_ptt_release()
    assert s.state == VoiceState.SPEAKING
    ctrl.poll_once()                                          # disarm
    backend.emit_many(LOUD, 30)                               # residual echo, NOT drained
    tts._speaking = False
    ctrl.poll_once()                                          # SPEAKING -> IDLE; tick 1 (not armed)
    assert s.state == VoiceState.IDLE and not ctrl._wake_armed
    fed_before = wake.fed
    ctrl.poll_once()                                          # tick 2 -> _arm (drains first)
    assert ctrl._wake_armed
    assert wake.fed == fed_before                             # the 30 residual frames were drained
    backend.emit(LOUD); broker.drain()
    assert wake.fed == fed_before + 1                         # only post-rearm audio is fed


# --- 22 / 23 / 24: KillSwitch / STOPPED / CLOSED ---------------

def test_killswitch_prevents_further_wake_activation():
    ks = KillSwitch()
    ctrl, s, broker, backend, wake, stt, tts, asst, k = _rig(kill_switch=ks)
    _arm(ctrl)
    ks.engage(reason="global stop")
    ctrl.poll_once()                                          # session.poll -> STOPPED; reconcile -> disarm
    assert s.state == VoiceState.STOPPED and not ctrl._wake_armed
    wake.fire()
    assert not ctrl._wake_capture_active and asst.calls == []
    assert s.state == VoiceState.STOPPED


def test_stopped_stays_latched_until_explicit_rearm_then_wake_returns():
    ks = KillSwitch()
    ctrl, s, broker, backend, wake, stt, tts, asst, k = _rig(kill_switch=ks)
    _arm(ctrl)
    ks.engage(reason="stop")
    for _ in range(5):
        ctrl.poll_once()
    assert s.state == VoiceState.STOPPED and not ctrl._wake_armed
    ks.reset()
    s.reset()                                                 # explicit non-voice REARM
    assert s.state == VoiceState.IDLE
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    assert ctrl._wake_armed
    wake.fire()
    assert s.state == VoiceState.LISTENING


def test_closed_is_terminal_no_wake_restart():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    ctrl.shutdown()
    assert s.state == VoiceState.CLOSED and backend.closes == 1
    for _ in range(5):
        ctrl.poll_once()
    wake.fire()
    assert s.state == VoiceState.CLOSED and not ctrl._wake_armed
    assert asst.calls == []


# --- 25 / 26: failure isolation ---------------------------------

def test_stt_failure_does_not_kill_the_wake_detector():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig(
        stt=RecordingSTT(raise_err=True))
    _arm(ctrl)
    wake.fire()
    backend.emit_many(LOUD, 4); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()             # endpoint -> STT raises
    # session cleaned up to IDLE (ERROR -> RECOVER); wake re-arms and works again
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    assert s.state == VoiceState.IDLE and ctrl._wake_armed
    started_before = wake.started
    wake.fire()
    assert s.state == VoiceState.LISTENING
    assert wake.started >= started_before                     # detector still alive


def test_wake_detector_start_failure_does_not_corrupt_the_session():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig(
        wake=ScriptedWake(start_error=RuntimeError("backend gone")))
    ctrl.start(monitor=False)
    ctrl.poll_once()                                          # arm attempt -> fails
    assert ctrl._wake_broken and not ctrl._wake_armed
    assert s.state == VoiceState.IDLE                         # session untouched
    # PTT still works end to end
    ctrl.on_ptt_press(); backend.emit_many(LOUD, 3); broker.drain()
    ctrl.on_ptt_release()
    assert asst.calls == ["do the thing"] and s.state == VoiceState.SPEAKING


def test_wake_error_message_carries_no_audio():
    msgs = []
    backend = FakeBackend()
    broker = AudioCaptureBroker(backend=backend)
    session = VoiceSession(FakeAssistant(), KillSwitch(),
                           capture=BrokerCapture(broker),
                           stt=RecordingSTT(), tts=FakeTTS())
    ctrl = VoiceController(session, None, broker=broker,
                           wake=ScriptedWake(start_error=RuntimeError("x")),
                           wake_policy=_fast_policy(), on_message=msgs.append)
    ctrl._activation = FakeActivation(ctrl.on_ptt_press, ctrl.on_ptt_release)
    ctrl.start(monitor=False)
    ctrl.poll_once()
    assert msgs and "RuntimeError" in msgs[0] and "push-to-talk" in msgs[0].lower()
    assert "\\x" not in "".join(msgs)                         # no raw bytes


# --- with the real serial worker (ManualWorker) ----------------

def test_wake_capture_runs_the_release_chain_on_the_worker():
    mw = ManualWorker()
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig(worker=mw)
    _arm(ctrl)
    wake.fire()                                               # -> worker.submit(_begin_wake_capture)
    assert len(mw.jobs) == 1 and s.state == VoiceState.IDLE   # nothing on the pump thread yet
    mw.run_all()                                              # begins capture
    assert s.state == VoiceState.LISTENING
    backend.emit_many(LOUD, 5); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()             # endpoint -> submit release
    assert asst.calls == []                                   # release chain queued, not run
    mw.run_all()
    assert asst.calls == ["do the thing"] and s.state == VoiceState.SPEAKING


# --- config validation ----------------------------------------

def test_wake_policy_from_config_defaults_and_validation():
    p = _WakePolicy.from_config(_StubConfig({}))
    assert (p.no_speech_s, p.silence_s, p.max_capture_s) == (4.0, 0.8, 15.0)
    assert p.rearm_delay_ms == 500 and p.energy_threshold == 500.0
    assert p.lead_grace_s == 0.4
    bad = _WakePolicy.from_config(_StubConfig({
        "voice.wake_no_speech_timeout": -3,
        "voice.wake_silence_timeout": "nonsense",
        "voice.wake_max_capture_seconds": 0,
        "voice.wake_rearm_delay_ms": -10,
        "voice.wake_energy_threshold": -1,
    }))
    assert bad.no_speech_s == 0.5 and bad.silence_s == 0.8      # clamped / defaulted
    assert bad.max_capture_s == 1.0 and bad.rearm_delay_ms == 0
    assert bad.energy_threshold == 0.0


def test_rms_int16_is_bounded_and_crash_free():
    assert _rms_int16(SILENCE) == 0.0
    assert _rms_int16(LOUD) > 1000.0
    assert _rms_int16(b"\x01") == 0.0                          # odd length -> no crash
    assert _rms_int16(b"") == 0.0


# --- 28 / 29: no raw audio persisted or logged ---------------

def test_integration_code_never_persists_or_logs_raw_audio():
    # Persistence of raw audio is forbidden anywhere in the integration code.
    src = (inspect.getsource(runtime_mod)
           + inspect.getsource(adapters_mod.BrokerCapture)
           + inspect.getsource(adapters_mod.BrokerCapture.open)
           + inspect.getsource(adapters_mod.BrokerCapture.stop))
    for forbidden in ("wave.", "soundfile", ".tofile(",
                      "pickle.", "json.dump", "np.save", "np.savez",
                      'open("', "open('", ", 'w')", ', "w")',
                      "write_bytes", "write_text"):
        assert forbidden not in src, f"integration code must not use {forbidden!r}"

    # Lifecycle logging is allowed in the CONTROL code (stage markers, counts,
    # reasons - never audio), but the units that actually handle raw audio bytes
    # must never log at all, so no audio payload can ever be logged. This is a
    # stronger, targeted guarantee than a blanket "no logging" heuristic.
    audio_handling = (
        inspect.getsource(adapters_mod.BrokerCapture)
        + inspect.getsource(runtime_mod._WakeEndpointer)
        + inspect.getsource(runtime_mod._rms_int16))
    for forbidden in ("logging", "getLogger", "_log", "print("):
        assert forbidden not in audio_handling, (
            f"raw-audio-handling code must not use {forbidden!r}")


# --- existing behaviour still intact -------------------------

# --- orphaned wake capture: reconcile when a NON-endpointer path ends it ---
#
# A wake capture is normally concluded only by its own endpointer (_wake_finalize).
# If PTT_UP / KillSwitch / shutdown / an error moves the session out of LISTENING
# first, _reconcile_wake must release the orphaned _wake_capture_active flag and
# unsubscribe the endpointer so wake can re-arm and a late _wake_finalize no-ops.

def test_ptt_release_supersedes_wake_capture_then_wake_rearms():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING and ctrl._wake_capture_active
    old_ep = ctrl._endpointer
    assert old_ep is not None and broker.subscriber_count == 2   # collector + endpointer

    ctrl.on_ptt_release()                       # a stray PTT tap concludes the capture
    assert s.state == VoiceState.SPEAKING       # ran the full chain inline
    assert asst.calls == ["do the thing"]
    assert ctrl._wake_capture_active            # session path left the orphan set
    assert broker.subscriber_count == 1         # collector gone, endpointer still on

    ctrl.poll_once()                            # reconcile: session left LISTENING
    assert s.state != VoiceState.LISTENING
    assert not ctrl._wake_capture_active        # orphan flag released
    assert ctrl._endpointer is None
    assert broker.subscriber_count == 0         # orphan endpointer unsubscribed

    tts._speaking = False
    ctrl.poll_once()                            # SPEAKING -> IDLE
    assert s.state == VoiceState.IDLE
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    assert ctrl._wake_armed                     # wake re-armed
    wake.fire()
    assert s.state == VoiceState.LISTENING      # and usable again


def test_killswitch_during_wake_capture_reconciles_orphan_and_stays_latched():
    ks = KillSwitch()
    ctrl, s, broker, backend, wake, stt, tts, asst, k = _rig(kill_switch=ks)
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING and ctrl._endpointer is not None
    ks.engage(reason="global stop")
    ctrl.poll_once()                            # session.poll -> STOPPED; reconcile
    assert s.state == VoiceState.STOPPED
    assert not ctrl._wake_capture_active
    assert ctrl._endpointer is None and not ctrl._wake_armed
    for _ in range(6):
        ctrl.poll_once()
    assert s.state == VoiceState.STOPPED and not ctrl._wake_armed   # never re-arms
    wake.fire()
    assert not ctrl._wake_capture_active and asst.calls == []
    assert s.state == VoiceState.STOPPED


def test_shutdown_during_wake_capture_removes_endpointer_terminally():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING and ctrl._endpointer is not None
    ctrl.shutdown()
    assert s.state == VoiceState.CLOSED and backend.closes == 1
    assert ctrl._endpointer is None and not ctrl._wake_capture_active
    # no later callback revives a capture
    wake.fire()
    for _ in range(5):
        ctrl.poll_once()
    assert s.state == VoiceState.CLOSED and not ctrl._wake_capture_active
    assert not ctrl._wake_armed and asst.calls == []


def test_delayed_orphan_endpointer_callback_cannot_barge_in_on_speaking():
    # The audit's exact scenario: wake capture -> stray PTT_UP -> TTS SPEAKING ->
    # the OLD endpointer times out seconds later and calls _wake_finalize. It must
    # NOT interrupt TTS or dispatch anything.
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    old_ep = ctrl._endpointer
    ctrl.on_ptt_release()                       # stray PTT concludes the wake capture
    assert s.state == VoiceState.SPEAKING and asst.calls == ["do the thing"]

    ctrl.poll_once()                            # monitor reconciles the orphan first
    assert not ctrl._wake_capture_active and ctrl._endpointer is None

    # the detached endpointer finally hits its no-speech budget and fires
    for _ in range(20):
        old_ep(SILENCE)
    assert s.state == VoiceState.SPEAKING       # NO barge-in
    assert asst.calls == ["do the thing"]       # nothing re-dispatched
    assert tts.stops == 0                       # TTS not interrupted
    assert not ctrl._wake_capture_active


def test_normal_wake_end_to_end_still_works_after_fix():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    assert s.state == VoiceState.LISTENING
    backend.emit_many(LOUD, 6); broker.drain()
    backend.emit_many(SILENCE, 4); broker.drain()       # silence endpoint -> full chain
    assert asst.calls == ["do the thing"] and s.state == VoiceState.SPEAKING
    assert len(stt.audios[0]) >= 6 * FRAME_SAMPLES
    assert not ctrl._wake_capture_active and ctrl._endpointer is None
    ctrl.poll_once()                                     # observe the backend speaking
    tts._speaking = False
    ctrl.poll_once()                                     # SPEAKING -> IDLE
    assert s.state == VoiceState.IDLE
    for _ in range(ctrl._rearm_ticks):
        ctrl.poll_once()
    assert ctrl._wake_armed                              # re-armed for the next command


def test_ptt_only_cycle_unaffected_by_the_orphan_reconcile():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    ctrl.on_ptt_press()                                  # pure PTT; wake untouched
    assert s.state == VoiceState.LISTENING and not ctrl._wake_capture_active
    ctrl.poll_once()                                     # reconcile: orphan branch is a no-op
    assert s.state == VoiceState.LISTENING               # PTT capture intact
    backend.emit_many(LOUD, 5); broker.drain()
    ctrl.on_ptt_release()
    assert asst.calls == ["do the thing"] and s.state == VoiceState.SPEAKING


def test_generation_guard_still_rejects_stale_finalize_after_fix():
    ctrl, s, broker, backend, wake, stt, tts, asst, ks = _rig()
    _arm(ctrl)
    wake.fire()
    old_ep = ctrl._endpointer
    s._apply(VoiceEvent.KILLSWITCH)                      # STOPPED + NEW_GENERATION
    ctrl.poll_once()                                     # orphan reconciled
    assert not ctrl._wake_capture_active and ctrl._endpointer is None
    for _ in range(20):                                  # detached endpointer fires late
        old_ep(SILENCE)
    assert asst.calls == [] and s.state == VoiceState.STOPPED   # stale finalize -> nothing


def test_broker_none_keeps_pre_9c3_ptt_controller_inert():
    # The plain VoiceController(session, activation) path (no broker/wake) must
    # be untouched: no broker calls, no wake reconciliation side effects.
    from void.core.kill_switch import KillSwitch as KS
    asst = FakeAssistant()
    ks = KS()

    class _Cap:
        def __init__(self):
            self.opens = self.closes = 0
            self._o = False

        @property
        def is_open(self):
            return self._o

        def open(self):
            self._o = True
            self.opens += 1

        def stop(self):
            self._o = False
            return "AUDIO"

        def close(self):
            self._o = False
            self.closes += 1

    s = VoiceSession(asst, ks, capture=_Cap(), stt=RecordingSTT(), tts=FakeTTS())
    ctrl = VoiceController(s, FakeActivation(s.on_ptt_press, s.on_ptt_release))
    ctrl.start(monitor=False)
    ctrl.poll_once()                                           # no-op reconcile
    ctrl._activation.press_and_release()
    assert asst.calls == ["do the thing"]
    ctrl.shutdown()
    assert s.state == VoiceState.CLOSED
