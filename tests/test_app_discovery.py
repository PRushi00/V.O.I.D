"""Automatic application discovery and deterministic name resolution.

V.O.I.D must never require a hand-maintained list of installed applications: it discovers them from Windows, keeps
that catalog fresh by itself, and resolves what the owner SAYS to exactly one of them - or to none. Everything here
runs over the fake backend (no pywin32, no registry, no GUI, no model); the real Windows sources are exercised by
verify/computer_smoke.py.

The property under test throughout is that a wrong match is worse than no match: resolution is exact by
construction (case, punctuation, spacing and vendor decoration are made comparable, never approximated), and
several candidates is an outcome in its own right that nothing is allowed to tie-break.
"""
import time

import pytest

from void.actions.app_names import (
    ALIASES, PUBLISHER_PREFIXES, canonical_query, is_prefix, normalise, squash, tokens, without_publisher,
)
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, AppEntry, ComputerActions, is_app_user_model_id
from void.actions.files import FileActions

from tests.test_computer import FakeBackend, _exe

WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"


def _cat(tmp_path, *apps, **kw):
    """A catalog over `apps`, each ("Display Name",) or ("Display Name", "file.lnk") or a full record dict."""
    rows = []
    for i, a in enumerate(apps):
        if isinstance(a, dict):
            rows.append(a)
            continue
        name, target = (a, f"{i}.exe") if isinstance(a, str) else a
        kind = "lnk" if target.endswith(".lnk") else "exe"
        rows.append({"name": name, "kind": kind, "target": _exe(tmp_path, target)})
    return AppCatalog(FakeBackend(apps=rows), **kw)


# --- normalisation (pure, no catalog) -------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("Opera GX", "opera gx"),            # case
    ("Opera GX", "Opera  GX"),           # repeated whitespace
    ("Opera GX", "Opera-GX"),            # separator punctuation
    ("Opera GX", "Opera_GX"),
    ("Opera GX", "  Opera GX  "),
    ("Node.js", "Node js"),
    ("Intel\u00ae Graphics", "Intel Graphics"),          # vendor decoration
    ("Sticky Notes (new)", "sticky notes new"),
])
def test_names_that_differ_only_in_formatting_normalise_together(a, b):
    assert normalise(a) == normalise(b) and tokens(a) == tokens(b)


@pytest.mark.parametrize("a, b", [
    ("Notepad", "Notepad++"),            # '+' carries meaning: two different programs
    ("C", "C#"),
    ("Windows Security", "Security"),    # a word is never dropped just because it is common
    ("Opera GX", "GX Opera"),
    ("Visual Studio", "Visual Studio Code"),
])
def test_names_of_different_programs_never_normalise_together(a, b):
    assert normalise(a) != normalise(b) and squash(a) != squash(b)


@pytest.mark.parametrize("said, installed", [
    ("Whats App", "WhatsApp"),           # speech-to-text splits a run-together name
    ("Opera G X", "Opera GX"),           # ... and spells out an acronym
    ("Note Pad", "Notepad"),
    ("Opera-GX", "Opera GX"),
])
def test_voice_and_punctuation_variations_squash_to_the_installed_name(said, installed):
    assert squash(said) == squash(installed)


def test_letter_runs_are_only_rejoined_when_they_were_split_apart():
    assert tokens("opera g x") == ("opera", "gx")
    assert tokens("a b c") == ("abc",)
    assert tokens("python 3 14") == ("python", "3", "14")      # a lone short word is not a spelled-out letter
    assert tokens("windows 11") == ("windows", "11")


@pytest.mark.parametrize("bad", [None, 12345, b"notepad", ["notepad"], object()])
def test_normalisation_never_raises_on_junk(bad):
    assert normalise(bad) == "" and tokens(bad) == () and squash(bad) == ""


def test_prefix_is_whole_words_from_the_start_only():
    name = ("opera", "gx", "browser")
    assert is_prefix(("opera", "gx"), name) and is_prefix(("opera",), name) and is_prefix(name, name)
    for q in [("gx",), ("browser",), ("gx", "browser"), ("gx", "opera"), ("opera", "browser"), ()]:
        assert not is_prefix(q, name)


def test_only_a_leading_vendor_word_is_strippable_and_never_the_whole_name():
    assert without_publisher(tokens("Microsoft Teams")) == ("teams",)
    assert without_publisher(tokens("Windows Security")) is None       # "windows" is meaningful, not a vendor
    assert without_publisher(tokens("Microsoft")) is None              # never strips a name down to nothing
    assert without_publisher(tokens("Teams Microsoft")) is None        # leading only
    assert "windows" not in PUBLISHER_PREFIXES


def test_the_alias_table_stays_a_handful_of_naming_differences_not_an_app_list():
    # Section 8: aliases exist for genuinely irregular spoken forms. An application list would defeat the point of
    # discovering applications automatically, so this is pinned small and normalised.
    assert len(ALIASES) <= 12
    for said, means in ALIASES.items():
        assert normalise(said) == said and normalise(means) == means
    assert canonical_query("VS Code") == ("visual", "studio", "code")
    assert canonical_query("Opera GX") == ("opera", "gx")              # no alias needed; discovery covers it


# --- discovery: one logical application per installed program -------------------------------------

def test_the_same_application_found_by_several_sources_becomes_one_entry(tmp_path):
    # The real machine: Discord has a Start-Menu shortcut in two folders, Excel has a shortcut AND a registry
    # entry. Two entries would make the exact name ambiguous and silently stop the command fast-pathing.
    cat = _cat(tmp_path,
               {"name": "Discord", "kind": "lnk", "target": _exe(tmp_path, "Discord.lnk")},
               {"name": "Discord", "kind": "lnk", "target": str(tmp_path / "sub" / "Discord.lnk")},
               {"name": "Excel", "kind": "exe", "target": _exe(tmp_path, "EXCEL.EXE")},
               {"name": "Excel", "kind": "lnk", "target": _exe(tmp_path, "Excel.lnk")})
    assert sorted(e.name for e in cat.entries()) == ["Discord", "Excel"]
    assert cat.resolve_name("discord").entry is not None
    assert cat.resolve_name("excel").entry is not None


def test_two_different_programs_sharing_a_name_stay_two_and_stay_ambiguous(tmp_path):
    cat = _cat(tmp_path, ("Editor", "alpha.exe"), ("editor", "beta.exe"))
    assert len(cat.entries()) == 2
    assert cat.resolve_name("editor").reason == "ambiguous"


def test_the_most_reliable_launch_identity_wins(tmp_path):
    # A Start-Menu shortcut carries the working directory, arguments and icon the publisher intended.
    cat = _cat(tmp_path,
               {"name": "Excel", "kind": "exe", "target": _exe(tmp_path, "EXCEL.EXE")},
               {"name": "Excel", "kind": "lnk", "target": _exe(tmp_path, "Excel.lnk")})
    assert cat.entries()[0].kind == "lnk"


def test_a_logon_startup_copy_never_replaces_the_ordinary_shortcut(tmp_path):
    startup = tmp_path / "Startup"
    startup.mkdir()
    (startup / "Ollama.lnk").write_text("stub")
    cat = _cat(tmp_path,
               {"name": "Ollama", "kind": "lnk", "target": str(startup / "Ollama.lnk")},
               {"name": "Ollama", "kind": "lnk", "target": _exe(tmp_path, "Ollama.lnk")})
    assert "Startup" not in cat.entries()[0].target


def test_deduplication_is_deterministic_whatever_order_discovery_returns(tmp_path):
    rows = [{"name": "Excel", "kind": "exe", "target": _exe(tmp_path, "EXCEL.EXE")},
            {"name": "Excel", "kind": "lnk", "target": _exe(tmp_path, "Excel.lnk")}]
    a = AppCatalog(FakeBackend(apps=list(rows))).entries()[0]
    b = AppCatalog(FakeBackend(apps=list(reversed(rows)))).entries()[0]
    assert a.app_id == b.app_id and a.target == b.target


def test_every_discovered_record_stays_launchable_by_its_own_id(tmp_path):
    """De-duplication chooses what the NAME resolves to; it must not invalidate an app_id already handed out."""
    dropped = _exe(tmp_path, "EXCEL.EXE")
    cat = _cat(tmp_path,
               {"name": "Excel", "kind": "exe", "target": dropped},
               {"name": "Excel", "kind": "lnk", "target": _exe(tmp_path, "Excel.lnk")})
    ids = {e.target: e.app_id for e in cat.entries()}
    assert dropped not in ids                                   # not the preferred entry
    from void.actions.computer import _make_app_id
    assert cat.resolve(_make_app_id("exe", dropped)) is not None


def test_store_apps_are_discovered_alongside_desktop_apps(tmp_path):
    cat = _cat(tmp_path,
               {"name": "WhatsApp", "kind": "uwp", "target": WHATSAPP},
               ("Opera GX Browser", "opera.lnk"))
    assert cat.resolve_name("whatsapp").entry.kind == "uwp"
    assert cat.resolve_name("opera gx").entry.kind == "lnk"


@pytest.mark.parametrize("row", [
    {"name": "", "kind": "exe", "target": "x.exe"},
    {"name": "X", "kind": "exe", "target": ""},
    {"name": None, "kind": "exe", "target": "x.exe"},
    {"name": "X", "kind": "exe", "target": 12345},
])
def test_a_malformed_discovery_record_is_dropped_not_trusted(tmp_path, row):
    assert AppCatalog(FakeBackend(apps=[row])).entries() == []


# --- the matching hierarchy ------------------------------------------------------------------------

@pytest.mark.parametrize("said, tier", [
    ("WhatsApp", "exact"), ("whatsapp", "exact"), ("  WHATSAPP ", "exact"),
    ("Whats App", "spacing"), ("Whats-App", "spacing"),
])
def test_an_installed_name_resolves_however_it_is_said(tmp_path, said, tier):
    m = _cat(tmp_path, "WhatsApp", "Discord").resolve_name(said)
    assert m.entry is not None and m.entry.name == "WhatsApp" and m.tier == tier


@pytest.mark.parametrize("said", ["Opera GX", "opera gx", "Opera-GX", "Opera  GX", "Opera G X", "OPERA GX"])
def test_a_spoken_name_resolves_to_the_installed_name_that_only_adds_trailing_words(tmp_path, said):
    m = _cat(tmp_path, "Opera GX Browser").resolve_name(said)
    assert m.entry is not None and m.entry.name == "Opera GX Browser" and m.tier == "prefix"


def test_a_vendor_word_nobody_says_is_not_required(tmp_path):
    m = _cat(tmp_path, "Microsoft Teams", "Discord").resolve_name("teams")
    assert m.entry.name == "Microsoft Teams" and m.tier == "publisher"


def test_an_alias_covers_an_irregular_spoken_form(tmp_path):
    m = _cat(tmp_path, "Visual Studio Code").resolve_name("vs code")
    assert m.entry.name == "Visual Studio Code" and m.tier == "exact"


@pytest.mark.parametrize("said, expected", [
    ("opera", "opera"),                          # an exact name beats a longer one it is a prefix of
    ("opera gx", "Opera GX Browser"),
])
def test_a_stronger_tier_always_wins(tmp_path, said, expected):
    cat = _cat(tmp_path, ("opera", "o.exe"), ("Opera GX Browser", "gx.lnk"))
    assert cat.resolve_name(said).entry.name == expected


def test_a_real_application_beats_a_vendor_stripped_one(tmp_path):
    cat = _cat(tmp_path, ("Teams", "t.exe"), ("Microsoft Teams", "mt.lnk"))
    m = cat.resolve_name("teams")
    assert m.entry.name == "Teams" and m.tier == "exact"


@pytest.mark.parametrize("said", [
    "gx", "browser", "gx browser",               # a substring is not a match
    "oper", "opera g",                           # a partial word is not a match
    "operagx",                                   # a run-together transcript is not a prefix
    "gx opera", "opera browser",                 # reordered / with a word missing
    "opera gx browser pro",                      # longer than the installed name
    "opora gx", "opera gz",                      # merely similar: there is no fuzzy matching at all
    "", "   ", "\u00a0",
])
def test_a_name_that_is_not_the_installed_name_does_not_resolve(tmp_path, said):
    m = _cat(tmp_path, "Opera GX Browser").resolve_name(said)
    assert m.entry is None and m.reason == "unknown" and not m.candidates


def test_the_briefs_own_open_studio_example_never_picks_an_application(tmp_path):
    """Section 6: with several "... Studio" applications installed, "open Studio" must not choose between them."""
    cat = _cat(tmp_path, "Android Studio", "Visual Studio", "Visual Studio Code")
    assert cat.resolve_name("studio").entry is None          # a trailing word is not a name: no match at all
    assert cat.resolve_name("visual studio").entry.name == "Visual Studio"     # exact beats the longer prefix
    assert cat.resolve_name("android studio").entry.name == "Android Studio"


def test_an_ambiguous_prefix_is_reported_not_guessed(tmp_path):
    cat = _cat(tmp_path, "Opera GX Browser", "Opera GX Developer", "Notepad")
    m = cat.resolve_name("opera gx")
    assert m.entry is None and m.reason == "ambiguous" and m.tier == "prefix"
    assert sorted(c.name for c in m.candidates) == ["Opera GX Browser", "Opera GX Developer"]


def test_an_ambiguous_strong_tier_never_falls_through_to_a_weaker_one(tmp_path):
    """Falling through after a strong tier matched several applications would be guessing by another name."""
    cat = _cat(tmp_path, ("Editor", "a.exe"), ("editor", "b.exe"), ("Editor Pro", "c.exe"))
    m = cat.resolve_name("editor")
    assert m.reason == "ambiguous" and m.tier == "exact"


# --- lifecycle: install, update, uninstall, rename --------------------------------------------------

class MutableBackend(FakeBackend):
    """A machine whose installed applications change, with a cheap change signal like the real one."""

    def __init__(self, apps=None, fingerprint="fp-0", **kw):
        super().__init__(apps=apps, **kw)
        self.fingerprint = fingerprint
        self.discoveries = 0
        self.fingerprints = 0

    def discover_apps(self):
        self.discoveries += 1
        return super().discover_apps()

    def discovery_fingerprint(self):
        self.fingerprints += 1
        return self.fingerprint

    def install(self, name, target, kind="exe", fingerprint=None):
        self._apps.append({"name": name, "kind": kind, "target": target})
        self.fingerprint = fingerprint or f"fp-{len(self._apps)}"

    def uninstall(self, name):
        self._apps = [a for a in self._apps if a["name"] != name]
        self.fingerprint = f"fp-{len(self._apps)}-{name}"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def test_an_application_installed_later_is_discovered_without_a_restart(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    assert cat.resolve_name("spotify").entry is None
    be.install("Spotify", _exe(tmp_path, "s.exe"))
    clock.advance(120)
    assert cat.resolve_name("spotify").entry.name == "Spotify"       # no source change, no restart, no LLM


def test_a_just_installed_application_works_on_the_very_first_command(tmp_path):
    """A name that matched NOTHING is worth a real rediscovery: it is already headed for the slow path, and this
    is what makes "I just installed it" work instead of "restart V.O.I.D"."""
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=10_000.0, clock=clock)
    cat.entries()
    be.install("Spotify", _exe(tmp_path, "s.exe"))
    clock.advance(AppCatalog.MIN_REBUILD_S + 1)
    assert cat.resolve_name("spotify").entry.name == "Spotify"       # inside the TTL, found anyway


def test_a_stream_of_unknown_names_cannot_turn_into_a_stream_of_rediscoveries(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=10_000.0, clock=clock)
    cat.entries()
    before = be.discoveries
    for _ in range(25):
        cat.resolve_name("no such application")
    assert be.discoveries == before                                   # rate-limited by MIN_REBUILD_S


def test_an_uninstalled_application_stops_resolving_and_is_never_launched(tmp_path):
    clock = _Clock()
    target = _exe(tmp_path, "s.exe")
    be = MutableBackend(apps=[{"name": "Spotify", "kind": "exe", "target": target}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    entry = cat.resolve_name("spotify").entry
    assert entry is not None
    import os
    os.remove(target)
    assert AppCatalog.revalidate(entry) is False                      # refused at launch, immediately
    be.uninstall("Spotify")
    clock.advance(120)
    assert cat.resolve_name("spotify").entry is None                  # and gone from the catalog on refresh


def test_a_renamed_application_resolves_under_its_new_name(tmp_path):
    clock = _Clock()
    target = _exe(tmp_path, "s.exe")
    be = MutableBackend(apps=[{"name": "Spotify", "kind": "exe", "target": target}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    assert cat.resolve_name("spotify").entry is not None
    be.uninstall("Spotify")
    be.install("Spotify Premium", target)
    clock.advance(120)
    assert cat.resolve_name("spotify premium").entry is not None
    assert cat.resolve_name("spotify").entry.name == "Spotify Premium"   # unique prefix of the new name


def test_an_updated_launch_identity_is_picked_up(tmp_path):
    clock = _Clock()
    old, new = _exe(tmp_path, "app-1.0.exe"), _exe(tmp_path, "app-2.0.exe")
    be = MutableBackend(apps=[{"name": "Editor", "kind": "exe", "target": old}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    first = cat.resolve_name("editor").entry
    be.uninstall("Editor")
    be.install("Editor", new)
    clock.advance(120)
    second = cat.resolve_name("editor").entry
    assert second.target == new and second.app_id != first.app_id


# --- refresh policy: cheap when nothing changed -----------------------------------------------------

def test_an_unchanged_machine_is_never_rediscovered(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    cat.entries()
    assert be.discoveries == 1
    for _ in range(5):
        clock.advance(120)
        cat.entries()
    assert be.discoveries == 1                        # the cheap signal answered every time
    assert be.fingerprints >= 5


def test_repeated_commands_inside_the_ttl_touch_nothing_at_all(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=600.0, clock=clock)
    cat.entries()
    be.fingerprints = 0
    for _ in range(50):
        cat.resolve_name("notepad")
    assert be.discoveries == 1 and be.fingerprints == 0


def test_a_backend_without_a_change_signal_still_refreshes_on_the_ttl(tmp_path):
    clock = _Clock()
    be = FakeBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    assert be.discovery_fingerprint() is None
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    cat.entries()
    builds = cat.builds
    clock.advance(120)
    cat.entries()
    assert cat.builds == builds + 1


def test_invalidate_forces_a_rediscovery_on_the_next_lookup(tmp_path):
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be)
    cat.entries()
    be.install("Spotify", _exe(tmp_path, "s.exe"))
    assert cat.resolve_name("spotify").entry is None
    cat.invalidate()
    assert cat.resolve_name("spotify").entry is not None


def test_ttl_zero_pins_the_catalog_for_callers_that_want_no_surprises(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=0, clock=clock)
    cat.entries()
    clock.advance(100_000)
    cat.entries()
    assert be.discoveries == 1


# --- failure behaviour -------------------------------------------------------------------------------

def test_a_discovery_failure_after_a_good_build_keeps_the_applications_we_had(tmp_path):
    """Section 16: a failing refresh must not leave the owner with no applications at all. The retained entries
    are still independently revalidated before anything launches."""
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=60.0, clock=clock)
    cat.entries()
    be._raise_on = {"discover_apps"}
    be.fingerprint = "changed"
    clock.advance(120)
    assert cat.resolve_name("notepad").entry is not None


def test_a_discovery_failure_on_the_first_build_is_reported_not_hidden(tmp_path):
    from void.actions.computer import ComputerBackendError
    cat = AppCatalog(FakeBackend(raise_on={"discover_apps"}))
    with pytest.raises(ComputerBackendError):
        cat.resolve_name("notepad")


def test_a_broken_change_signal_never_breaks_a_command(tmp_path):
    class Hostile(MutableBackend):
        def discovery_fingerprint(self):
            raise RuntimeError("registry exploded")

    clock = _Clock()
    cat = AppCatalog(Hostile(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}]),
                     ttl_s=60.0, clock=clock)
    cat.entries()
    clock.advance(120)
    assert cat.resolve_name("notepad").entry is not None


# --- security -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    r"C:\Windows\System32\cmd.exe", r"C:\Users\me\malicious.exe", "/usr/bin/python", r"..\..\secret",
    r"\\server\share\x.exe", "http://evil.example/a.exe", "file:///c:/x",
    "notepad && calc", "notepad; del *.*", "notepad | calc", "$(calc)", "`calc`", "notepad & calc",
    "%windir%\\notepad", "~/notes", "*", "a=b",
])
def test_an_arbitrary_path_or_shell_string_never_resolves_to_an_application(tmp_path, said):
    """Resolution maps a NAME to a discovered application. It must never be a way to name something else."""
    cat = _cat(tmp_path, "Notepad", "Command Prompt")
    m = cat.resolve_name(said)
    assert m.entry is None and not m.candidates


def test_a_hostile_display_name_cannot_smuggle_a_second_target(tmp_path):
    cat = _cat(tmp_path, ("Evil\x1b[31m & calc.exe", "e.exe"))
    entry = cat.entries()[0]
    assert entry.target.endswith("e.exe")                  # the NAME is decoration; the target is the engine's
    assert cat.resolve_name(r"C:\evil.exe").entry is None


@pytest.mark.parametrize("target, ok", [
    (WHATSAPP, True), ("Publisher.App!Entry", True),
    ("no-bang-here", False), (r"C:\Windows\System32\cmd.exe", False), ("App!Entry extra", False),
    ("App!Entry&calc", False), ("App!Entry/../x", False), ("", False), (None, False),
])
def test_store_identities_are_still_strictly_validated(target, ok):
    assert is_app_user_model_id(target) is ok


def test_a_store_app_with_a_malformed_id_is_refused_by_revalidation(tmp_path):
    assert AppCatalog.revalidate(AppEntry("app-x", "WhatsApp", "uwp", WHATSAPP)) is True
    assert AppCatalog.revalidate(AppEntry("app-y", "Evil", "uwp", "App!Entry & calc.exe")) is False


def test_resolution_never_launches_anything_by_itself(tmp_path):
    """resolve_name is a lookup. The catalog module has no launch, no subprocess and no risk-gate import."""
    import inspect

    import void.actions.app_names as names_mod
    src = inspect.getsource(names_mod)
    for forbidden in ("subprocess", "os.startfile", "Popen", "RiskGate", "shell=True", "import os"):
        assert forbidden not in src


def test_launching_a_resolved_entry_still_goes_through_validation(tmp_path):
    """The resolver picks WHICH application; launch_app still decides whether it may start."""
    be = FakeBackend(apps=[{"name": "Spotify", "kind": "exe", "target": _exe(tmp_path, "s.exe")}])
    cat = AppCatalog(be)
    launched = []
    aa = AppActions(FileActions([tmp_path]), catalog=cat, launcher=lambda k, t: launched.append((k, t)))
    entry = cat.resolve_name("spotify").entry
    assert aa.launch_app(entry.app_id).ok and len(launched) == 1
    assert not aa.launch_app(r"C:\Windows\System32\cmd.exe").ok       # a path is never an application id
    assert not aa.launch_app("Spotify").ok                            # nor is a display name
    assert len(launched) == 1


def test_find_app_reports_ambiguity_instead_of_resolving_it(tmp_path):
    be = FakeBackend(apps=[{"name": "Opera GX Browser", "kind": "lnk", "target": _exe(tmp_path, "a.lnk")},
                           {"name": "Opera GX Developer", "kind": "lnk", "target": _exe(tmp_path, "b.lnk")}])
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.find_app("Opera GX")
    assert r.ok and len(r.data) == 2 and "ambiguous" in r.summary.lower()
    assert ca.find_app("Opera GX Dev").data == []            # "dev" is a partial word, never a match
    r1 = ca.find_app("Opera GX Developer")
    assert r1.ok and len(r1.data) == 1 and r1.data[0]["name"] == "Opera GX Developer"


# --- performance ---------------------------------------------------------------------------------------

def test_a_warm_lookup_over_a_realistic_catalog_is_effectively_instant(tmp_path):
    """The whole point of this layer is that a voice command does not wait for discovery. A realistic machine has
    roughly 200 applications; a warm lookup must be microseconds, not milliseconds."""
    rows = [{"name": f"Application Number {i}", "kind": "exe", "target": _exe(tmp_path, f"a{i}.exe")}
            for i in range(200)]
    rows.append({"name": "Opera GX Browser", "kind": "lnk", "target": _exe(tmp_path, "opera.lnk")})
    cat = AppCatalog(FakeBackend(apps=rows), ttl_s=600.0)
    cat.entries()                                    # warm
    best = min(_time_lookups(cat) for _ in range(5))
    assert best < 0.002, f"warm lookup took {best * 1000:.2f} ms"


def _time_lookups(cat, n=50):
    t0 = time.perf_counter()
    for _ in range(n):
        cat.resolve_name("opera gx")
    return (time.perf_counter() - t0) / n


# --- a shortcut is not the application (live validation, 2026-09-23) ---------------------------------
#
# "open Discord" made Windows pop a UAC dialog and fail with WinError 1223: Discord had been uninstalled, and both
# its Start-Menu shortcuts were leftovers pointing at an executable that no longer exists. A shortcut existing
# says nothing about the application existing.

class ShortcutBackend(FakeBackend):
    """A backend that can say what a shortcut points at, the way the Windows one does."""

    def __init__(self, apps=None, targets=None, **kw):
        super().__init__(apps=apps, **kw)
        self.targets = dict(targets or {})
        self.resolved = []

    def shortcut_target(self, path):
        self.resolved.append(path)
        return self.targets.get(path)


def test_a_shortcut_left_behind_by_an_uninstall_is_refused_before_it_can_prompt(tmp_path):
    lnk = _exe(tmp_path, "Discord.lnk")
    be = ShortcutBackend(apps=[{"name": "Discord", "kind": "lnk", "target": lnk}],
                         targets={lnk: str(tmp_path / "app-1.0.9257" / "Discord.exe")})   # never created
    cat = AppCatalog(be)
    entry = cat.resolve_name("discord").entry
    assert entry is not None                       # still discovered: Windows still lists it
    assert AppCatalog.revalidate(entry) is True    # the shortcut itself is there...
    assert cat.validate(entry) is False            # ... but what it points at is not


def test_a_live_shortcut_is_still_accepted(tmp_path):
    lnk, exe = _exe(tmp_path, "Discord.lnk"), _exe(tmp_path, "Discord.exe")
    be = ShortcutBackend(apps=[{"name": "Discord", "kind": "lnk", "target": lnk}], targets={lnk: exe})
    cat = AppCatalog(be)
    assert cat.validate(cat.resolve_name("discord").entry) is True


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_a_shortcut_that_cannot_be_read_is_unknown_not_missing(tmp_path, answer):
    """A folder/URL shortcut, or a backend without COM, must not make a working application unlaunchable."""
    lnk = _exe(tmp_path, "Thing.lnk")
    be = ShortcutBackend(apps=[{"name": "Thing", "kind": "lnk", "target": lnk}], targets={lnk: answer})
    cat = AppCatalog(be)
    assert cat.validate(cat.resolve_name("thing").entry) is True


def test_a_backend_that_throws_while_reading_a_shortcut_never_blocks_a_launch(tmp_path):
    class Hostile(ShortcutBackend):
        def shortcut_target(self, path):
            raise RuntimeError("COM exploded")

    lnk = _exe(tmp_path, "Thing.lnk")
    cat = AppCatalog(Hostile(apps=[{"name": "Thing", "kind": "lnk", "target": lnk}]))
    assert cat.validate(cat.resolve_name("thing").entry) is True


def test_only_shortcuts_pay_for_target_resolution(tmp_path):
    """It costs ~7 ms per call, so it happens once per launch - never for a Store app or a plain executable, and
    never for the ~110 shortcuts of a full rediscovery."""
    be = ShortcutBackend(apps=[{"name": "Spotify", "kind": "exe", "target": _exe(tmp_path, "s.exe")},
                               {"name": "WhatsApp", "kind": "uwp", "target": WHATSAPP}])
    cat = AppCatalog(be)
    cat.entries()
    assert be.resolved == []                                   # nothing resolved while merely discovering
    assert cat.validate(cat.resolve_name("spotify").entry) is True
    assert cat.validate(cat.resolve_name("whatsapp").entry) is True
    assert be.resolved == []


def test_launch_app_refuses_a_stale_shortcut_and_starts_nothing(tmp_path):
    lnk = _exe(tmp_path, "Discord.lnk")
    be = ShortcutBackend(apps=[{"name": "Discord", "kind": "lnk", "target": lnk}],
                         targets={lnk: str(tmp_path / "gone" / "Discord.exe")})
    cat = AppCatalog(be)
    launched = []
    aa = AppActions(FileActions([tmp_path]), catalog=cat, launcher=lambda k, t: launched.append((k, t)))
    r = aa.launch_app(cat.resolve_name("discord").entry.app_id)
    assert not r.ok and "no longer available" in r.summary and launched == []


def test_a_shortcut_planted_under_a_trusted_name_cannot_take_that_name_over(tmp_path):
    """Catalog poisoning: anything that can write a Start-Menu shortcut can put "WhatsApp" on a different program.

    De-duplication must not help it. Entries only merge when the display name AND the program agree, so a planted
    shortcut stays a SECOND application: the name becomes ambiguous, the fast path refuses it, and nothing starts.
    """
    real = {"name": "WhatsApp", "kind": "uwp", "target": WHATSAPP}
    planted = {"name": "WhatsApp", "kind": "lnk", "target": _exe(tmp_path, "totally-not-evil.lnk")}
    cat = AppCatalog(FakeBackend(apps=[real, planted]))
    m = cat.resolve_name("whatsapp")
    assert m.entry is None and m.reason == "ambiguous" and len(m.candidates) == 2

    launched = []
    aa = AppActions(FileActions([tmp_path]), catalog=cat, launcher=lambda k, t: launched.append((k, t)))
    from void.core.fast_path import FastPath
    assert FastPath(cat).decide("open whatsapp").plan is None
    assert launched == []


def test_a_planted_shortcut_cannot_outrank_a_store_app_by_sorting_better(tmp_path):
    """Source priority prefers a Start-Menu shortcut, so it must never be usable to displace an application that
    was found under the same identity from a more trustworthy source."""
    cat = AppCatalog(FakeBackend(apps=[
        {"name": "WhatsApp", "kind": "uwp", "target": WHATSAPP},
        {"name": "WhatsApp", "kind": "lnk", "target": _exe(tmp_path, "WhatsApp.lnk")}]))
    assert cat.resolve_name("whatsapp").reason == "ambiguous"      # never silently swapped for the shortcut


# --- the miss path must be cheap (2026-09-24) -----------------------------------------------------
#
# Speech-to-text mishears constantly, so "nothing matched that name" is a COMMON event: 5 of the owner's 14 spoken
# commands on 2026-09-23 arrived as nonsense. Each one was paying for a full ~430 ms rediscovery that could not
# possibly help, on top of the model round trip it was already headed for.

def test_a_misheard_phrase_does_not_pay_for_a_rediscovery(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=600.0, clock=clock)
    cat.entries()
    builds, fps = be.discoveries, be.fingerprints
    for _ in range(6):
        clock.advance(AppCatalog.MIN_REBUILD_S + 1)
        assert cat.resolve_name("our projects").entry is None
    assert be.discoveries == builds                      # the cheap signal answered every time
    assert be.fingerprints > fps


def test_a_miss_still_notices_an_application_installed_since_the_last_build(tmp_path):
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=10_000.0, clock=clock)
    cat.entries()
    be.install("Spotify", _exe(tmp_path, "s.exe"))       # changes the signal, as a desktop install does
    clock.advance(AppCatalog.MIN_REBUILD_S + 1)
    assert cat.resolve_name("spotify").entry is not None


def test_a_store_install_is_still_caught_even_though_it_changes_no_signal(tmp_path):
    """A Store app touches neither the Start Menu nor App Paths, so only a blind rediscovery finds it. That is
    worth one rebuild every few minutes - not one per misheard word."""
    clock = _Clock()
    be = MutableBackend(apps=[{"name": "Notepad", "kind": "exe", "target": _exe(tmp_path, "n.exe")}])
    cat = AppCatalog(be, ttl_s=10_000.0, clock=clock)
    cat.entries()
    be._apps.append({"name": "Netflix", "kind": "uwp", "target": "Netflix.App_abc!App"})   # signal unchanged
    clock.advance(AppCatalog.MIN_REBUILD_S + 1)
    assert cat.resolve_name("netflix").entry is None     # too soon to pay for a blind rebuild
    clock.advance(AppCatalog.MIN_BLIND_REBUILD_S + 1)
    assert cat.resolve_name("netflix").entry is not None
    assert AppCatalog.MIN_BLIND_REBUILD_S >= 20 * AppCatalog.MIN_REBUILD_S


# --- a vendor word on either side (2026-09-24) ----------------------------------------------------
#
# "Open Windows Terminal" found nothing: the installed Store app is called just "Terminal". People add the vendor
# an installed name omits as readily as they drop one it carries.

@pytest.mark.parametrize("said, opens", [
    ("windows terminal", "Terminal"),
    ("terminal", "Terminal"),
    ("microsoft teams", "Microsoft Teams"),
    ("teams", "Microsoft Teams"),
])
def test_a_vendor_word_may_be_present_on_either_side(tmp_path, said, opens):
    cat = _cat(tmp_path, "Terminal", "Microsoft Teams")
    assert cat.resolve_name(said).entry.name == opens


def test_an_exact_name_always_beats_a_qualifier_stripped_one(tmp_path):
    """"Windows Security" must answer to its own name, and "security" must not reach it."""
    cat = _cat(tmp_path, "Windows Security", ("Security", "sec.exe"))
    assert cat.resolve_name("windows security").entry.name == "Windows Security"
    assert cat.resolve_name("security").entry.name == "Security"


def test_stripping_a_qualifier_never_invents_a_match(tmp_path):
    from void.actions.app_names import without_qualifier
    cat = _cat(tmp_path, "Windows Security")
    assert cat.resolve_name("security").entry is None        # a trailing word is still not a name
    assert without_qualifier(("windows",)) is None           # never strips a query down to nothing
    assert without_qualifier(("terminal",)) is None
    # (a bare "windows" still reaches "Windows Security" - but as a unique whole-word PREFIX, which is the
    #  ordinary tier-3 behaviour and has nothing to do with qualifier stripping.)
    assert cat.resolve_name("windows").tier == "prefix"


def test_the_qualifier_tier_still_refuses_to_guess(tmp_path):
    cat = _cat(tmp_path, ("Terminal", "a.exe"), ("terminal", "b.exe"))
    assert cat.resolve_name("windows terminal").entry is None


# --- the sound tier: right sounds, wrong spelling (2026-09-24) -------------------------------------
#
# 5 of 14 real spoken commands were misheard, and every one of them was phonetically faithful: "what's up" for
# WhatsApp, "not pad" for Notepad, "chat gpd" for ChatGPT. Each cost a 14-16 s failed model round trip instead of
# a 50 ms launch. Two other approaches were measured and rejected first (docs/VOICE_PIPELINE_V2_2026-09-24.md):
# the decoder's own n-best beam (not lexically diverse, and beam_size>=3 doubled decode time), and phonetic
# SIMILARITY with a threshold (ordinary sentences reached 0.77 while genuine matches fell to 0.71 - the
# distributions overlap, so no cut separates them).
#
# What ships is EQUALITY on a canonical key, the same kind of rule as the spacing tier.

@pytest.mark.parametrize("heard, opens", [
    ("chat gpd", "ChatGPT"), ("chat gpt", "ChatGPT"), ("chatgpt", "ChatGPT"),
    ("what's up", "WhatsApp"), ("whats up", "WhatsApp"), ("whatsapp", "WhatsApp"),
    ("not pad", "Notepad"), ("note pad", "Notepad"), ("notepad", "Notepad"),
    ("net flix", "Netflix"), ("spotifi", "Spotify"),
])
def test_a_name_that_sounds_right_but_is_spelled_wrong_still_resolves(tmp_path, heard, opens):
    cat = _cat(tmp_path, "ChatGPT", "WhatsApp", "Notepad", "Netflix", "Spotify", "Discord")
    m = cat.resolve_name(heard)
    assert m.entry is not None and m.entry.name == opens
    assert m.tier in ("exact", "spacing", "sound")


@pytest.mark.parametrize("heard", [
    # the owner's real misheard sentences: none of these is an application command at all
    "our projects", "all projects", "to do with these", "skill once", "short gpd",
    # ordinary speech that happens to follow a launch verb
    "the lights", "some music", "my email", "a new document", "the meeting",
    "the shopping list", "directions home", "my downloads", "the project folder",
    # single common words - the shape most likely to collide by accident
    "could", "would", "other", "work", "the", "two", "good", "be",
])
def test_an_unrelated_phrase_never_sounds_like_an_application(tmp_path, heard):
    """The whole safety case. A wrong launch is worse than no launch, so this must hold for ordinary speech."""
    cat = _cat(tmp_path, "ChatGPT", "WhatsApp", "Notepad", "Claude", "Word", "Weather", "Code", "wt",
               "Opera GX Browser", "Netflix", "Discord", "Terminal")
    m = cat.resolve_name(heard)
    assert m.entry is None, f"{heard!r} resolved to {m.entry.name if m.entry else None!r}"


def test_the_historical_opera_gx_mishearing_never_launches_opera(tmp_path):
    """"Open Opera GX" was transcribed "Open our projects." Recovering THAT would be guessing from an unrelated
    sentence, not reading a mishearing - so it must not happen, however much the owner meant Opera GX."""
    cat = _cat(tmp_path, "Opera GX Browser", "ChatGPT", "WhatsApp")
    for heard in ("our projects", "all projects"):
        assert cat.resolve_name(heard).entry is None


def test_a_key_too_short_to_identify_a_program_is_never_used(tmp_path):
    """Measured: at a 3-character key "could" matches Claude, "would" matches Word and "other" matches Weather.
    At 4 the same corpus produces no false positive at all, and every real recovery survives."""
    from void.actions.app_names import MIN_SOUND_KEY, sound_key
    assert MIN_SOUND_KEY >= 4
    cat = _cat(tmp_path, "Claude", "Word", "Weather")
    for heard in ("could", "would", "other"):
        assert len(sound_key(heard)) < MIN_SOUND_KEY
        assert cat.resolve_name(heard).entry is None


def test_two_applications_that_sound_alike_are_never_guessed_between(tmp_path):
    cat = _cat(tmp_path, ("Microsoft News", "news.lnk"), ("Microsoft Teams", "teams.lnk"))
    m = cat.resolve_name("microsoft nues")
    assert m.entry is None and m.reason == "ambiguous" and m.tier == "sound"
    assert len(m.candidates) == 2


def test_the_sound_tier_never_overrides_a_name_that_actually_matches(tmp_path):
    """It is the LAST tier: an application spelled exactly as asked always wins."""
    cat = _cat(tmp_path, ("Code", "code.exe"), ("Claude", "claude.lnk"))
    assert cat.resolve_name("code").entry.name == "Code"
    assert cat.resolve_name("claude").entry.name == "Claude"
    assert cat.resolve_name("code").tier == "exact"


def test_sounding_alike_is_an_equality_not_a_similarity(tmp_path):
    """There is no threshold anywhere: a name that is merely CLOSE does not resolve, it misses."""
    from void.actions.app_names import sound_key
    cat = _cat(tmp_path, "ChatGPT")
    assert sound_key("chat gpd") == sound_key("ChatGPT")        # same sounds -> resolves
    assert cat.resolve_name("chat gpd").entry is not None
    assert sound_key("chat gpp") != sound_key("ChatGPT")        # one sound off -> does NOT
    assert cat.resolve_name("chat gpp").entry is None


@pytest.mark.parametrize("junk", [None, 12345, "", "   ", b"x", ["a"]])
def test_the_sound_key_never_raises(junk):
    from void.actions.app_names import sound_key
    assert isinstance(sound_key(junk), str)


def test_resolution_stays_fast_with_the_extra_tier(tmp_path):
    """The tier is an index lookup, not a scan: a miss must not start comparing against every application."""
    import time
    rows = [{"name": f"Application Number {i}", "kind": "exe", "target": _exe(tmp_path, f"a{i}.exe")}
            for i in range(200)]
    cat = AppCatalog(FakeBackend(apps=rows), ttl_s=600.0)
    cat.entries()
    best = min(_time_lookups(cat, n=50) for _ in range(5))
    assert best < 0.002, f"warm lookup took {best * 1000:.2f} ms"
