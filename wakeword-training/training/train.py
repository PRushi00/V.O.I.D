"""Model training: reuses openwakeword's OWN classifier architecture and ONNX
export method directly (openwakeword.train.Model) rather than reimplementing
the network. A small, transparent PyTorch training loop is written here
instead of openwakeword's own higher-level fit loop, because that loop is
built around their large-scale mmap'd batch-generator data format
(openwakeword.data.mmap_batch_generator) tuned for production-scale runs;
for this project's small, in-memory, dev-scale feature arrays, a plain loop
over the same Model/optimizer/loss is simpler to read, debug, and test,
while still training the IDENTICAL network architecture and using their
IDENTICAL export_to_onnx() method - so the reuse boundary is the model, not
their training-loop plumbing.
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np


class TrainingDataError(RuntimeError):
    """Raised when there isn't enough labeled data to train at all - never
    silently trains on an empty or single-class dataset."""


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    import torch
    torch.manual_seed(seed)


def build_dataset(features: dict[str, "np.ndarray"]) -> tuple["np.ndarray", "np.ndarray"]:
    """features: {"positive_train": arr, "adversarial_train": arr, ...} (any
    key starting with "positive" is label 1, everything else is label 0).
    Returns (X, y) with X shape (N, 16, 96), y shape (N,)."""
    xs, ys = [], []
    for name, arr in features.items():
        if arr.size == 0:
            continue
        label = 1.0 if name.startswith("positive") else 0.0
        xs.append(arr)
        ys.append(np.full(arr.shape[0], label, dtype=np.float32))

    if not xs:
        raise TrainingDataError("no feature data at all - run --features first")
    X = np.concatenate(xs, axis=0)
    y = np.concatenate(ys, axis=0)
    if not (y == 1.0).any():
        raise TrainingDataError("no positive examples in the training set")
    if not (y == 0.0).any():
        raise TrainingDataError("no negative examples in the training set")
    return X.astype(np.float32), y


def _val_score(net, val_features: dict[str, "np.ndarray"], threshold: float = 0.5) -> dict:
    """Cheap, offline (embed_clips-domain) checkpoint-selection score,
    computed from already-extracted validation feature arrays - NOT a
    replacement for the real-runtime evaluation (training/runtime_eval.py),
    which is far too expensive to run every val_every_n_steps during
    training. Weights adversarial false-accepts most heavily, matching this
    project's own stated priority ("the adversarial FP target is the most
    important target") - a checkpoint with slightly worse recall but a much
    lower adversarial FP rate should be preferred over the reverse.
    """
    import torch

    def scores(arr):
        if arr is None or arr.size == 0:
            return np.array([], dtype=np.float32)
        with torch.no_grad():
            return net(torch.from_numpy(arr)).squeeze(-1).numpy()

    pos = scores(val_features.get("positive_val"))
    adv = scores(val_features.get("adversarial_val"))
    speech = scores(val_features.get("speech_val"))
    noise = scores(val_features.get("noise_val"))

    recall = float((pos >= threshold).mean()) if pos.size else None
    adv_fp = float((adv >= threshold).mean()) if adv.size else 0.0
    speech_fp = float((speech >= threshold).mean()) if speech.size else 0.0
    noise_fp = float((noise >= threshold).mean()) if noise.size else 0.0

    combined = (recall if recall is not None else 0.0) - 2.0 * adv_fp - speech_fp - noise_fp
    return {"recall": recall, "adv_fp": adv_fp, "speech_fp": speech_fp,
            "noise_fp": noise_fp, "combined_score": combined}


def train_model(config: dict, features: dict[str, "np.ndarray"], output_dir: str | Path,
                val_features: dict[str, "np.ndarray"] | None = None):
    """Trains a fresh openwakeword.train.Model on the given feature arrays
    and exports it to <output_dir>/<model_name>.onnx. Returns
    (model, history_dict) for the evaluation stage.

    `val_features`, when given (e.g. {"positive_val": arr, "adversarial_val":
    arr, ...}), enables VALIDATION-BASED CHECKPOINT SELECTION: at every
    `val_every_n_steps`, the current weights are scored offline (see
    `_val_score`) and the BEST-scoring checkpoint's weights are restored and
    exported at the end, instead of unconditionally exporting whatever the
    final training step happened to produce.

    This exists because a direct A/B step-count sweep (500/750/.../2000
    steps, identical seed/data/config, only step count varying) found this
    training setup's accuracy trajectory is NOT smoothly convergent - it
    oscillates: e.g. one sweep saw adversarial FP go 50.7% (500) -> 46.7%
    (750) -> 98.7% (1000, a near-total collapse) -> 86.7% (1250) -> 38.0%
    (1500) -> 68.0% (1750) -> 78.7% (2000). Picking a fixed step count and
    trusting it to land on a good point in that oscillation is not a
    reliable engineering practice - it amounts to gambling on which phase of
    an unstable trajectory training happens to stop at. Systematically
    tracking and keeping the best-validated checkpoint across the whole run
    removes that gamble without requiring the oscillation itself to be
    fixed (a separate, deeper question about learning rate/optimizer
    stability for this tiny network, out of scope for this specific fix).
    When `val_features` is None (the default), behavior is UNCHANGED from
    before: the last step's weights are always exported, exactly as every
    prior call site did.
    """
    import copy as _copy

    import torch
    from openwakeword.train import Model as OWWModel

    set_seed(config["seed"])
    model_cfg = config["model"]

    X, y = build_dataset(features)
    input_shape = (X.shape[1], X.shape[2])

    owwmodel = OWWModel(
        n_classes=1, input_shape=input_shape,
        model_type=model_cfg["model_type"],
        layer_dim=model_cfg["layer_dim"],
        n_blocks=model_cfg["n_blocks"],
    )
    net = owwmodel.model
    optimizer = torch.optim.Adam(net.parameters(), lr=model_cfg["learning_rate"])
    loss_fn = owwmodel.loss

    X_t = torch.from_numpy(X)
    y_t = torch.from_numpy(y)
    n = X_t.shape[0]
    batch_size = min(model_cfg["batch_size"], n)

    rng = np.random.default_rng(config["seed"])
    history = {"step": [], "loss": [], "accuracy": []}
    if val_features is not None:
        history["val_step"] = []
        history["val_combined_score"] = []
        history["val_recall"] = []
        history["val_adv_fp"] = []
    best_state = None
    best_score = None
    best_step = None

    net.train()
    for step in range(model_cfg["training_steps"]):
        idx = rng.integers(0, n, size=batch_size)
        xb, yb = X_t[idx], y_t[idx]

        optimizer.zero_grad()
        preds = net(xb).squeeze(-1)
        loss = loss_fn(preds, yb)
        loss.backward()
        optimizer.step()

        if step % max(1, model_cfg["val_every_n_steps"]) == 0 or step == model_cfg["training_steps"] - 1:
            with torch.no_grad():
                acc = ((preds >= 0.5).float() == yb).float().mean().item()
            history["step"].append(step)
            history["loss"].append(float(loss.item()))
            history["accuracy"].append(acc)

            if val_features is not None:
                net.eval()
                v = _val_score(net, val_features, config.get("evaluation", {}).get("threshold", 0.5))
                net.train()
                history["val_step"].append(step)
                history["val_combined_score"].append(v["combined_score"])
                history["val_recall"].append(v["recall"])
                history["val_adv_fp"].append(v["adv_fp"])
                if best_score is None or v["combined_score"] > best_score:
                    best_score = v["combined_score"]
                    best_step = step
                    best_state = _copy.deepcopy(net.state_dict())

    if val_features is not None and best_state is not None:
        net.load_state_dict(best_state)
        history["selected_checkpoint_step"] = best_step
        history["selected_checkpoint_score"] = best_score

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = output_dir / f"{config['model_name']}.onnx"
    net.eval()
    owwmodel.export_to_onnx(str(onnx_path), class_mapping=config["model_name"])

    return owwmodel, history, onnx_path
