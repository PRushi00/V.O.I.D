#!/usr/bin/env python
"""Reproducible driver for the position-invariance generalization-fix
experiment series (see docs/generalization_fix.md for the full writeup).

BACKGROUND: a direct diagnostic (see the diagnostic script referenced in
docs/generalization_fix.md) found that models trained by this pipeline are
strongly POSITION-SENSITIVE: `training/extract_features.py`'s
`_fit_to_length` always left-aligns a clip's real audio within the
fixed-length training window (silence trails at the end for short clips).
Every training example - positive AND negative alike - was built this way,
so the classifier could learn "audio energy positioned at the START of the
window" as its decision cue instead of genuine, position-invariant
phonetic content. Measured effect: identical "hey void" audio scored 0.91
at its trained (offset=0) position and ~0.01 once shifted 200ms later in
the same window.

This script trains a candidate using RANDOMIZED within-window placement for
ALL training categories (via `extract_features.extract_all_features`'s
`jitter_seed`) while leaving validation features and the runtime evaluation
methodology completely unchanged, so results stay comparable to every
previously-reported number.

Usage (run from wakeword-training/, using .venv-training):
    python run_generalization_experiment.py --name gen2_exp01_jitter_only
    python run_generalization_experiment.py --name gen2_exp02_jitter_plus_hardneg --hard-negatives
    python run_generalization_experiment.py --name gen2_exp03_jitter_plus_hardneg_longer --hard-negatives --training-steps 1000

Every run:
  1. (Re)generates adversarial [+ speech if --regen-speech] raw/augmented
     data deterministically (seed=1234, optionally with the evidence-derived
     hard-negative phrase list from the prior FP-reduction pass).
  2. Re-extracts ALL "*_train" feature arrays (positive/adversarial/noise/
     speech) WITH position jitter; "*_val" arrays keep the original
     deterministic left-aligned placement.
  3. Trains a uniquely-named model (never overwrites hey_void.onnx/.pt or
     any prior experiment's files).
  4. Evaluates via the real openWakeWord streaming path (predict_clip +
     the window-aware content_influenced region from training/runtime_eval.py)
     across a full threshold sweep, against: adversarial_val, noise_val,
     speech_val, and the 23 genuine personal recordings (never touched).
  5. Writes models/<name>_generalization_eval.json with the complete
     result, including dataset sizes, seeds, and the model hash.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from training.augment import augment_directory
from training.config import load_config
from training.extract_features import (
    build_audio_features, extract_all_features, resolve_clip_seconds,
)
from training.generate_negative import (
    generate_adversarial_with_hard_negatives, generate_negative_samples,
)
from training.runtime_eval import evaluate_wav_via_predict_clip, sweep_thresholds
import train_void

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "models"

PROD_ONNX = MODEL_DIR / "hey_void.onnx"
PROD_PT = MODEL_DIR / "hey_void.pt"
PROD_ONNX_SHA256 = "7da1fc7c3f9de0094b38e3c8e3214595ffe5fb433acbc21587cb0cc61ab3a39e"
PROD_PT_SHA256 = "1f5b94e74f11778ad42068d35b93da4b4005dca6e3549101b02eb85a5c0fe01a"

RESERVED_NAMES = {
    "hey_void", "hey_void_16khz_fixed_500",
    "hey_void_experiment_negative_voices", "hey_void_experiment_negative_voices_500",
    "hey_void_fp_exp01", "hey_void_fp_exp02", "hey_void_fp_exp03",
    "hey_void_fp_exp04", "hey_void_fp_exp05",
}

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

# Evidence-derived hard-negative phrases from the prior FP-reduction pass's
# cluster analysis (see models/hey_void_16khz_fixed_500_fp_cluster_analysis.json):
# phrases containing a bare "-oid/-oyd" rime scored 100% FP regardless of
# voice/augmentation/duration.
RIME_ONLY = [
    "void", "lloyd", "boyd", "avoid", "devoid", "annoyed", "employed",
    "destroyed", "paranoid", "android", "tabloid", "asteroid",
]
HEY_NEAR_MISS = [
    "hey voice", "hey boy", "hey world", "hey voided", "hey voiced",
    "hey boyd", "hey void id", "hey v", "hey voidy", "hey avoid",
    "hey lloyd", "hey annoyed", "hey android", "hey devoid", "hey paranoid",
]
HARD_NEGATIVE_PHRASES = RIME_ONLY + HEY_NEAR_MISS
assert "hey void" not in [p.strip().lower() for p in HARD_NEGATIVE_PHRASES]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def verify_production_untouched() -> None:
    assert sha256_of(PROD_ONNX) == PROD_ONNX_SHA256, "PRODUCTION MODEL hey_void.onnx CHANGED - ABORT"
    assert sha256_of(PROD_PT) == PROD_PT_SHA256, "PRODUCTION MODEL hey_void.pt CHANGED - ABORT"


def regenerate_adversarial(config: dict, hard_negatives: list[str] | None,
                           train_multiplier: int) -> dict[str, Path]:
    for d in [DATA_DIR / "negative" / "adversarial" / "train",
             DATA_DIR / "negative" / "adversarial" / "val",
             DATA_DIR / "augmented" / "adversarial_train",
             DATA_DIR / "augmented" / "adversarial_val"]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    if hard_negatives:
        out = generate_adversarial_with_hard_negatives(
            config, DATA_DIR, hard_negatives, train_multiplier=train_multiplier)
    else:
        full = generate_negative_samples(config, DATA_DIR)
        out = {"adversarial_train": full["adversarial_train"], "adversarial_val": full["adversarial_val"]}

    aug_cfg = config["augmentation"]
    seed = config["seed"]
    for raw_key in ["adversarial_train", "adversarial_val"]:
        augment_directory(out[raw_key], DATA_DIR / "augmented" / raw_key, aug_cfg, seed)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="unique experiment/model name")
    ap.add_argument("--hard-negatives", action="store_true",
                    help="regenerate adversarial data with the evidence-derived hard-negative phrase list")
    ap.add_argument("--hard-negative-multiplier", type=int, default=1,
                    help="train-only oversampling multiplier for hard negatives (val always 1x)")
    ap.add_argument("--training-steps", type=int, default=500)
    ap.add_argument("--layer-dim", type=int, default=None, help="override model.layer_dim (default: config value)")
    ap.add_argument("--jitter-seed", type=int, default=20260913,
                    help="seed for position-jitter training-feature extraction; pass a negative number to disable jitter")
    ap.add_argument("--config", default=str(ROOT / "config" / "hey_void.yaml"))
    args = ap.parse_args()

    if args.name in RESERVED_NAMES:
        raise SystemExit(f"refusing to use reserved/protected model name {args.name!r}")

    config = load_config(args.config)
    verify_production_untouched()

    jitter_seed = None if args.jitter_seed < 0 else args.jitter_seed
    print(f"=== EXPERIMENT {args.name} ===")
    print(f"  hard_negatives={args.hard_negatives} (multiplier={args.hard_negative_multiplier})")
    print(f"  training_steps={args.training_steps}  layer_dim_override={args.layer_dim}")
    print(f"  jitter_seed={jitter_seed}  (None = jitter disabled, exact legacy left-aligned placement)")

    hard_negatives = HARD_NEGATIVE_PHRASES if args.hard_negatives else None
    regenerate_adversarial(config, hard_negatives, args.hard_negative_multiplier)
    print(f"  adversarial_train: {len(list((DATA_DIR/'augmented'/'adversarial_train').glob('*.wav')))} wavs")
    print(f"  adversarial_val:   {len(list((DATA_DIR/'augmented'/'adversarial_val').glob('*.wav')))} wavs")

    # Re-extract ALL 8 categories - jitter must apply to POSITIVE training
    # data too (that's literally what the diagnostic measured), not just
    # negatives, so positive_train/noise_train/speech_train are
    # re-extracted here as well even though their underlying WAVs are
    # untouched. Validation categories always keep jitter_seed's effect
    # OFF (extract_all_features only jitters "*_train" names).
    audio_features = build_audio_features()
    augmented_dirs = {
        "positive_train": DATA_DIR / "augmented" / "positive_train",
        "positive_val": DATA_DIR / "augmented" / "positive_val",
        "adversarial_train": DATA_DIR / "augmented" / "adversarial_train",
        "adversarial_val": DATA_DIR / "augmented" / "adversarial_val",
        "noise_train": DATA_DIR / "augmented" / "noise_train",
        "noise_val": DATA_DIR / "augmented" / "noise_val",
        "speech_train": DATA_DIR / "augmented" / "speech_train",
        "speech_val": DATA_DIR / "augmented" / "speech_val",
    }
    features = extract_all_features(config, augmented_dirs, audio_features=audio_features,
                                    jitter_seed=jitter_seed)
    features_dir = DATA_DIR / "features"
    for name, arr in features.items():
        np.save(features_dir / f"{name}.npy", arr)
        print(f"  features {name}: shape={arr.shape}")

    experiment_config = copy.deepcopy(config)
    experiment_config["model_name"] = args.name
    experiment_config["model"]["training_steps"] = args.training_steps
    if args.layer_dim is not None:
        experiment_config["model"]["layer_dim"] = args.layer_dim

    owwmodel, onnx_path = train_void.stage_train(experiment_config, DATA_DIR, MODEL_DIR, features=None)
    print(f"  TRAINED_MODEL_PATH: {onnx_path}")

    verify_production_untouched()
    print("  production model hash verified unchanged")

    onnx_path = MODEL_DIR / f"{args.name}.onnx"
    model_sha256 = sha256_of(onnx_path)
    print(f"  model SHA-256: {model_sha256}")

    from openwakeword.model import Model
    rt_model = Model(wakeword_models=[str(onnx_path)], inference_framework="onnx")
    model_key = list(rt_model.models.keys())[0]

    def category_max_scores(dir_path: Path) -> list[float]:
        wavs = sorted(Path(dir_path).glob("*.wav"))
        maxes = []
        for wav in wavs:
            rt_model.reset()
            r = evaluate_wav_via_predict_clip(rt_model, wav, model_key)
            maxes.append(r["content_influenced"]["max"])
        return maxes

    print("  scoring adversarial_val (real runtime path)...")
    adv_scores = category_max_scores(DATA_DIR / "augmented" / "adversarial_val")
    print("  scoring noise_val...")
    noise_scores = category_max_scores(DATA_DIR / "augmented" / "noise_val")
    print("  scoring speech_val...")
    speech_scores = category_max_scores(DATA_DIR / "augmented" / "speech_val")
    print("  scoring 23 personal recordings (never used in training)...")
    human_scores = category_max_scores(DATA_DIR / "personal_positive")

    human_sweep = sweep_thresholds(human_scores, THRESHOLDS)
    adv_sweep = sweep_thresholds(adv_scores, THRESHOLDS)
    speech_sweep = sweep_thresholds(speech_scores, THRESHOLDS)
    noise_sweep = sweep_thresholds(noise_scores, THRESHOLDS)

    results = {
        "experiment": args.name,
        "model_path": str(onnx_path),
        "model_sha256": model_sha256,
        "config": {
            "hard_negatives": args.hard_negatives,
            "hard_negative_multiplier": args.hard_negative_multiplier,
            "training_steps": args.training_steps,
            "layer_dim": experiment_config["model"]["layer_dim"],
            "n_blocks": experiment_config["model"]["n_blocks"],
            "jitter_seed": jitter_seed,
            "seed": config["seed"],
        },
        "dataset_sizes": {k: int(v.shape[0]) for k, v in features.items()},
        "n_human": len(human_scores), "n_adversarial_val": len(adv_scores),
        "n_noise_val": len(noise_scores), "n_speech_val": len(speech_scores),
        "sweep": {
            str(t): {"human_recall": human_sweep[t], "adv_fp": adv_sweep[t],
                    "speech_fp": speech_sweep[t], "noise_fp": noise_sweep[t]}
            for t in THRESHOLDS
        },
    }

    print(f"\n  {'thr':>5}  {'human_recall':>12}  {'adv_fp':>8}  {'speech_fp':>10}  {'noise_fp':>9}")
    for t in THRESHOLDS:
        v = results["sweep"][str(t)]
        print(f"  {t:5.2f}  {v['human_recall']*100:11.1f}%  {v['adv_fp']*100:7.1f}%  "
              f"{v['speech_fp']*100:9.1f}%  {v['noise_fp']*100:8.1f}%")

    out_path = MODEL_DIR / f"{args.name}_generalization_eval.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
