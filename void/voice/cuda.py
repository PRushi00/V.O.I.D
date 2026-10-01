"""Make the CUDA runtime libraries discoverable before CTranslate2 looks for them.

CTranslate2 bundles cuDNN but not cuBLAS, and the ``nvidia-*-cu12`` wheels install their DLLs into
``site-packages/nvidia/*/bin`` - a directory Windows does not search. Without this, ``device="cuda"`` fails with
"Library cublas64_12.dll is not found or cannot be loaded" even though the GPU, the driver and CTranslate2's CUDA
support are all present.

``os.add_dll_directory`` is NOT sufficient: CTranslate2's native loader resolves these through PATH, so PATH is
what this sets. Importing it is harmless on a machine with no CUDA at all - it simply finds no directories and
does nothing.
"""
from __future__ import annotations

import logging
import os
import sys

_log = logging.getLogger("void.voice.cuda")

_CANDIDATES = (
    ("nvidia", "cublas", "bin"),
    ("nvidia", "cudnn", "bin"),
    ("nvidia", "cuda_runtime", "bin"),
    ("ctranslate2",),
)

_registered = False


def register() -> list[str]:
    """Prepend any bundled CUDA library directories to PATH. Idempotent; never raises."""
    global _registered
    if _registered:
        return []
    _registered = True
    found = []
    for parts in _CANDIDATES:
        path = os.path.join(sys.prefix, "Lib", "site-packages", *parts)
        if os.path.isdir(path):
            found.append(path)
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
        _log.debug("CUDA_LIBRARY_PATH_REGISTERED count=%d", len(found))
    return found
