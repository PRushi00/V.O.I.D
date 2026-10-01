#!/usr/bin/env python
"""Builds the Gen 3 dataset: positive, adversarial (isolated confounds +
confounds embedded naturally in sentences + openWakeWord's own phonetic
generator), speech, and noise - all isolated under data/gen3/, entirely
separate from Gen 2's data/ directories so Gen 2 remains an untouched
research baseline.

Reuses the already-validated SAPI generation, audio-validation, and
augmentation machinery from the Gen 2 pipeline (training/generate_negative.py,
training/generate_positive.py, training/augment.py) - only the OUTPUT
LOCATION and phrase composition are new for Gen 3, not the underlying
generation mechanics, which were already hardened by the 16kHz sample-rate
fix and lineage-tracking work.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from training._sklearn_import_shim import install_sklearn_import_shim
install_sklearn_import_shim()

from training.augment import augment_directory
from training.config import load_config
from training.generate_negative import _generate_with_lineage, generate_adversarial_negative_texts
from training.generate_positive import generate_positive_samples
from training.tts_backends import get_tts_backend, voice_tokens_for

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "gen3"

# Isolated confound words/phrases (never the literal target phrase).
RIME_ONLY = [
    "void", "lloyd", "boyd", "avoid", "devoid", "annoyed", "employed",
    "destroyed", "paranoid", "android", "tabloid", "asteroid",
]
HEY_NEAR_MISS = [
    "hey voice", "hey boy", "hey world", "hey voided", "hey voiced",
    "hey boyd", "hey void id", "hey v", "hey voidy", "hey avoid",
    "hey lloyd", "hey annoyed", "hey android", "hey devoid", "hey paranoid",
]

# NEW for Gen 3: the same confound roots embedded naturally in ordinary
# sentences, not just spoken in isolation - a genuinely different negative
# signal from Gen 2's isolated-phrase-only adversarial set.
NATURAL_CONFOUND_SENTENCES = [
    "i would like to avoid that mistake",
    "please don't annoy me right now",
    "the boy ran home quickly",
    "her voice was very clear on the phone",
    "he tried to avoid the crowd downtown",
    "the android phone rang loudly on the desk",
    "that was quite a clever ploy",
    "they were overjoyed with the results",
    "the paranoid man locked every door twice",
    "the asteroid passed close to earth last night",
    "lloyd works at the bank downtown",
    "please don't destroy the evidence",
    "the tabloid printed another wild story",
    "she felt devoid of any energy today",
    "he was employed at the factory for years",
    "the toy fell off the shelf and broke",
]

ADVERSARIAL_PHRASES = RIME_ONLY + HEY_NEAR_MISS + NATURAL_CONFOUND_SENTENCES
assert "hey void" not in [p.strip().lower() for p in ADVERSARIAL_PHRASES]


def build(config_path: str, n_positive_train=200, n_positive_val=50,
         n_adversarial_train=400, n_adversarial_val=100,
         n_speech_train=100, n_speech_val=25,
         n_noise_train=200, n_noise_val=50) -> None:
    config = load_config(config_path)

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)

    # 1) Positive - reuse generate_positive_samples exactly (already gives
    # SAPI rate jitter across David/Zira; position variety is handled at
    # the feature-extraction stage via placement jitter, not here).
    pos_config = copy.deepcopy(config)
    pos_config["positive"]["n_samples"] = n_positive_train
    pos_config["positive"]["n_samples_val"] = n_positive_val
    pos_out = generate_positive_samples(pos_config, DATA_DIR)
    print(f"positive: train={len(list(pos_out['train'].glob('*.wav')))} "
          f"val={len(list(pos_out['val'].glob('*.wav')))}")

    # 2) Adversarial - isolated confounds + natural-sentence confounds +
    # openWakeWord's own phonetic generator, ALL in one category, lineage-tracked.
    sr = config["features"]["sample_rate"]
    voices = config["positive"].get("voices") or None
    backend = get_tts_backend(config["negative"]["tts_backend"], sample_rate=sr, voices=voices,
                              piper_sample_generator_path=config["positive"].get("piper_sample_generator_path"))
    organic_phrases = generate_adversarial_negative_texts(config)
    full_adversarial_pool = organic_phrases + ADVERSARIAL_PHRASES

    for split, count in [("train", n_adversarial_train), ("val", n_adversarial_val)]:
        d = DATA_DIR / "negative" / "adversarial" / split
        d.mkdir(parents=True, exist_ok=True)
        _generate_with_lineage(backend, full_adversarial_pool, d, count,
                               category="adversarial", split=split,
                               seed=config["seed"], voices=voices)
    print(f"adversarial pool size: {len(full_adversarial_pool)} phrases "
         f"({len(organic_phrases)} organic + {len(ADVERSARIAL_PHRASES)} curated)")

    # 3) Speech (neutral sentences) - reuse Gen 2's existing sentence list,
    # same mechanism as generate_negative.py's speech category.
    sentences = list(config["negative"].get("neutral_sentences") or [])
    for split, count in [("train", n_speech_train), ("val", n_speech_val)]:
        d = DATA_DIR / "negative" / "speech" / split
        d.mkdir(parents=True, exist_ok=True)
        _generate_with_lineage(backend, sentences, d, count,
                               category="speech", split=split,
                               seed=config["seed"], voices=voices)

    # 4) Noise - identical deterministic synthetic generator Gen 2 used
    # (never the problem; no reason to change it).
    from training.generate_negative import _generate_noise_clips
    for split, count, seed_offset in [("train", n_noise_train, 0), ("val", n_noise_val, 1)]:
        d = DATA_DIR / "negative" / "noise" / split
        d.mkdir(parents=True, exist_ok=True)
        _generate_noise_clips(d, count, sample_rate=sr,
                              duration_s=config["features"]["clip_seconds"],
                              seed=config["seed"] + seed_offset)

    # 5) Augment everything (reuses Gen 2's now-properly-seeded augment_directory).
    aug_cfg = config["augmentation"]
    seed = config["seed"]
    categories = {
        "positive_train": pos_out["train"], "positive_val": pos_out["val"],
        "adversarial_train": DATA_DIR / "negative" / "adversarial" / "train",
        "adversarial_val": DATA_DIR / "negative" / "adversarial" / "val",
        "speech_train": DATA_DIR / "negative" / "speech" / "train",
        "speech_val": DATA_DIR / "negative" / "speech" / "val",
        "noise_train": DATA_DIR / "negative" / "noise" / "train",
        "noise_val": DATA_DIR / "negative" / "noise" / "val",
    }
    for name, raw_dir in categories.items():
        aug_dir = DATA_DIR / "augmented" / name
        written = augment_directory(raw_dir, aug_dir, aug_cfg, seed)
        print(f"augmented {name}: {len(written)} files")

    # 6) Dataset-integrity gate (fail loud, never trust metadata implicitly).
    from training.audio_validation import scan_directory_audio_format
    all_valid = True
    for name in categories:
        report = scan_directory_audio_format(DATA_DIR / "augmented" / name)
        all_valid = all_valid and report["all_valid"]
        print(f"  integrity[{name}]: count={report['count']} all_valid={report['all_valid']} "
              f"sr_dist={report['sample_rate_distribution']}")
    if not all_valid:
        raise SystemExit("DATASET INTEGRITY GATE FAILED - see report above")
    print("\nDataset integrity gate: PASSED (100% 16kHz/mono/16-bit PCM)")


if __name__ == "__main__":
    build(str(ROOT / "config" / "hey_void.yaml"))
