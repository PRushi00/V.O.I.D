"""Taking a website apart: the routes, the fingerprints, the clean browser, and the drill.

Split by what each part costs. The route parsing and the fingerprint table are pure functions and are
tested in quantity, including every sentence that must trigger *nothing* - a teardown opens a browser
and runs a third-party analyser, so an over-eager route is a real cost. The browser lifecycle is
tested against a real Chromium when one is present and skipped honestly when it is not, because the
properties that matter there (a fresh profile every time, always torn down, no stray process) cannot
be established against a fake.

The live drill at the end is the pack's Prompt 12: it runs one real teardown against a page served on
loopback, checks the evidence, and leaves nothing behind - so the first teardown of a real site is
genuinely the first.
"""
import json
import os
import shutil
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from void.actions.files import FileActions
from void.app import Assistant
from void.config import Config
from void.teardown import (BOTH, OWN, PINNED_REA, REA, REA_REPOSITORY, Evidence, Kit, Line,
                           Teardown, TeardownUnavailable, digest, parse_note, write_note)
from void.teardown import address as teardown_address
from void.teardown import fingerprints
from void.teardown.browser import (CleanBrowser, InvestigationPage, TeardownBrowserError,
                                   _host_of, find_chrome, stray_profiles)
from void.vault import Vault

#: A page with every signal the fingerprint table should find, and nothing else.
BUNDLE_JS = """
/*! * @license Three.js Authors r165
 * MIT */
const REVISION = "165";
/*! GSAP 3.12.5 */
/* postprocessing v6.35.5 */ const nearCoCBuffer = 1; const bloomIntensity = 0.7;
const DRACOLoader = 1, KTX2Loader = 2; window.__NEXT_DATA__ = {}; webpackChunk = [];
/*! Copyright (c) 2026 Some Other Vendor Inc */
"""

INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Synthetic 3D Site</title>
<script src="/bundle.js"></script></head><body style="height:600vh;margin:0">
<h1>A synthetic page</h1><div style="height:500vh"></div><img src="/texture.png" alt="t">
<script>window.addEventListener('wheel', () => {
  document.title = 'scrolled ' + Math.round(window.scrollY); });</script>
</body></html>"""


# ================================================================== the route

@pytest.mark.parametrize("utterance, address", [
    ("reverse engineer igloo dot inc", "igloo.inc"),
    ("reverse engineer igloo.inc", "igloo.inc"),
    ("tear down linear.app", "linear.app"),
    ("take apart vercel.com", "vercel.com"),
    ("how does stripe.com do that", "stripe.com"),
    ("what is example.com built with", "example.com"),
    ("what does vercel.com use", "vercel.com"),
    ("tear down stripe.com slash pricing", "stripe.com/pricing"),
    ("reverse engineer localhost colon 5001", "localhost:5001"),
    ("Void, tear down linear.app please", "linear.app"),
    ("take apart this site", "this"),
    ("reverse engineer this page", "this"),
])
def test_a_sentence_that_names_a_site_is_a_teardown_request(utterance, address):
    assert teardown_address.parse(utterance) == address


@pytest.mark.parametrize("utterance", [
    # the pack's own counter-examples
    "tear down the old shed",
    "how should I reverse engineer my pricing",
    # a site named but not as an address
    "reverse engineer Stripe's homepage",
    "tear down the competitor's landing page",
    # not about a site at all
    "take apart the engine", "what is love", "tear down my notes", "how does this work",
    "reverse engineer apart", "what is the retainer built with", "", "   ",
    "deconstruct the argument", "dissect the problem",
])
def test_a_sentence_that_names_no_site_triggers_nothing(utterance):
    assert teardown_address.parse(utterance) is None


def test_a_spoken_address_becomes_a_real_one():
    assert teardown_address.spoken_to_address("igloo dot inc") == "igloo.inc"
    assert teardown_address.spoken_to_address("stripe.com slash pricing") == "stripe.com/pricing"
    assert teardown_address.spoken_to_address("my dash site dot com") == "my-site.com"


def test_an_unknown_top_level_domain_is_not_mistaken_for_a_host():
    """"take apart" plus a word with a dot must not read as an address."""
    assert teardown_address.parse("tear down the thing.whatever") is None
    assert teardown_address.parse("tear down example.com") == "example.com"


# ================================================================== fingerprints

def test_a_licence_banner_names_an_exact_version():
    signals = {s.name: s for s in fingerprints.fingerprint(BUNDLE_JS)}
    assert signals["Three.js"].version == "165"
    assert signals["Three.js"].how == fingerprints.BANNER
    assert signals["GSAP"].version == "3.12.5"
    assert signals["postprocessing"].version == "6.35.5"


def test_a_code_string_is_labelled_as_a_code_string_and_carries_no_invented_version():
    signals = {s.name: s for s in fingerprints.fingerprint(BUNDLE_JS)}
    assert signals["depth of field"].how == fingerprints.STRING
    assert signals["depth of field"].version == ""
    assert signals["Draco"].version == ""


def test_the_stack_is_ordered_by_what_a_reader_asks_first():
    categories = [s.category for s in fingerprints.fingerprint(BUNDLE_JS)]
    assert categories[0] == "3D engine"
    assert categories.index("animation") < categories.index("build")


def test_a_banner_beats_a_code_string_for_the_same_library():
    only_string = fingerprints.fingerprint('const REVISION = "165";')
    assert only_string[0].how == fingerprints.STRING
    with_banner = fingerprints.fingerprint(
        'Three.js Authors r165\nconst REVISION = "165";')
    assert with_banner[0].how == fingerprints.BANNER


def test_a_guess_describes_itself_as_one():
    guess = fingerprints.Signal(category="3D engine", name="Something", how=fingerprints.GUESS)
    assert not guess.factual and "not evidence" in guess.described()


def test_unidentified_banners_are_reported_separately():
    signals = fingerprints.fingerprint(BUNDLE_JS)
    others = fingerprints.other_banners(BUNDLE_JS, signals)
    assert any("Some Other Vendor" in line for line in others)
    assert not any("Three.js" in line for line in others)


def test_fingerprinting_nothing_finds_nothing():
    assert fingerprints.fingerprint("") == []
    assert fingerprints.fingerprint(None) == []
    assert fingerprints.other_banners("") == []


# ================================================================== the kit

def test_the_kit_reports_what_is_missing_in_plain_english(tmp_path):
    kit = Kit(tmp_path / "nothing-here", node="/definitely/not/node")
    problems = kit.problems()
    assert not kit.ready
    assert any("Node" in p for p in problems)
    assert any("not installed" in p for p in problems)


def test_the_kit_names_the_exact_commands_and_the_repository_to_check(tmp_path):
    commands = Kit(tmp_path / "kit").install_commands()
    joined = " ".join(commands)
    assert REA_REPOSITORY in joined
    assert "--ignore-scripts" in joined and "--save-exact" in joined
    assert f"rea-agents@{PINNED_REA}" in joined


def test_the_version_is_pinned_and_never_latest(tmp_path):
    assert PINNED_REA and PINNED_REA != "latest"
    assert "latest" not in " ".join(Kit(tmp_path / "kit").install_commands())


def test_running_rea_without_the_kit_refuses_rather_than_raising_something_obscure(tmp_path):
    with pytest.raises(TeardownUnavailable):
        Kit(tmp_path / "nothing", node="/definitely/not/node").run("doctor")


def test_the_diagnostic_is_statuses_and_versions_only(tmp_path):
    diag = Kit(tmp_path / "kit").diag()
    assert set(diag) >= {"ready", "rea_installed", "rea_pinned", "node", "chrome_found",
                         "problems", "install_commands", "stray_profiles"}
    assert json.dumps(diag)                            # serialisable, so a caller can print it


def test_void_never_installs_the_kit_by_itself():
    """Structural: no process the teardown package STARTS is a package manager.

    Checked by walking the syntax tree rather than by grepping, because the kit's own
    ``install_commands`` quite properly contains the words "npm install" - as text to show the owner.
    The property that matters is that nothing here executes it.
    """
    import ast
    package = Path(__import__("void.teardown", fromlist=["x"]).__file__).parent
    managers = ("npm", "npx", "pip", "pip3", "curl", "wget", "yarn", "pnpm", "powershell", "sh",
                "bash", "cmd")
    for name in ("__init__.py", "browser.py", "address.py", "fingerprints.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            if not called.endswith(("subprocess.run", "subprocess.Popen", "os.system",
                                    "os.execv", "subprocess.call", "subprocess.check_output")):
                continue
            literals = [value.value for value in ast.walk(node)
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)]
            for literal in literals:
                head = literal.strip().split()[0] if literal.strip() else ""
                assert Path(head).name not in managers, (
                    f"{name} starts a process with {literal!r}")


# ================================================================== evidence and the digest

def sample_evidence() -> Evidence:
    evidence = Evidence(url="https://example.com/", host="example.com", question="how is it built?",
                        seconds=41.2,
                        page={"title": "Example", "elements": 27, "height_screens": 1.0,
                              "load_s": 2.4})
    evidence.scripts = [{"url": "https://example.com/app.js", "bytes": 1_400_000,
                         "source_included": True}]
    evidence.assets = [
        {"url": "https://example.com/app.js", "kind": "script", "bytes": 1_400_000,
         "origin": "example.com"},
        {"url": "https://cdn.other.com/t.ktx2", "kind": "img", "bytes": 1_200_000,
         "origin": "cdn.other.com"}]
    evidence.signals = fingerprints.fingerprint(BUNDLE_JS)
    evidence.lines = [Line("attached", REA), Line("read the asset log", OWN),
                      Line("3D engine: Three.js 165", BOTH)]
    evidence.limitations = ["Nothing here can see server-side code."]
    return evidence


def test_the_digest_carries_the_evidence_and_who_saw_each_part():
    text = digest(sample_evidence())
    assert "example.com" in text and "Three.js" in text
    assert REA in text and OWN in text and BOTH in text
    assert "WHAT THE EVIDENCE CANNOT SHOW" in text
    assert "server-side" in text


def test_the_digest_is_bounded():
    evidence = sample_evidence()
    evidence.limitations = ["a very long limitation " * 200] * 50
    assert len(digest(evidence)) <= 15_000


def test_the_totals_separate_third_party_origins():
    totals = sample_evidence().totals()
    assert totals["files"] == 2 and totals["bytes"] == 2_600_000
    assert totals["third_party"] == ["cdn.other.com"]
    assert totals["by_kind"]["script"]["bytes"] == 1_400_000


def test_the_largest_files_come_first():
    assert sample_evidence().largest()[0]["bytes"] == 1_400_000


def test_a_stack_signal_credits_both_the_capture_and_the_match():
    """The pack's own mistake: crediting REA with something V.O.I.D read itself, or the reverse."""
    lines = {line.source for line in sample_evidence().lines}
    assert BOTH in lines
    assert "code captured by REA" in BOTH and "V.O.I.D" in BOTH


# ================================================================== the note

REPLY = """LABEL: Teardown - example.com
SPOKEN: It is a Three.js scene, and the scroll moves a camera rather than the page.
SUMMARY: One script does everything.
---NOTE---
# example.com - teardown

**Verdict:** a Three.js scene.

## The answer
One screen tall.

## The stack
- Three.js r165 (licence banner)

## What the evidence can't show
Anything server-side.
"""


def test_the_reply_is_split_into_its_parts():
    label, spoken, summary, markdown = parse_note(REPLY)
    assert label == "Teardown - example.com"
    assert spoken.startswith("It is a Three.js scene")
    assert summary == "One script does everything."
    assert markdown.startswith("# example.com - teardown")


def test_a_missing_section_comes_back_empty_rather_than_invented():
    _label, spoken, _summary, markdown = parse_note("---NOTE---\n# just a note\n")
    assert spoken == "" and markdown.startswith("# just a note")


def vault_for(tmp_path) -> Vault:
    root = tmp_path / "work" / "Notes"
    root.mkdir(parents=True)
    (root / "Seed.md").write_text("# Seed\nsomething\n", encoding="utf-8")
    vault = Vault(root, file_actions=FileActions(allowed_roots=[tmp_path / "work"]))
    vault.reload()
    return vault


def test_the_note_lands_in_the_vault_indexed_and_citing_its_evidence(tmp_path):
    vault = vault_for(tmp_path)
    before = vault.node_count
    written = write_note(sample_evidence(), REPLY, vault=vault)
    assert vault.node_count == before + 1
    body = Path(written.note_path).read_text(encoding="utf-8")
    assert "## The stack" in body and "## Evidence" in body
    assert "Three.js 165" in body and REA in body
    assert written.note_id in vault.search("three.js verdict teardown").ids


def test_the_note_keeps_its_markdown_structure(tmp_path):
    written = write_note(sample_evidence(), REPLY, vault=vault_for(tmp_path))
    body = Path(written.note_path).read_text(encoding="utf-8")
    assert body.count("\n## ") >= 3


def test_a_reply_with_no_note_section_is_refused(tmp_path):
    with pytest.raises(TeardownBrowserError):
        write_note(sample_evidence(), "SPOKEN: nice site", vault=vault_for(tmp_path))


def test_a_vault_that_refuses_the_write_is_reported_not_swallowed(tmp_path, monkeypatch):
    vault = vault_for(tmp_path)
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    with pytest.raises(TeardownBrowserError, match="NOT written"):
        write_note(sample_evidence(), REPLY, vault=vault)


# ================================================================== the investigation browser

def test_a_debugger_url_that_is_not_on_loopback_is_refused():
    from void.teardown.browser import _WebSocket
    with pytest.raises(TeardownBrowserError, match="loopback"):
        _WebSocket("ws://example.com:9333/devtools/page/x")
    with pytest.raises(TeardownBrowserError, match="ws://"):
        _WebSocket("wss://127.0.0.1:9333/devtools/page/x")


def test_the_page_client_has_no_way_to_drive_the_page():
    """"REA observes, it never drives" - enforced by absence, not by policy."""
    for verb in ("click", "type", "navigate", "submit", "fill", "press", "goto"):
        assert not hasattr(InvestigationPage, verb), f"the page client can {verb}"
    assert hasattr(InvestigationPage, "evaluate") and hasattr(InvestigationPage, "scroll")


def test_a_bare_host_is_read_as_a_host():
    """It was not: urlsplit reads a bare host as a path and reported no hostname at all."""
    assert _host_of("127.0.0.1:8731") == "127.0.0.1"
    assert _host_of("http://www.example.com/x") == "example.com"
    assert _host_of("example.com") == "example.com"
    assert _host_of("") == ""


def test_opening_a_browser_without_one_installed_says_so(tmp_path):
    browser = CleanBrowser(chrome=None, profile_root=tmp_path / "profiles")
    browser.chrome = None
    with pytest.raises(TeardownBrowserError, match="No Chromium-family browser"):
        browser.open("https://example.com")


def test_stray_profiles_reports_what_a_crash_left_behind(tmp_path):
    root = tmp_path / "profiles"
    assert stray_profiles(root) == []
    (root / "abandoned").mkdir(parents=True)
    assert stray_profiles(root) == [str(root / "abandoned")]


# ================================================================== the live drill

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def site(tmp_path):
    """A synthetic site on loopback, so the drill needs no outside network."""
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "bundle.js").write_text(BUNDLE_JS, encoding="utf-8")
    (root / "texture.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(3000))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *_a):
            pass

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


needs_browser = pytest.mark.skipif(find_chrome() is None,
                                   reason="no Chromium-family browser on this machine")
needs_kit = pytest.mark.skipif(not Kit().ready,
                               reason="the REA kit is not installed here (void teardown diag)")


@needs_browser
@pytest.mark.real_socket
def test_the_investigation_browser_is_fresh_every_time_and_always_torn_down(site, tmp_path):
    root = tmp_path / "profiles"
    seen = []
    for _ in range(2):
        browser = CleanBrowser(headless=True, profile_root=root, port=_free_port())
        try:
            launched = browser.open(site)
            seen.append(launched.profile.name)
            assert launched.profile.is_dir()
        finally:
            browser.close()
        assert browser.cleanup_error == ""
    assert len(set(seen)) == 2, "a profile was reused"
    assert stray_profiles(root) == [], "a profile was left on disk"


@needs_browser
@pytest.mark.real_socket
def test_a_read_only_pass_reads_the_page_and_scrolls_it_with_wheel_events(site, tmp_path):
    browser = CleanBrowser(headless=True, profile_root=tmp_path / "profiles", port=_free_port())
    try:
        browser.open(site)
        page = browser.page(site)
        page.wait_for_load()
        facts = page.facts()
        assert facts.title == "Synthetic 3D Site"
        assert facts.elements > 5 and facts.height_screens > 2
        assert any(asset["url"].endswith("bundle.js") for asset in page.resources())
        page.scroll(4)
        # The page only renames itself from a WHEEL listener, which scrollTo would never fire.
        assert page.evaluate("document.title").startswith("scrolled")
        page.close()
    finally:
        browser.close()


@needs_browser
@needs_kit
@pytest.mark.real_socket
def test_the_drill_takes_a_real_page_apart_and_leaves_no_trace(site, tmp_path):
    """The pack's Prompt 12: a real teardown, checked, with nothing left behind."""
    vault = vault_for(tmp_path)
    notes_before = vault.node_count
    profiles = tmp_path / "profiles"
    teardown = Teardown(Kit(), headless=True, profile_root=profiles)

    evidence = teardown.capture(site, question="how does the scroll work?")

    # The evidence REA was asked for
    assert evidence.evidence_ids, "no REA evidence id came back"
    assert evidence.screenshot[:4] == b"\x89PNG", "the snapshot is not a PNG"
    assert evidence.page["title"] == "Synthetic 3D Site"
    assert evidence.page["height_screens"] > 2

    # The stack, named from the bundle REA captured
    names = {signal.name: signal for signal in evidence.signals}
    assert names["Three.js"].version == "165"
    assert names["GSAP"].version == "3.12.5"
    assert names["Three.js"].how == fingerprints.BANNER

    # The asset log V.O.I.D read itself, which REA cannot have seen in full
    assert any(asset["url"].endswith("bundle.js") for asset in evidence.assets)
    assert {line.source for line in evidence.lines} >= {REA, OWN, BOTH}
    assert any("cannot" in text or "unavailable" in text or "only" in text
               for text in evidence.limitations)

    # The note, written and indexed
    written = write_note(evidence, REPLY, vault=vault)
    assert vault.node_count == notes_before + 1
    assert written.note_id in vault.search("teardown verdict three.js").ids

    # And nothing left behind
    assert stray_profiles(profiles) == []
    assert not teardown.running


@needs_browser
@needs_kit
@pytest.mark.real_socket
def test_only_one_teardown_runs_at_a_time(site, tmp_path):
    teardown = Teardown(Kit(), headless=True, profile_root=tmp_path / "profiles")
    teardown._running = True                           # noqa: SLF001 - the state a capture sets
    with pytest.raises(TeardownUnavailable, match="already running"):
        teardown.capture(site)


# ================================================================== through the Assistant

def assistant(tmp_path, **over) -> Assistant:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    data = {"app": {"state_dir": str(tmp_path / "state")},
            "security": {"allowed_roots": [str(tmp_path / "work")]},
            "memory": {"enabled": False}}
    data.update(over)
    return Assistant(config=Config(data))


def test_the_capability_is_off_by_default_and_says_so(tmp_path):
    a = assistant(tmp_path)
    assert a.teardown is None
    out = a.run("tear down example.com")
    assert out.steps == 0 and "switched off" in out.result


def test_a_sentence_about_nothing_in_particular_does_not_reach_the_teardown(tmp_path):
    a = assistant(tmp_path, teardown={"enabled": True})
    assert a._teardown_route("tear down the old shed") is None
    assert a._teardown_route("open example.com") is None


def test_an_enabled_teardown_with_no_kit_names_what_to_install(tmp_path):
    a = assistant(tmp_path, teardown={"enabled": True,
                                      "kit_root": str(tmp_path / "no-kit-here")})
    out = a.run("tear down example.com")
    assert out.steps == 0 and out.engine_authored is True
    assert "rea-agents" in out.result or "not installed" in out.result


def test_an_unsafe_address_never_reaches_a_browser(tmp_path):
    from void.browser import UnsafeUrl
    teardown = Teardown(Kit(tmp_path / "kit"), profile_root=tmp_path / "profiles")
    for address in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"):
        with pytest.raises((UnsafeUrl, TeardownUnavailable)):
            teardown.capture(address)
