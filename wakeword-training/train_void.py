#!/usr/bin/env python
"""CLI for the "Hey V.O.I.D." wake-word training pipeline.

    python train_void.py --config config/hey_void.yaml --all

Stages (each can be run independently; --all runs them in order):
    --generate   synthesize positive + negative WAV clips
    --augment    apply audio augmentation, writing augmented WAV variants
    --features   extract openwakeword-compatible (16, 96) feature arrays
    --train      train the classifier and export models/hey_void.onnx
    --evaluate   report validation metrics (never claims production-ready)

Personalized training (explicit opt-in only - never runs unless asked):
    --train --personalized
        Trains using positive_train/adversarial_train/noise_train/
        speech_train PLUS positive_personal_train (never
        positive_personal_holdout). Writes to <model_name>_personalized.onnx/
        .pt/_history.json - never touches the normal <model_name>.onnx/.pt.
    --evaluate --personalized
        Evaluates the personalized checkpoint against the SAME, unchanged
        validation set (positive_val/adversarial_val/noise_val/speech_val) -
        identical logic to plain --evaluate, just pointed at the
        personalized checkpoint/report filenames.
    --evaluate-personal-holdout [--personalized]
        A separate, explicit stage that evaluates a model (the personalized
        one if --personalized is also given, else the normal one) against
        positive_personal_holdout ONLY - never mixed into the normal
        validation metrics above.

This script requires ONLY the isolated training venv described in README.md.
It never imports anything from V.O.I.D's own `void` package and never
requires the V.O.I.D runtime/venv to be present or running.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from training.config import ConfigError, load_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("train_void")


def stage_generate(config: dict, data_dir: Path) -> None:
    from training.generate_negative import generate_negative_samples
    from training.generate_positive import generate_positive_samples

    logger.info("Generating positive samples...")
    pos = generate_positive_samples(config, data_dir)
    logger.info("  positive train: %s, positive val: %s", pos["train"], pos["val"])

    logger.info("Generating negative samples...")
    neg = generate_negative_samples(config, data_dir)
    for k, v in neg.items():
        logger.info("  negative %s: %s", k, v)


def _raw_categories(data_dir: Path) -> dict[str, Path]:
    return {
        "positive_train": data_dir / "positive" / "train",
        "positive_val": data_dir / "positive" / "val",
        "adversarial_train": data_dir / "negative" / "adversarial" / "train",
        "adversarial_val": data_dir / "negative" / "adversarial" / "val",
        "noise_train": data_dir / "negative" / "noise" / "train",
        "noise_val": data_dir / "negative" / "noise" / "val",
        "speech_train": data_dir / "negative" / "speech" / "train",
        "speech_val": data_dir / "negative" / "speech" / "val",
    }


def stage_augment(config: dict, data_dir: Path) -> dict[str, Path]:
    from training.augment import augment_directory

    aug_cfg = config["augmentation"]
    augmented_dir = data_dir / "augmented"
    out = {}
    for name, raw_dir in _raw_categories(data_dir).items():
        if not raw_dir.exists() or not any(raw_dir.glob("*.wav")):
            logger.warning("skipping augmentation for %s: no raw clips at %s", name, raw_dir)
            out[name] = augmented_dir / name
            (augmented_dir / name).mkdir(parents=True, exist_ok=True)
            continue
        logger.info("Augmenting %s...", name)
        target = augmented_dir / name
        augment_directory(raw_dir, target, aug_cfg, config["seed"])
        out[name] = target
    return out


def stage_features(config: dict, data_dir: Path) -> dict[str, "object"]:
    from training.extract_features import build_audio_features, extract_all_features

    augmented_dirs = {name: data_dir / "augmented" / name for name in _raw_categories(data_dir)}
    logger.info("Building openwakeword AudioFeatures (downloads shared backbone on first use)...")
    audio_features = build_audio_features()
    logger.info("Extracting features...")
    features = extract_all_features(config, augmented_dirs, audio_features=audio_features)

    features_dir = data_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    for name, arr in features.items():
        import numpy as np
        np.save(features_dir / f"{name}.npy", arr)
        logger.info("  %s: shape=%s -> %s", name, arr.shape, features_dir / f"{name}.npy")
    return features


def _load_saved_features(data_dir: Path) -> dict[str, "object"]:
    import numpy as np
    features_dir = data_dir / "features"
    out = {}
    for name in _raw_categories(data_dir):
        path = features_dir / f"{name}.npy"
        if path.exists():
            out[name] = np.load(path)
    if not out:
        raise SystemExit(
            f"no feature files found under {features_dir} - run --features first")
    return out


def stage_train(config: dict, data_dir: Path, model_dir: Path, features: dict | None = None,
                use_validation_checkpoint_selection: bool = True):
    from training.train import train_model

    features = features or _load_saved_features(data_dir)
    train_features = {k: v for k, v in features.items() if k.endswith("_train")}
    val_features = None
    if use_validation_checkpoint_selection:
        found = {k: v for k, v in features.items() if k.endswith("_val")}
        # Only enable checkpoint selection if a positive_val array actually
        # exists - a val-features dict with no positive examples can't
        # compute a recall term and would silently always prefer whichever
        # checkpoint happens to have the lowest FP (e.g. an all-zero-output
        # one), which is not a meaningful selection criterion.
        if found.get("positive_val") is not None and found["positive_val"].size:
            val_features = found
    owwmodel, history, onnx_path = train_model(config, train_features, model_dir,
                                               val_features=val_features)

    import torch
    # openwakeword.train.Model defines its inner network as a class LOCAL to
    # __init__ (unpicklable by reference), so the whole wrapper object can't
    # be torch.save()'d directly (confirmed by trying it - AttributeError:
    # "Can't pickle local object 'Model.__init__.<locals>.Net'"). Standard
    # PyTorch practice applies instead: save the state_dict plus the small
    # amount of metadata needed to reconstruct an identical Model() wrapper.
    checkpoint_path = model_dir / f"{config['model_name']}.pt"
    torch.save({
        "state_dict": owwmodel.model.state_dict(),
        "input_shape": owwmodel.input_shape,
        "model_type": config["model"]["model_type"],
        "layer_dim": config["model"]["layer_dim"],
        "n_blocks": config["model"]["n_blocks"],
    }, checkpoint_path)

    history_path = model_dir / f"{config['model_name']}_history.json"
    with open(history_path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2)
    logger.info("Exported %s", onnx_path)
    logger.info("Checkpoint written to %s", checkpoint_path)
    logger.info("Training history written to %s", history_path)
    return owwmodel, onnx_path


def stage_evaluate(config: dict, data_dir: Path, model_dir: Path, owwmodel=None,
                   features: dict | None = None) -> dict:
    from training.evaluate import evaluate_model, format_report
    from training.extract_features import build_audio_features, resolve_clip_seconds

    features = features or _load_saved_features(data_dir)
    val_features = {k: v for k, v in features.items() if k.endswith("_val")}

    if owwmodel is None:
        import torch
        from openwakeword.train import Model as OWWModel

        checkpoint_path = model_dir / f"{config['model_name']}.pt"
        if not checkpoint_path.exists():
            raise SystemExit(
                f"no trained model in memory and no checkpoint at "
                f"{checkpoint_path} - run --train first (or --all)")
        checkpoint = torch.load(checkpoint_path, weights_only=True)
        owwmodel = OWWModel(
            n_classes=1, input_shape=checkpoint["input_shape"],
            model_type=checkpoint["model_type"],
            layer_dim=checkpoint["layer_dim"], n_blocks=checkpoint["n_blocks"])
        owwmodel.model.load_state_dict(checkpoint["state_dict"])

    audio_features = build_audio_features()
    clip_seconds = resolve_clip_seconds(
        audio_features, target_frames=16,
        configured_seconds=config["features"]["clip_seconds"])

    metrics = evaluate_model(config, owwmodel, val_features, clip_seconds)
    report_path = model_dir / f"{config['model_name']}_evaluation.json"
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
    print(format_report(metrics))
    logger.info("Evaluation report written to %s", report_path)
    return metrics


def stage_train_personalized(config: dict, data_dir: Path, model_dir: Path,
                             oversample_factor: int | None = None):
    """Explicit opt-in personalized training. Reuses stage_train() COMPLETELY
    UNCHANGED below - personalization is expressed only through the
    `features` dict (the existing 8 saved categories plus an OVERSAMPLED
    positive_personal_train, which stage_train()'s own existing
    `k.endswith("_train")` filter already picks up correctly - no filter
    logic is duplicated here) and through `config["model_name"]` (suffixed
    with "_personalized", so stage_train()'s existing checkpoint/history/
    onnx-naming logic writes to new, distinct files without stage_train()
    needing any awareness of personalization at all).

    Strategy B (approved design analysis): positive_personal_train is
    physically duplicated `oversample_factor` times (default
    training.personal_positive.PERSONAL_OVERSAMPLE_FACTOR = 5, e.g. 54 -> 270)
    via oversample_personal_train_features() - a pure data-preprocessing step.
    positive_train/adversarial_train/noise_train/speech_train are passed
    through UNCHANGED, at their original counts. training.train.py's
    sampling algorithm (rng.integers(...), uniform with replacement) is not
    modified in any way - it simply receives a larger positive_personal_train
    array as part of the same features dict it always has.

    positive_personal_holdout is never loaded or referenced here - it cannot
    enter training through this path because nothing ever puts it into the
    features dict passed to stage_train().
    """
    from training.personal_positive import (
        PERSONAL_OVERSAMPLE_FACTOR, PERSONAL_TRAIN_FEATURE_NAME,
        load_personal_train_features, oversample_personal_train_features,
    )

    factor = PERSONAL_OVERSAMPLE_FACTOR if oversample_factor is None else oversample_factor

    features = dict(_load_saved_features(data_dir))   # existing 8 keys, unchanged; copy so we don't mutate a cached dict
    personal_raw = load_personal_train_features(data_dir)
    features[PERSONAL_TRAIN_FEATURE_NAME] = oversample_personal_train_features(
        personal_raw, factor)

    personalized_config = dict(config)
    personalized_config["model_name"] = f"{config['model_name']}_personalized"

    logger.info(
        "Personalized training: dataset = %s (positive_personal_train "
        "oversampled x%d: %d -> %d)",
        sorted(k for k in features if k.endswith("_train")), factor,
        len(personal_raw), len(features[PERSONAL_TRAIN_FEATURE_NAME]))
    return stage_train(personalized_config, data_dir, model_dir, features=features)


def stage_personal_holdout_evaluate(config: dict, data_dir: Path, model_dir: Path,
                                    owwmodel=None) -> dict:
    """A separate, explicit evaluation stage against positive_personal_holdout
    ONLY - never merged into stage_evaluate()'s normal validation metrics,
    which remain positive_val/adversarial_val/noise_val/speech_val exactly as
    before. Writes its own distinct report file
    (<model_name>_personal_holdout_evaluation.json), never the normal
    <model_name>_evaluation.json.
    """
    from training.personal_positive import (
        evaluate_personal_holdout, load_personal_holdout_features,
    )

    holdout_features = load_personal_holdout_features(data_dir)

    if owwmodel is None:
        import torch
        from openwakeword.train import Model as OWWModel

        checkpoint_path = model_dir / f"{config['model_name']}.pt"
        if not checkpoint_path.exists():
            raise SystemExit(
                f"no trained model in memory and no checkpoint at "
                f"{checkpoint_path} - run --train first (or --all)")
        checkpoint = torch.load(str(checkpoint_path), weights_only=True)
        owwmodel = OWWModel(
            n_classes=1, input_shape=checkpoint["input_shape"],
            model_type=checkpoint["model_type"],
            layer_dim=checkpoint["layer_dim"], n_blocks=checkpoint["n_blocks"])
        owwmodel.model.load_state_dict(checkpoint["state_dict"])

    net = owwmodel.model
    net.eval()
    threshold = config["evaluation"]["threshold"]
    result = evaluate_personal_holdout(
        net, holdout_features, threshold,
        extra_thresholds=[0.1, 0.3, 0.5, 0.7, 0.9])

    report_path = model_dir / f"{config['model_name']}_personal_holdout_evaluation.json"
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    print(f"=== Personal holdout evaluation ({config['model_name']}) ===")
    print(f"n_holdout={result['n_holdout']}  threshold={result['threshold']}")
    print(f"true_positives={result['true_positives_at_threshold']}  "
         f"false_negatives={result['false_negatives_at_threshold']}  "
         f"recall={result['recall_at_threshold']}")
    print(f"scores={result['scores']}")
    for row in result.get("threshold_sweep", []):
        print(f"  t={row['threshold']}: TP={row['true_positives']} "
             f"FN={row['false_negatives']} recall={row['recall']}")
    logger.info("Personal holdout evaluation report written to %s", report_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="path to a training YAML config")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--augment", action="store_true")
    parser.add_argument("--features", action="store_true")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="run generate, augment, features, train, evaluate in order")
    parser.add_argument("--personalized", action="store_true",
                        help="opt-in: with --train, additionally train on "
                             "positive_personal_train and write to "
                             "<model_name>_personalized.onnx/.pt (never "
                             "touching the normal model files); with "
                             "--evaluate or --evaluate-personal-holdout, "
                             "target that personalized checkpoint instead "
                             "of the normal one")
    parser.add_argument("--evaluate-personal-holdout", action="store_true",
                        help="separate, explicit stage: evaluate a model "
                             "against positive_personal_holdout only (never "
                             "mixed into --evaluate's normal validation "
                             "metrics)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    data_dir = Path(config["data_dir"])
    if not data_dir.is_absolute():
        data_dir = (Path(args.config).resolve().parent.parent / config["data_dir"]).resolve()
    model_dir = Path(config["output_dir"])
    if not model_dir.is_absolute():
        model_dir = (Path(args.config).resolve().parent.parent / config["output_dir"]).resolve()

    ran_anything = False
    owwmodel = None
    features_cache: dict | None = None

    if args.generate or args.all:
        stage_generate(config, data_dir)
        ran_anything = True
    if args.augment or args.all:
        stage_augment(config, data_dir)
        ran_anything = True
    if args.features or args.all:
        features_cache = stage_features(config, data_dir)
        ran_anything = True
    if args.train or args.all:
        if args.personalized:
            owwmodel, _onnx_path = stage_train_personalized(config, data_dir, model_dir)
        else:
            owwmodel, _onnx_path = stage_train(config, data_dir, model_dir, features=features_cache)
        ran_anything = True
    if args.evaluate or args.all:
        eval_config = config
        if args.personalized:
            eval_config = dict(config)
            eval_config["model_name"] = f"{config['model_name']}_personalized"
        stage_evaluate(eval_config, data_dir, model_dir, owwmodel=owwmodel, features=features_cache)
        ran_anything = True
    if args.evaluate_personal_holdout:
        holdout_config = config
        if args.personalized:
            holdout_config = dict(config)
            holdout_config["model_name"] = f"{config['model_name']}_personalized"
        # Only reuse an in-memory owwmodel if it actually corresponds to the
        # checkpoint this stage is targeting (i.e. we just trained it with
        # the matching --personalized flag this same invocation); otherwise
        # let the stage load the right checkpoint from disk itself.
        in_memory_model = owwmodel if (args.train and args.personalized) else None
        stage_personal_holdout_evaluate(holdout_config, data_dir, model_dir,
                                        owwmodel=in_memory_model)
        ran_anything = True

    if not ran_anything:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
