"""Tests for the voice latency/rate pass: STT decode tuning + warmup, SAPI TTS
speaking rate, and the snappier wake endpointing default. All use fakes/seams -
no faster-whisper, no numpy requirement for the STT paths that inject a model,
no SAPI/COM, no microphone."""
from __future__ import annotations

import time

from void.voice.adapters import FasterWhisperSTT, SapiTTS
from void.voice.runtime import _WakePolicy
from void.voice.tts import create_tts_provider


# --- STT decode tuning ------------------------------------------------------

class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeModel:
    """Records the kwargs faster-whisper would be called with."""
    def __init__(self, *, fail_on_vad=False):
        self.calls = []
        self._fail_on_vad = fail_on_vad

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if self._fail_on_vad and kwargs.get("vad_filter"):
            raise RuntimeError("vad asset missing")
        return [_Seg("open chrome")], {"language": "en"}


def test_stt_defaults_are_latency_tuned():
    stt = FasterWhisperSTT()
    assert stt._beam_size == 1           # greedy by default (fast)
    assert stt._vad_filter is True


def test_stt_transcribe_uses_greedy_and_no_prev_context():
    stt = FasterWhisperSTT(vad_filter=False)
    stt._model = _FakeModel()            # bypass real model load
    out = stt.transcribe(object())
    assert out == "open chrome"
    call = stt._model.calls[-1]
    assert call["beam_size"] == 1
    assert call["condition_on_previous_text"] is False
    assert call["language"] == "en"


def test_stt_vad_failure_falls_back_without_vad():
    stt = FasterWhisperSTT(vad_filter=True)
    stt._model = _FakeModel(fail_on_vad=True)
    out = stt.transcribe(object())       # first call (vad) raises -> retry no-vad
    assert out == "open chrome"
    assert stt._model.calls[0].get("vad_filter") is True
    assert "vad_filter" not in stt._model.calls[1]


def test_stt_warmup_never_raises_without_deps():
    # No model set and faster-whisper/numpy may be absent -> must be a safe no-op.
    FasterWhisperSTT().warmup()


def test_stt_beam_size_is_floored_to_one():
    assert FasterWhisperSTT(beam_size=0)._beam_size == 1


# --- SAPI TTS speaking rate -------------------------------------------------

class _FakeSpVoice:
    def __init__(self):
        self.spoke = []
        self.Rate = 0

    def Speak(self, text, flags):
        self.spoke.append(text)
        return 0

    def WaitUntilDone(self, ms):
        return 1                         # utterance completes immediately


def _speak_and_settle(tts, text, timeout=2.0):
    tts.speak(text)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not tts.is_speaking:
            break
        time.sleep(0.01)


def test_sapi_rate_is_clamped_to_valid_range():
    assert SapiTTS(rate=99)._rate == 10
    assert SapiTTS(rate=-99)._rate == -10
    t = SapiTTS(rate=0)
    t.set_rate(50)
    assert t._rate == 10


def test_sapi_applies_rate_before_speaking():
    fake = _FakeSpVoice()
    tts = SapiTTS(rate=2, _voice_factory=lambda: fake,
                  _com_setup=lambda: None, _com_teardown=lambda: None)
    try:
        _speak_and_settle(tts, "hello")
        assert fake.Rate == 2                       # rate applied to the voice
        assert "hello" in fake.spoke                # and the text was spoken
    finally:
        tts.close()


# --- factory wires the configured rate --------------------------------------

class _FakeConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


def test_factory_applies_configured_tts_rate_to_sapi():
    cfg = _FakeConfig({"voice.tts_provider": "sapi", "voice.tts_rate": 3})
    provider = create_tts_provider(cfg)
    # _ResilientTTS wraps the real delegate; the rate was pushed to it.
    assert provider.delegate._rate == 3


def test_factory_rate_is_safe_for_null_provider():
    cfg = _FakeConfig({"voice.tts_provider": "null", "voice.tts_rate": 5})
    provider = create_tts_provider(cfg)          # NullTTS.set_rate is a no-op
    assert provider.name == "null"


# --- endpointing default ----------------------------------------------------

def test_wake_endpoint_default_silence_is_snappier():
    # Dataclass default and the config-derived default both drop to 0.8s.
    assert _WakePolicy().silence_s == 0.8
    cfg = _FakeConfig({})                         # get(key, default) -> default
    assert _WakePolicy.from_config(cfg).silence_s == 0.8
