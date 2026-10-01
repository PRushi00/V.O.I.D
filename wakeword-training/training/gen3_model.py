"""Gen 3 classifier: a small, STRUCTURALLY position-invariant head trained
on top of the frozen Whisper encoder (training/whisper_features.py).

Architecture choice, smallest-first: a single 1D convolution over the time
axis (captures local phonetic transitions in the encoder sequence) followed
by GLOBAL MAX POOLING over time, then a small dense layer to one sigmoid
output.

Why global max pooling specifically: Gen 2's classifier flattened its
fixed (16, 96) feature window into one vector before its dense layers -
which meant every unit's weight was tied to a SPECIFIC absolute position
in the window. A direct diagnostic proved this caused severe position
sensitivity (identical "hey void" audio scored 0.91 at its trained
position, ~0.01 shifted 200ms later). Global max pooling asks a
structurally different question - "does the target pattern's evidence
appear ANYWHERE in this window" - which is invariant to where in the
window the phrase actually falls, by construction, not merely by hoping
enough jittered training examples cover every offset. Position jitter is
still applied during training (see whisper_features.extract_fixed_window_
features's `rng` parameter) as defense in depth, but the architecture no
longer *requires* it to generalize across offsets.
"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np


class Gen3Classifier:
    """Thin wrapper mirroring openwakeword.train.Model's role in the Gen 2
    pipeline closely enough to reuse the same checkpoint/export patterns,
    without depending on that class (Gen 3 does not use openWakeWord's
    embedding or classifier at all - only its own Whisper-encoder features
    feed this network)."""

    def __init__(self, input_dim: int = 768, conv_channels: int = 64, hidden_dim: int = 32):
        import torch.nn as nn

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.conv1 = nn.Conv1d(input_dim, conv_channels, kernel_size=5, padding=2)
                self.fc1 = nn.Linear(conv_channels, hidden_dim)
                self.fc2 = nn.Linear(hidden_dim, 1)
                self.relu = nn.ReLU()
                self.dropout = nn.Dropout(0.2)

            def forward(self, x):
                # x: (B, T, input_dim) -> (B, input_dim, T) for Conv1d
                x = x.transpose(1, 2)
                x = self.relu(self.conv1(x))
                x, _ = x.max(dim=2)  # global max pool over time: (B, conv_channels)
                x = self.dropout(x)
                x = self.relu(self.fc1(x))
                x = self.fc2(x)
                return x.squeeze(-1)  # raw logits; sigmoid applied by loss/inference separately

        self.model = Net()
        self.input_dim = input_dim
        self.conv_channels = conv_channels
        self.hidden_dim = hidden_dim

    def export_to_onnx(self, output_path: str, seq_len: int) -> None:
        """Exports with a FIXED seq_len for the dummy input (ONNX traces a
        concrete shape), but marks the time axis dynamic so the exported
        graph accepts any T at inference - required since real utterances
        vary in duration and the encoder's frame count scales with it."""
        import torch
        self.model.eval()
        dummy = torch.rand(1, seq_len, self.input_dim)
        torch.onnx.export(
            self.model, dummy, output_path,
            input_names=["encoder_features"], output_names=["logit"],
            dynamic_axes={"encoder_features": {0: "batch", 1: "time"}, "logit": {0: "batch"}},
        )


def _val_score(net, val_features: dict[str, "np.ndarray"], threshold: float = 0.5) -> dict:
    """Same checkpoint-selection criterion as Gen 2's training/train.py -
    weights adversarial false-accepts most heavily. Kept as an independent
    copy (not a shared import) so Gen 2/Gen 3 pipelines stay fully
    decoupled."""
    import torch

    def scores(arr):
        if arr is None or arr.size == 0:
            return np.array([], dtype=np.float32)
        with torch.no_grad():
            return torch.sigmoid(net(torch.from_numpy(arr))).numpy()

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


def build_dataset(features: dict[str, "np.ndarray"]) -> tuple["np.ndarray", "np.ndarray"]:
    xs, ys = [], []
    for name, arr in features.items():
        if arr.size == 0:
            continue
        label = 1.0 if name.startswith("positive") else 0.0
        xs.append(arr)
        ys.append(np.full(arr.shape[0], label, dtype=np.float32))
    if not xs:
        raise ValueError("no feature data at all")
    return np.concatenate(xs, axis=0).astype(np.float32), np.concatenate(ys, axis=0)


def train_gen3_model(config: dict, features: dict[str, "np.ndarray"], output_dir: str | Path,
                     val_features: dict[str, "np.ndarray"] | None = None):
    """Trains a Gen3Classifier and exports it to <output_dir>/<model_name>.onnx.
    Mirrors training/train.py's train_model() training-loop shape and its
    validation-based checkpoint-selection mechanism (a genuine, validated
    Gen 2 fix - kept here since the underlying problem it addresses,
    unstable/oscillating training dynamics for a tiny network on a hard
    task, is not specific to Gen 2's architecture)."""
    import random
    import torch

    seed = config["seed"]
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    model_cfg = config["model"]
    X, y = build_dataset(features)
    input_dim = X.shape[2]

    clf = Gen3Classifier(input_dim=input_dim, conv_channels=model_cfg.get("conv_channels", 64),
                         hidden_dim=model_cfg.get("hidden_dim", 32))
    net = clf.model
    optimizer = torch.optim.Adam(net.parameters(), lr=model_cfg["learning_rate"])
    loss_fn = torch.nn.BCEWithLogitsLoss()

    X_t = torch.from_numpy(X)
    y_t = torch.from_numpy(y)
    n = X_t.shape[0]
    batch_size = min(model_cfg["batch_size"], n)

    rng = np.random.default_rng(seed)
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
        logits = net(xb)
        loss = loss_fn(logits, yb)
        loss.backward()
        optimizer.step()

        if step % max(1, model_cfg["val_every_n_steps"]) == 0 or step == model_cfg["training_steps"] - 1:
            with torch.no_grad():
                acc = ((torch.sigmoid(logits) >= 0.5).float() == yb).float().mean().item()
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
                    best_state = copy.deepcopy(net.state_dict())

    if val_features is not None and best_state is not None:
        net.load_state_dict(best_state)
        history["selected_checkpoint_step"] = best_step
        history["selected_checkpoint_score"] = best_score

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = output_dir / f"{config['model_name']}.onnx"
    clf.export_to_onnx(str(onnx_path), seq_len=X.shape[1])

    return clf, history, onnx_path
