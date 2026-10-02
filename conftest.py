"""Collection scope for the repository root.

**Why this file is at the root when nothing else is.** The owner deliberately moved the project's
documents and configuration - ``README.md``, ``pytest.ini``, the ``requirements*`` files, ``.gitignore`` -
into ``workspace/``, so the root is not cluttered with them. That preference is respected here: nothing is
moved back. But pytest only reads an ini file from the rootdir it discovers, so ``workspace/pytest.ini``
cannot scope a run started from the root, and a bare ``pytest`` aborted during collection:

    ERROR wakeword-training/tests - ImportPathMismatchError:
        ('tests.conftest', 'C:/V.O.I.D/tests/conftest.py',
         WindowsPath('C:/V.O.I.D/wakeword-training/tests/conftest.py'))

``wakeword-training`` is a separate, untracked project that happens to live inside the working copy, and
its ``tests`` package collides with this repository's. ``collect_ignore`` is pytest's own mechanism for
exactly this, it works from a ``conftest.py`` with no ini file at all, and a root ``conftest.py`` is a
thing pytest requires to be at the rootdir - it is tooling, not one of the files the owner relocated.

It is also strictly more robust than ``testpaths``: ``testpaths`` applies only when no path argument is
given, so ``pytest .`` would still have collided, whereas an ignored path is ignored however the run is
started. ``workspace/pytest.ini`` is left exactly where the owner put it and still works for anyone who
runs ``pytest -c workspace/pytest.ini``.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent

#: Directories inside the working copy that are not part of this repository's test suite.
#:
#: ``wakeword-training`` is a separate project with its own colliding ``tests`` package.
#: ``workspace`` holds the owner's documents and configuration, not tests.
#: ``.venv`` is the interpreter, and collecting site-packages' tests would be absurd.
NOT_OURS = ("wakeword-training", "workspace", ".venv", "build", "dist")

collect_ignore = [str(_ROOT / name) for name in NOT_OURS]
collect_ignore_glob = [f"{name}/*" for name in NOT_OURS]
