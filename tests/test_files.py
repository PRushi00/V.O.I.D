"""Tests for the file action layer: confinement, search, read, write, delete."""
from pathlib import Path

import pytest

from void.actions.files import FileActions, PathNotAllowed


@pytest.fixture
def fs(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "cybersecurity_notes.md").write_text("firewall basics")
    (tmp_path / "notes" / "grocery.txt").write_text("milk")
    (tmp_path / "readme.md").write_text("hello")
    return FileActions(allowed_roots=[tmp_path], delete_to_recycle_bin=True)


def test_search_substring(fs, tmp_path):
    res = fs.search("cyber")
    assert res.ok
    assert any("cybersecurity_notes.md" in p for p in res.data)


def test_search_glob(fs):
    res = fs.search("*.md")
    assert res.ok
    names = [Path(p).name for p in res.data]
    assert "readme.md" in names and "cybersecurity_notes.md" in names
    assert "grocery.txt" not in names


def test_search_no_match(fs):
    res = fs.search("nonexistentxyz")
    assert res.ok and res.data == []


def test_read(fs, tmp_path):
    res = fs.read(str(tmp_path / "readme.md"))
    assert res.ok and res.data == "hello"


def test_confinement_blocks_outside(fs, tmp_path):
    # A path outside the allowed root must be refused.
    outside = tmp_path.parent / "somewhere_else.txt"
    res = fs.read(str(outside))
    assert not res.ok
    assert "outside the allowed roots" in res.summary


def test_confine_raises_directly(fs, tmp_path):
    with pytest.raises(PathNotAllowed):
        fs._confine(str(tmp_path.parent / "evil.txt"))


def test_write_create_and_overwrite(fs, tmp_path):
    target = tmp_path / "new" / "file.txt"
    res = fs.write(str(target), "v1")
    assert res.ok and target.read_text() == "v1"

    # Without overwrite it should refuse.
    res2 = fs.write(str(target), "v2")
    assert not res2.ok

    res3 = fs.write(str(target), "v2", overwrite=True)
    assert res3.ok and target.read_text() == "v2"


def test_delete_to_trash(fs, tmp_path):
    target = tmp_path / "notes" / "grocery.txt"
    assert target.exists()
    res = fs.delete(str(target))
    assert res.ok, res.summary
    assert not target.exists()  # moved to trash/recycle bin


def test_delete_missing(fs, tmp_path):
    res = fs.delete(str(tmp_path / "notes" / "does_not_exist.txt"))
    assert not res.ok


def test_empty_roots_denies_everything(tmp_path):
    # V1 safety: no configured roots => deny all (never allow the whole machine).
    fa = FileActions(allowed_roots=[])
    target = tmp_path / "readme.md"
    target.write_text("hi")
    assert not fa.read(str(target)).ok           # denied
    assert fa.search("readme").data == []        # nothing searchable
    with pytest.raises(PathNotAllowed):
        fa._confine(str(target))


def test_write_risk_existing_vs_new(fs, tmp_path):
    from void.security.risk import RiskLevel
    write_tool = next(t for t in fs.tools() if t.name == "write_file")
    existing = tmp_path / "readme.md"           # created by the fs fixture
    new = tmp_path / "brand_new.md"
    # New file => MEDIUM (autonomous); existing file => HIGH (needs confirmation).
    assert write_tool.effective_risk({"path": str(new)}) is RiskLevel.MEDIUM
    assert write_tool.effective_risk({"path": str(existing)}) is RiskLevel.HIGH
