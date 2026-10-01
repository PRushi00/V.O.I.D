"""Conversation mode: wake once, then keep talking. V2 domain 4.

Before this, every sentence needed its own wake word - the detector re-armed the instant a reply finished, so
"open Opera" / "and what's the weather" / "thanks" was three wake words. These tests drive the real
VoiceSession reducer, the real AudioCaptureBroker and the real reconciler through the fakes in
``test_voice_wake_integration``, and assert the behaviour end to end rather than inspecting flags.

The security question this feature raises is the one most of the file is about. During a follow-up window the
microphone is open with no wake word in front of it, so:

* it must close itself when nobody speaks, every time;
* it must be bounded in turns however talkative the owner is;
* the owner must be able to end it in words;
* the kill switch must end it, and a later re-arm must NOT resume it;
* and - the important one - it must change no authorization whatsoever. A follow-up turn is a new command
  through the same funnel. Being mid-conversation is not a credential, and that is asserted directly.
"""
import pytest

from tests.test_voice_wake_integration import (LOUD, SILENCE, FakeActivation, FakeAssistant, FakeBackend,
                                               FakeTTS, RecordingSTT, ScriptedWake)
from void.core.kill_switch import KillSwitch
from void.voice.adapters import BrokerCapture
from void.voice.capture_broker import AudioCaptureBroker
from void.voice.runtime import (VoiceController, _ConversationPolicy, _WakePolicy, is_standby_phrase)
from void.voice.session import VoiceSession
from void.voice.state import VoiceState


def _policy() -> _WakePolicy:
    """Short, frame-exact endpointing so a capture ends in a handful of synthetic frames."""
    return _WakePolicy(no_speech_s=0.12, silence_s=0.09, fast_silence_s=0.09, max_capture_s=1.0,
                       rearm_delay_ms=0, lead_grace_s=0.0, pause_evidence_s=0.0,
                       fast_after_speech_s=0.0, fast_until_speech_s=99.0)


def _rig(*, conversation=None, assistant=None, kill_switch=None, stt=None):
    backend = FakeBackend()
    broker = AudioCaptureBroker(backend=backend)
    stt = stt or RecordingSTT()
    asst = assistant or FakeAssistant()
    ks = kill_switch or KillSwitch()
    holder = {}
    # from_assistant chains the controller's own hook in front of the caller's; replicated here so the
    # standby-phrase path is actually connected. test_a_transcript_reaches_the_callers_own_callback_too
    # covers the production wiring itself.
    session = VoiceSession(asst, ks, capture=BrokerCapture(broker), stt=stt, tts=FakeTTS(),
                           speak_response=False, min_speech_ms=0,
                           on_transcript=lambda text: holder["ctrl"].note_transcript(text))
    wake = ScriptedWake()
    ctrl = VoiceController(
        session, None, poll_interval=0.01, broker=broker, wake=wake,
        wake_policy=_policy(),
        conversation=conversation or _ConversationPolicy(enabled=True, follow_up_s=0.12, max_turns=3))
    ctrl._activation = FakeActivation(ctrl.on_ptt_press, ctrl.on_ptt_release)
    holder["ctrl"] = ctrl
    return ctrl, session, broker, backend, wake, asst, ks, stt


def _arm(ctrl):
    ctrl.start(monitor=False)
    for _ in range(max(1, ctrl._rearm_ticks)):
        ctrl.poll_once()
    assert ctrl._wake_armed


def _speak_a_command(broker, backend, *, words=6, quiet=6):
    """Emit a short utterance followed by enough silence to endpoint it."""
    backend.emit_many(LOUD, words)
    broker.drain()
    backend.emit_many(SILENCE, quiet)
    broker.drain()


def _settle(ctrl, ticks=4):
    for _ in range(ticks):
        ctrl.poll_once()


# --- the policy -------------------------------------------------------------------------------------

def test_conversation_mode_is_on_in_the_shipped_configuration():
    """The product requirement: waking once should be enough."""
    from void.config import Config
    policy = _ConversationPolicy.from_config(Config.load())
    assert policy.enabled is True
    assert 1.0 <= policy.follow_up_s <= 30.0
    assert policy.max_turns >= 1


def test_the_window_and_turn_cap_are_clamped():
    from void.config import Config
    wild = _ConversationPolicy.from_config(
        Config({"voice": {"conversation_follow_up_s": 10_000, "conversation_max_turns": 10_000}}))
    assert wild.follow_up_s == 30.0
    assert wild.max_turns == 100
    tiny = _ConversationPolicy.from_config(
        Config({"voice": {"conversation_follow_up_s": -5, "conversation_max_turns": 0}}))
    assert tiny.follow_up_s == 1.0 and tiny.max_turns == 1


def test_a_malformed_value_keeps_a_working_default():
    from void.config import Config
    policy = _ConversationPolicy.from_config(
        Config({"voice": {"conversation_follow_up_s": "quite a while"}}))
    assert policy.follow_up_s == 7.0, "a bad config value left the owner unable to talk"


# --- standby phrases ---------------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "that's all", "thats all", "that is all", "that's it", "goodbye", "bye", "bye bye",
    "standby", "stand by", "never mind", "nevermind", "forget it", "we're done", "I'm done",
    "nothing else", "go to sleep", "stop listening", "see you later", "all done",
    "okay, that's all", "Thanks, goodbye!", "alright, standby.", "Ok bye.",
])
def test_an_utterance_that_is_only_a_dismissal_ends_the_conversation(said):
    assert is_standby_phrase(said) is True


@pytest.mark.parametrize("said", [
    "never mind, open Opera",
    "stop the music",
    "that's all the files I need, show me the rest",
    "what does standby mean",
    "tell me about going to sleep",
    "say goodbye to my calendar",
    "done is better than perfect, write that down",
    "open all done.txt",
    "", "   ",
])
def test_a_command_that_merely_contains_a_dismissal_is_still_a_command(said):
    """The failure mode this prevents: silently swallowing "never mind, open Opera" as an exit."""
    assert is_standby_phrase(said) is False


def test_phrase_matching_survives_odd_input():
    for junk in (None, 12345, "\x00\x00", "?" * 500):
        assert is_standby_phrase(junk) in (True, False)


# --- the conversation lifecycle ----------------------------------------------------------------------

def test_a_wake_word_is_needed_for_the_first_command():
    ctrl, session, broker, backend, wake, _asst, _ks, _stt = _rig()
    _arm(ctrl)
    # Speech with no wake word must not start a capture.
    backend.emit_many(LOUD, 6)
    broker.drain()
    assert session.state == VoiceState.IDLE
    assert ctrl.conversation_snapshot()["open"] is False


def test_after_a_reply_a_follow_up_needs_no_wake_word():
    """The product requirement, asserted as behaviour: a second command with one wake word."""
    ctrl, session, broker, backend, wake, asst, _ks, stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert len(asst.calls) == 1, "the first command did not reach the assistant"
    assert ctrl.conversation_snapshot()["open"] is True
    # Back to IDLE: the reconciler should open a follow-up capture rather than re-arm the detector.
    _settle(ctrl)
    assert session.state == VoiceState.LISTENING, "no follow-up capture was opened"
    assert ctrl._wake_armed is False, "the detector was armed during a follow-up window"
    # A second command, with no second wake word.
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert len(asst.calls) == 2, "a follow-up command needed another wake word"


def test_a_follow_up_window_nobody_speaks_into_ends_the_conversation():
    """The bound that matters most: an unused window closes, and the wake word comes back."""
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is True
    _settle(ctrl)
    assert session.state == VoiceState.LISTENING            # the follow-up window is open
    # Say nothing for longer than the window.
    backend.emit_many(SILENCE, 10)
    broker.drain()
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is False, "a silent window kept the conversation open"
    assert len(asst.calls) == 1, "silence was dispatched as a command"
    _settle(ctrl)
    assert ctrl._wake_armed is True, "the wake word did not come back"


def test_the_conversation_is_capped_in_turns():
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig(
        conversation=_ConversationPolicy(enabled=True, follow_up_s=0.12, max_turns=2))
    _arm(ctrl)
    wake.fire()
    for _ in range(4):
        _speak_a_command(broker, backend)
        _settle(ctrl)
    snapshot = ctrl.conversation_snapshot()
    assert snapshot["open"] is False, "the turn cap did not end the conversation"
    assert len(asst.calls) <= 2 + 1, f"more turns ran than the cap allowed: {len(asst.calls)}"
    _settle(ctrl)
    assert ctrl._wake_armed is True


def test_saying_that_is_all_ends_the_conversation():
    ctrl, session, broker, backend, wake, asst, _ks, stt = _rig(stt=RecordingSTT("that's all"))
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is False, "a dismissal left the conversation open"
    assert asst.calls == ["that's all"], "the dismissal was swallowed instead of answered"
    _settle(ctrl)
    assert ctrl._wake_armed is True


def test_a_command_containing_a_dismissal_keeps_the_conversation_open():
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig(
        stt=RecordingSTT("never mind that, open Opera"))
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is True
    assert asst.calls == ["never mind that, open Opera"]


def test_a_false_wake_with_no_speech_does_not_open_a_conversation():
    """A wake word nobody followed up on must leave the machine exactly as it was."""
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    backend.emit_many(SILENCE, 10)
    broker.drain()
    _settle(ctrl)
    assert asst.calls == []
    assert ctrl.conversation_snapshot()["open"] is False
    _settle(ctrl)
    assert ctrl._wake_armed is True


def test_disabling_conversation_mode_restores_a_wake_word_per_sentence():
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig(
        conversation=_ConversationPolicy(enabled=False))
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert len(asst.calls) == 1
    assert ctrl.conversation_snapshot()["open"] is False
    _settle(ctrl)
    assert ctrl._wake_armed is True, "the detector did not re-arm with conversation mode off"
    assert session.state == VoiceState.IDLE, "a follow-up capture opened with the feature off"


def test_the_detector_is_not_armed_while_a_follow_up_window_is_open():
    """Both listening at once would let a wake word start a second capture over the follow-up."""
    ctrl, session, broker, backend, wake, _asst, _ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    _settle(ctrl)
    assert session.state == VoiceState.LISTENING
    assert ctrl._wake_armed is False


def test_a_follow_up_capture_reuses_the_single_microphone_owner():
    """The invariant the broker exists to hold: conversation mode must not open a second stream."""
    ctrl, session, broker, backend, wake, _asst, _ks, _stt = _rig()
    _arm(ctrl)
    starts_before = backend.starts
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    _settle(ctrl)
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert backend.starts == starts_before, "a follow-up opened another capture backend"
    assert backend.closes == 0


# --- the kill switch and shutdown --------------------------------------------------------------------

def test_the_kill_switch_ends_the_conversation():
    ctrl, session, broker, backend, wake, asst, ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is True
    ks.engage("owner said stop")
    session.stop("kill switch")
    _settle(ctrl)
    assert session.state == VoiceState.STOPPED
    assert ctrl.conversation_snapshot()["open"] is False, "a stopped session kept a conversation open"


def test_a_conversation_does_not_resume_after_a_stop_and_re_arm():
    """The defect this closes: a conversation left open by the kill switch resuming - with no wake word -
    the moment the session was explicitly re-armed, which is exactly when a clean slate is expected."""
    ctrl, session, broker, backend, wake, asst, ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    ks.engage("owner said stop")
    session.stop("kill switch")
    _settle(ctrl)
    ks.reset()
    session.reset()
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is False
    assert session.state != VoiceState.LISTENING, "a capture resumed without a wake word after a stop"


def test_shutdown_ends_the_conversation():
    ctrl, session, broker, backend, wake, _asst, _ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert ctrl.conversation_snapshot()["open"] is True
    ctrl.shutdown("test")
    assert ctrl.conversation_snapshot()["open"] is False


# --- conversation mode grants nothing ----------------------------------------------------------------

def test_a_follow_up_turn_authorizes_nothing_the_first_turn_did_not():
    """The security claim, stated directly: being mid-conversation is not a credential.

    Each turn is dispatched through ``Assistant.run`` exactly as a wake-initiated turn is - same call, same
    channel, no extra argument, no carried decision. If a follow-up ever started handing the assistant
    something a first turn does not, this is where it would show.
    """
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig()
    _arm(ctrl)
    wake.fire()
    _speak_a_command(broker, backend)
    _settle(ctrl)
    _settle(ctrl)
    _speak_a_command(broker, backend)
    _settle(ctrl)
    assert len(asst.calls) == 2
    assert asst.calls[0] == asst.calls[1], "the two turns reached the assistant differently"
    assert getattr(asst, "owner_decisions", None) in (None, [], {}), \
        "a conversation turn carried an owner decision"


def test_the_controller_never_inspects_a_transcript_for_anything_but_standby():
    """``note_transcript`` reads the text to answer one question. It must not act on the content."""
    import ast
    import inspect
    source = inspect.getsource(VoiceController.note_transcript)
    tree = ast.parse(source.lstrip())
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert called <= {"_end_conversation", "exception"}, f"note_transcript calls {called}"


def test_a_transcript_reaches_the_callers_own_callback_too():
    """The controller's hook is chained in front of the caller's, never in place of it."""
    seen = []

    class Config:
        def get(self, key, default=None):
            return {"voice.wake_provider": "null", "voice.tts_provider": "null",
                    "voice.conversation_mode": True}.get(key, default)

        def state_dir(self):
            raise RuntimeError("no state dir in this test")

    class Stub:
        def __init__(self):
            self.config = Config()
            self.kill_switch = KillSwitch()
    import void.voice.runtime as runtime_mod
    ctrl = runtime_mod.VoiceController.from_assistant(Stub(), on_transcript=seen.append)
    ctrl._session._on_transcript("that's all")
    assert seen == ["that's all"], "the caller's transcript callback was replaced"
    assert ctrl.conversation_snapshot()["open"] is False


def test_a_failing_phrase_check_never_breaks_the_pipeline(monkeypatch):
    ctrl, _session, _broker, _backend, _wake, _asst, _ks, _stt = _rig()
    monkeypatch.setattr("void.voice.runtime.is_standby_phrase",
                        lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
    ctrl.note_transcript("anything")          # must not raise


def test_the_snapshot_is_observation_only():
    ctrl, _session, _broker, _backend, _wake, _asst, _ks, _stt = _rig()
    snapshot = ctrl.conversation_snapshot()
    assert set(snapshot) == {"enabled", "open", "turns", "max_turns", "follow_up_s", "in_follow_up"}
    snapshot["open"] = True                   # a copy, not the controller's state
    assert ctrl.conversation_snapshot()["open"] is False


# --- the PTT path is untouched ------------------------------------------------------------------------

def test_push_to_talk_still_works_and_does_not_start_a_conversation():
    """PTT is an explicit, held activation. It needs no follow-up window and must not get one."""
    ctrl, session, broker, backend, wake, asst, _ks, _stt = _rig()
    _arm(ctrl)
    ctrl.on_ptt_press()
    backend.emit_many(LOUD, 6)
    broker.drain()
    ctrl.on_ptt_release()
    _settle(ctrl)
    assert len(asst.calls) == 1
    assert ctrl.conversation_snapshot()["open"] is False, "a PTT turn opened a follow-up window"


def test_a_controller_without_a_broker_is_unaffected():
    """Conversation mode is inert in a PTT-only controller, like the wake integration it builds on."""
    backend = FakeBackend()
    broker = AudioCaptureBroker(backend=backend)
    session = VoiceSession(FakeAssistant(), KillSwitch(), capture=BrokerCapture(broker),
                           stt=RecordingSTT(), tts=FakeTTS(), speak_response=False, min_speech_ms=0)
    ctrl = VoiceController(session, None, poll_interval=0.01)   # no broker, no wake
    ctrl._activation = FakeActivation(ctrl.on_ptt_press, ctrl.on_ptt_release)
    ctrl.start(monitor=False)
    ctrl.poll_once()
    assert ctrl.conversation_snapshot()["open"] is False
