"""Speech-to-text: one deterministic decode, and the verb un-glued from the name.

Both changes came out of measurement (docs/STT_AND_MULTI_APP_2026-09-28.md):

  * faster-whisper's default retries a segment at RISING temperature whenever a decode looks bad - up to six passes.
    Measured, alternating both settings on the same warm model: no difference at all on audio it decodes confidently
    (102 vs 105 ms, identical text) and 2.0x-7.2x slower on audio it does not, where each run also returned a
    DIFFERENT transcript because sampling above 0 is stochastic. Resolution accuracy over 46 synthesised commands was
    identical (91 %). So the retry costs latency and repeatability and buys nothing here.

  * speech-to-text runs the launch verb into the name on ordinary, clean audio: "Open ChatGPT." is transcribed
    'OpenChat GPT' and "Open Chrome." is transcribed 'OpenCrow.' - 2 of 23 commands at normal level, in both voices.
"""
import pytest

from void.config import Config
from void.core.fast_path import launch_targets, parse_launch
from void.voice.adapters import FasterWhisperSTT
from void.voice.runtime import _stt_temperature


class _FakeModel:
    """Records the options each decode was given, and what it was asked to decode."""
    def __init__(self, text="hello"):
        self.calls = []
        self.text = text

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)

        class _Seg:
            def __init__(self, t):
                self.text = t
        return [_Seg(self.text)], object()


def _stt(**kw):
    s = FasterWhisperSTT(**kw)
    s._model = _FakeModel()
    return s


# --- the decode is deterministic by default -------------------------------------------------------

def test_a_single_temperature_is_passed_so_the_decode_cannot_retry():
    s = _stt()
    s.transcribe([0.0])
    assert s._model.calls[0]["temperature"] == 0.0


def test_the_default_is_zero_not_whispers_rising_retry():
    assert FasterWhisperSTT()._temperature == 0.0


def test_none_restores_whispers_own_behaviour_by_passing_nothing():
    s = _stt(temperature=None)
    s.transcribe([0.0])
    assert "temperature" not in s._model.calls[0]


def test_a_configured_temperature_is_honoured():
    s = _stt(temperature=0.4)
    s.transcribe([0.0])
    assert s._model.calls[0]["temperature"] == 0.4


def test_the_other_decode_options_are_unchanged():
    s = _stt(beam_size=1, vad_filter=True)
    s.transcribe([0.0])
    kw = s._model.calls[0]
    assert kw["beam_size"] == 1 and kw["condition_on_previous_text"] is False and kw["vad_filter"] is True
    assert kw["language"] == "en"


def test_the_vad_retry_still_happens_and_keeps_the_temperature():
    """A VAD asset problem must not fail a command - and the retry must not quietly become non-deterministic."""
    s = _stt(vad_filter=True)
    calls = {"n": 0}
    real = s._model.transcribe

    def flaky(audio, **kwargs):
        calls["n"] += 1
        if "vad_filter" in kwargs:
            raise RuntimeError("no VAD asset")
        return real(audio, **kwargs)
    s._model.transcribe = flaky
    assert s.transcribe([0.0]) == "hello"
    assert s._model.calls[-1]["temperature"] == 0.0 and "vad_filter" not in s._model.calls[-1]


# --- the config knob ------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0.0, 0.0), (0, 0.0), (0.4, 0.4), ("0.2", 0.2), (None, None),
    ("nonsense", 0.0), ([], 0.0), ({}, 0.0),           # a malformed value must never break the voice path
])
def test_the_configured_temperature_is_read_safely(value, expected):
    assert _stt_temperature(Config({"voice": {"stt_temperature": value}})) == expected


def test_the_shipped_default_is_the_deterministic_one():
    assert _stt_temperature(Config.load()) == 0.0


def test_the_shipped_config_documents_the_measurement():
    from pathlib import Path
    text = Path("config/default_config.yaml").read_text(encoding="utf-8")
    assert "stt_temperature" in text
    assert "7.2x" in text or "7.2" in text, "the number that justifies the default is not written down"


# --- the verb glued to the name -------------------------------------------------------------------

@pytest.mark.parametrize("said,expected", [
    ("openchat gpt", ("openchat gpt", "chat gpt")),
    ("OpenChat GPT.", ("openchat gpt", "chat gpt")),
    ("openvisual studio code", ("openvisual studio code", "visual studio code")),
    ("launchvs code", ("launchvs code", "vs code")),
    ("startfile explorer", ("startfile explorer", "file explorer")),
])
def test_a_glued_verb_is_offered_as_an_extra_reading(said, expected):
    """The GLUED spelling comes first, so a program actually called "OpenOffice" or "OpenVPN" still wins."""
    assert launch_targets(said) == (expected,)


def test_the_glued_spelling_is_tried_before_the_de_glued_one():
    readings = launch_targets("openoffice writer")[0]
    assert readings[0] == "openoffice writer" and readings[1] == "office writer"


@pytest.mark.parametrize("said", [
    "opennotepad", "opencrow", "openchat", "launchcode", "startmenu",
])
def test_one_word_is_never_treated_as_a_glued_command(said):
    """An existing guard refuses "opennotepad", and rightly: one word is no evidence a verb was run into a name."""
    assert launch_targets(said) == ()
    assert parse_launch(said) is None


@pytest.mark.parametrize("said", [
    "openchat gpt and whatsapp", "openvs code, whatsapp and chatgpt",
])
def test_a_glued_verb_still_splits_into_targets(said):
    assert len(launch_targets(said)) >= 2


@pytest.mark.parametrize("said", [
    r"openC:\Windows\cmd.exe", "opensetup.exe", "openrun.ps1 now", "open$(calc) x",
])
def test_de_gluing_cannot_express_anything_the_grammar_refuses(said):
    """The de-glued reading passes the same character, connector and extension rules as any other."""
    assert launch_targets(said) == ()


def test_a_glued_verb_with_nothing_after_it_is_not_a_command():
    assert launch_targets("open") == () and launch_targets("op x") == ()


# --- telemetry ------------------------------------------------------------------------------------

def test_the_stt_event_can_carry_a_transcript_LENGTH_but_never_the_transcript():
    from void.perf.schema import validate
    clean, dropped = validate("stt", {"decode_s": 0.2, "audio_s": 2.0, "empty": False,
                                      "backend": "FasterWhisperSTT", "chars": 24})
    assert clean["chars"] == 24 and dropped == 0
    clean2, dropped2 = validate("stt", {"chars": 24, "text": "open whatsapp"})
    assert "text" not in clean2 and dropped2 == 1


def test_the_session_records_the_transcript_length():
    import inspect
    from void.voice import session as sess
    src = inspect.getsource(sess.VoiceSession._run_stt)
    assert '"chars": len(transcript)' in src, "a slow decode's transcript length is not recorded"
    assert "transcript}" not in src.replace('"chars": len(transcript)', "")
