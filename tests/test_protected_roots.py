"""Tests for protected (excluded) filesystem roots overriding allowed roots.

Simulates a broad "C:\\"-style allowed root with an excluded OneDrive subtree,
using ONLY temporary directories. No real filesystem/OneDrive, no API keys, no
Gemini calls.
"""
import os
from pathlib import Path

import pytest
import yaml

from void import roots
from void.roots import RootError
from void.actions.files import FileActions, PathNotAllowed
from void.actions.apps import AppActions
from void.config import Config


def _k(p):
    return os.path.normcase(str(Path(p).resolve()))


@pytest.fixture
def tree(tmp_path):
    """A broad allowed root with a protected OneDrive subtree."""
    broad = tmp_path / "SystemDrive"           # acts like C:\
    users = broad / "Users" / "nanda"
    desktop = users / "Desktop"
    onedrive = users / "OneDrive"              # protected
    onedrive_backup = users / "OneDriveBackup"  # NOT protected (prefix trap)
    for d in (desktop, onedrive, onedrive_backup):
        d.mkdir(parents=True)
    (desktop / "ok.txt").write_text("y")
    (onedrive / "secret.txt").write_text("x")
    (onedrive / "Sub").mkdir()
    (onedrive / "Sub" / "deep.txt").write_text("z")
    fa = FileActions(allowed_roots=[broad], protected_roots=[onedrive])
    return {
        "broad": broad, "users": users, "desktop": desktop,
        "onedrive": onedrive, "backup": onedrive_backup, "fa": fa,
    }


# A. broad root normalization -------------------------------------------

def test_broad_root_normalization(tmp_path):
    broad = tmp_path / "Drive"
    broad.mkdir()
    fa = FileActions(allowed_roots=[str(broad) + os.sep])  # trailing sep
    assert fa.allowed_roots[0] == broad.resolve()


# B. descendants of an authorized root are allowed ----------------------

def test_descendants_allowed(tree):
    fa = tree["fa"]
    deep = tree["broad"] / "anything" / "deeper" / "f.txt"
    assert fa._confine(str(deep)) == deep.resolve()
    assert fa._confine(str(tree["desktop"] / "ok.txt")) == (
        tree["desktop"] / "ok.txt").resolve()


# C. separate drive remains denied unless authorized --------------------

def test_separate_drive_denied(tree, tmp_path):
    fa = tree["fa"]
    other = tmp_path / "OtherDrive" / "f.txt"
    (tmp_path / "OtherDrive").mkdir()
    with pytest.raises(PathNotAllowed):
        fa._confine(str(other))


# D & E. protected root overrides allowed; protected files denied -------

def test_protected_overrides_allowed(tree):
    fa = tree["fa"]
    with pytest.raises(PathNotAllowed):
        fa._confine(str(tree["onedrive"] / "secret.txt"))
    with pytest.raises(PathNotAllowed):
        fa._confine(str(tree["onedrive"]))  # the protected root itself


# F. nested dirs inside protected root denied ---------------------------

def test_nested_protected_denied(tree):
    fa = tree["fa"]
    with pytest.raises(PathNotAllowed):
        fa._confine(str(tree["onedrive"] / "Sub" / "deep.txt"))


# G. sibling outside protected root allowed (incl. prefix trap) ---------

def test_sibling_and_prefix_trap_allowed(tree):
    fa = tree["fa"]
    assert fa._confine(str(tree["desktop"] / "ok.txt"))
    # 'OneDriveBackup' must NOT be treated as inside 'OneDrive'.
    assert fa._confine(str(tree["backup"] / "f.txt")) == (
        tree["backup"] / "f.txt").resolve()


# H. parent of protected root allowed (inside allowed, outside protected)

def test_parent_of_protected_allowed(tree):
    fa = tree["fa"]
    assert fa._confine(str(tree["users"] / "f.txt"))  # Users\nanda
    assert fa._confine(str(tree["broad"] / "top.txt"))


# I. traversal into protected root denied -------------------------------

def test_traversal_into_protected_denied(tree):
    fa = tree["fa"]
    sneaky = tree["desktop"] / ".." / "OneDrive" / "secret.txt"
    with pytest.raises(PathNotAllowed):
        fa._confine(str(sneaky))


# J & K. symlink/junction resolution ------------------------------------

def test_symlink_into_protected_denied(tree, tmp_path):
    fa = tree["fa"]
    link = tree["desktop"] / "link_to_onedrive"
    try:
        os.symlink(tree["onedrive"], link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    with pytest.raises(PathNotAllowed):
        fa._confine(str(link / "secret.txt"))  # resolves into protected


def test_symlink_outside_allowed_denied(tree, tmp_path):
    fa = tree["fa"]
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tree["desktop"] / "link_out"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    with pytest.raises(PathNotAllowed):
        fa._confine(str(link / "f.txt"))


# N. backward compatibility: no protected roots -------------------------

def test_backward_compat_no_protected(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("hi")
    fa = FileActions(allowed_roots=[ws])          # protected defaults to none
    assert fa.protected_roots == []
    assert fa.read(str(ws / "a.txt")).ok


# O. multiple drives behave independently -------------------------------

def test_multiple_drives_independent(tmp_path):
    c = tmp_path / "C"
    d = tmp_path / "D"
    c.mkdir()
    d.mkdir()
    fa = FileActions(allowed_roots=[c])
    with pytest.raises(PathNotAllowed):
        fa._confine(str(d / "f.txt"))
    fa2 = FileActions(allowed_roots=[c, d], protected_roots=[c / "OneDrive"])
    assert fa2._confine(str(d / "f.txt"))         # D allowed, unaffected
    with pytest.raises(PathNotAllowed):
        fa2._confine(str(c / "OneDrive" / "f.txt"))


# Q. list_directory cannot expose protected-root contents ---------------

def test_list_directory_hides_protected(tree):
    fa = tree["fa"]
    res = fa.list_dir(str(tree["users"]))
    names = {e["name"] for e in res.data}
    assert "Desktop" in names
    assert "OneDriveBackup" in names
    assert "OneDrive" not in names                 # protected entry hidden
    # Listing the protected dir directly is denied.
    denied = fa.list_dir(str(tree["onedrive"]))
    assert not denied.ok


# R. search_files cannot return protected-root files --------------------

def test_search_excludes_protected(tree):
    fa = tree["fa"]
    assert fa.search("secret").data == []          # onedrive/secret.txt hidden
    assert fa.search("deep").data == []            # onedrive/Sub/deep.txt hidden
    ok = fa.search("ok")
    assert any("ok.txt" in p for p in ok.data)     # desktop file still found


# S/T/U/V. read/write/delete/open denied inside protected ---------------

def test_read_write_delete_open_denied_in_protected(tree):
    fa = tree["fa"]
    secret = tree["onedrive"] / "secret.txt"
    assert not fa.read(str(secret)).ok
    assert not fa.write(str(tree["onedrive"] / "new.txt"), "x").ok
    assert not fa.delete(str(secret)).ok
    assert secret.exists()                          # delete was refused
    aa = AppActions(fa)
    assert not aa.open_path(str(secret)).ok


# L. nested protected roots deduplicate ---------------------------------

@pytest.fixture
def local(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    lp = tmp_path / "local_config.yaml"
    lp.write_text(yaml.safe_dump({"security": {"allowed_roots": [str(ws)]}}),
                  encoding="utf-8")
    return ws, lp


def test_protected_nested_dedup(local, tmp_path):
    ws, lp = local
    od = tmp_path / "OneDrive"
    (od / "Sub").mkdir(parents=True)
    roots.add_protected(str(od), local_path=lp)
    with pytest.raises(RootError):                  # nested is redundant
        roots.add_protected(str(od / "Sub"), local_path=lp)


def test_protected_ancestor_collapses(local, tmp_path):
    ws, lp = local
    od = tmp_path / "OneDrive"
    (od / "Sub").mkdir(parents=True)
    roots.add_protected(str(od / "Sub"), local_path=lp)
    roots.add_protected(str(od), local_path=lp)     # broader ancestor collapses
    prot = {_k(p) for p in roots.list_protected(local_path=lp)}
    assert prot == {_k(od)}


def test_protected_rejects_file_and_missing(local, tmp_path):
    ws, lp = local
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(RootError):
        roots.add_protected(str(f), local_path=lp)
    with pytest.raises(RootError):
        roots.add_protected(str(tmp_path / "nope"), local_path=lp)


def test_remove_protected(local, tmp_path):
    ws, lp = local
    od = tmp_path / "OneDrive"
    od.mkdir()
    roots.add_protected(str(od), local_path=lp)
    roots.remove_protected(str(od), local_path=lp)
    assert roots.list_protected(local_path=lp) == []
    with pytest.raises(RootError):
        roots.remove_protected(str(od), local_path=lp)  # already gone


# M. redundant allowed roots handled ------------------------------------

def test_redundant_allowed_root_rejected_and_collapsed(local, tmp_path):
    ws, lp = local
    broad = tmp_path
    # ws is inside tmp_path(broad); adding broad should collapse ws.
    roots.add_root(str(broad), local_path=lp)
    keys = {_k(p) for p in roots.list_roots(local_path=lp)}
    assert keys == {_k(broad)}                      # ws collapsed into broad
    # Adding a descendant of the broad root is now redundant.
    sub = broad / "sub"
    sub.mkdir()
    with pytest.raises(RootError):
        roots.add_root(str(sub), local_path=lp)


# Config plumbing + backward compat -------------------------------------

def test_config_protected_roots(tmp_path):
    ws = tmp_path / "ws"
    od = tmp_path / "od"
    ws.mkdir()
    od.mkdir()
    lp = tmp_path / "local_config.yaml"
    lp.write_text(yaml.safe_dump({"security": {
        "allowed_roots": [str(ws)], "protected_roots": [str(od)]}}),
        encoding="utf-8")
    cfg = Config.load(local_path=lp)
    assert {_k(p) for p in cfg.protected_roots()} == {_k(od)}


def test_config_protected_roots_default_empty(tmp_path):
    lp = tmp_path / "none.yaml"  # does not exist
    cfg = Config.load(local_path=lp)
    assert cfg.protected_roots() == []


# CLI (owner-only) ------------------------------------------------------

def test_cli_protect_add_and_list_persist(local, monkeypatch, tmp_path, capsys):
    ws, lp = local
    od = tmp_path / "OneDrive"
    od.mkdir()
    monkeypatch.setattr("void.roots.local_config_path", lambda: lp)
    from void import cli
    assert cli.main(["protect", "add", str(od)]) == 0
    data = yaml.safe_load(lp.read_text())
    assert {os.path.normcase(p) for p in data["security"]["protected_roots"]} \
        == {_k(od)}
    capsys.readouterr()
    assert cli.main(["protect", "list"]) == 0
    out = capsys.readouterr().out
    assert "Protected (excluded) filesystem roots:" in out
    assert str(od.resolve()) in out


def test_cli_protect_rejects_file(local, monkeypatch, tmp_path):
    ws, lp = local
    f = tmp_path / "f.txt"
    f.write_text("x")
    monkeypatch.setattr("void.roots.local_config_path", lambda: lp)
    from void import cli
    assert cli.main(["protect", "add", str(f)]) == 1


# P & 17. Mechanical security regression --------------------------------

def test_agent_has_no_filesystem_policy_tool(tmp_path):
    fa = FileActions(allowed_roots=[tmp_path])
    aa = AppActions(fa)
    names = {t.name for t in fa.tools()} | {t.name for t in aa.tools()}
    for n in names:
        assert "root" not in n.lower() and "protect" not in n.lower()
    assert not (names & {"add_root", "remove_root", "roots", "add_protected",
                         "remove_protected", "protect", "set_policy"})


def test_security_regression_protected_enforced_mechanically(tree):
    """C:\\ authorized + OneDrive protected: every file op on the protected
    tree is denied by the authorization boundary, regardless of the request,
    and the file tools expose no way to change policy."""
    fa = tree["fa"]
    secret = tree["onedrive"] / "secret.txt"
    # Enforced by _confine / traversal helpers, not by any prompt text:
    assert not fa.read(str(secret)).ok
    assert not fa.write(str(secret), "x", overwrite=True).ok
    assert not fa.delete(str(secret)).ok
    assert not fa.list_dir(str(tree["onedrive"])).ok
    assert fa.search("secret").data == []
    assert not AppActions(fa).open_path(str(secret)).ok
    # Even via traversal from an allowed dir.
    with pytest.raises(PathNotAllowed):
        fa._confine(str(tree["desktop"] / ".." / "OneDrive" / "secret.txt"))
    # No tool can widen access or drop the exclusion.
    tool_names = {t.name for t in fa.tools()}
    assert not (tool_names & {"add_root", "remove_protected", "protect"})
