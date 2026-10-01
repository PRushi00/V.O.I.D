"""Adaptive trailing silence: a shorter wait once the utterance is clearly under way.

Two earlier milestones concluded 0.6 s was the floor, and both measured a FIXED budget (V3 shortened the timeout,
V4 swapped in streaming Silero). What neither asked is WHICH utterances need the long one. Measured over 95
recordings of the owner's voice (52 internal gaps), the longest silence inside an utterance tracks how much speech
has already been heard:

    gap after < 0.30 s of speech   max 0.51 s        gap after 0.60-1.00 s   max 0.30 s
    gap after 0.30-0.60 s          max 0.15 s        gap after > 1.00 s      max 0.09 s

So the full budget is paid at the fragile beginning, a shorter one in the middle, and the full one again past the
point where the owner is plainly speaking a sentence rather than a command - sentences have long clause breaks
(measured: a 0.45 s break after 2.58 s of speech) and are followed by a model call of several seconds anyway.

Measured effect, owner's own recordings: endpoint p50 0.45 -> 0.30 s, p90 0.57 -> 0.39 s, ZERO truncations, where
the same 0.40 s applied unconditionally truncates 2 of 20. See docs/VOICE_ENDPOINTING_V5_2026-10-01.md.

Both failure modes are pinned here, because they pull in opposite directions:
  * waiting too long  - dead air after every command;
  * ending too early  - "Open ChatGPT" becomes "Open Chat" and the command is lost.
"""
import struct

import pytest

from void.config import Config
from void.voice.runtime import _WakeEndpointer, _WakePolicy

FRAME = 0.03
SAMPLES = 480


def voiced(level=1500):
    return struct.pack("<%dh" % SAMPLES, *([level, -level] * (SAMPLES // 2)))


def silent():
    return b"\x00\x00" * SAMPLES


V = voiced()
S = silent()


def policy(**kw):
    base = dict(no_speech_s=4.0, silence_s=0.6, max_capture_s=15.0, rearm_delay_ms=500,
                energy_threshold=300.0, lead_grace_s=0.4,
                fast_silence_s=0.4, fast_after_speech_s=0.3, fast_until_speech_s=1.8,
                pause_evidence_s=0.2)
    base.update(kw)
    return _WakePolicy(**base)


def fire_at(frames, pol=None):
    """Seconds of capture elapsed when the endpointer closed it, and why. None if it never closed."""
    out = {}
    ep = _WakeEndpointer(pol or policy(), lambda reason: out.setdefault("reason", reason))
    for i, f in enumerate(frames):
        ep(f)
        if out:
            return round((i + 1) * FRAME, 2), out["reason"]
    return None, None


def speech(n):
    return [V] * n


def quiet(n):
    return [S] * n


# --- the shipped configuration ------------------------------------------------------------------

def test_the_shipped_settings_are_the_measured_ones():
    cfg = Config.load()
    assert float(cfg.get("voice.wake_silence_timeout", 0)) == 0.6
    assert float(cfg.get("voice.wake_fast_silence_timeout", 0)) == 0.4
    assert float(cfg.get("voice.wake_fast_after_speech", -1)) == 0.3
    assert float(cfg.get("voice.wake_fast_until_speech", 0)) == 1.8
    assert float(cfg.get("voice.wake_pause_evidence", -1)) == 0.2


def test_the_safe_budget_is_not_shortened_past_what_was_validated():
    """0.5 s truncated 3 of the owner's recordings; 0.6 s truncated none. The SAFE budget stays 0.6 s."""
    assert float(Config.load().get("voice.wake_silence_timeout", 0)) >= 0.6


def test_the_fast_budget_is_not_shortened_past_what_was_validated():
    """0.35 s leaves 50 ms over the worst gap measured once an utterance is under way (0.30 s); 0.40 s leaves 100."""
    assert float(Config.load().get("voice.wake_fast_silence_timeout", 0)) >= 0.4


def test_the_policy_reads_the_configuration():
    pol = _WakePolicy.from_config(Config.load())
    assert (pol.silence_s, pol.fast_silence_s) == (0.6, 0.4)
    assert (pol.fast_after_speech_s, pol.fast_until_speech_s, pol.pause_evidence_s) == (0.3, 1.8, 0.2)


@pytest.mark.parametrize("bad", [None, "", "abc", [], {}])
def test_a_malformed_configuration_value_keeps_a_safe_default(bad):
    pol = _WakePolicy.from_config(Config({"voice": {"wake_fast_silence_timeout": bad,
                                                    "wake_fast_until_speech": bad,
                                                    "wake_pause_evidence": bad}}))
    assert pol.fast_silence_s >= 0.1 and pol.fast_until_speech_s >= 0.1 and pol.pause_evidence_s >= 0.0


# --- the budget can never be LONGER than it was -------------------------------------------------

def test_the_fast_budget_never_exceeds_the_safe_one():
    """A misconfiguration must not make V.O.I.D wait longer than the validated budget."""
    at, _ = fire_at(speech(20) + quiet(60), policy(fast_silence_s=5.0))
    assert at == pytest.approx(0.6 + 0.6, abs=0.031)


def test_setting_the_fast_budget_to_the_safe_one_restores_the_old_behaviour_exactly():
    """The off switch. With fast == safe the endpointer is the single fixed timeout it was before."""
    frames = speech(20) + quiet(60)
    assert fire_at(frames, policy(fast_silence_s=0.6)) == fire_at(frames, policy(fast_silence_s=0.6,
                                                                                pause_evidence_s=0.0))
    at, reason = fire_at(frames, policy(fast_silence_s=0.6))
    assert reason == "silence" and at == pytest.approx(1.2, abs=0.031)


# --- clean endings ------------------------------------------------------------------------------

def test_an_utterance_under_way_ends_on_the_fast_budget():
    """0.6 s of speech then silence: 0.40 s, not 0.60 s."""
    at, reason = fire_at(speech(20) + quiet(40))
    assert reason == "silence"
    assert at == pytest.approx(0.6 + 0.4, abs=0.031)


def test_a_short_command_still_pays_the_safe_budget_at_the_fragile_start():
    """Only 0.15 s of speech so far: a 0.5 s gap is normal there, so the full budget is required."""
    at, reason = fire_at(speech(5) + quiet(40))
    assert reason == "silence"
    assert at == pytest.approx(0.15 + 0.6, abs=0.031)


@pytest.mark.parametrize("n_speech", [10, 20, 30, 40, 50])
def test_the_fast_budget_applies_across_ordinary_command_lengths(n_speech):
    at, reason = fire_at(speech(n_speech) + quiet(40))
    assert reason == "silence"
    assert at == pytest.approx(n_speech * FRAME + 0.4, abs=0.031)


def test_a_long_sentence_gets_the_safe_budget_back():
    """Past 1.8 s of speech this is a sentence, whose clause breaks are long - and a model call follows anyway."""
    at, reason = fire_at(speech(70) + quiet(40))     # 2.1 s of speech
    assert reason == "silence"
    assert at == pytest.approx(70 * FRAME + 0.6, abs=0.031)


def test_the_same_command_endpoints_identically_every_time():
    """Determinism: elapsed time comes from frame counts, never a wall clock."""
    frames = speech(20) + quiet(40)
    assert len({fire_at(frames) for _ in range(5)}) == 1


# --- pauses -------------------------------------------------------------------------------------

def test_a_pause_shorter_than_the_fast_budget_does_not_end_the_command():
    """"Open VS Code <0.3 s> and WhatsApp" survives: the capture closes after the SECOND half."""
    at, reason = fire_at(speech(20) + quiet(10) + speech(20) + quiet(40))
    assert reason == "silence"
    assert at and at > (20 + 10 + 20) * FRAME


def test_a_survived_pause_restores_the_safe_budget_for_the_rest_of_the_utterance():
    """One hesitation buys back the old behaviour - measured: a second pause of up to 420 ms then survives."""
    frames = speech(20) + quiet(8) + speech(20) + quiet(40)       # a 0.24 s gap, over the 0.2 s evidence bar
    at, reason = fire_at(frames)
    assert reason == "silence"
    assert at == pytest.approx((20 + 8 + 20) * FRAME + 0.6, abs=0.031)


def test_without_the_latch_the_second_pause_would_be_cut_sooner():
    """The latch is what the measurement justified: with it 420 ms survives, without it only 240 ms."""
    frames = speech(20) + quiet(8) + speech(20) + quiet(40)
    with_latch, _ = fire_at(frames, policy(pause_evidence_s=0.2))
    without, _ = fire_at(frames, policy(pause_evidence_s=0.0))
    assert with_latch > without


def test_a_second_pause_survives_once_the_utterance_has_hesitated_once():
    frames = (speech(20) + quiet(8)              # first hesitation: sets the latch
              + speech(10) + quiet(14)           # a 0.42 s second pause - over the fast budget
              + speech(10) + quiet(40))
    at, reason = fire_at(frames)
    assert reason == "silence"
    assert at and at > (20 + 8 + 10 + 14 + 10) * FRAME, "the second pause ended the command"


def test_the_latch_is_sticky_for_the_whole_capture():
    frames = speech(20) + quiet(8) + speech(10) + quiet(2) + speech(10) + quiet(40)
    at, _ = fire_at(frames)
    assert at == pytest.approx((20 + 8 + 10 + 2 + 10) * FRAME + 0.6, abs=0.031)


def test_a_pause_longer_than_the_safe_budget_still_ends_the_command():
    """By design, and unchanged: at some point silence IS the end of the utterance."""
    at, reason = fire_at(speech(20) + quiet(30) + speech(10))
    assert reason == "silence"
    assert at == pytest.approx(0.6 + 0.4, abs=0.031)


def test_natural_hesitation_at_the_start_is_protected_by_the_grace_and_the_window():
    """A false start then the real command: nothing may end the capture during the first 0.4 s."""
    at, reason = fire_at(quiet(5) + speech(3) + quiet(10) + speech(20) + quiet(40))
    assert reason == "silence"
    assert at and at > (5 + 3 + 10 + 20) * FRAME


# --- the other bounded exits, unchanged ---------------------------------------------------------

def test_silence_after_the_wake_word_still_gives_up_rather_than_listening_for_ever():
    at, reason = fire_at(quiet(200))
    assert reason == "no_speech"
    assert at == pytest.approx(4.0, abs=0.031)


def test_a_capture_is_always_bounded_even_if_the_room_never_goes_quiet():
    at, reason = fire_at(speech(600))
    assert reason == "max_duration"
    assert at == pytest.approx(15.0, abs=0.031)


def test_the_lead_in_grace_still_protects_the_start_of_the_command():
    """Nothing may close the capture in the first 0.4 s, whatever the budget says."""
    at, reason = fire_at(speech(2) + quiet(60), policy(fast_after_speech_s=0.0))
    assert at is not None and at >= 0.4


def test_background_noise_above_the_gate_never_ends_a_capture_early():
    """A room that is never quiet ends on the hard cap, not on a false "silence"."""
    at, reason = fire_at([voiced(400)] * 600)
    assert reason == "max_duration"


def test_quiet_speech_is_still_speech():
    """The gate must hear a quiet voice: 320 is above the 300 threshold and must count."""
    at, reason = fire_at([voiced(320)] * 20 + quiet(40))
    assert reason == "silence"
    assert at == pytest.approx(0.6 + 0.4, abs=0.031)


def test_the_gate_does_not_hear_the_room():
    at, reason = fire_at([voiced(200)] * 20 + quiet(200))
    assert reason == "no_speech"


# --- structure -----------------------------------------------------------------------------------

def test_the_endpointer_still_decides_when_to_stop_and_nothing_else():
    """It must not buffer audio, transcribe, or touch the session: it reports a reason, once."""
    import inspect
    src = inspect.getsource(_WakeEndpointer)
    for word in ("transcribe", "Popen", "startfile", "open(", "append(frame", "self._frames"):
        assert word not in src, word
    calls = []
    ep = _WakeEndpointer(policy(), lambda reason: calls.append(reason))
    for f in speech(20) + quiet(60):
        ep(f)
    assert calls == ["silence"], "the endpointer fired more than once"


def test_the_decision_costs_nothing_but_arithmetic():
    """No model, no network, no thread: the budget is a comparison of floats."""
    import inspect
    src = inspect.getsource(_WakeEndpointer._silence_budget)
    for word in ("import", "requests", "predict", "Thread", "time."):
        assert word not in src, word


@pytest.mark.parametrize("frame", [b"", b"\x00", b"\x00" * 3])
def test_a_runt_frame_never_breaks_the_endpointer(frame):
    ep = _WakeEndpointer(policy(), lambda reason: None)
    ep(frame)                                    # must not raise


# --- telemetry ------------------------------------------------------------------------------------

def test_the_endpointer_reports_which_budget_ended_the_capture():
    """So the fast-vs-safe split over real use is readable from perf.jsonl, not only from a benchmark."""
    ep = _WakeEndpointer(policy(), lambda reason: None)
    for f in speech(20) + quiet(40):
        ep(f)
    assert ep.fired_budget_s == pytest.approx(0.4)

    ep2 = _WakeEndpointer(policy(), lambda reason: None)
    for f in speech(5) + quiet(40):
        ep2(f)
    assert ep2.fired_budget_s == pytest.approx(0.6)


def test_a_capture_that_did_not_end_on_silence_reports_no_budget():
    """'no_speech' and 'max_duration' are not silence decisions, so there is no budget to report."""
    ep = _WakeEndpointer(policy(), lambda reason: None)
    for f in quiet(200):
        ep(f)
    assert ep.fired_budget_s is None


def test_the_budget_is_a_number_in_the_telemetry_schema_and_text_is_dropped():
    from void.perf.schema import validate
    clean, dropped = validate("endpoint", {"reason": "silence", "capture_s": 1.2, "budget_s": 0.4})
    assert clean["budget_s"] == 0.4 and dropped == 0
    clean2, dropped2 = validate("endpoint", {"reason": "silence", "transcript": "open whatsapp"})
    assert "transcript" not in clean2 and dropped2 == 1


def test_the_session_records_the_budget_without_deciding_anything_from_it():
    import inspect
    from void.voice import session as sess
    src = inspect.getsource(sess.VoiceSession.note_endpoint_reason)
    assert "budget_s" in src
    body = inspect.getsource(sess.VoiceSession)
    assert "if self._endpoint_budget_s" in body          # only ever used to decide whether to EMIT the field
    assert "_endpoint_budget_s >" not in body and "_endpoint_budget_s <" not in body
