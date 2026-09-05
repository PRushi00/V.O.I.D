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


# --- list_directory -----------------------------------------------------

def _by_name(data):
    return {e["name"]: e for e in data}


def test_list_directory_default_lists_allowed_root(fs, tmp_path):
    # No path -> lists the configured allowed root(s).
    res = fs.list_dir()
    assert res.ok
    names = _by_name(res.data)
    assert "readme.md" in names       # a file at the root
    assert "notes" in names           # a subdirectory at the root


def test_list_directory_type_markers(fs, tmp_path):
    res = fs.list_dir(str(tmp_path))
    assert res.ok
    names = _by_name(res.data)
    assert names["notes"]["type"] == "directory"
    assert names["readme.md"]["type"] == "file"
    # The human-readable summary carries [type] markers too.
    assert "[directory]" in res.summary and "[file]" in res.summary


def test_list_directory_lists_files_in_subdir(fs, tmp_path):
    res = fs.list_dir(str(tmp_path / "notes"))
    assert res.ok
    names = _by_name(res.data)
    assert set(names) == {"cybersecurity_notes.md", "grocery.txt"}
    assert all(e["type"] == "file" for e in res.data)


def test_list_directory_lists_subdirectories(fs, tmp_path):
    (tmp_path / "Projects").mkdir()
    res = fs.list_dir(str(tmp_path))
    names = _by_name(res.data)
    assert names["Projects"]["type"] == "directory"


def test_list_directory_returns_usable_paths(fs, tmp_path):
    res = fs.list_dir(str(tmp_path))
    entry = _by_name(res.data)["notes"]
    # Absolute path pointing at the real entry, usable by later tool calls.
    assert Path(entry["path"]) == (tmp_path / "notes")
    assert Path(entry["path"]).is_dir()


def test_list_directory_confined_to_allowed_root(fs, tmp_path):
    # A confined subpath is allowed and lists correctly.
    res = fs.list_dir(str(tmp_path / "notes"))
    assert res.ok
    assert all(str(tmp_path) in e["path"] for e in res.data)


def test_list_directory_rejects_outside_allowed_roots(fs, tmp_path):
    outside = tmp_path.parent  # parent of the allowed root
    res = fs.list_dir(str(outside))
    assert not res.ok
    assert "outside the allowed roots" in res.summary


def test_list_directory_empty_directory(fs, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = fs.list_dir(str(empty))
    assert res.ok
    assert res.data == []
    assert "empty" in res.summary.lower()


def test_list_directory_result_limit(fs, tmp_path):
    many = tmp_path / "many"
    many.mkdir()
    for i in range(10):
        (many / f"f{i}.txt").write_text("x")
    res = fs.list_dir(str(many), max_entries=4)
    assert res.ok
    assert len(res.data) == 4
    assert "truncated to 4" in res.summary


def test_list_directory_skips_noise_dirs(fs, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "__pycache__").mkdir()
    res = fs.list_dir(str(tmp_path))
    names = _by_name(res.data)
    assert ".git" not in names and "__pycache__" not in names


def test_list_directory_not_a_directory(fs, tmp_path):
    res = fs.list_dir(str(tmp_path / "readme.md"))
    assert not res.ok
    assert "Not a directory" in res.summary


def test_list_directory_empty_roots_denies(tmp_path):
    # Deny-by-default: no configured roots means no listing at all.
    fa = FileActions(allowed_roots=[])
    res = fa.list_dir()
    assert not res.ok
    assert "denied" in res.summary.lower()


def test_list_directory_registered_as_low_risk_tool(fs):
    from void.security.risk import RiskLevel
    tool = next((t for t in fs.tools() if t.name == "list_directory"), None)
    assert tool is not None
    assert tool.risk is RiskLevel.LOW
    assert "path" in tool.parameters["properties"]
    assert tool.parameters["required"] == []
