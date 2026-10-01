"""Negative sample generation: phonetically-adversarial phrases, synthetic
noise, and neutral spoken sentences.

Reuses openwakeword's OWN adversarial-phrase generator
(openwakeword.data.generate_adversarial_texts, the same function
openwakeword's train.py uses) rather than inventing a different phonetic
similarity heuristic. That function needs only the lightweight `pronouncing`
package for in-vocabulary words; it falls back to downloading and importing
`deep-phonemizer` for out-of-vocabulary words, which this project does NOT
install (kept out of the minimal dependency set - see README). Both words in
"hey void" are common English words and are expected to be in the CMU
pronunciation dictionary; this module verifies that explicitly and raises a
clear error instead of letting an unexpected out-of-vocabulary word trigger a
surprise network download and a missing-package crash.

Negative audio for "ordinary speech"/"background noise" is synthesized here
rather than sourced from an external corpus - synthetic white/pink noise
(pure numpy, no license or download concerns) and TTS-spoken neutral
sentences. This is explicitly a development-scale stand-in, not a substitute
for a real curated negative corpus (openwakeword's own pretrained models use
~30,000 hours of it) - see the evaluation report's limitations section.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from training.tts_backends import get_tts_backend, voice_tokens_for


class NegativeDataError(RuntimeError):
    """Raised instead of silently proceeding with a broken negative-data
    setup (e.g. an out-of-vocabulary target-phrase word)."""


def _check_words_in_pronouncing_dict(phrase: str) -> None:
    try:
        import pronouncing
    except ImportError as exc:
        raise NegativeDataError(
            "the 'pronouncing' package is required for adversarial negative "
            "phrase generation (pip install pronouncing)") from exc
    missing = [w for w in phrase.split() if not pronouncing.phones_for_word(w)]
    if missing:
        raise NegativeDataError(
            f"word(s) {missing} from target_phrase {phrase!r} are not in the "
            f"CMU pronouncing dictionary. openwakeword.data."
            f"generate_adversarial_texts would fall back to downloading and "
            f"importing the 'deep-phonemizer' package for these, which this "
            f"project deliberately does not install (see README). Either add "
            f"deep-phonemizer to requirements.txt, or adjust target_phrase.")


def generate_adversarial_negative_texts(config: dict) -> list[str]:
    """Phonetically-similar phrases via openwakeword's own generator, plus
    any explicitly configured custom negative phrases. Never includes the
    actual target phrase.

    openwakeword.data.generate_adversarial_texts() draws from the GLOBAL
    np.random state and accepts no seed parameter of its own (confirmed by
    reading its source) - previously this meant the resulting phrase set was
    not reproducible even by re-running this function against the same
    config. Seeding the global np.random state with config["seed"]
    immediately before the call (and nowhere else in this module) makes the
    generated phrase set deterministic given the same config, at the cost of
    the well-known side effect of reseeding the process-wide RNG - acceptable
    here since this runs inside the one-shot `--generate` data-preparation
    stage, not any long-lived process.
    """
    from openwakeword.data import generate_adversarial_texts
    import numpy as np

    phrase = config["target_phrase"]
    _check_words_in_pronouncing_dict(phrase)
    neg_cfg = config["negative"]

    np.random.seed(config["seed"])
    generated = generate_adversarial_texts(
        input_text=phrase,
        N=neg_cfg["n_adversarial_phrases"],
        include_partial_phrase=1.0,
        include_input_words=0.2,
    )
    custom = list(neg_cfg.get("custom_negative_phrases") or [])
    phrases = [p for p in (generated + custom) if p.strip().lower() != phrase.strip().lower()]
    if not phrases:
        raise NegativeDataError(
            "no adversarial negative phrases were produced - refusing to "
            "continue with an empty negative-phrase set")
    return phrases


def _generate_with_lineage(backend, texts: list[str], output_dir: str | Path, count: int, *,
                           category: str, split: str, seed: int, voices: list[str] | None,
                           source: str = "sapi_tts") -> list[Path]:
    """Wraps backend.generate() with deterministic, index-based file names
    and writes a JSON metadata sidecar (`_lineage.json`) recording exactly
    which phrase and voice each generated file corresponds to.

    This exists because a false-positive investigation into the adversarial
    validation set found its lineage permanently unrecoverable: files were
    named with random UUIDs at generation time (see the previous
    `backend.generate(texts, d, count)` call with no `file_names=`), so once
    written there was no way to determine which of the N generated phrases
    or which voice a given on-disk file corresponded to. Deterministic,
    index-based file names plus this sidecar close that gap for all
    NEWLY-generated negative data; the existing UUID-named files this
    replaces cannot be retroactively attributed and were analyzed at a
    coarser (duration/augmentation/score) granularity instead.

    Only regenerates audio if the directory doesn't already have (most of)
    the expected count, matching the pre-existing skip-if-enough-samples
    behavior - but the metadata sidecar is always (re)written so it never
    silently drifts out of sync with what generated the files actually on
    disk. Callers are responsible for clearing `output_dir` first if they
    want a clean, fully-attributable regeneration (mixing old UUID-named
    files with new deterministically-named ones in the same directory would
    produce a sidecar that only covers the new files).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_names = [f"{category}_{split}_{i:04d}.wav" for i in range(count)]
    tokens = voice_tokens_for(voices, count)

    existing = len(list(output_dir.glob("*.wav")))
    if existing < int(0.95 * count):
        backend.generate(texts, output_dir, count, file_names=file_names)

    metadata = [
        {"index": i, "filename": file_names[i],
         "phrase": texts[i % len(texts)] if texts else None,
         "voice": tokens[i], "category": category, "split": split,
         "seed": seed, "source": source}
        for i in range(count)
    ]
    with open(output_dir / "_lineage.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return [output_dir / n for n in file_names]


def generate_adversarial_with_hard_negatives(
        config: dict, data_dir: str | Path, hard_negative_phrases: list[str], *,
        train_multiplier: int = 1) -> dict[str, Path]:
    """Regenerates ONLY the adversarial category (train + val) for the
    false-positive-reduction experiment loop: the same phonetically-generated
    + custom_negative_phrases phrase pool `generate_negative_samples()` uses,
    PLUS an explicit hard-negative phrase list curated from false-positive
    cluster analysis.

    `train_multiplier` controls how many extra times each hard-negative
    phrase is repeated in the TRAIN phrase pool (an oversampling knob for
    controlled-ratio experiments) - the VALIDATION phrase pool always uses
    multiplier=1 regardless, so validation composition stays fixed and
    comparable across every experiment that varies train_multiplier, and
    oversampling never leaks into what's being measured against.

    Every generated file's lineage sidecar records `is_hard_negative` and
    the multiplier used, so the actual on-disk train/val composition is
    always independently verifiable from `_lineage.json`, not just asserted.
    """
    neg_cfg = config["negative"]
    data_dir = Path(data_dir)
    val_fraction = neg_cfg.get("val_fraction", 0.25)
    sr = config["features"]["sample_rate"]
    voices = config["positive"].get("voices") or None
    backend = get_tts_backend(
        neg_cfg["tts_backend"], sample_rate=sr, voices=voices,
        piper_sample_generator_path=config["positive"].get("piper_sample_generator_path"),
    )

    base_phrases = generate_adversarial_negative_texts(config)
    hard_set = {p.strip().lower() for p in hard_negative_phrases}
    out: dict[str, Path] = {}

    for split, count, multiplier in (
        ("train", neg_cfg["n_adversarial_samples"], train_multiplier),
        ("val", max(1, int(neg_cfg["n_adversarial_samples"] * val_fraction)), 1),
    ):
        phrases = base_phrases + list(hard_negative_phrases) * multiplier
        d = data_dir / "negative" / "adversarial" / split
        d.mkdir(parents=True, exist_ok=True)
        file_names = [f"adversarial_{split}_{i:04d}.wav" for i in range(count)]
        tokens = voice_tokens_for(voices, count)

        backend.generate(phrases, d, count, file_names=file_names)

        metadata = [
            {"index": i, "filename": file_names[i], "phrase": phrases[i % len(phrases)],
             "voice": tokens[i], "category": "adversarial", "split": split,
             "seed": config["seed"], "source": "sapi_tts",
             "is_hard_negative": phrases[i % len(phrases)].strip().lower() in hard_set,
             "hard_negative_train_multiplier": multiplier}
            for i in range(count)
        ]
        with open(d / "_lineage.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        out[f"adversarial_{split}"] = d

    return out


def generate_negative_samples(config: dict, data_dir: str | Path) -> dict[str, Path]:
    """Generates negative/{adversarial,noise,speech}/{train,val} WAV
    directories. Train and val clips are generated independently (not a
    post-hoc split of the same clips), so augmentation later never leaks a
    variant of a validation clip into training or vice versa. Returns a
    flat dict of "{category}_{split}" -> directory Path."""
    neg_cfg = config["negative"]
    data_dir = Path(data_dir)
    val_fraction = neg_cfg.get("val_fraction", 0.25)
    sr = config["features"]["sample_rate"]
    # Reuse the SAME voice list as positive generation (not a separate
    # negative.voices config key) so the two classes' speaker distributions
    # stay symmetric by construction - previously this backend was built
    # with no voices= at all, so every adversarial/speech negative used a
    # single default SAPI voice while positives cycled through
    # positive.voices, a confirmed contributor to the adversarial-category
    # false-positive concentration found in prior error analysis.
    backend = get_tts_backend(
        neg_cfg["tts_backend"], sample_rate=sr,
        voices=config["positive"].get("voices") or None,
        piper_sample_generator_path=config["positive"].get("piper_sample_generator_path"),
    )

    out: dict[str, Path] = {}

    # 1) Phonetically-adversarial phrases, spoken.
    phrases = None

    def _adversarial_phrases():
        nonlocal phrases
        if phrases is None:
            phrases = generate_adversarial_negative_texts(config)
        return phrases

    voices = config["positive"].get("voices") or None
    for split, count in (("train", neg_cfg["n_adversarial_samples"]),
                        ("val", max(1, int(neg_cfg["n_adversarial_samples"] * val_fraction)))):
        d = data_dir / "negative" / "adversarial" / split
        d.mkdir(parents=True, exist_ok=True)
        _generate_with_lineage(backend, _adversarial_phrases(), d, count,
                               category="adversarial", split=split,
                               seed=config["seed"], voices=voices)
        out[f"adversarial_{split}"] = d

    # 2) Synthetic noise (white + approximate pink), no TTS involved.
    for split, count, seed_offset in (("train", neg_cfg["n_noise_samples"], 0),
                                      ("val", max(1, int(neg_cfg["n_noise_samples"] * val_fraction)), 1)):
        d = data_dir / "negative" / "noise" / split
        d.mkdir(parents=True, exist_ok=True)
        if len(list(d.glob("*.wav"))) < int(0.95 * count):
            _generate_noise_clips(
                d, count, sample_rate=sr,
                duration_s=config["features"]["clip_seconds"],
                seed=config["seed"] + seed_offset,
            )
        out[f"noise_{split}"] = d

    # 3) Neutral spoken sentences (a speech-negative stand-in).
    sentences = list(neg_cfg.get("neutral_sentences") or [])
    if not sentences:
        raise NegativeDataError(
            "negative.neutral_sentences is empty; cannot generate "
            "neutral-speech negatives")
    for split, count in (("train", neg_cfg["n_neutral_speech_samples"]),
                        ("val", max(1, int(neg_cfg["n_neutral_speech_samples"] * val_fraction)))):
        d = data_dir / "negative" / "speech" / split
        d.mkdir(parents=True, exist_ok=True)
        _generate_with_lineage(backend, sentences, d, count,
                               category="speech", split=split,
                               seed=config["seed"], voices=voices)
        out[f"speech_{split}"] = d

    return out


def _generate_noise_clips(output_dir: Path, n_samples: int, *, sample_rate: int,
                          duration_s: float, seed: int) -> None:
    """Deterministic (seeded) synthetic white/pink noise WAV clips. Pink
    noise here is an approximate 1/f shaping via cumulative summation of
    white noise, not a scientifically exact pink-noise model - adequate for
    a development-scale negative set, not claimed as acoustically precise."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    n_frames = int(sample_rate * duration_s)
    for i in range(n_samples):
        if i % 2 == 0:
            samples = rng.standard_normal(n_frames)
        else:
            white = rng.standard_normal(n_frames)
            samples = np.cumsum(white)
            samples = samples - np.mean(samples)
        samples = samples / (np.max(np.abs(samples)) + 1e-9)
        pcm = (samples * 0.3 * 32767).astype(np.int16)  # -10 dBFS-ish headroom
        out_path = output_dir / f"noise_{i:04d}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())
