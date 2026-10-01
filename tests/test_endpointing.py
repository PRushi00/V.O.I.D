"""Endpointing: how long V.O.I.D keeps listening after the owner stops talking.

This is the largest remaining term in a spoken command - speech-to-text is ~80 ms on the GPU and the command
itself ~10-60 ms, so the wait the owner feels is almost entirely this. It was retuned on 2026-09-24 by replaying
95 recordings of the OWNER's own voice (originals plus speed and noise variants) through the production
endpointer; the numbers quoted here come from that run (docs/VOICE_PIPELINE_V3_2026-09-24.md).

On 2026-10-01 the single fixed budget became an ADAPTIVE one, from the same corpus: the full 0.6 s is still paid at
the fragile start of an utterance and once the owner is plainly speaking a sentence, while a command that is clearly
under way ends 0.4 s after the last word. This file keeps the invariants that do not depend on which budget applies;
the adaptive behaviour itself is pinned in test_endpointing_adaptive.py
(docs/VOICE_ENDPOINTING_V5_2026-10-01.md).

The two failure modes pull in opposite directions, and both are pinned below:
  * waiting too long  - dead air after every command;
  * ending too early  - "Open ChatGPT" becomes "Open Chat" and the command is lost.
"""
import pytest

from void.config import Config
from void.voice.runtime import _rms_int16, _WakeEndpointer, _WakePolicy

FRAME = 0.03
SAMPLES = 480


def _voiced(level=1500):
    """One 30 ms frame of "speech" at a given int16 amplitude."""
    import struct
    return struct.pack(f"<{SAMPLES}h", *([level, -level] * (SAMPLES // 2)))


def _silent():
    return b"\x00\x00" * SAMPLES


def _feed(policy, frames):
    """Push frames through the real endpointer; return (frame index it fired on, reason)."""
    out = {}
    ep = _WakeEndpointer(policy, lambda reason: out.setdefault("reason", reason))
    for i, f in enumerate(frames):
        ep(f)
        if "reason" in out:
            return i, out["reason"]
    return None, None


def _policy(**kw):
    base = dict(silence_s=0.6, energy_threshold=300.0, no_speech_s=4.0,
                max_capture_s=15.0, lead_grace_s=0.4)
    base.update(kw)
    return _WakePolicy(**base)


# --- the configuration itself ------------------------------------------------------------------

def test_the_shipped_settings_are_the_measured_ones():
    cfg = Config.load()
    assert float(cfg.get("voice.wake_silence_timeout", 0)) == 0.6
    assert float(cfg.get("voice.wake_energy_threshold", 0)) == 300


def test_the_trailing_wait_is_not_shortened_past_what_was_validated():
    """0.5 s truncated 3 of the owner's recordings and 0.3 s truncated 4. 0.6 s truncated none."""
    cfg = Config.load()
    assert float(cfg.get("voice.wake_silence_timeout", 0)) >= 0.6


def test_the_gate_hears_quiet_speech_without_hearing_the_room():
    """At 500, 14 of 95 recordings of the owner never registered as speech at all. Their room floor is ~14 RMS."""
    cfg = Config.load()
    gate = float(cfg.get("voice.wake_energy_threshold", 0))
    assert 100 <= gate <= 400


# --- what the endpointer actually does ----------------------------------------------------------

def test_it_ends_the_capture_one_trailing_silence_after_the_last_word():
    """One trailing-silence BUDGET after the last word - and since 2026-10-01 that budget adapts.

    0.9 s of speech is clearly under way, so the fast budget applies (0.4 s). The safe budget still governs the
    fragile start and long sentences; both are pinned in test_endpointing_adaptive.py.
    """
    frames = [_voiced()] * 30 + [_silent()] * 60       # 0.9 s of speech, then silence
    fired, reason = _feed(_policy(), frames)
    assert reason == "silence"
    assert 0.38 <= (fired - 29) * FRAME <= 0.46        # ~0.4 s after the last voiced frame


def test_a_pause_inside_a_command_does_not_end_it():
    """"Open ChatGPT ... please" - the owner pauses mid-sentence and must not be cut off.

    The pause length here is now the MEASURED one. Over 95 recordings of the owner's voice the longest silence
    inside an utterance, once it is under way, is 0.30 s; the 0.45 s used before this was a frame count, not an
    observation. A pause at or past the fast budget does end the capture, and the hesitation latch below is what
    protects an owner who really does pause - see test_endpointing_adaptive.py.
    """
    frames = ([_voiced()] * 20 + [_silent()] * 10 + [_voiced()] * 20 + [_silent()] * 40)   # a 0.30 s pause
    fired, reason = _feed(_policy(), frames)
    assert reason == "silence"
    assert fired > 50, "the capture ended during the pause, losing the rest of the command"


def test_a_pause_that_the_fast_budget_would_cut_is_survived_after_one_hesitation():
    """The replacement guarantee for a longer mid-command pause, and it is measured.

    A survived gap of 0.2 s or more restores the SAFE budget for the rest of the utterance, so a 0.45 s second
    pause - the case the old fixed budget covered unconditionally - still survives once the owner has hesitated
    once. Measured on spliced real speech: a second pause of up to 420 ms survives, exactly as before.
    """
    frames = ([_voiced()] * 20 + [_silent()] * 8      # a 0.24 s hesitation: sets the latch
              + [_voiced()] * 10 + [_silent()] * 15   # a 0.45 s pause, past the fast budget
              + [_voiced()] * 10 + [_silent()] * 40)
    fired, reason = _feed(_policy(), frames)
    assert reason == "silence"
    assert fired > 63, "the second pause ended the command despite an earlier hesitation"


def test_a_pause_longer_than_the_timeout_does_end_it():
    """The boundary is the timeout itself; nothing here is adaptive or magical."""
    frames = [_voiced()] * 20 + [_silent()] * 25 + [_voiced()] * 20
    fired, reason = _feed(_policy(), frames)
    assert reason == "silence" and fired < 45


@pytest.mark.parametrize("length,expected", [
    (5, 0.6),       # 0.15 s of speech: still at the fragile start, where a 0.5 s gap is normal
    (10, 0.4),      # 0.30 s: under way
    (30, 0.4),      # 0.90 s: an ordinary command
    (50, 0.4),      # 1.50 s: a long command, still inside the window
    (60, 0.6),      # 1.80 s: this is a sentence now, and sentences have long clause breaks
    (120, 0.6),     # 3.60 s: likewise
])
def test_the_wait_depends_on_how_much_speech_was_heard_in_the_measured_way(length, expected):
    """It used to be the same for every length. Since 2026-10-01 it adapts, and this pins exactly how.

    The shape comes from measurement, not taste: over 95 recordings of the owner's voice the longest silence inside
    an utterance is 0.51 s before 0.30 s of speech has accumulated and never more than 0.30 s after it, while a long
    SENTENCE carries clause breaks of 0.45 s (measured at 2.58 s of speech). So: safe budget at the start, fast
    budget through the length a command occupies, safe budget again once this is plainly a sentence.
    """
    frames = [_voiced()] * length + [_silent()] * 60
    fired, _reason = _feed(_policy(), frames)
    assert expected - 0.02 <= (fired - (length - 1)) * FRAME <= expected + 0.04


def test_quiet_speech_is_still_speech():
    """A frame at 400 RMS is below the old gate and above the new one - the case that lost whole commands."""
    assert _rms_int16(_voiced(400)) >= 300
    frames = [_voiced(400)] * 30 + [_silent()] * 160   # long enough to reach the 4 s no-speech timeout
    _fired, reason = _feed(_policy(), frames)
    assert reason == "silence"                         # heard, not dismissed as "no speech"
    _fired, reason = _feed(_policy(energy_threshold=500.0), frames)
    assert reason == "no_speech"                       # what the old setting did with the same audio


def test_the_lead_in_grace_still_protects_the_start_of_the_command():
    """The pause between the wake word and the command must never be read as an empty utterance."""
    frames = [_silent()] * 10 + [_voiced()] * 20 + [_silent()] * 40
    fired, reason = _feed(_policy(), frames)
    assert reason == "silence" and fired >= 30


def test_silence_alone_still_gives_up_rather_than_listening_for_ever():
    fired, reason = _feed(_policy(), [_silent()] * 200)
    assert reason == "no_speech" and (fired + 1) * FRAME <= 4.2


def test_a_capture_is_always_bounded_even_if_the_room_never_goes_quiet():
    fired, reason = _feed(_policy(max_capture_s=2.0), [_voiced()] * 400)
    assert reason == "max_duration" and (fired + 1) * FRAME <= 2.1


def test_the_endpointer_decides_when_to_stop_and_nothing_else():
    """It never sees a transcript and can never run anything: it takes audio frames and reports one reason."""
    import inspect
    src = inspect.getsource(_WakeEndpointer)
    for forbidden in ("launch", "subprocess", "Assistant", "transcribe", "RiskGate", "catalog"):
        assert forbidden not in src
