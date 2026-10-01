#!/usr/bin/env python
"""Gen 3 training + streaming evaluation driver.

Usage (from wakeword-training/, using .venv-training):
    python run_gen3_experiment.py --name gen3_exp01

Pipeline: load data/gen3/augmented/* WAVs -> extract frozen Whisper-encoder
features (position-jittered for *_train, left-aligned/fixed for *_val) ->
train a Gen3Classifier (conv + global-max-pool) with validation-based
checkpoint selection -> evaluate via the real sliding-window streaming
simulation (training/gen3_runtime_eval.py) against adversarial_val,
noise_val, speech_val, and the 23 genuine personal recordings (never used
in training) -> full threshold sweep, never cherry-picked.

Production safety: verifies hey_void.onnx/.pt/.onnx.data are byte-identical
to the known-good baseline both BEFORE and AFTER this run, and refuses to
use a reserved/production model name.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from training._sklearn_import_shim import install_sklearn_import_shim
install_sklearn_import_shim()

import numpy as np

from training.config import load_config
from training.gen3_model import train_gen3_model
from training.gen3_runtime_eval import evaluate_wav_streaming, sweep_thresholds
from training.whisper_features import build_whisper_encoder, extract_fixed_window_features

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "gen3"
MODEL_DIR = ROOT / "models"
PERSONAL_DIR = ROOT / "data" / "personal_positive"

PROD_ONNX = MODEL_DIR / "hey_void.onnx"
PROD_PT = MODEL_DIR / "hey_void.pt"
PROD_ONNX_DATA = MODEL_DIR / "hey_void.onnx.data"
PROD_ONNX_SHA256 = "7da1fc7c3f9de0094b38e3c8e3214595ffe5fb433acbc21587cb0cc61ab3a39e"
PROD_PT_SHA256 = "1f5b94e74f11778ad42068d35b93da4b4005dca6e3549101b02eb85a5c0fe01a"
PROD_ONNX_DATA_SHA256 = "ec2321b1c39cf48ec530bba09d3041ded94a67e504081a38419152066039d3fb"

RESERVED_NAMES = {"hey_void", "hey_void_16khz_fixed_500",
                  "hey_void_experiment_negative_voices", "hey_void_experiment_negative_voices_500"}

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def verify_production_untouched() -> None:
    assert sha256_of(PROD_ONNX) == PROD_ONNX_SHA256, "PRODUCTION hey_void.onnx CHANGED - ABORT"
    assert sha256_of(PROD_PT) == PROD_PT_SHA256, "PRODUCTION hey_void.pt CHANGED - ABORT"
    assert sha256_of(PROD_ONNX_DATA) == PROD_ONNX_DATA_SHA256, "PRODUCTION hey_void.onnx.data CHANGED - ABORT"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--training-steps", type=int, default=3000)
    ap.add_argument("--val-every-n-steps", type=int, default=25)
    ap.add_argument("--conv-channels", type=int, default=64)
    ap.add_argument("--hidden-dim", type=int, default=32)
    ap.add_argument("--clip-seconds", type=float, default=2.0)
    ap.add_argument("--jitter-seed", type=int, default=20260913)
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--config", default=str(ROOT / "config" / "hey_void.yaml"))
    args = ap.parse_args()

    if args.name in RESERVED_NAMES or not str(MODEL_DIR / f"{args.name}.onnx") != str(PROD_ONNX):
        pass
    assert MODEL_DIR / f"{args.name}.onnx" != PROD_ONNX, "refusing production model name"
    assert args.name not in RESERVED_NAMES, f"refusing reserved model name {args.name!r}"

    config = load_config(args.config)
    verify_production_untouched()
    print(f"=== GEN 3 EXPERIMENT {args.name} ===")
    print(f"  training_steps={args.training_steps} conv_channels={args.conv_channels} "
          f"hidden_dim={args.hidden_dim} clip_seconds={args.clip_seconds} compute_type={args.compute_type}")

    whisper = build_whisper_encoder(compute_type=args.compute_type)

    categories = ["positive_train", "positive_val", "adversarial_train", "adversarial_val",
                 "speech_train", "speech_val", "noise_train", "noise_val"]
    features = {}
    for name in categories:
        d = DATA_DIR / "augmented" / name
        wavs = sorted(str(p) for p in d.glob("*.wav"))
        rng = np.random.default_rng(args.jitter_seed + hash(name) % 10000) if name.endswith("_train") else None
        feats = extract_fixed_window_features(wavs, whisper, args.clip_seconds, rng=rng)
        features[name] = feats
        print(f"  features {name}: shape={feats.shape}")

    train_features = {k: v for k, v in features.items() if k.endswith("_train")}
    val_features = {k: v for k, v in features.items() if k.endswith("_val")}

    experiment_config = copy.deepcopy(config)
    experiment_config["model_name"] = args.name
    experiment_config["model"] = {
        "training_steps": args.training_steps, "val_every_n_steps": args.val_every_n_steps,
        "conv_channels": args.conv_channels, "hidden_dim": args.hidden_dim,
        "batch_size": 32, "learning_rate": 0.001,
    }

    clf, history, onnx_path = train_gen3_model(experiment_config, train_features, MODEL_DIR,
                                               val_features=val_features)
    print(f"  TRAINED: {onnx_path}")
    print(f"  selected_checkpoint_step: {history.get('selected_checkpoint_step')}")
    print(f"  selected_checkpoint_score: {history.get('selected_checkpoint_score')}")

    with open(MODEL_DIR / f"{args.name}_history.json", "w") as f:
        json.dump(history, f, indent=2)

    verify_production_untouched()
    print("  production model hash verified unchanged")

    model_sha256 = sha256_of(onnx_path)
    print(f"  model SHA-256: {model_sha256}")

    # streaming evaluation
    net = clf.model
    net.eval()

    def category_max_scores(dir_path):
        wavs = sorted(Path(dir_path).glob("*.wav"))
        out = []
        for wav in wavs:
            r = evaluate_wav_streaming(wav, whisper, net, clip_seconds=args.clip_seconds)
            out.append(r["content_influenced"]["max"])
        return out

    print("  scoring adversarial_val (streaming)...")
    adv = category_max_scores(DATA_DIR / "augmented" / "adversarial_val")
    print("  scoring noise_val (streaming)...")
    noise = category_max_scores(DATA_DIR / "augmented" / "noise_val")
    print("  scoring speech_val (streaming)...")
    speech = category_max_scores(DATA_DIR / "augmented" / "speech_val")
    print("  scoring 23 personal recordings (streaming, never used in training)...")
    human = category_max_scores(PERSONAL_DIR)

    sweep = {str(t): {"human_recall": sweep_thresholds(human, THRESHOLDS)[t],
                      "adv_fp": sweep_thresholds(adv, THRESHOLDS)[t],
                      "speech_fp": sweep_thresholds(speech, THRESHOLDS)[t],
                      "noise_fp": sweep_thresholds(noise, THRESHOLDS)[t]}
            for t in THRESHOLDS}

    print(f"\n  {'thr':>5}  {'human':>8}  {'adv_fp':>8}  {'speech_fp':>10}  {'noise_fp':>9}")
    for t in THRESHOLDS:
        v = sweep[str(t)]
        print(f"  {t:5.2f}  {v['human_recall']*100:7.1f}%  {v['adv_fp']*100:7.1f}%  "
              f"{v['speech_fp']*100:9.1f}%  {v['noise_fp']*100:8.1f}%")

    result = {
        "experiment": args.name, "model_sha256": model_sha256,
        "config": {"training_steps": args.training_steps, "conv_channels": args.conv_channels,
                  "hidden_dim": args.hidden_dim, "clip_seconds": args.clip_seconds,
                  "compute_type": args.compute_type, "seed": config["seed"]},
        "dataset_sizes": {k: int(v.shape[0]) for k, v in features.items()},
        "n_human": len(human), "n_adversarial_val": len(adv),
        "n_noise_val": len(noise), "n_speech_val": len(speech),
        "sweep": sweep,
    }
    with open(MODEL_DIR / f"{args.name}_gen3_eval.json", "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nwrote {args.name}_gen3_eval.json")


if __name__ == "__main__":
    main()
