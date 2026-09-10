"""open_path: file/folder/URL launching, confinement, and protected-root denial.

Closes the filesystem audit gap: open_path was the only registered
filesystem-touching tool with no happy-path coverage. Deterministic - every
launcher (os.startfile / subprocess.Popen / webbrowser.open) is monkeypatched,
so no real application or browser is ever started. Temporary directories only;
RiskGate and filesystem policy are untouched.
"""
import pytest

import void.actions.apps as apps
from void.actions.apps import AppActions
from void.actions.files import FileActions


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A confined FileActions + AppActions with all OS launchers stubbed.

    Layout under tmp_path:
        workspace/          <- the single allowed root
            doc.txt         <- an existing, allowed file
            vault/          <- a protected (excluded) subtree
                secret.txt
        outside_area/
            other.txt       <- exists, but outside every allowed root
    """
    workspace = tmp_path / "workspace"
    vault = workspace / "vault"
    outside = tmp_path / "outside_area"
    for d in (workspace, vault, outside):
        d.mkdir(parents=True)
    (workspace / "doc.txt").write_text("hello")
    (vault / "secret.txt").write_text("classified")
    (outside / "other.txt").write_text("not yours")

    fa = FileActions(allowed_roots=[workspace], protected_roots=[vault])
    aa = AppActions(fa)

    launches: list[tuple[str, str]] = []
    browser: list[str] = []

    def _startfile(path, *a, **k):
        launches.append(("startfile", str(path)))

    def _popen(argv, *a, **k):
        # POSIX open path: argv is ["open"|"xdg-open", <path>]
        launches.append(("popen", str(argv[-1])))
        return None

    def _browser_open(url, *a, **k):
        browser.append(str(url))
        return True

    # Force the Windows branch for a deterministic launcher, and stub all three.
    monkeypatch.setattr(AppActions, "_is_windows", lambda self: True)
    monkeypatch.setattr(apps.os, "startfile", _startfile, raising=False)
    monkeypatch.setattr(apps.subprocess, "Popen", _popen)
    monkeypatch.setattr(apps.webbrowser, "open", _browser_open)

    return {
        "aa": aa, "fa": fa, "workspace": workspace, "vault": vault,
        "outside": outside, "launches": launches, "browser": browser,
    }


def _launched_path(call: tuple[str, str]) -> str:
    return call[1]


# 1. existing allowed file -> success, launcher called with the confined path
def test_open_existing_allowed_file_launches_confined_path(rig):
    target = rig["workspace"] / "doc.txt"
    confined = str(rig["fa"]._confine(str(target)))

    res = rig["aa"].open_path(str(target))

    assert res.ok
    assert f"Opened {confined}" in res.summary
    assert len(rig["launches"]) == 1
    assert _launched_path(rig["launches"][0]) == confined
    assert rig["browser"] == []


def test_open_existing_allowed_directory_also_launches(rig):
    # A folder is a valid open_path target too.
    res = rig["aa"].open_path(str(rig["workspace"]))
    assert res.ok
    assert len(rig["launches"]) == 1
    assert _launched_path(rig["launches"][0]) == str(
        rig["fa"]._confine(str(rig["workspace"])))


# 2. non-existent (but in-bounds) path -> failure, launcher NOT called
def test_open_nonexistent_allowed_path_fails_without_launching(rig):
    missing = rig["workspace"] / "no_such_file.txt"

    res = rig["aa"].open_path(str(missing))

    assert not res.ok
    assert "does not exist" in res.summary.lower()
    assert rig["launches"] == []
    assert rig["browser"] == []


# 3. http/https URL -> webbrowser.open only, no filesystem launcher
@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "http://localhost:8080/thing?q=1",
])
def test_open_url_routes_to_browser_not_filesystem(rig, url):
    res = rig["aa"].open_path(url)

    assert res.ok
    assert rig["browser"] == [url]
    assert rig["launches"] == []          # _confine / os launcher never touched


# 4. path outside every allowed root -> failure, nothing launched
def test_open_path_outside_allowed_roots_is_denied(rig):
    outside_file = rig["outside"] / "other.txt"
    assert outside_file.exists()          # exists, but out of bounds

    res = rig["aa"].open_path(str(outside_file))

    assert not res.ok
    assert "outside the allowed roots" in res.summary
    assert rig["launches"] == []
    assert rig["browser"] == []


# 5. path inside a protected (excluded) root -> failure, nothing launched
def test_open_path_inside_protected_root_is_denied(rig):
    secret = rig["vault"] / "secret.txt"
    assert secret.exists()

    res = rig["aa"].open_path(str(secret))

    assert not res.ok
    assert "protected" in res.summary.lower()
    assert rig["launches"] == []
    assert rig["browser"] == []
    assert secret.read_text() == "classified"   # untouched
