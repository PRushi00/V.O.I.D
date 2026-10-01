"""Choosing the speech-to-text backend, and never being stranded by that choice.

Measured on the owner's machine (docs/VOICE_PIPELINE_V2_2026-09-24.md), same audio and same decoding options:
cpu/int8 1092 ms mean decode against cuda/float16 115 ms, with IDENTICAL end-to-end accuracy - 11 of 14 spoken
commands opened the right application under both. That is a second off every voice command for nothing, so cuda
is the default.

The risk that buys is a machine where cuda cannot be used: no GPU, no CUDA runtime, or a GPU already full. The
owner must still be able to speak to V.O.I.D there, so the device is a preference, not a requirement.
"""
import pytest

from void.voice.adapters import FasterWhisperSTT
from void.voice.cuda import register


class _Boom:
    """Stands in for WhisperModel: refuses whatever devices it was told to refuse."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.built = []

    def __call__(self, name, device="cpu", compute_type="int8"):
        self.built.append((device, compute_type))
        if device in self.refuse:
            raise RuntimeError(f"Library cublas64_12.dll is not found or cannot be loaded ({device})")
        return object()


@pytest.fixture
def whisper(monkeypatch):
    fake = _Boom()

    def _install(refuse=()):
        fake.refuse = set(refuse)
        import faster_whisper
        monkeypatch.setattr(faster_whisper, "WhisperModel", fake)
        return fake

    return _install


def test_the_configured_device_is_used_when_it_works(whisper):
    fake = whisper()
    stt = FasterWhisperSTT(device="cuda", compute_type="float16")
    stt._load()
    assert fake.built == [("cuda", "float16")] and stt.device == "cuda"


def test_a_machine_without_a_usable_gpu_falls_back_to_the_cpu(whisper, caplog):
    """No GPU, no CUDA runtime, or a GPU already full - the owner must still be able to speak."""
    fake = whisper(refuse={"cuda"})
    stt = FasterWhisperSTT(device="cuda", compute_type="float16")
    with caplog.at_level("WARNING"):
        stt._load()
    assert fake.built == [("cuda", "float16"), ("cpu", "int8")]
    assert stt.device == "cpu"
    assert any("STT_DEVICE_UNAVAILABLE" in r.message for r in caplog.records)


def test_the_fallback_is_only_tried_once(whisper):
    """A second failure is a real failure: no loop, no third device."""
    from void.voice.adapters import STTError
    fake = whisper(refuse={"cuda", "cpu"})
    stt = FasterWhisperSTT(device="cuda", compute_type="float16")
    with pytest.raises(STTError):
        stt._load()
    assert fake.built == [("cuda", "float16"), ("cpu", "int8")]


def test_a_cpu_failure_is_reported_rather_than_retried(whisper):
    from void.voice.adapters import STTError
    fake = whisper(refuse={"cpu"})
    stt = FasterWhisperSTT(device="cpu", compute_type="int8")
    with pytest.raises(STTError):
        stt._load()
    assert fake.built == [("cpu", "int8")]      # already the fallback: nothing else to try


def test_registering_the_cuda_libraries_is_harmless_and_idempotent():
    """A machine with no CUDA at all simply finds no directories."""
    first = register()
    assert register() == []                      # idempotent
    assert all(isinstance(p, str) for p in first)


def test_the_configured_default_is_the_measured_one():
    from void.config import Config
    cfg = Config.load()
    assert cfg.get("voice.stt_device", "") == "cuda"
    assert cfg.get("voice.stt_compute_type", "") == "float16"
    assert int(cfg.get("voice.stt_beam_size", 1)) == 1        # greedy: beam search doubled decode time
