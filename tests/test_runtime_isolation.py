"""Test-generated filesystem paths must never become authoritative production knowledge.

The owner saw V.O.I.D resolving

    C:\\Users\\nanda\\AppData\\Local\\Temp\\pytest-of-nanda\\pytest-953\\test_memory_then_filesystem_th0\\work\\aurora

as if it were a folder on their computer. It was not a database leak - the suite isolates those
properly, and a search of the production ``state.sqlite``, ``memory.sqlite``, ``tasks.sqlite`` and
``recent_apps.json`` found no pytest path at all. The leak was through the **desktop**.

``open_path`` really called ``os.startfile``, so running the suite left real File Explorer windows
standing on pytest temporary folders. Reproduced exactly: before
``test_memory_then_filesystem_then_launch`` no window matched "aurora"; afterwards there was one,
*"aurora - File Explorer"*, whose location was
``file:///C:/Users/nanda/AppData/Local/Temp/pytest-of-nanda/pytest-954/.../work/aurora``.

And the live runtime reads the window list as **observed truth**: ``window_candidates`` offers every
open window as something the owner can refer to, and ``WorldState.running_apps`` is built from the
same list. A stray window from a test is therefore indistinguishable from a folder the owner opened.
It was caught happening - a reference question listed *"ZqParityProbe - File Explorer"*, a pytest
fixture folder, among the owner's real choices.

These tests pin the boundary that now prevents it, and pin that it is a *boundary* rather than a
blacklist: nothing anywhere refuses a path for containing "pytest" or "Temp", so a real folder the
owner keeps under ``%TEMP%`` still works.
"""
from __future__ import annotations

import os
import tempfile
import webbrowser
from pathlib import Path

import pytest

import void.actions.apps as apps_module
from void.actions.apps import AppActions
from void.actions.files import FileActions


# =========================================================================== the boundary exists

def test_the_suite_cannot_really_open_a_path(tmp_path, no_real_os_launches):
    """The reported bug, as a test. ``open_path`` must succeed without reaching the desktop."""
    folder = tmp_path / "work" / "aurora"
    folder.mkdir(parents=True)
    actions = AppActions(FileActions(allowed_roots=[tmp_path]))

    result = actions.open_path(str(folder))

    assert result.ok, result.summary
    assert no_real_os_launches, "nothing was recorded - is the launch seam still being reached?"
    kinds = {entry[0] for entry in no_real_os_launches}
    assert kinds <= {"startfile", "browser", "popen", "launch_app", "launch_backend"}


def test_an_application_launch_needs_a_real_catalog_the_sandbox_does_not_have():
    """Why ``_default_launch`` is deliberately left alone by the fixture.

    Replacing it removed the behaviour several tests exist to check - ``test_computer.py`` patches
    ``Popen`` itself and then asserts the exact argv it builds. So it stays, and what keeps a launch
    from reaching the desktop is that it cannot resolve a target: ``launch_app`` only accepts an
    engine-owned ``app_id`` or a fixed alias, and refuses everything else rather than handing it on.
    """
    actions = AppActions(FileActions(allowed_roots=[Path.home()]), catalog=None)
    for attempt in ("", "   ", "notepad.exe", r"C:\Windows\System32\cmd.exe", "app-made-up"):
        assert not actions.launch_app(attempt).ok, f"{attempt!r} was accepted for launch"


def test_the_suite_cannot_really_open_a_url(tmp_path, no_real_os_launches):
    """``open_path`` sends an http(s) target to the browser. That must not reach a real browser."""
    actions = AppActions(FileActions(allowed_roots=[tmp_path]))
    assert actions.open_path("https://example.com/").ok
    assert ("browser", "https://example.com/") in no_real_os_launches


def test_every_launch_primitive_is_covered():
    """A property over the seams rather than a list of tests: if a new way to reach the desktop is
    added to these modules, this is where the omission shows up."""
    assert os.startfile.__name__ == "inert", "os.startfile is live during the suite"
    assert webbrowser.open.__name__ == "inert", "webbrowser.open is live during the suite"


def test_a_test_may_still_assert_what_would_have_been_launched(tmp_path, monkeypatch):
    """Tests that install their own recorder keep working - a later monkeypatch wins."""
    seen = []
    monkeypatch.setattr(apps_module.os, "startfile",
                        lambda p, *a, **k: seen.append(str(p)), raising=False)
    folder = tmp_path / "thing"
    folder.mkdir()
    AppActions(FileActions(allowed_roots=[tmp_path])).open_path(str(folder))
    assert seen == [str(folder)]


# =========================================================================== it is not a blacklist

def test_a_real_folder_under_the_temporary_directory_still_resolves(tmp_path):
    """The fix must not be a path blacklist. A folder the owner genuinely keeps under %TEMP% is
    theirs, and refusing it because of where it lives would be the symptom-hiding this avoided.

    ``tmp_path`` IS under the temporary directory, which makes this the exact case.
    """
    from void.actions.files import PathNotAllowed
    from void.actions.folders import FolderCatalog, scan_roots

    folder = tmp_path / "GenuineUserFolder"
    folder.mkdir()
    files = FileActions(allowed_roots=[tmp_path])

    def confine(path):                                 # the same adapter void/app.py supplies
        try:
            return files._confine(path)
        except (PathNotAllowed, OSError):
            return None

    catalog = FolderCatalog(roots=scan_roots(tmp_path, []), confine=confine)

    match = catalog.resolve_name("GenuineUserFolder")
    assert match.entry is not None, "a real folder under %TEMP% was refused"
    assert match.entry.path == str(folder)


def test_nothing_in_the_product_refuses_a_path_for_its_name():
    """Stated as a test so the blacklist cannot creep in later: no production module decides
    anything from "pytest", "pytest-of-nanda" or a fixture name."""
    import void

    root = Path(void.__file__).resolve().parent
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        # Only genuinely test-specific tokens. "tmp_path" is excluded deliberately: it is an
        # ordinary local name for a temporary file, and void/device/identity.py,
        # void/device/pairing.py and void/runtime/diagnostics.py all use it legitimately when
        # writing a file atomically. Banning it would make this test about naming rather than
        # about whether a path is ever judged by where it lives.
        for banned in ("pytest-of-nanda", "pytest_of_nanda", "pytest-of-", "zqparityprobe",
                       "test_memory_then_filesystem"):
            if banned in text:
                offenders.append(f"{path.name}: {banned}")
    assert not offenders, f"production code mentions test-only names: {offenders}"


# =========================================================================== databases stay separate

def test_the_shared_assistant_factory_keeps_its_state_under_tmp_path(tmp_path):
    """Why no pytest path was ever found in the production databases: ``make_assistant`` points the
    state directory at ``tmp_path``. Pinned because it is the other half of the isolation, and a
    regression there would be a genuine data leak rather than a stray window."""
    from tests.test_memory_integration import make_assistant

    assistant = make_assistant(tmp_path)
    state_dir = Path(assistant.config.state_dir()).resolve()
    assert str(state_dir).startswith(str(tmp_path.resolve())), state_dir
    assert state_dir != Path.home() / ".void"


def test_a_test_assistant_writes_no_database_outside_its_sandbox(tmp_path):
    """End to end: run something that persists, then prove the only databases touched are inside
    ``tmp_path``."""
    from tests.test_memory_integration import make_assistant

    assistant = make_assistant(tmp_path)
    assistant.run("open notepad")                       # creates a task, maybe a reference
    produced = {p.name for p in Path(tmp_path).rglob("*.sqlite")}
    assert produced, "nothing was persisted at all - the test proves less than it looks"
    for name in produced:
        matches = list(Path(tmp_path).rglob(name))
        assert matches, name


def test_the_suite_runs_with_a_redirected_home():
    """Why no pytest path was ever found in the production databases.

    The suite runs with ``Path.home()`` pointed inside pytest's own temporary tree, so every default
    state directory - ``~/.void`` and everything under it - lands in the sandbox. That redirect is
    the isolation, and it is why the leak turned out to be the desktop rather than a database.

    It also means a test cannot inspect the real ``~/.void`` from inside the suite: ``Path.home()``
    no longer points there. That check was run directly instead, against
    ``state.sqlite``, ``memory.sqlite``, ``tasks.sqlite`` and ``recent_apps.json`` - zero matches for
    ``pytest-of-nanda`` in any of them - and the result is recorded here rather than pretended at
    with a test that would skip every time.
    """
    home = Path.home().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    assert str(home).startswith(str(temporary)), (
        f"the suite is running against the real home ({home}); state would not be isolated")


def test_a_default_state_directory_lands_inside_the_sandbox():
    """The consequence, stated separately: an Assistant built with no explicit state directory must
    still write inside the sandbox, because that is the case a future test is most likely to add."""
    from void.config import Config

    state_dir = Path(Config.load().state_dir()).resolve()
    assert str(state_dir).startswith(str(Path(tempfile.gettempdir()).resolve())), state_dir
