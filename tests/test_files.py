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


# --- find_directory -----------------------------------------------------

def test_find_directory_finds_nested(tmp_path):
    target = tmp_path / "a" / "b" / "Hackathon"
    target.mkdir(parents=True)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Hackathon")
    assert res.ok
    assert len(res.data) == 1
    assert res.data[0]["name"] == "Hackathon"
    assert Path(res.data[0]["path"]) == target.resolve()


def test_find_directory_nonexistent(tmp_path):
    (tmp_path / "something").mkdir()
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Nope")
    assert res.ok and res.data == []
    assert "No directories matched" in res.summary


def test_find_directory_case_insensitive_exact(tmp_path):
    (tmp_path / "Hackathon").mkdir()
    fa = FileActions(allowed_roots=[tmp_path])
    assert fa.find_dir("hackathon").data[0]["name"] == "Hackathon"
    assert fa.find_dir("HACKATHON").data[0]["name"] == "Hackathon"


def test_find_directory_no_substring_match(tmp_path):
    (tmp_path / "Projects").mkdir()
    fa = FileActions(allowed_roots=[tmp_path])
    assert fa.find_dir("Project").data == []      # exact only, not substring
    assert len(fa.find_dir("Projects").data) == 1


def test_find_directory_glob(tmp_path):
    (tmp_path / "Proj_A").mkdir()
    (tmp_path / "Proj_B").mkdir()
    (tmp_path / "Other").mkdir()
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Proj*")
    names = {d["name"] for d in res.data}
    assert names == {"Proj_A", "Proj_B"}


def test_find_directory_empty_query_fails(tmp_path):
    fa = FileActions(allowed_roots=[tmp_path])
    assert not fa.find_dir("   ").ok


def test_find_directory_multiple_is_ambiguous(tmp_path):
    (tmp_path / "x" / "Data").mkdir(parents=True)
    (tmp_path / "y" / "Data").mkdir(parents=True)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Data")
    assert res.ok and len(res.data) == 2
    assert "ambiguous" in res.summary.lower()
    # Tool does not pick a winner: both paths present.
    assert {Path(d["path"]).parent.name for d in res.data} == {"x", "y"}


def test_find_directory_max_results_caps(tmp_path):
    for i in range(5):
        (tmp_path / f"p{i}" / "Dup").mkdir(parents=True)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Dup", max_results=2)
    assert len(res.data) == 2
    assert "results may be incomplete" in res.summary


def test_find_directory_hard_ceiling(tmp_path):
    for i in range(51):
        (tmp_path / f"p{i:02d}" / "Dup").mkdir(parents=True)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Dup", max_results=100)   # request over the ceiling
    assert len(res.data) == 50                  # clamped to hard ceiling


def test_find_directory_visited_truncation(tmp_path, monkeypatch):
    import void.actions.files as filesmod
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    monkeypatch.setattr(filesmod, "_FIND_MAX_VISITED", 1)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("does_not_exist")
    assert res.ok and res.data == []
    assert "INCOMPLETE" in res.summary          # must NOT claim "no directories"
    assert "No directories matched" not in res.summary


def test_find_directory_prunes_noise_and_dotdirs(tmp_path):
    (tmp_path / "Windows" / "Target").mkdir(parents=True)      # system noise
    (tmp_path / "node_modules" / "Target").mkdir(parents=True)  # _SKIP_DIRS
    (tmp_path / ".hidden" / "Target").mkdir(parents=True)       # dotted
    (tmp_path / "normal" / "Target").mkdir(parents=True)        # should be found
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Target")
    assert len(res.data) == 1
    assert Path(res.data[0]["path"]).parent.name == "normal"


def test_find_directory_root_authorized(tmp_path):
    (tmp_path / "sub" / "Target").mkdir(parents=True)
    (tmp_path / "other" / "Target").mkdir(parents=True)
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("Target", root=str(tmp_path / "sub"))
    assert len(res.data) == 1
    assert Path(res.data[0]["path"]).parent.name == "sub"


def test_find_directory_root_unauthorized(tmp_path):
    (tmp_path / "allowed").mkdir()
    fa = FileActions(allowed_roots=[tmp_path / "allowed"])
    res = fa.find_dir("x", root=str(tmp_path.parent))   # outside allowed
    assert not res.ok
    assert "outside the allowed roots" in res.summary


def test_find_directory_root_is_a_file(tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x")
    fa = FileActions(allowed_roots=[tmp_path])
    res = fa.find_dir("x", root=str(f))
    assert not res.ok
    assert "Not a directory" in res.summary


def test_find_directory_registered_as_low_risk_tool(fs):
    from void.security.risk import RiskLevel
    tool = next((t for t in fs.tools() if t.name == "find_directory"), None)
    assert tool is not None
    assert tool.risk is RiskLevel.LOW
    assert tool.parameters["required"] == ["query"]
