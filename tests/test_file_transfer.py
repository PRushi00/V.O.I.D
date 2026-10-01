"""File metadata, copy and move: the V2 domain-2 gaps, and the confinement they must not widen.

A transfer is the first file operation with *two* paths, which makes it the first one that could be used to
reach somewhere ``write_file`` would refuse - by naming an allowed source and a forbidden destination, or the
reverse. Most of this file is that question, asked from both ends.

The rest pins the behaviour that makes these usable: a destination that names a folder means "into that
folder", replacing an existing file asks the owner while creating a new one does not, and folders are
deliberately not transferable.
"""
import os
from pathlib import Path

import pytest

from void.actions.files import FileActions, _size, _when
from void.security.protected import EngineProtected
from void.security.risk import RiskLevel


@pytest.fixture
def rig(tmp_path):
    """An allowed root, a forbidden sibling, a protected subtree, and a file to move around."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "theirs.txt").write_text("not yours", encoding="utf-8")
    protected = root / "secrets"
    protected.mkdir()
    (protected / "key.txt").write_text("sensitive", encoding="utf-8")
    source = root / "notes.txt"
    source.write_text("hello world", encoding="utf-8")
    (root / "Archive").mkdir()
    fa = FileActions([root], protected_roots=[protected],
                     engine_protected=EngineProtected.default(state_dir=tmp_path / ".void"))
    return fa, root, outside, protected, source


# --- metadata --------------------------------------------------------------------------------------

def test_file_metadata_describes_without_reading(rig):
    fa, root, _out, _prot, source = rig
    out = fa.stat(str(source))
    assert out.ok
    assert out.data["kind"] == "file"
    assert out.data["size_bytes"] == len("hello world")
    assert out.data["name"] == "notes.txt"
    assert out.data["suffix"] == ".txt"
    assert out.data["modified"]
    # The contents must not appear anywhere in the answer - that is what read_file is for.
    assert "hello world" not in out.summary
    assert "hello world" not in repr(out.data)


def test_folder_metadata_counts_without_listing(rig):
    fa, root, _out, _prot, _source = rig
    (root / "Archive" / "a.txt").write_text("a", encoding="utf-8")
    (root / "Archive" / "b.txt").write_text("b", encoding="utf-8")
    out = fa.stat(str(root / "Archive"))
    assert out.data["kind"] == "folder"
    assert out.data["entries"] == 2
    assert out.data["size_bytes"] is None, "a folder reported a byte size"
    assert "a.txt" not in out.summary, "a count turned into a listing"


def test_metadata_refuses_a_path_outside_the_allowed_roots(rig):
    fa, _root, outside, _prot, _source = rig
    out = fa.stat(str(outside / "theirs.txt"))
    assert out.ok is False
    assert "outside the allowed roots" in out.summary


def test_metadata_refuses_a_protected_path(rig):
    fa, _root, _out, protected, _source = rig
    assert fa.stat(str(protected / "key.txt")).ok is False


def test_metadata_on_nothing_is_a_clear_answer(rig):
    fa, root, _out, _prot, _source = rig
    out = fa.stat(str(root / "absent.txt"))
    assert out.ok is False and "Nothing exists" in out.summary


def test_a_read_only_file_is_reported_as_such(rig, monkeypatch):
    fa, _root, _out, _prot, source = rig
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    out = fa.stat(str(source))
    assert out.data["read_only"] is True
    assert "read-only" in out.summary


def test_a_byte_count_is_rendered_for_a_person():
    assert _size(220) == "220 B"
    assert _size(4 * 1024) == "4.0 KB"
    assert _size(3 * 1024 ** 2) == "3.0 MB"
    assert _size(2 * 1024 ** 3) == "2.0 GB"
    assert _size(9 * 1024 ** 4).endswith("GB"), "a huge file produced an unknown unit"


def test_an_unusable_timestamp_is_none_rather_than_a_wrong_date():
    assert _when(1_700_000_000) is not None
    assert _when(float("nan")) is None
    assert _when(10 ** 20) is None


# --- copy and move: confinement, from both ends -----------------------------------------------------

def test_a_copy_inside_the_allowed_root_works(rig):
    fa, root, _out, _prot, source = rig
    out = fa.copy(str(source), str(root / "copy.txt"))
    assert out.ok
    assert (root / "copy.txt").read_text(encoding="utf-8") == "hello world"
    assert source.exists(), "a copy removed the source"


def test_a_move_relocates_and_removes_the_source(rig):
    fa, root, _out, _prot, source = rig
    out = fa.move(str(source), str(root / "moved.txt"))
    assert out.ok
    assert (root / "moved.txt").exists()
    assert not source.exists()


def test_a_destination_outside_the_allowed_roots_is_refused(rig):
    """The whole point of confining both ends: an allowed source must not be a way out."""
    fa, _root, outside, _prot, source = rig
    for call in (fa.copy, fa.move):
        out = call(str(source), str(outside / "stolen.txt"))
        assert out.ok is False, call.__name__
        assert "outside the allowed roots" in out.summary
    assert not (outside / "stolen.txt").exists()
    assert source.exists(), "a refused move removed the source anyway"


def test_a_source_outside_the_allowed_roots_is_refused(rig):
    fa, root, outside, _prot, _source = rig
    for call in (fa.copy, fa.move):
        out = call(str(outside / "theirs.txt"), str(root / "taken.txt"))
        assert out.ok is False, call.__name__
    assert not (root / "taken.txt").exists()
    assert (outside / "theirs.txt").exists()


def test_traversal_in_either_path_is_refused(rig):
    fa, root, outside, _prot, source = rig
    escape = str(root / ".." / "outside" / "t.txt")
    assert fa.copy(str(source), escape).ok is False
    assert fa.move(str(source), escape).ok is False
    assert not (outside / "t.txt").exists()


def test_a_protected_destination_is_refused(rig):
    """Protected roots override allowed roots, and a transfer must not be the exception."""
    fa, _root, _out, protected, source = rig
    for call in (fa.copy, fa.move):
        out = call(str(source), str(protected / "planted.txt"))
        assert out.ok is False, call.__name__
    assert not (protected / "planted.txt").exists()


def test_a_protected_source_cannot_be_exfiltrated_into_an_allowed_folder(rig):
    """The attack this blocks: read a protected secret by copying it somewhere readable."""
    fa, root, _out, protected, _source = rig
    for call in (fa.copy, fa.move):
        out = call(str(protected / "key.txt"), str(root / "leaked.txt"))
        assert out.ok is False, call.__name__
    assert not (root / "leaked.txt").exists()
    assert (protected / "key.txt").exists()


def test_an_engine_protected_location_is_refused(rig, tmp_path):
    """V.O.I.D's own state is engine-protected, above config: a transfer must not reach it either."""
    fa, root, _out, _prot, source = rig
    state = tmp_path / ".void"
    for call in (fa.copy, fa.move):
        assert call(str(source), str(state / "tasks.sqlite")).ok is False, call.__name__


def test_no_allowed_roots_means_no_transfer(tmp_path):
    fa = FileActions([], engine_protected=EngineProtected.default(state_dir=tmp_path / ".void"))
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    out = fa.copy(str(a), str(tmp_path / "b.txt"))
    assert out.ok is False
    assert "No allowed roots" in out.summary


# --- copy and move: behaviour --------------------------------------------------------------------

def test_a_folder_destination_means_into_that_folder(rig):
    fa, root, _out, _prot, source = rig
    out = fa.copy(str(source), str(root / "Archive"))
    assert out.ok
    assert (root / "Archive" / "notes.txt").exists()


def test_a_folder_destination_still_respects_confinement(rig):
    fa, _root, outside, _prot, source = rig
    assert fa.copy(str(source), str(outside)).ok is False


def test_an_existing_destination_is_not_replaced_without_being_asked(rig):
    fa, root, _out, _prot, source = rig
    target = root / "taken.txt"
    target.write_text("original", encoding="utf-8")
    out = fa.copy(str(source), str(target))
    assert out.ok is False
    assert "already exists" in out.summary
    assert target.read_text(encoding="utf-8") == "original", "the file was replaced anyway"


def test_overwrite_replaces_it(rig):
    fa, root, _out, _prot, source = rig
    target = root / "taken.txt"
    target.write_text("original", encoding="utf-8")
    assert fa.copy(str(source), str(target), overwrite=True).ok
    assert target.read_text(encoding="utf-8") == "hello world"


def test_a_folder_cannot_be_transferred(rig):
    """Deliberately out of scope: one wrong argument on a recursive move is unrecoverable."""
    fa, root, _out, _prot, _source = rig
    for call in (fa.copy, fa.move):
        out = call(str(root / "Archive"), str(root / "Archive2"))
        assert out.ok is False, call.__name__
        assert "only" in out.summary and "folders" in out.summary
    assert not (root / "Archive2").exists()


def test_a_link_is_not_transferred(rig):
    """A link's target may be anywhere; copying one produces something ambiguous."""
    fa, root, outside, _prot, _source = rig
    link = root / "link.txt"
    try:
        link.symlink_to(outside / "theirs.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this environment cannot create symlinks")
    out = fa.copy(str(link), str(root / "resolved.txt"))
    assert out.ok is False
    assert "link" in out.summary


def test_transferring_a_file_onto_itself_is_refused(rig):
    fa, _root, _out, _prot, source = rig
    assert fa.copy(str(source), str(source)).ok is False
    assert fa.move(str(source), str(source)).ok is False
    assert source.read_text(encoding="utf-8") == "hello world", "the file was damaged"


def test_transferring_something_absent_is_a_clear_answer(rig):
    fa, root, _out, _prot, _source = rig
    out = fa.move(str(root / "ghost.txt"), str(root / "elsewhere.txt"))
    assert out.ok is False and "Nothing to move" in out.summary


def test_a_missing_parent_directory_is_created(rig):
    fa, root, _out, _prot, source = rig
    out = fa.copy(str(source), str(root / "deep" / "nested" / "notes.txt"))
    assert out.ok
    assert (root / "deep" / "nested" / "notes.txt").exists()


def test_a_copy_preserves_the_modification_time(rig):
    """copy2, not copy: "when did I last change this?" must survive being filed away."""
    fa, root, _out, _prot, source = rig
    before = source.stat().st_mtime
    fa.copy(str(source), str(root / "kept.txt"))
    assert (root / "kept.txt").stat().st_mtime == pytest.approx(before, abs=2)


# --- risk ------------------------------------------------------------------------------------------

def test_creating_something_new_is_medium_and_replacing_is_high(rig):
    """Same reasoning as write_file: a new file is recoverable, a destroyed one is not."""
    fa, root, _out, _prot, source = rig
    existing = root / "existing.txt"
    existing.write_text("x", encoding="utf-8")
    assert fa._transfer_risk({"source": str(source),
                              "destination": str(root / "new.txt")}) is RiskLevel.MEDIUM
    assert fa._transfer_risk({"source": str(source),
                              "destination": str(existing)}) is RiskLevel.HIGH


def test_a_folder_destination_is_judged_by_the_file_it_would_land_on(rig):
    fa, root, _out, _prot, source = rig
    assert fa._transfer_risk({"source": str(source),
                              "destination": str(root / "Archive")}) is RiskLevel.MEDIUM
    (root / "Archive" / "notes.txt").write_text("already here", encoding="utf-8")
    assert fa._transfer_risk({"source": str(source),
                              "destination": str(root / "Archive")}) is RiskLevel.HIGH


def test_risk_fails_safe_to_high(rig):
    fa, _root, _out, _prot, _source = rig
    for arguments in ({}, {"source": "x"}, {"destination": None},
                      {"source": None, "destination": "\x00"}):
        assert fa._transfer_risk(arguments) is RiskLevel.HIGH, arguments


def test_the_registered_tools_carry_the_dynamic_risk(rig):
    fa, root, _out, _prot, source = rig
    by_name = {t.name: t for t in fa.tools()}
    assert {"get_file_info", "copy_file", "move_file"} <= set(by_name)
    assert by_name["get_file_info"].risk is RiskLevel.LOW
    assert by_name["get_file_info"].risk_fn is None
    existing = root / "existing.txt"
    existing.write_text("x", encoding="utf-8")
    for name in ("copy_file", "move_file"):
        tool = by_name[name]
        assert tool.risk_fn is not None, name
        assert tool.effective_risk({"source": str(source),
                                    "destination": str(root / "fresh.txt")}) is RiskLevel.MEDIUM
        assert tool.effective_risk({"source": str(source),
                                    "destination": str(existing)}) is RiskLevel.HIGH


def test_the_new_tools_declare_usable_schemas(rig):
    fa = rig[0]
    for tool in fa.tools():
        if tool.name not in ("get_file_info", "copy_file", "move_file"):
            continue
        assert len(tool.description) > 60, tool.name
        assert tool.parameters["type"] == "object"
        for required in tool.parameters["required"]:
            assert required in tool.parameters["properties"], f"{tool.name}.{required}"
