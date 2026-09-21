"""Minimum-speech guard: clearly unusable captures never reach STT, the agent, or a cloud model.

Found by live validation: a clipped wake-word tail (100-200 ms of speech + silence) makes Whisper invent words
("Thank you.", "Wait.", "Please subscribe to the channel.") which were dispatched as commands. The signal that
separates the two populations on synthesised speech is how long the speech lasts: every hallucinating sliver
spanned <= ~150 ms, every genuine short command ("Stop.", "Yes.", "Pause.") spanned >= 300 ms.
"""
import numpy as np
import pytest

from void.core.kill_switch import KillSwitch
from void.core.task import Status
from void.voice import audio_guard
from void.voice.runtime import _min_speech_ms
from void.voice.session import VoiceSession
from void.voice.state import VoiceState
from tests.test_voice import FakeAssistant, FakeCapture, FakeResult, FakeSTT, FakeTTS

SR = 16000


def _speech(ms, amp=0.1):
    """Voiced audio: a 200 Hz tone, float32 in [-1, 1] like BrokerCapture.stop() returns."""
    t = np.arange(int(SR * ms / 1000)) / SR
    return (amp * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


def _silence(ms):
    return np.zeros(int(SR * ms / 1000), np.float32)


def _clip(*parts):
    return np.concatenate(parts)


# ------------------------------------------------------------------ the measure
@pytest.mark.parametrize("audio", [
    _silence(2000),
    _silence(20),                                             # shorter than one 30 ms frame
    np.array([], np.float32),
    _clip(_silence(300), _speech(6), _silence(1500)),          # a lone click / key tap
    _clip(_speech(60), _silence(1500)),                        # clipped wake-word tail
    _clip(_silence(300), _speech(100), _silence(1500)),
    _clip(_speech(150), _silence(1500)),
    _clip(_silence(300), _speech(200), _silence(1500)),
    (np.random.default_rng(3).normal(0, 20, SR) / 32768).astype(np.float32),   # very low noise floor
])
def test_clearly_unusable_captures_are_rejected(audio):
    assert not audio_guard.has_enough_speech(audio)


@pytest.mark.parametrize("ms", [300, 450, 600, 1500])
def test_genuine_short_commands_are_kept(ms):
    assert audio_guard.has_enough_speech(_clip(_silence(200), _speech(ms), _silence(800)))


def test_a_quiet_speaker_is_not_penalised():
    # RMS ~ 150 int16 units: far below the 500 wake gate, but a real (quiet) voice
    assert audio_guard.has_enough_speech(_speech(500, amp=0.0065))


def test_plosive_gaps_inside_a_word_do_not_make_it_too_short():
    # "p-ause": loud, a 60 ms quiet closure, loud again - spans ~450 ms
    word = _clip(_speech(180), _speech(60, amp=0.003), _speech(210))
    assert audio_guard.has_enough_speech(word)


def test_two_stray_clicks_far_apart_are_not_speech():
    click = _speech(6)
    assert not audio_guard.has_enough_speech(_clip(click, _silence(2000), click))     # span is long, voiced time is not


def test_int16_capture_is_measured_too():
    assert audio_guard.has_enough_speech((_speech(500) * 32768).astype(np.int16))
    assert not audio_guard.has_enough_speech((_clip(_speech(100), _silence(1500)) * 32768).astype(np.int16))


def test_nan_samples_do_not_crash_or_count_as_speech():
    bad = np.full(SR, np.nan, np.float32)
    assert not audio_guard.has_enough_speech(bad)


@pytest.mark.parametrize("unmeasurable", ["AUDIO", [0.1] * 100, None, np.zeros((2, 100), np.float32), np.zeros(100, np.int32)])
def test_anything_it_cannot_measure_passes_through(unmeasurable):
    assert audio_guard.has_enough_speech(unmeasurable)                    # fail open: never invent a rejection


def test_zero_disables_the_guard():
    assert audio_guard.has_enough_speech(_silence(1000), 0)
    assert audio_guard.has_enough_speech(_silence(1000), -5)


@pytest.mark.parametrize("cfg,expected", [
    ({}, 250.0), ({"voice.min_speech_ms": 0}, 0.0), ({"voice.min_speech_ms": "400"}, 400.0),
    ({"voice.min_speech_ms": -10}, 0.0), ({"voice.min_speech_ms": "junk"}, 250.0), ({"voice.min_speech_ms": None}, 250.0),
])
def test_config_value_is_parsed_defensively(cfg, expected):
    class C:
        def get(self, key, default=None):
            return cfg.get(key, default)
    assert _min_speech_ms(C()) == expected


def test_default_config_ships_the_guard_on():
    from void.config import Config
    assert float(Config.load().get("voice.min_speech_ms")) == audio_guard.DEFAULT_MIN_SPEECH_MS


# ------------------------------------------------------------------ in the session: nothing reaches the agent
class _HallucinatingSTT(FakeSTT):
    """Says 'Thank you.' whatever it hears - what Whisper did with slivers. Counts how often it is asked."""
    def __init__(self):
        super().__init__(text="Thank you.")
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return super().transcribe(audio)


def _session(audio, **kw):
    stt, asst, tts = _HallucinatingSTT(), FakeAssistant(), FakeTTS()
    msgs = []
    s = VoiceSession(asst, KillSwitch(), capture=FakeCapture(audio=audio), stt=stt, tts=tts, on_message=msgs.append, **kw)
    return s, stt, asst, tts, msgs


def test_a_sliver_never_reaches_stt_the_agent_or_tts_and_returns_to_idle():
    s, stt, asst, tts, msgs = _session(_clip(_silence(300), _speech(100), _silence(1500)))
    s.on_ptt_press()
    s.on_ptt_release()
    assert stt.calls == 0, "STT must not even be asked: it would invent text"
    assert asst.calls == [] and tts.spoke == []
    assert s.state == VoiceState.IDLE
    assert "No speech detected." in msgs


def test_a_genuine_short_command_still_reaches_the_agent():
    s, stt, asst, tts, msgs = _session(_clip(_silence(100), _speech(450), _silence(800)))
    s.on_ptt_press()
    s.on_ptt_release()
    assert stt.calls == 1 and asst.calls == ["Thank you."]


def test_the_guard_can_be_disabled_per_session():
    s, stt, asst, *_ = _session(_clip(_speech(100), _silence(1500)), min_speech_ms=0)
    s.on_ptt_press()
    s.on_ptt_release()
    assert stt.calls == 1 and asst.calls == ["Thank you."]


def test_a_skipped_capture_can_be_followed_by_a_normal_command():
    s, stt, asst, tts, msgs = _session(_speech(100))
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == []
    s._capture._audio = _speech(600)
    s.on_ptt_press(); s.on_ptt_release()
    assert asst.calls == ["Thank you."]


def test_the_skip_is_logged_without_any_audio_or_text(caplog):
    import logging
    s, *_ = _session(_speech(100))
    with caplog.at_level(logging.INFO, logger="void.voice.session"):
        s.on_ptt_press(); s.on_ptt_release()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "STT_SKIPPED reason=too_little_speech" in text
    assert "Thank you" not in text


def test_the_skip_is_recorded_in_the_perf_stream_as_an_empty_stt_without_content(tmp_path):
    import json
    from void import perf
    perf.configure(tmp_path)
    try:
        s, *_ = _session(_speech(100))
        s.on_ptt_press(); s.on_ptt_release()
    finally:
        perf.shutdown()
    rows = [json.loads(l) for f in tmp_path.glob("perf.jsonl*") for l in f.read_text(encoding="utf-8").splitlines()]
    stt = [r for r in rows if r.get("event") == "stt"]
    assert len(stt) == 1 and stt[0]["empty"] is True and stt[0]["backend"] == "audio_guard" and stt[0]["decode_s"] == 0.0
    assert "Thank you" not in json.dumps(rows)
