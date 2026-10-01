"""Positive-generation CONFIGURATION tests: verify the right backend is
selected and the right counts/directories are requested, using a fake
backend (no real SAPI/COM call, so this runs fast and deterministically
without touching audio hardware)."""
from __future__ import annotations

from pathlib import Path

import pytest

import training.generate_positive as gp


class FakeBackend:
    def __init__(self):
        self.calls = []

    def generate(self, texts, output_dir, n_samples, file_names=None):
        self.calls.append({"texts": list(texts), "output_dir": Path(output_dir),
                           "n_samples": n_samples})
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        for i in range(n_samples):
            (Path(output_dir) / f"gen_{i}.wav").write_bytes(b"RIFF....WAVEfmt ")
        return []


def test_generates_train_and_val_with_configured_counts(monkeypatch, tmp_path, minimal_config):
    fake = FakeBackend()
    monkeypatch.setattr(gp, "get_tts_backend", lambda *a, **k: fake)

    result = gp.generate_positive_samples(minimal_config, tmp_path / "data")

    assert result["train"] == tmp_path / "data" / "positive" / "train"
    assert result["val"] == tmp_path / "data" / "positive" / "val"
    assert len(fake.calls) == 2
    train_call, val_call = fake.calls
    assert train_call["n_samples"] == minimal_config["positive"]["n_samples"]
    assert val_call["n_samples"] == minimal_config["positive"]["n_samples_val"]
    # The exact target phrase text is what's handed to the TTS backend -
    # never "V.O.I.D." with periods, never spelled-out letters.
    assert train_call["texts"] == [minimal_config["target_phrase"]]
    assert train_call["texts"] == ["hey void"]


def test_skips_regeneration_when_enough_samples_already_exist(monkeypatch, tmp_path, minimal_config):
    fake = FakeBackend()
    monkeypatch.setattr(gp, "get_tts_backend", lambda *a, **k: fake)

    train_dir = tmp_path / "data" / "positive" / "train"
    train_dir.mkdir(parents=True)
    n = minimal_config["positive"]["n_samples"]
    for i in range(n):
        (train_dir / f"existing_{i}.wav").write_bytes(b"RIFF....WAVEfmt ")

    gp.generate_positive_samples(minimal_config, tmp_path / "data")

    # Only the val directory (which had nothing) should have triggered a call.
    assert len(fake.calls) == 1
    assert fake.calls[0]["output_dir"] == tmp_path / "data" / "positive" / "val"


# --- speaker representation (David AND Zira actually represented) ---------

def test_both_configured_voices_are_actually_cycled_through():
    from training.tts_backends import SapiSampleGenerator

    gen = SapiSampleGenerator(voices=["David", "Zira"])
    tokens = gen._voice_tokens(10)

    assert "David" in tokens
    assert "Zira" in tokens
    # Deterministic round-robin, not a random subset - both voices get
    # exactly half of an even sample count.
    assert tokens.count("David") == tokens.count("Zira") == 5


def test_single_configured_voice_list_element_still_cycles_correctly():
    from training.tts_backends import SapiSampleGenerator

    gen = SapiSampleGenerator(voices=["David"])
    tokens = gen._voice_tokens(4)
    assert tokens == ["David"] * 4


@pytest.mark.integration
def test_real_sapi_generation_produces_16khz_mono_pcm16_wavs(tmp_path):
    """Exercises the REAL SapiSampleGenerator (actual SAPI/COM call, Windows
    only) - regression guard for the exact bug this pass fixed: SAPI was
    previously writing 44.1kHz audio (wrong SpeechAudioFormatType enum
    value) while every downstream stage assumed 16kHz."""
    pytest.importorskip("win32com.client")
    from training.audio_validation import validate_wav_format
    from training.tts_backends import SapiSampleGenerator

    gen = SapiSampleGenerator(voices=["David", "Zira"])
    written = gen.generate(["hey void"], tmp_path, n_samples=2)

    assert len(written) == 2
    for path in written:
        assert path.exists()
        validate_wav_format(path)  # raises AudioFormatError if not 16kHz/mono/16-bit
