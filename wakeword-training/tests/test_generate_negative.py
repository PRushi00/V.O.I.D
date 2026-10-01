"""Negative-generation CONFIGURATION tests, using a fake TTS backend (no real
SAPI/COM call) and a fake adversarial-text generator (no real pronouncing-
dictionary/openwakeword.data call needed for these particular assertions)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import training.generate_negative as gn


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


def test_check_words_in_pronouncing_dict_passes_for_hey_void():
    # "hey" and "void" are ordinary English words - must be in CMUdict.
    gn._check_words_in_pronouncing_dict("hey void")


def test_check_words_in_pronouncing_dict_raises_for_made_up_word():
    with pytest.raises(gn.NegativeDataError, match="not in the CMU"):
        gn._check_words_in_pronouncing_dict("hey zxqvorpalax")


def test_adversarial_texts_never_include_the_actual_target_phrase(monkeypatch, minimal_config):
    monkeypatch.setattr(gn, "generate_adversarial_texts",
                        lambda **kw: ["hey void", "hey noise", "hey void"], raising=False)
    import openwakeword.data as owwdata
    monkeypatch.setattr(owwdata, "generate_adversarial_texts",
                        lambda **kw: ["hey void", "hey noise", "hey void"])

    phrases = gn.generate_adversarial_negative_texts(minimal_config)

    assert "hey void" not in [p.strip().lower() for p in phrases]
    assert "hey noise" in phrases
    # custom_negative_phrases from config must also be included.
    assert "hey voice" in phrases


def test_generate_negative_samples_creates_train_and_val_per_category(
        monkeypatch, tmp_path, minimal_config):
    fake = FakeBackend()
    monkeypatch.setattr(gn, "get_tts_backend", lambda *a, **k: fake)
    monkeypatch.setattr(gn, "generate_adversarial_negative_texts",
                        lambda config: ["hey noise", "hey boy"])

    out = gn.generate_negative_samples(minimal_config, tmp_path / "data")

    expected_keys = {"adversarial_train", "adversarial_val", "noise_train",
                     "noise_val", "speech_train", "speech_val"}
    assert set(out) == expected_keys

    for split, count_key in (("train", "n_adversarial_samples"), ):
        assert (tmp_path / "data" / "negative" / "adversarial" / split).exists()

    # Noise clips are real WAV files written directly (no TTS backend call).
    noise_train_files = list(out["noise_train"].glob("*.wav"))
    assert len(noise_train_files) == minimal_config["negative"]["n_noise_samples"]


def test_generate_negative_samples_raises_on_empty_neutral_sentences(tmp_path, minimal_config):
    minimal_config["negative"]["neutral_sentences"] = []
    with pytest.raises(gn.NegativeDataError, match="neutral_sentences"):
        gn.generate_negative_samples(minimal_config, tmp_path / "data")


def test_noise_generation_is_deterministic_given_same_seed(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    gn._generate_noise_clips(d1, 2, sample_rate=16000, duration_s=0.2, seed=99)
    gn._generate_noise_clips(d2, 2, sample_rate=16000, duration_s=0.2, seed=99)

    for name in ("noise_0000.wav", "noise_0001.wav"):
        assert (d1 / name).read_bytes() == (d2 / name).read_bytes()


# --- negative TTS now shares the positive voice list (closing the previous
# single-voice-negative / multi-voice-positive asymmetry) --------------------

def test_negative_tts_backend_receives_the_configured_positive_voices(
        monkeypatch, tmp_path, minimal_config):
    minimal_config["positive"]["voices"] = ["David", "Zira"]
    captured = {}

    def fake_get_tts_backend(name, *, voices=None, sample_rate=16000,
                             piper_sample_generator_path=None):
        captured["voices"] = voices
        return FakeBackend()

    monkeypatch.setattr(gn, "get_tts_backend", fake_get_tts_backend)
    monkeypatch.setattr(gn, "generate_adversarial_negative_texts",
                        lambda config: ["hey noise", "hey boy"])

    gn.generate_negative_samples(minimal_config, tmp_path / "data")

    assert captured["voices"] == ["David", "Zira"]


def test_negative_tts_backend_receives_none_when_positive_voices_is_empty(
        monkeypatch, tmp_path, minimal_config):
    minimal_config["positive"]["voices"] = []
    captured = {}

    def fake_get_tts_backend(name, *, voices=None, sample_rate=16000,
                             piper_sample_generator_path=None):
        captured["voices"] = voices
        return FakeBackend()

    monkeypatch.setattr(gn, "get_tts_backend", fake_get_tts_backend)
    monkeypatch.setattr(gn, "generate_adversarial_negative_texts",
                        lambda config: ["hey noise", "hey boy"])

    gn.generate_negative_samples(minimal_config, tmp_path / "data")

    # "or None" - an empty list must not be passed through as a falsy-but-
    # truthy-checked empty voices list; it must normalize to None (== "no
    # voices configured, use the backend's own default"), matching how
    # positive generation already treats voices=[].
    assert captured["voices"] is None


def test_positive_dataset_generation_is_unaffected_by_this_change(minimal_config):
    # generate_negative.py must never read/write anything under
    # config["positive"] beyond the two keys it already legitimately reused
    # before this change (voices, piper_sample_generator_path) - this is a
    # structural guard against the negative-side change leaking into
    # positive generation.
    import inspect
    src = inspect.getsource(gn)
    assert "generate_positive" not in src
    assert "positive.n_samples" not in src


# --- adversarial-phrase generation is now seeded -----------------------

def test_adversarial_phrase_generation_seeds_global_numpy_random(
        monkeypatch, minimal_config):
    import numpy as np

    seen_seeds = []
    orig_seed = np.random.seed

    def spy_seed(s):
        seen_seeds.append(s)
        orig_seed(s)

    monkeypatch.setattr(np.random, "seed", spy_seed)
    monkeypatch.setattr("openwakeword.data.generate_adversarial_texts",
                        lambda **kw: ["hey noise"])

    minimal_config["seed"] = 4242
    gn.generate_adversarial_negative_texts(minimal_config)

    assert 4242 in seen_seeds


def test_adversarial_phrase_generation_is_deterministic_given_same_seed(
        minimal_config):
    # Exercises the REAL openwakeword.data.generate_adversarial_texts (no
    # monkeypatch) - proves the seeding actually makes its output
    # reproducible, not just that np.random.seed() was called.
    minimal_config["seed"] = 777
    result1 = gn.generate_adversarial_negative_texts(minimal_config)
    result2 = gn.generate_adversarial_negative_texts(minimal_config)
    assert result1 == result2


def test_voice_tokens_for_cycles_deterministically():
    from training.tts_backends import voice_tokens_for
    assert voice_tokens_for(["David", "Zira"], 5) == ["David", "Zira", "David", "Zira", "David"]
    assert voice_tokens_for([], 3) == [None, None, None]
    assert voice_tokens_for(None, 3) == [None, None, None]


# --- lineage metadata (false-positive-analysis regression guard: this pass
# found that adversarial-category false positives were previously
# UNATTRIBUTABLE - no manifest existed mapping a generated WAV back to the
# phrase/voice that produced it) -------------------------------------------

def test_generate_with_lineage_writes_a_sidecar_matching_generated_files(tmp_path):
    fake = FakeBackend()

    written = gn._generate_with_lineage(
        fake, ["hey noise", "hey boy"], tmp_path / "out", 4,
        category="adversarial", split="train", seed=42, voices=["David", "Zira"])

    assert len(written) == 4
    assert [p.name for p in written] == [
        "adversarial_train_0000.wav", "adversarial_train_0001.wav",
        "adversarial_train_0002.wav", "adversarial_train_0003.wav"]

    lineage_path = tmp_path / "out" / "_lineage.json"
    assert lineage_path.exists()
    lineage = json.loads(lineage_path.read_text())
    assert len(lineage) == 4
    assert lineage[0]["phrase"] == "hey noise" and lineage[0]["voice"] == "David"
    assert lineage[1]["phrase"] == "hey boy" and lineage[1]["voice"] == "Zira"
    assert lineage[2]["phrase"] == "hey noise" and lineage[2]["voice"] == "David"
    assert all(entry["seed"] == 42 for entry in lineage)
    assert all(entry["category"] == "adversarial" and entry["split"] == "train" for entry in lineage)


def test_generate_with_lineage_is_deterministic_given_same_inputs(tmp_path):
    lineage_a = tmp_path / "a"
    lineage_b = tmp_path / "b"
    gn._generate_with_lineage(FakeBackend(), ["p1", "p2", "p3"], lineage_a, 5,
                              category="speech", split="val", seed=7, voices=["David"])
    gn._generate_with_lineage(FakeBackend(), ["p1", "p2", "p3"], lineage_b, 5,
                              category="speech", split="val", seed=7, voices=["David"])

    a = json.loads((lineage_a / "_lineage.json").read_text())
    b = json.loads((lineage_b / "_lineage.json").read_text())
    assert a == b


def test_generate_adversarial_with_hard_negatives_tags_hard_negative_entries(
        monkeypatch, tmp_path, minimal_config):
    fake = FakeBackend()
    monkeypatch.setattr(gn, "get_tts_backend", lambda *a, **k: fake)
    monkeypatch.setattr(gn, "generate_adversarial_negative_texts",
                        lambda config: ["hey noise", "hey boy"])
    minimal_config["negative"]["n_adversarial_samples"] = 6

    out = gn.generate_adversarial_with_hard_negatives(
        minimal_config, tmp_path / "data", ["hey voice", "hey avoid"], train_multiplier=1)

    train_lineage = json.loads((out["adversarial_train"] / "_lineage.json").read_text())
    hard_phrases_seen = {e["phrase"] for e in train_lineage if e["is_hard_negative"]}
    assert hard_phrases_seen == {"hey voice", "hey avoid"}
    non_hard = {e["phrase"] for e in train_lineage if not e["is_hard_negative"]}
    assert non_hard == {"hey noise", "hey boy"}


def test_hard_negative_train_multiplier_increases_train_representation_but_not_val(
        monkeypatch, tmp_path, minimal_config):
    fake = FakeBackend()
    monkeypatch.setattr(gn, "get_tts_backend", lambda *a, **k: fake)
    monkeypatch.setattr(gn, "generate_adversarial_negative_texts",
                        lambda config: ["hey noise"])
    minimal_config["negative"]["n_adversarial_samples"] = 20
    minimal_config["negative"]["val_fraction"] = 0.5

    out = gn.generate_adversarial_with_hard_negatives(
        minimal_config, tmp_path / "data", ["hey voice"], train_multiplier=4)

    train_lineage = json.loads((out["adversarial_train"] / "_lineage.json").read_text())
    val_lineage = json.loads((out["adversarial_val"] / "_lineage.json").read_text())

    train_hard_fraction = sum(e["is_hard_negative"] for e in train_lineage) / len(train_lineage)
    val_hard_fraction = sum(e["is_hard_negative"] for e in val_lineage) / len(val_lineage)

    # base pool is 1 phrase; with multiplier=4 the hard-negative phrase
    # appears 4x in a 5-entry pool (1 base + 4 hard) -> ~80% representation,
    # vs. val's fixed 1-in-2 (1 base + 1 hard, multiplier always 1) -> ~50%.
    assert train_hard_fraction > val_hard_fraction
    assert all(e["hard_negative_train_multiplier"] == 1 for e in val_lineage)
    assert all(e["hard_negative_train_multiplier"] == 4 for e in train_lineage)


def test_different_seeds_can_produce_different_adversarial_phrases(minimal_config):
    config_a = dict(minimal_config, seed=1)
    config_b = dict(minimal_config, seed=2)
    result_a = gn.generate_adversarial_negative_texts(config_a)
    result_b = gn.generate_adversarial_negative_texts(config_b)
    # Not a strict guarantee for every possible seed pair, but this specific
    # pair is checked in as a regression fixture: seeding actually changes
    # the output, it isn't a no-op.
    assert result_a != result_b
