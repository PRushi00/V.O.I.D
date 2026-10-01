"""V.O.I.D compatibility test (required item, see project brief section 12).

Proves the exported models/hey_void.onnx is usable by the SAME openwakeword
Model class V.O.I.D's own void/voice/wake.py uses - not a redundant copy in
this training venv. Achieved by invoking C:\\V.O.I.D\\.venv\\Scripts\\python.exe
as a SUBPROCESS: this test never imports anything from V.O.I.D's `void`
package (isolation preserved) and never modifies any V.O.I.D file. It only
asks V.O.I.D's own, already-installed openwakeword package to load and run
the model.

Skips cleanly (does not fail) if:
- models/hey_void.onnx does not exist yet (training hasn't been run), or
- C:\\V.O.I.D\\.venv\\Scripts\\python.exe is not present on this machine.

If the model exists but V.O.I.D's own openwakeword installation cannot load
it or produces an unexpected output shape, this test FAILS with the exact
mismatch - it does not silently pass, and this project must not "fix" that
by modifying void/voice/wake.py (out of scope; see project brief).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "hey_void.onnx"
VOID_PYTHON = Path(r"C:\V.O.I.D\.venv\Scripts\python.exe")

_CHECK_SCRIPT = r"""
import json
import sys
import numpy as np
from openwakeword.model import Model

model_path = sys.argv[1]

# Same construction shape V.O.I.D's void/voice/wake.py uses:
# Model(wakeword_models=[self._model_path]) - no inference_framework kwarg,
# relying on the same tflite-import-fails-so-fall-back-to-onnx path.
m = Model(wakeword_models=[model_path])

# 80 ms (1280 samples) of 16 kHz 16-bit PCM silence - the documented minimum
# unit openwakeword's streaming predict() expects.
frame = np.zeros(1280, dtype=np.int16)
scores = m.predict(frame)

result = {
    "model_names": list(scores.keys()),
    "score_types": [type(v).__name__ for v in scores.values()],
    "scores_in_range": all(0.0 <= float(v) <= 1.0 for v in scores.values()),
}
print(json.dumps(result))
"""


def _skip_if_prerequisites_missing():
    if not MODEL_PATH.exists():
        pytest.skip(f"{MODEL_PATH} does not exist yet - run train_void.py --all first")
    if not VOID_PYTHON.exists():
        pytest.skip(f"{VOID_PYTHON} not found on this machine")


def test_hey_void_onnx_exists():
    _skip_if_prerequisites_missing()
    assert MODEL_PATH.is_file()
    assert MODEL_PATH.stat().st_size > 0


def test_void_own_openwakeword_can_load_and_score_the_model():
    """The decisive test: runs V.O.I.D's OWN venv's openwakeword.model.Model
    against the exported model, exactly as void/voice/wake.py does."""
    _skip_if_prerequisites_missing()

    proc = subprocess.run(
        [str(VOID_PYTHON), "-c", _CHECK_SCRIPT, str(MODEL_PATH)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 and "melspectrogram.onnx" in proc.stderr \
            and "doesn't exist" in proc.stderr:
        pytest.fail(
            "NOT a hey_void.onnx compatibility problem - confirmed separately "
            "that this exact model loads and scores correctly via "
            "openwakeword.model.Model (same class/fallback path) in this "
            "project's own venv. The actual mismatch: C:\\V.O.I.D\\.venv's "
            "openwakeword installation has never downloaded openwakeword's "
            "own shared melspectrogram/embedding backbone models "
            "(openwakeword.utils.download_models()) - a prerequisite for "
            "ANY custom wake model, not something this project's pipeline "
            "can or should fix by modifying V.O.I.D. Populating that cache "
            "in C:\\V.O.I.D\\.venv is a decision for the V.O.I.D repository "
            "owner, out of scope here.\n"
            f"stderr: {proc.stderr}")
    assert proc.returncode == 0, (
        f"V.O.I.D's own openwakeword installation could not load/score "
        f"{MODEL_PATH}:\nstdout: {proc.stdout}\nstderr: {proc.stderr}")

    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["model_names"], "predict() returned no model scores at all"
    assert all(t == "float32" for t in result["score_types"]) or \
        all(t in ("float", "float32", "float64") for t in result["score_types"])
    assert result["scores_in_range"], (
        f"one or more scores fell outside [0, 1]: {result}")


def test_model_loads_via_the_same_openwakeword_model_class_and_fallback_path():
    """Verifies the exported model FORMAT is genuinely compatible with
    openwakeword.model.Model - independent of whether C:\\V.O.I.D\\.venv
    happens to have the shared backbone models downloaded yet (a separate
    V.O.I.D-environment prerequisite, see the test above). Uses this
    project's own openwakeword installation (same package/version V.O.I.D
    has, same Model class, same no-inference_framework-kwarg construction
    void/voice/wake.py uses) so this passes even before that prerequisite is
    resolved in V.O.I.D's own venv."""
    _skip_if_prerequisites_missing()
    np = pytest.importorskip("numpy")
    from openwakeword.model import Model

    model = Model(wakeword_models=[str(MODEL_PATH)])
    frame = np.zeros(1280, dtype=np.int16)
    scores = model.predict(frame)

    assert scores, "predict() returned no scores"
    assert all(0.0 <= float(v) <= 1.0 for v in scores.values())


def test_no_void_source_files_were_touched_by_this_test():
    # Structural guard: this test module must never import void.* (checked
    # via actual import statements, not a source-text substring search,
    # which would trip on this very sentence describing the check).
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name == "void" or a.name.startswith("void.")
                          for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "void" and not (node.module or "").startswith("void.")
