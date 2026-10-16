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

import importlib
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent

#: Directories inside the working copy that are not part of this repository's test suite.
#:
#: ``wakeword-training`` is a separate project with its own colliding ``tests`` package.
#: ``workspace`` holds the owner's documents and configuration, not tests.
#: ``.venv`` is the interpreter, and collecting site-packages' tests would be absurd.
NOT_OURS = ("wakeword-training", "workspace", ".venv", "build", "dist")

collect_ignore = [str(_ROOT / name) for name in NOT_OURS]
collect_ignore_glob = [f"{name}/*" for name in NOT_OURS]

# --------------------------------------------------------------------------- real-world side effects

@pytest.fixture(autouse=True)
def no_real_os_launches(monkeypatch, request):
    """Stop the suite from acting on the owner's real desktop.

    This is the isolation boundary the suite was missing, and the bug it existed to prevent had
    already happened. Tests isolate their *databases* properly - ``make_assistant`` points
    ``app.state_dir`` at ``tmp_path``, and a search of the production ``state.sqlite``,
    ``memory.sqlite``, ``tasks.sqlite`` and ``recent_apps.json`` finds no pytest path at all. What
    was not isolated was the **desktop**: ``open_path`` really called ``os.startfile``, so running
    the suite left real File Explorer windows open on real pytest temporary folders.

    Reproduced: before ``test_memory_then_filesystem_then_launch`` there was no window matching
    "aurora"; afterwards there was exactly one, *"aurora - File Explorer"*, pointing inside
    ``pytest-of-nanda``.

    That is how a test path became authoritative production knowledge, and it is worth being
    precise about the mechanism because it is not a database leak. The live runtime reads the
    **window list** as observed truth - ``referents.window_candidates`` offers every open window as
    something the owner can refer to, and ``WorldState.running_apps`` is built from the same list.
    So a stray Explorer window from a test is indistinguishable, to the running assistant, from a
    folder the owner opened themselves. It was seen happening: a reference question listed
    *"ZqParityProbe - File Explorer"* - a pytest fixture folder - among the owner's real choices.

    The fix belongs here rather than in the product. A production guard that refused paths under
    the temporary directory would be a blacklist: it would break a legitimate folder the owner
    keeps under ``%TEMP%``, it would not stop the next side effect that is not a path, and it would
    leave the suite still reaching out of its sandbox. Making the launch primitives inert for the
    whole suite fixes the cause once, cannot be forgotten by the next test, and leaves production
    behaviour untouched - nothing here is imported outside pytest.

    Tests that need to assert *what* would have been launched keep working: they install their own
    recorder over this one (``test_apps.py`` and ``test_computer.py`` both do), and a later
    monkeypatch wins. A test that genuinely needs the real thing can ask for it with
    ``@pytest.mark.real_os_launch``, which is deliberately awkward to type and currently unused.
    """
    if request.node.get_closest_marker("real_os_launch"):
        return []

    launched: list[tuple] = []

    def record(kind):
        def inert(*args, **kwargs):
            launched.append((kind, *args))
            return None
        return inert

    apps = importlib.import_module("void.actions.apps")
    computer = importlib.import_module("void.actions.computer")  # imported for the note below

    # ``os.startfile`` and ``webbrowser.open`` are patched on the shared modules, which is the only
    # place they can be reached from - ``apps.py`` calls them through a plain ``import``, so there is
    # no module-local alias to patch instead. Nothing in this suite calls either for its own
    # purposes, so the reach is wide but the collateral is nil.
    monkeypatch.setattr(apps.os, "startfile", record("startfile"), raising=False)
    monkeypatch.setattr(apps.webbrowser, "open", record("browser"), raising=False)

    # Two things are deliberately NOT patched, and both for the same reason: they are code under
    # test rather than a way out of the sandbox.
    #
    # ``subprocess.Popen`` - ``subprocess.run`` is built on it, and three tests legitimately spawn
    # ``tests/_memory_child.py`` to prove memory survives a real separate process. Blocking it
    # globally would break the tests that check the strongest persistence guarantee there is.
    #
    # ``AppActions._default_launch`` and ``RealWindowsBackend.launch`` - replacing these removed the
    # behaviour several tests exist to check: ``test_computer.py`` patches ``Popen`` itself and then
    # asserts the exact argv ``_default_launch`` builds ("explorer.exe", "shell:AppsFolder\..." -
    # two fixed items, no shell). A fixture that swallowed the call made that assertion unreachable.
    #
    # Leaving them is safe because the real desktop effect this suite produced came through
    # ``os.startfile``: that is what ``open_path`` uses for a folder, and the stray Explorer windows
    # were its doing. A launch through ``Popen`` needs a resolved executable from a real catalog,
    # which the sandbox does not have - and the assertion below checks that claim rather than
    # trusting it.
    del apps, computer
    return launched


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_os_launch: this test may really open a window or launch a program on this desktop")

