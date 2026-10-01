#!/usr/bin/env python
"""Builds and trains a STAGE-2 VERIFIER: a separate, larger-capacity
classifier trained ONLY on the specific hard discrimination task that the
main (Stage 1) detector cannot resolve - "hey void" vs. the confirmed
"-oid/-oyd" phonetic confound family (void, lloyd, avoid, hey voice, hey
boy, etc; see hard_negatives.py's HARD_NEGATIVE_PHRASES).

WHY a second stage: an extensive, methodologically-controlled experiment
series (see docs/generalization_fix.md) fixed two real bugs in the Stage-1
pipeline - position-sensitivity (via training-time placement jitter) and
training instability (via validation-based checkpoint selection) - and
still found a reproducible ~35-40% adversarial false-positive CEILING for
the single 32-unit DNN classifier, confirmed via a 4000-step, 161-checkpoint
systematic sweep (not a lucky guess). This is a genuine capacity/task
mismatch: the same tiny network is being asked to (a) recognize "hey void"
robustly against arbitrary background speech/noise AND (b) finely
discriminate it from acoustically near-identical short phrases. Splitting
these into two purpose-built stages - a high-recall, low-precision Stage 1
(the existing detector) and a narrow, high-precision Stage 2 verifier that
only runs on Stage-1 candidates - lets each stage be good at one job.

This script only TRAINS AND EVALUATES the verifier concept OFFLINE via the
real runtime (predict_clip) path for both stages independently, then
simulates the two-stage decision post-hoc. It does NOT wire anything into
void/voice/wake.py or any other production runtime file - see the final
report for what an actual runtime integration would require.

Usage:
    python build_stage2_verifier.py --name gen2_stage2_verifier01
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
    build_audio_features, extract_features_for_wavs, resolve_clip_seconds,
)
from training.generate_negative import _generate_with_lineage
from training.tts_backends import get_tts_backend
from training.runtime_eval import evaluate_wav_via_predict_clip, sweep_thresholds
import train_void

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "models"
STAGE2_DIR = DATA_DIR / "stage2_verifier"

PROD_ONNX = MODEL_DIR / "hey_void.onnx"
PROD_PT = MODEL_DIR / "hey_void.pt"
PROD_ONNX_SHA256 = "7da1fc7c3f9de0094b38e3c8e3214595ffe5fb433acbc21587cb0cc61ab3a39e"
PROD_PT_SHA256 = "1f5b94e74f11778ad42068d35b93da4b4005dca6e3549101b02eb85a5c0fe01a"

RIME_ONLY = [
    "void", "lloyd", "boyd", "avoid", "devoid", "annoyed", "employed",
    "destroyed", "paranoid", "android", "tabloid", "asteroid",
]
HEY_NEAR_MISS = [
    "hey voice", "hey boy", "hey world", "hey voided", "hey voiced",
    "hey boyd", "hey void id", "hey v", "hey voidy", "hey avoid",
    "hey lloyd", "hey annoyed", "hey android", "hey devoid", "hey paranoid",
]
STAGE2_NEGATIVE_PHRASES = RIME_ONLY + HEY_NEAR_MISS
assert "hey void" not in [p.strip().lower() for p in STAGE2_NEGATIVE_PHRASES]

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def verify_production_untouched() -> None:
    assert sha256_of(PROD_ONNX) == PROD_ONNX_SHA256, "PRODUCTION MODEL hey_void.onnx CHANGED - ABORT"
    assert sha256_of(PROD_PT) == PROD_PT_SHA256, "PRODUCTION MODEL hey_void.pt CHANGED - ABORT"


def build_stage2_dataset(config: dict) -> None:
    """Builds a DEDICATED, SPECIALIST negative set - CONFOUND PHRASES ONLY
    (no generic phonetic-generator filler diluting the signal) - under
    data/stage2_verifier/, entirely separate from the Stage-1
    adversarial_train/val directories, so Stage 2's training/validation
    never touches or leaks into Stage 1's."""
    if STAGE2_DIR.exists():
        shutil.rmtree(STAGE2_DIR)
    STAGE2_DIR.mkdir(parents=True)

    sr = config["features"]["sample_rate"]
    voices = config["positive"].get("voices") or None
    backend = get_tts_backend(
        config["negative"]["tts_backend"], sample_rate=sr, voices=voices,
        piper_sample_generator_path=config["positive"].get("piper_sample_generator_path"))

    for d in [STAGE2_DIR / "raw" / "train", STAGE2_DIR / "raw" / "val",
             STAGE2_DIR / "augmented" / "train", STAGE2_DIR / "augmented" / "val"]:
        d.mkdir(parents=True, exist_ok=True)

    # Larger counts than the default adversarial category - this IS the
    # verifier's entire negative training signal, no generic filler to
    # dilute it, so it needs its own reasonably-sized corpus.
    for split, count in [("train", 400), ("val", 100)]:
        raw_dir = STAGE2_DIR / "raw" / split
        _generate_with_lineage(backend, STAGE2_NEGATIVE_PHRASES, raw_dir, count,
                               category="stage2_confound", split=split,
                               seed=config["seed"], voices=voices)
        augment_directory(raw_dir, STAGE2_DIR / "augmented" / split,
                          config["augmentation"], config["seed"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--layer-dim", type=int, default=128)
    ap.add_argument("--n-blocks", type=int, default=2)
    ap.add_argument("--training-steps", type=int, default=4000)
    ap.add_argument("--val-every-n-steps", type=int, default=25)
    ap.add_argument("--jitter-seed", type=int, default=20260913)
    ap.add_argument("--config", default=str(ROOT / "config" / "hey_void.yaml"))
    args = ap.parse_args()

    config = load_config(args.config)
    verify_production_untouched()

    print(f"=== STAGE 2 VERIFIER: {args.name} ===")
    print(f"  layer_dim={args.layer_dim} n_blocks={args.n_blocks} "
          f"training_steps={args.training_steps} (checkpoint-selected)")

    build_stage2_dataset(config)
    n_train = len(list((STAGE2_DIR / "augmented" / "train").glob("*.wav")))
    n_val = len(list((STAGE2_DIR / "augmented" / "val").glob("*.wav")))
    print(f"  stage2 negative train: {n_train} wavs, val: {n_val} wavs")

    # Stage 2 shares Stage 1's positive data (positive_train/val) - the
    # positive class is identical; only the NEGATIVE side is specialized.
    audio_features = build_audio_features()
    clip_seconds = resolve_clip_seconds(audio_features, target_frames=16,
                                        configured_seconds=config["features"]["clip_seconds"])
    sr = config["features"]["sample_rate"]

    positive_train_wavs = sorted(str(p) for p in (DATA_DIR / "augmented" / "positive_train").glob("*.wav"))
    positive_val_wavs = sorted(str(p) for p in (DATA_DIR / "augmented" / "positive_val").glob("*.wav"))
    stage2_neg_train_wavs = sorted(str(p) for p in (STAGE2_DIR / "augmented" / "train").glob("*.wav"))
    stage2_neg_val_wavs = sorted(str(p) for p in (STAGE2_DIR / "augmented" / "val").glob("*.wav"))

    rng_pos_train = np.random.default_rng(args.jitter_seed + 1)
    rng_neg_train = np.random.default_rng(args.jitter_seed + 2)

    positive_train_feats = extract_features_for_wavs(
        positive_train_wavs, audio_features, clip_seconds, sr, rng=rng_pos_train)
    stage2_neg_train_feats = extract_features_for_wavs(
        stage2_neg_train_wavs, audio_features, clip_seconds, sr, rng=rng_neg_train)
    positive_val_feats = extract_features_for_wavs(positive_val_wavs, audio_features, clip_seconds, sr)
    stage2_neg_val_feats = extract_features_for_wavs(stage2_neg_val_wavs, audio_features, clip_seconds, sr)

    print(f"  positive_train={positive_train_feats.shape} stage2_neg_train={stage2_neg_train_feats.shape}")
    print(f"  positive_val={positive_val_feats.shape} stage2_neg_val={stage2_neg_val_feats.shape}")

    train_features = {"positive_train": positive_train_feats, "stage2_confound_train": stage2_neg_train_feats}
    val_features = {"positive_val": positive_val_feats, "adversarial_val": stage2_neg_val_feats}

    stage2_config = copy.deepcopy(config)
    stage2_config["model_name"] = args.name
    stage2_config["model"]["training_steps"] = args.training_steps
    stage2_config["model"]["val_every_n_steps"] = args.val_every_n_steps
    stage2_config["model"]["layer_dim"] = args.layer_dim
    stage2_config["model"]["n_blocks"] = args.n_blocks

    from training.train import train_model
    owwmodel, history, onnx_path = train_model(
        stage2_config, train_features, MODEL_DIR, val_features=val_features)
    print(f"  TRAINED: {onnx_path}")
    print(f"  selected_checkpoint_step: {history.get('selected_checkpoint_step')}")
    print(f"  selected_checkpoint_score: {history.get('selected_checkpoint_score')}")

    import torch
    checkpoint_path = MODEL_DIR / f"{args.name}.pt"
    torch.save({
        "state_dict": owwmodel.model.state_dict(), "input_shape": owwmodel.input_shape,
        "model_type": stage2_config["model"]["model_type"],
        "layer_dim": args.layer_dim, "n_blocks": args.n_blocks,
    }, checkpoint_path)
    with open(MODEL_DIR / f"{args.name}_history.json", "w") as f:
        json.dump(history, f, indent=2)

    verify_production_untouched()
    print("  production model hash verified unchanged")

    # Evaluate the verifier ALONE (Stage 2 in isolation) via the real
    # runtime path against its own held-out confound val set, plus the
    # existing noise_val/speech_val/human recordings for completeness.
    from openwakeword.model import Model
    rt_model = Model(wakeword_models=[str(onnx_path)], inference_framework="onnx")
    model_key = list(rt_model.models.keys())[0]

    def category_max_scores(dir_path):
        wavs = sorted(Path(dir_path).glob("*.wav"))
        out = []
        for wav in wavs:
            rt_model.reset()
            r = evaluate_wav_via_predict_clip(rt_model, wav, model_key)
            out.append(r["content_influenced"]["max"])
        return out

    print("  scoring stage2 confound val set (this verifier's own hard task)...")
    confound = category_max_scores(STAGE2_DIR / "augmented" / "val")
    print("  scoring noise_val, speech_val, human (transfer to the general categories)...")
    noise = category_max_scores(DATA_DIR / "augmented" / "noise_val")
    speech = category_max_scores(DATA_DIR / "augmented" / "speech_val")
    human = category_max_scores(DATA_DIR / "personal_positive")

    sweep = {str(t): {"human_recall": sweep_thresholds(human, THRESHOLDS)[t],
                      "confound_fp": sweep_thresholds(confound, THRESHOLDS)[t],
                      "speech_fp": sweep_thresholds(speech, THRESHOLDS)[t],
                      "noise_fp": sweep_thresholds(noise, THRESHOLDS)[t]}
            for t in THRESHOLDS}
    print(f"\n  {'thr':>5}  {'human':>8}  {'confound_fp':>12}  {'speech_fp':>10}  {'noise_fp':>9}")
    for t in THRESHOLDS:
        v = sweep[str(t)]
        print(f"  {t:5.2f}  {v['human_recall']*100:7.1f}%  {v['confound_fp']*100:11.1f}%  "
              f"{v['speech_fp']*100:9.1f}%  {v['noise_fp']*100:8.1f}%")

    result = {"name": args.name, "model_sha256": sha256_of(onnx_path),
             "layer_dim": args.layer_dim, "n_blocks": args.n_blocks,
             "selected_checkpoint_step": history.get("selected_checkpoint_step"),
             "n_confound_val": len(confound), "sweep": sweep,
             "raw_scores": {"confound": confound, "noise": noise, "speech": speech, "human": human}}
    with open(MODEL_DIR / f"{args.name}_stage2_standalone_eval.json", "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nwrote {args.name}_stage2_standalone_eval.json")


if __name__ == "__main__":
    main()
