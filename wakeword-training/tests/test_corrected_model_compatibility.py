"""V.O.I.D compatibility tests for the sample-rate-CORRECTED model
(models/hey_void_16khz_fixed_500.onnx), mirroring test_void_compatibility.py's
already-established pattern for the production model. This is a distinct,
NOT-YET-DEPLOYED model - these tests check it is technically loadable and
runtime-compatible; they make no claim about whether it should replace
production (that is a Phase 14 decision documented in the final report, not
something this test file automates).

Also checks the model's input_shape metadata (Phase 15 item: "model input
shape remains compatible") and exercises V.O.I.D's own
void.voice.wake.OpenWakeWordDetector against it via subprocess (isolation
preserved: this file never imports void.* directly), addressing "runtime
wake detector accepts the corrected model".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import torch

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
ONNX_PATH = MODEL_DIR / "hey_void_16khz_fixed_500.onnx"
CHECKPOINT_PATH = MODEL_DIR / "hey_void_16khz_fixed_500.pt"
VOID_PYTHON = Path(r"C:\V.O.I.D\.venv\Scripts\python.exe")


def _skip_if_prerequisites_missing():
    if not ONNX_PATH.exists():
        pytest.skip(f"{ONNX_PATH} does not exist - corrected model not trained in this checkout")
    if not VOID_PYTHON.exists():
        pytest.skip(f"{VOID_PYTHON} not found on this machine")


def test_corrected_onnx_exists_and_is_nonempty():
    _skip_if_prerequisites_missing()
    assert ONNX_PATH.is_file()
    assert ONNX_PATH.stat().st_size > 0


def test_corrected_model_input_shape_matches_16_frame_architecture():
    # Phase 15: "model input shape remains compatible" - the classifier
    # architecture is unchanged from Experiment #2 (16 embedding frames x
    # 96-dim), only the underlying audio/features were corrected.
    if not CHECKPOINT_PATH.exists():
        pytest.skip(f"{CHECKPOINT_PATH} does not exist")
    checkpoint = torch.load(str(CHECKPOINT_PATH), weights_only=True)
    assert tuple(checkpoint["input_shape"]) == (16, 96)


def test_corrected_model_loads_via_openwakeword_model_class():
    _skip_if_prerequisites_missing()
    np = pytest.importorskip("numpy")
    from openwakeword.model import Model

    model = Model(wakeword_models=[str(ONNX_PATH)], inference_framework="onnx")
    frame = np.zeros(1280, dtype=np.int16)
    scores = model.predict(frame)

    assert scores, "predict() returned no scores"
    assert all(0.0 <= float(v) <= 1.0 for v in scores.values())


_WAKE_DETECTOR_CHECK_SCRIPT = r"""
import json
import sys
import numpy as np
from void.voice.wake import OpenWakeWordDetector

model_path = sys.argv[1]
events = []
det = OpenWakeWordDetector(model_path=model_path, threshold=0.6,
                           on_wake=lambda evt: events.append(evt))
det.start()
# 30ms frame (480 samples), matching AudioCaptureBroker's default frame
# contract - proves the detector accepts the corrected model end-to-end
# through the SAME class production wiring uses.
frame = np.zeros(480, dtype="<i2").tobytes()
det.feed_audio(frame)
det.stop()
det.close()
print(json.dumps({"started_ok": True, "events_on_silence": events}))
"""


def test_void_own_wake_detector_accepts_the_corrected_model():
    """Phase 15: "runtime wake detector accepts the corrected model" -
    exercises the REAL void.voice.wake.OpenWakeWordDetector (via subprocess
    into V.O.I.D's own venv, preserving this project's isolation from void.*
    imports) pointed at the corrected model. Silence must not wake it."""
    _skip_if_prerequisites_missing()

    proc = subprocess.run(
        [str(VOID_PYTHON), "-c", _WAKE_DETECTOR_CHECK_SCRIPT, str(ONNX_PATH)],
        capture_output=True, text=True, timeout=60, cwd=r"C:\V.O.I.D",
    )
    assert proc.returncode == 0, (
        f"void.voice.wake.OpenWakeWordDetector could not start/run with the "
        f"corrected model:\nstdout: {proc.stdout}\nstderr: {proc.stderr}")

    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["started_ok"] is True
    assert result["events_on_silence"] == [], "silence must never trigger WAKE_DETECTED"


def test_corrected_model_reset_clears_streaming_state():
    """Phase 15/19 (FP-reduction pass): openWakeWord's streaming Model.predict()
    accumulates a rolling feature_buffer across calls - the evaluation
    harness (training/runtime_eval.py) and the FP-reduction experiment
    scripts call model.reset() between clips specifically to prevent one
    clip's audio from leaking into the next clip's score. This pins down
    that reset() actually has that effect: feeding real audio then
    resetting and scoring silence must reproduce the SAME silence-only
    score a freshly-constructed model would give, not an elevated one
    contaminated by the prior clip."""
    _skip_if_prerequisites_missing()
    np = pytest.importorskip("numpy")
    from openwakeword.model import Model

    model = Model(wakeword_models=[str(ONNX_PATH)], inference_framework="onnx")
    silence = np.zeros(1280, dtype=np.int16)
    loud_noise = (np.random.RandomState(0).uniform(-1, 1, 1280) * 32767).astype(np.int16)

    baseline_scores = [model.predict(silence)[list(model.models.keys())[0]] for _ in range(5)]

    for _ in range(20):
        model.predict(loud_noise)

    model.reset()
    post_reset_scores = [model.predict(silence)[list(model.models.keys())[0]] for _ in range(5)]

    # Both sequences score genuine silence starting from a clean buffer -
    # they should land in the same ballpark, not have the post-reset
    # sequence artificially elevated by the noise fed before reset().
    assert abs(post_reset_scores[-1] - baseline_scores[-1]) < 0.1, (
        f"reset() did not clear streaming state: baseline silence score "
        f"{baseline_scores[-1]:.4f} vs post-reset silence score "
        f"{post_reset_scores[-1]:.4f} diverge too much")


def test_no_void_source_files_were_touched_by_this_test():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name == "void" or a.name.startswith("void.")
                          for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "void" and not (node.module or "").startswith("void.")
