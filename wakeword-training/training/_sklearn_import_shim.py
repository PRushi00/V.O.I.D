"""Workaround for an environment-specific import failure discovered during
the Gen 3 engineering pass: this machine's Windows Application Control
policy blocks a scikit-learn native DLL (`sklearn.utils._isfinite`'s
`_cyutility` dependency) with "An Application Control policy has blocked
this file." Confirmed NOT caused by any package this project installed -
the block persists identically after reinstalling scikit-learn and after
uninstalling every package added for Gen 3 (faster-whisper/ctranslate2/av/
tokenizers). This is a system-level security policy event outside this
project's control, requiring admin intervention (an Application Control /
WDAC allow-list update) to fix at the root.

`openwakeword/__init__.py` unconditionally imports
`custom_verifier_model.py`, which imports `sklearn.linear_model.
LogisticRegression` etc. at MODULE LOAD TIME - even though this project
never calls `train_custom_verifier()` (the only function that actually
uses those names). Every other openwakeword feature this project relies on
(Model, predict_clip, AudioFeatures, data.generate_adversarial_texts) has
nothing to do with scikit-learn.

This module installs a minimal stub for just the names
`custom_verifier_model.py` imports, registered in `sys.modules` BEFORE
`openwakeword` is imported anywhere in a process. It must be called first,
before any `import openwakeword` (directly or via `training.*`). Calling it
after openwakeword (or its dependents) are already imported is a no-op
w.r.t. that already-completed import, so callers should call this as the
very first thing in a script/conftest, not defensively before a later
import.
"""
from __future__ import annotations

import sys
import types


def install_sklearn_import_shim() -> None:
    if "sklearn" in sys.modules and getattr(sys.modules["sklearn"], "__version__", None):
        return  # a real scikit-learn already imported successfully; nothing to shim
    if isinstance(sys.modules.get("sklearn"), types.ModuleType) and \
            getattr(sys.modules["sklearn"], "_is_import_shim", False):
        return  # our own shim already installed

    stub = types.ModuleType("sklearn")
    stub._is_import_shim = True

    linear_model = types.ModuleType("sklearn.linear_model")
    linear_model.LogisticRegression = object

    pipeline = types.ModuleType("sklearn.pipeline")
    pipeline.make_pipeline = lambda *a, **k: None

    preprocessing = types.ModuleType("sklearn.preprocessing")
    preprocessing.FunctionTransformer = object
    preprocessing.StandardScaler = object

    stub.linear_model = linear_model
    stub.pipeline = pipeline
    stub.preprocessing = preprocessing

    sys.modules["sklearn"] = stub
    sys.modules["sklearn.linear_model"] = linear_model
    sys.modules["sklearn.pipeline"] = pipeline
    sys.modules["sklearn.preprocessing"] = preprocessing
