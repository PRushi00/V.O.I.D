"""The folder index: bounded, identity-only, and with no security policy of its own.

Why it exists rather than reusing ``FileActions.find_dir``: measured on the owner's machine with
``allowed_roots = ["C:\\"]``, that walk takes 13.7-24.6 s and returns *ambiguous* for "Projects", "workspace" and
"Downloads" alike. What is pinned here is everything that makes a much smaller index safe:

  * deny by default - no confinement callable means nothing is indexed, never "the whole machine";
  * identity matching only - the same name, or the same name spaced differently. No prefix, no phonetic, no
    substring, and no way to address a path;
  * two matches ask, they never tie-break;
  * breadth-first traversal, because with overlapping roots depth-first plus de-duplication silently pruned a whole
    subtree (the defect that hid the owner's own Projects folder during development);
  * a bounded scan: a depth, a visit budget, and the noise/system/profile-template directories skipped.
"""
import threading
import time

import pytest

from void.actions.folders import (DEFAULT_DEPTH, SKIP_DIRS, FolderCatalog, scan_roots)


def tree(root, *paths):
    for p in paths:
        (root / p).mkdir(parents=True, exist_ok=True)
    return root


def cat(root, **kw):
    """A catalog over ``root`` that allows everything inside it (the file layer's job is tested separately)."""
    kw.setdefault("confine", lambda p: p)
    return FolderCatalog(roots=(str(root),), **kw)


# --- deny by default ------------------------------------------------------------------------------

def test_without_a_confinement_check_nothing_is_indexed(tmp_path):
    """No policy available means no access. The opposite default would expose the machine."""
    tree(tmp_path, "Projects", "Notes")
    c = FolderCatalog(roots=(str(tmp_path),))
    assert c.entries() == ()
    assert c.resolve_name("Projects").entry is None


def test_a_refused_path_is_not_indexed_but_its_children_are_still_considered(tmp_path):
    """Refusing to descend would depend on policy this module deliberately does not read."""
    tree(tmp_path, "outer/inner")
    refused = str(tmp_path / "outer")
    c = cat(tmp_path, confine=lambda p: None if str(p) == refused else p)
    names = {e.name for e in c.entries()}
    assert "outer" not in names and "inner" in names


def test_a_confinement_check_that_raises_is_a_refusal(tmp_path):
    tree(tmp_path, "Projects")

    def boom(p):
        raise RuntimeError("policy exploded")
    assert cat(tmp_path, confine=boom).entries() == ()


# --- identity matching only -----------------------------------------------------------------------

def test_the_same_name_resolves(tmp_path):
    tree(tmp_path, "Projects")
    m = cat(tmp_path).resolve_name("projects")
    assert m.entry is not None and m.tier == "exact" and m.entry.name == "Projects"


def test_the_same_name_spaced_differently_resolves(tmp_path):
    tree(tmp_path, "My Notes")
    m = cat(tmp_path).resolve_name("mynotes")
    assert m.entry is not None and m.tier == "spacing"


@pytest.mark.parametrize("query", [
    "project",            # a prefix of the name is NOT the name
    "projects archive",   # the name is a prefix of the query
    "projekts",           # phonetically close is not the same
    "pro",
    "jects",              # never a substring
    "",
    "   ",
])
def test_anything_short_of_identity_misses(tmp_path, query):
    tree(tmp_path, "Projects")
    assert cat(tmp_path).resolve_name(query).entry is None


@pytest.mark.parametrize("query", [
    r"C:\Windows", "../../secret", "Projects/inner", r"Projects\inner", "/etc/passwd",
    "%windir%", "\\\\server\\share", "Projects inner",
])
def test_a_multi_component_path_can_never_select_a_directory(tmp_path, query):
    """A path does not normalise to any indexed directory's BARE NAME, so it cannot address a location.

    "~/Projects" is deliberately absent: it normalises to the single word "projects", and so matches the indexed
    folder of that name - which is what it means anyway. The guarantee that matters is the next test's: whatever the
    query looked like, the path opened is the INDEX's, never the query's.
    """
    tree(tmp_path, "Projects", "Projects/inner")
    assert cat(tmp_path).resolve_name(query).entry is None


def test_the_resolved_path_always_comes_from_the_index_never_from_the_query(tmp_path):
    tree(tmp_path, "Projects")
    for query in ("projects", "Projects", "  PROJECTS  ", "~/Projects", "project's"):
        m = cat(tmp_path).resolve_name(query)
        if m.entry is not None:
            assert m.entry.path == str(tmp_path / "Projects")


def test_the_launch_grammar_never_lets_a_path_reach_the_folder_index():
    """Belt and braces: a phrase carrying a separator is refused by the grammar before resolution is attempted."""
    from void.core.fast_path import launch_targets
    for said in (r"open C:\Windows", "open ~/Projects", "open ../secret", "open %windir%",
                 r"open \\server\share", "open /etc/passwd"):
        assert launch_targets(said) == (), said


def test_two_folders_with_one_name_are_ambiguous_and_nothing_is_chosen(tmp_path):
    tree(tmp_path, "a/Notes", "b/Notes")
    m = cat(tmp_path).resolve_name("notes")
    assert m.entry is None and m.reason == "ambiguous"
    assert {c.name for c in m.candidates} == {"Notes"}
    assert len({c.path for c in m.candidates}) == 2


def test_ambiguous_candidates_are_capped(tmp_path):
    tree(tmp_path, *[f"r{i}/Notes" for i in range(9)])
    m = cat(tmp_path, depth=3).resolve_name("notes")
    assert m.reason == "ambiguous" and len(m.candidates) <= 4


def test_a_bad_query_type_never_raises(tmp_path):
    tree(tmp_path, "Projects")
    c = cat(tmp_path)
    for bad in (None, 12345, b"projects", ["projects"], object()):
        assert c.resolve_name(bad).entry is None


# --- the scan is bounded --------------------------------------------------------------------------

def test_the_scan_is_breadth_first_so_overlapping_roots_cannot_prune(tmp_path):
    """The real defect: depth-first plus de-duplication hid a subtree reachable shallower from another root."""
    tree(tmp_path, "home/OneDrive/Attachments/Projects")
    home = tmp_path / "home"
    c = FolderCatalog(roots=(str(home), str(tmp_path)), confine=lambda p: p, depth=3)
    assert c.resolve_name("projects").entry is not None, "a subtree was pruned by traversal order"


def test_depth_bounds_how_deep_a_name_can_be_seen(tmp_path):
    tree(tmp_path, "a/b/c/d/Deep")
    assert cat(tmp_path, depth=2).resolve_name("deep").entry is None
    assert cat(tmp_path, depth=5).resolve_name("deep").entry is not None


def test_the_default_depth_reaches_a_redirected_desktop(tmp_path):
    """OneDrive redirection puts the owner's folders three levels down; depth 2 could not see them."""
    tree(tmp_path, "OneDrive/Attachments/Projects")
    assert DEFAULT_DEPTH >= 3
    assert cat(tmp_path).resolve_name("projects").entry is not None


def test_the_visit_budget_stops_the_scan(tmp_path):
    tree(tmp_path, *[f"d{i}" for i in range(50)])
    assert len(cat(tmp_path, budget=10).entries()) <= 10


def test_the_budget_gives_up_the_deepest_level_first(tmp_path):
    """A consequence of breadth-first: what is lost is the least useful level, not an arbitrary subtree."""
    tree(tmp_path, *[f"top{i}" for i in range(6)], *[f"top0/deep{i}" for i in range(20)])
    names = {e.name for e in cat(tmp_path, budget=8).entries()}
    assert any(n.startswith("top") for n in names)


@pytest.mark.parametrize("noisy", ["node_modules", ".git", "AppData", "Windows", "Program Files",
                                   "Default", "Public", "All Users", "ProgramData"])
def test_noise_system_and_profile_template_directories_are_skipped(tmp_path, noisy):
    """"Public" and "Default" carry a full set of Downloads/Documents/Desktop and made every one ambiguous."""
    tree(tmp_path, noisy + "/Inside")
    c = cat(tmp_path)
    assert noisy in SKIP_DIRS
    assert c.resolve_name(noisy).entry is None
    assert c.resolve_name("inside").entry is None


def test_a_hidden_directory_is_skipped(tmp_path):
    tree(tmp_path, ".secret")
    assert cat(tmp_path).resolve_name("secret").entry is None


def test_an_unreadable_directory_does_not_break_the_scan(tmp_path):
    tree(tmp_path, "good")

    def scandir(path):
        if str(path).endswith("good"):
            raise PermissionError("no")
        import os
        return os.scandir(path)
    c = cat(tmp_path, scandir=scandir)
    assert {e.name for e in c.entries()} == {"good"}


# --- caching and concurrency ----------------------------------------------------------------------

def test_the_scan_is_cached_for_the_ttl(tmp_path):
    tree(tmp_path, "Projects")
    now = [1000.0]
    c = cat(tmp_path, ttl_s=60.0, clock=lambda: now[0])
    c.entries()
    c.resolve_name("projects")
    assert c.scans == 1
    now[0] += 30
    c.resolve_name("projects")
    assert c.scans == 1
    now[0] += 40
    c.resolve_name("projects")
    assert c.scans == 2


def test_invalidate_forces_a_rescan(tmp_path):
    tree(tmp_path, "Projects")
    c = cat(tmp_path)
    c.entries()
    c.invalidate()
    c.entries()
    assert c.scans == 2


def test_a_folder_created_after_the_scan_is_found_once_the_ttl_expires(tmp_path):
    now = [0.0]
    c = cat(tmp_path, ttl_s=10.0, clock=lambda: now[0])
    assert c.resolve_name("later").entry is None
    tree(tmp_path, "Later")
    now[0] += 11
    assert c.resolve_name("later").entry is not None


def test_callers_arriving_together_share_one_scan(tmp_path):
    tree(tmp_path, "Projects")
    slow = {"n": 0}
    import os as _os

    def scandir(path):
        if str(path) == str(tmp_path):
            slow["n"] += 1
            time.sleep(0.15)
        return _os.scandir(path)
    c = cat(tmp_path, scandir=scandir)
    threads = [threading.Thread(target=c.entries) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert slow["n"] == 1 and c.scans == 1


def test_entries_is_published_after_the_indexes_it_stands_for():
    """A non-None _entries means the indexes behind it are ready - that is what makes lock-free reads safe."""
    import inspect
    src = inspect.getsource(FolderCatalog._build)
    published = src.index("self._entries = tuple(entries)")
    for attr in ("self._by_norm, self._by_squash = by_norm, by_squash",):
        assert attr in src
        assert src.index(attr) < published, "an index is assigned AFTER _entries; a reader could see it empty"


# --- roots ----------------------------------------------------------------------------------------

def test_scan_roots_puts_home_first_and_drops_duplicates(tmp_path):
    assert scan_roots(tmp_path, [str(tmp_path), str(tmp_path)]) == (str(tmp_path),)
    got = scan_roots(tmp_path, ["C:\\"])
    assert got[0] == str(tmp_path) and len(got) == 2


def test_scan_roots_ignores_empty_and_unusable_entries(tmp_path):
    assert scan_roots(None, ["", None, str(tmp_path)]) == (str(tmp_path),)


def test_a_root_that_does_not_exist_is_harmless(tmp_path):
    c = FolderCatalog(roots=(str(tmp_path / "nope"),), confine=lambda p: p)
    assert c.entries() == ()
