"""Tests for owner-managed trusted filesystem roots.

All tests use temporary directories and an injected temporary local_config.yaml
- never the user's real OneDrive/profile. No Gemini calls, no API keys.
"""
import os
from pathlib import Path

import pytest
import yaml

from void import roots
from void.roots import RootError
from void.actions.files import FileActions, PathNotAllowed


def _keys(paths):
    return {os.path.normcase(str(p)) for p in paths}


def _k(p):
    return os.path.normcase(str(Path(p).resolve()))


@pytest.fixture
def env(tmp_path):
    """A temp workspace root + a temp local_config.yaml pointing at it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local = tmp_path / "local_config.yaml"
    local.write_text(
        yaml.safe_dump({"security": {"allowed_roots": [str(workspace)]}}),
        encoding="utf-8",
    )
    return workspace, local


# 1. Existing workspace root remains available --------------------------

def test_workspace_root_listed(env):
    workspace, local = env
    assert _keys(roots.list_roots(local_path=local)) == {_k(workspace)}


# 2 & 3. Add a valid external directory; it becomes an allowed root -----

def test_add_external_root_preserves_workspace(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    added = roots.add_root(str(ext), local_path=local)
    assert added == ext.resolve()
    keys = _keys(roots.list_roots(local_path=local))
    assert _k(ext) in keys and _k(workspace) in keys


def test_added_root_allows_file_operations(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    roots.add_root(str(ext), local_path=local)
    fa = FileActions(allowed_roots=roots.list_roots(local_path=local))
    f = ext / "a.txt"
    f.write_text("x")
    assert fa.read(str(f)).ok


# 4. Duplicate normalized paths rejected --------------------------------

def test_duplicate_roots_rejected(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    roots.add_root(str(ext), local_path=local)
    with pytest.raises(RootError):
        roots.add_root(str(ext), local_path=local)                 # exact dup
    with pytest.raises(RootError):
        roots.add_root(str(ext) + os.sep + ".", local_path=local)  # normalized dup
    with pytest.raises(RootError):
        roots.add_root(str(workspace), local_path=local)           # workspace dup


# 5 & 6. Nonexistent path and file paths rejected -----------------------

def test_nonexistent_path_rejected(env, tmp_path):
    workspace, local = env
    with pytest.raises(RootError):
        roots.add_root(str(tmp_path / "does_not_exist"), local_path=local)


def test_file_path_rejected(env, tmp_path):
    workspace, local = env
    f = tmp_path / "afile.txt"
    f.write_text("x")
    with pytest.raises(RootError):
        roots.add_root(str(f), local_path=local)


def test_empty_path_rejected(env):
    workspace, local = env
    with pytest.raises(RootError):
        roots.add_root("   ", local_path=local)


# 7. Removing an external root works ------------------------------------

def test_remove_external_root(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    roots.add_root(str(ext), local_path=local)
    removed = roots.remove_root(str(ext), local_path=local)
    assert removed == ext.resolve()
    keys = _keys(roots.list_roots(local_path=local))
    assert _k(ext) not in keys and _k(workspace) in keys


def test_remove_unknown_root_rejected(env, tmp_path):
    workspace, local = env
    with pytest.raises(RootError):
        roots.remove_root(str(tmp_path / "never_added"), local_path=local)


# 8. Primary/last workspace root cannot be accidentally removed ---------

def test_cannot_remove_last_root(env):
    workspace, local = env
    with pytest.raises(RootError):
        roots.remove_root(str(workspace), local_path=local)
    assert _k(workspace) in _keys(roots.list_roots(local_path=local))


# 9-12. Confinement behavior with an authorized external root -----------

def test_confinement_boundaries(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "OneDriveSim" / "Desktop" / "Projects"
    ext.mkdir(parents=True)
    roots.add_root(str(ext), local_path=local)
    fa = FileActions(allowed_roots=roots.list_roots(local_path=local))

    # 9. nested paths inside the authorized root are allowed
    sub = ext / "Subfolder"
    sub.mkdir()
    nested = sub / "file.txt"
    nested.write_text("x")
    assert fa._confine(str(nested)) == nested.resolve()

    # 10. sibling directory outside the authorized root is denied
    sibling = ext.parent / "OtherFolder"
    sibling.mkdir()
    with pytest.raises(PathNotAllowed):
        fa._confine(str(sibling / "g.txt"))

    # 11. parent directories are denied unless separately authorized
    with pytest.raises(PathNotAllowed):
        fa._confine(str(ext.parent))                    # ...\Desktop
    with pytest.raises(PathNotAllowed):
        fa._confine(str(ext.parent.parent / "x.txt"))   # ...\OneDriveSim

    # 12. path traversal out of the authorized root is denied
    with pytest.raises(PathNotAllowed):
        fa._confine(str(ext / ".." / "OtherFolder" / "h.txt"))


# list_directory reflects newly authorized roots -----------------------

def test_list_directory_reflects_new_root(env, tmp_path):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    (ext / "proj.txt").write_text("x")
    roots.add_root(str(ext), local_path=local)
    fa = FileActions(allowed_roots=roots.list_roots(local_path=local))
    res = fa.list_dir()  # default: lists all configured roots
    assert "proj.txt" in {e["name"] for e in res.data}


# 15 & 16. Persistence + preserving other local config keys -------------

def test_persist_preserves_other_local_keys(env, tmp_path):
    workspace, local = env
    data = yaml.safe_load(local.read_text())
    data.setdefault("kill_switch", {})["require_pin"] = True
    local.write_text(yaml.safe_dump(data), encoding="utf-8")

    ext = tmp_path / "external"
    ext.mkdir()
    roots.add_root(str(ext), local_path=local)

    data2 = yaml.safe_load(local.read_text())
    assert data2["kill_switch"]["require_pin"] is True          # unrelated key kept
    stored = {os.path.normcase(p) for p in data2["security"]["allowed_roots"]}
    assert _k(ext) in stored and _k(workspace) in stored


# 14 & 15 via the CLI ---------------------------------------------------

def test_cli_roots_list(env, monkeypatch, capsys):
    workspace, local = env
    monkeypatch.setattr("void.roots.local_config_path", lambda: local)
    from void import cli
    rc = cli.main(["roots", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Allowed filesystem roots:" in out
    assert str(workspace.resolve()) in out


def test_cli_roots_add_persists(env, monkeypatch, tmp_path, capsys):
    workspace, local = env
    ext = tmp_path / "external"
    ext.mkdir()
    monkeypatch.setattr("void.roots.local_config_path", lambda: local)
    from void import cli
    rc = cli.main(["roots", "add", str(ext)])
    assert rc == 0
    data = yaml.safe_load(local.read_text())
    stored = {os.path.normcase(p) for p in data["security"]["allowed_roots"]}
    assert _k(ext) in stored and _k(workspace) in stored


def test_cli_roots_add_rejects_file(env, monkeypatch, tmp_path, capsys):
    workspace, local = env
    f = tmp_path / "file.txt"
    f.write_text("x")
    monkeypatch.setattr("void.roots.local_config_path", lambda: local)
    from void import cli
    rc = cli.main(["roots", "add", str(f)])
    assert rc == 1  # rejected, non-zero exit


# 17. The agent/LLM has no tool to manage roots -------------------------

def test_agent_has_no_root_management_tool(tmp_path):
    from void.actions.apps import AppActions
    fa = FileActions(allowed_roots=[tmp_path])
    aa = AppActions(fa)
    names = {t.name for t in fa.tools()} | {t.name for t in aa.tools()}
    assert not any("root" in n.lower() for n in names)
    assert not (names & {"add_root", "remove_root", "roots",
                         "allow_root", "set_root", "list_roots"})
