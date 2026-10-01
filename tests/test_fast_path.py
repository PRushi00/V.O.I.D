"""The deterministic fast path ("open <known app>" with no model call): what it accepts, what it refuses, and that
everything it does still goes through the kill switch, the risk gate and telemetry."""
import ast
import json
from pathlib import Path

import pytest

import void.actions.apps as apps
from void import perf
from void.actions.apps import _APP_ALIASES, AppActions
from void.actions.computer import AppCatalog
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.app import Assistant
from void.config import Config
from void.core import fast_path
from void.core.fast_path import FastPath, parse_launch
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.protected import EngineProtected

from tests.helpers import FakeProvider
from tests.test_computer import FakeBackend


# --- the grammar (pure) -----------------------------------------------------------------------

@pytest.mark.parametrize("text, phrase", [
    ("open notepad", "notepad"),
    ("Open Notepad.", "notepad"),
    ("  OPEN   notepad  ", "notepad"),
    ("please open notepad", "notepad"),
    ("hey void, open notepad", "notepad"),
    # Live validation, 2026-09-23: "Hey V.O.I.D., open WhatsApp" did not fast-path at all, because the wake word
    # was only recognised spelled "void". Speech-to-text writes the initialism out, and a typed command keeps it.
    ("Hey V.O.I.D., open notepad", "notepad"),
    ("hey v.o.i.d. open notepad", "notepad"),
    ("V.O.I.D., open notepad", "notepad"),
    ("hey v o i d, open notepad", "notepad"),
    ("could you open the calculator app please", "calculator"),
    ("launch vs code", "vs code"),
    ("start chrome", "chrome"),
    ("open up spotify", "spotify"),
    ("open Visual Studio Code!", "visual studio code"),
])
def test_grammar_accepts_a_plain_launch_sentence(text, phrase):
    assert parse_launch(text) == phrase


@pytest.mark.parametrize("text", [
    # a filesystem path / executable path
    r"open C:\Windows\System32\cmd.exe", r"open c:\tools\thing", "open /usr/bin/python", r"open ..\..\secret",
    r"open \\server\share\x", "open ~/notes", r"open %windir%\notepad", "open ./run",
    r"open C:\Users\me\.void\pairing_window.json", "open file:///c:/x", "open http://evil.example", "open https://a.b",
    # an executable / script by name
    "open setup.exe", "open run.bat", "open evil.ps1", "open payload.vbs", "open thing.lnk", "open x.dll",
    # shell metacharacters and injection
    "open notepad && calc", "open notepad & calc", "open notepad; del *.*", "open notepad | calc", "open notepad > x",
    "open $(calc)", "open `calc`", 'open "notepad"', "open 'notepad'; rm", "open notepad || calc",
    "open {calc}", "open [abc]", "open *", "open no?te", "open (calc)", "open ^calc", "open notepad,calc", "open a=b",
    # a compound / qualified request is not a plain launch
    "open notepad and delete my files", "open chrome then go to youtube", "open notepad with secret.txt",
    "open notepad in the background", "open the file in notepad", "start chrome for me", "open notepad after that",
    # not a launch at all
    "what time is it", "close notepad", "open", "open ", "launch", "", "   ", "opennotepad",
    "remember that I open notepad", "delete notepad",
    # bad types / sizes
    None, 12345, b"open notepad", ["open notepad"], "open " + "a" * 200, "x" * 5000, "open notepad\x00.exe",
])
def test_grammar_refuses_everything_else(text):
    assert parse_launch(text) is None


def test_multiline_input_is_never_two_commands():
    # Whitespace collapses to one line, so the second line becomes part of the (unmatchable) app name.
    assert parse_launch("open notepad\nrm -rf everything") not in ("notepad",)


def test_spoken_names_only_point_at_real_alias_keys():
    assert set(fast_path._SPOKEN.values()) <= set(_APP_ALIASES)
    assert set(fast_path._DISPLAY) <= set(_APP_ALIASES)


# --- the resolver -----------------------------------------------------------------------------

def _exe(tmp_path, name):
    p = tmp_path / name
    p.write_text("stub")
    return str(p)


def _fp(tmp_path, *names, backend=None):
    be = backend or FakeBackend(apps=[{"name": n, "kind": "exe", "target": _exe(tmp_path, f"{i}.exe")}
                                      for i, n in enumerate(names)])
    return FastPath(AppCatalog(be))


def test_alias_resolves_to_the_alias_key_not_to_speech(tmp_path):
    d = _fp(tmp_path).decide("open notepad")
    call = d.plan.calls[0]
    assert (call.name, call.arguments, call.reply) == ("launch_app", {"name": "notepad"}, "Opening Notepad.")
    assert d.plan.kind == "alias"


@pytest.mark.parametrize("said, key, shown", [
    ("open vs code", "vscode", "VS Code"), ("open visual studio code", "vscode", "VS Code"),
    ("open calculator", "calc", "Calculator"), ("launch file explorer", "explorer", "File Explorer"),
    ("start google chrome", "chrome", "Chrome"), ("open microsoft edge", "edge", "Edge"),
])
def test_spoken_alias_forms(tmp_path, said, key, shown):
    call = _fp(tmp_path).decide(said).plan.calls[0]
    assert call.arguments == {"name": key} and call.reply == f"Opening {shown}."


def test_catalog_app_with_one_exact_match_resolves_to_its_app_id(tmp_path):
    fp = _fp(tmp_path, "Spotify", "Steam")
    d = fp.decide("open spotify")
    call = d.plan.calls[0]
    assert d.plan.kind == "catalog" and call.reply == "Opening Spotify."
    assert call.arguments["name"].startswith("app-") and "exe" not in call.arguments["name"]


def test_unknown_app_is_not_handled_and_never_guessed(tmp_path):
    # An unrelated name resolves to nothing. ("open spotifi" deliberately DOES resolve now - it sounds exactly
    # like Spotify - which is what the sound tier is for; see test_app_discovery.py.)
    d = _fp(tmp_path, "Spotify").decide("open telegram")
    assert d.plan is None and d.why == "unknown" and d.matched


def test_a_spoken_name_resolves_to_an_installed_name_that_only_adds_trailing_words(tmp_path):
    """The measured cause of a 77 s "Open Opera GX" (docs/VOICE_LATENCY_2026-09-23.md): the Start-Menu entry is
    "Opera GX Browser", exact matching found nothing, and the command went to the model for four round trips.
    A WHOLE-WORD prefix is accepted when it is unique."""
    d = _fp(tmp_path, "Opera GX Browser").decide("open opera gx")
    assert d.plan is not None and d.plan.calls[0].reply == "Opening Opera GX Browser."
    assert d.plan.calls[0].arguments["name"].startswith("app-")      # still an engine-chosen app_id, never speech


def test_a_substring_is_still_not_a_match(tmp_path):
    # Matching starts at the FIRST word: a name the spoken phrase merely appears inside never resolves.
    # ("code" is deliberately avoided here - it is a fixed ALIAS key, so it never reaches the catalog at all.)
    assert _fp(tmp_path, "Visual Studio Enterprise").decide("open studio").plan is None
    assert _fp(tmp_path, "My Spotify Player").decide("open spotify").plan is None


def test_a_partial_word_is_not_a_match(tmp_path):
    # Whole words only, so a truncated or run-together transcript cannot silently launch something.
    for said in ("open oper", "open opera g", "open operagx"):
        assert _fp(tmp_path, "Opera GX Browser").decide(said).plan is None


def test_reordered_or_extra_words_are_not_a_match(tmp_path):
    for said in ("open gx opera", "open browser opera gx", "open opera browser"):
        assert _fp(tmp_path, "Opera GX Browser").decide(said).plan is None


def test_an_exact_name_wins_over_a_longer_entry_it_is_a_prefix_of(tmp_path):
    # "opera" is installed in its own right; saying it must open THAT, not the longer Opera GX entry.
    be = FakeBackend(apps=[{"name": "opera", "kind": "exe", "target": _exe(tmp_path, "o.exe")},
                           {"name": "Opera GX Browser", "kind": "lnk", "target": _exe(tmp_path, "gx.lnk")}])
    cat = AppCatalog(be)
    d = FastPath(cat).decide("open opera")
    assert d.plan is not None and d.plan.calls[0].reply == "Opening opera."
    exact = cat.find("opera")
    assert len(exact) == 1 and d.plan.calls[0].arguments["name"] == exact[0].app_id


def test_an_ambiguous_prefix_is_never_guessed(tmp_path):
    # Two installed names share the spoken prefix: the fast path refuses and the agent asks the owner.
    d = _fp(tmp_path, "Opera GX Browser", "Opera GX Developer").decide("open opera gx")
    assert d.plan is None and d.why == "ambiguous"


def test_the_exclusion_applies_to_the_resolved_entry_however_it_was_resolved(tmp_path):
    # Reaching an excluded program by prefix rather than by its exact name must not get round the refusal.
    d = _fp(tmp_path, "Registry Editor (x64)").decide("open registry editor")
    assert d.plan is None and d.why == "excluded"


def test_ambiguous_catalog_match_is_not_picked(tmp_path):
    be = FakeBackend(apps=[{"name": "Editor", "kind": "exe", "target": _exe(tmp_path, "a.exe")},
                           {"name": "editor", "kind": "lnk", "target": _exe(tmp_path, "b.lnk")}])
    d = FastPath(AppCatalog(be)).decide("open editor")
    assert d.plan is None and d.why == "ambiguous"


# Where the line is drawn, and why (2026-09-24). The fast path refuses what ACTS the moment it starts: consoles
# that edit the machine's configuration, and the script hosts whose purpose is to run something else. A terminal
# emulator does not act - it shows a prompt - and V.O.I.D cannot type into it, so refusing to OPEN one bought no
# safety and broke "open Terminal", a command the owner uses. The property that makes this sound is pinned by
# test_the_fast_path_never_supplies_an_argument_to_anything_it_starts below.

@pytest.mark.parametrize("said", [
    "open regedit", "open registry editor", "open task scheduler", "open gpedit", "open secpol",
    "open diskpart", "open bcdedit", "open mmc", "open mshta", "open wscript", "open cscript",
    "open rundll32", "open msiexec", "open schtasks"])
def test_administration_consoles_and_script_hosts_are_never_fast_pathed(tmp_path, said):
    d = _fp(tmp_path, "Registry Editor", "Task Scheduler", "Windows PowerShell").decide(said)
    assert d.plan is None and d.why == "excluded"


@pytest.mark.parametrize("said, opens", [
    ("open terminal", "Terminal"), ("open windows terminal", "Terminal"), ("launch terminal", "Terminal"),
    ("open command prompt", "Command Prompt"), ("open windows powershell", "Windows PowerShell")])
def test_a_terminal_window_is_an_ordinary_application(tmp_path, said, opens):
    d = _fp(tmp_path, "Terminal", "Command Prompt", "Windows PowerShell").decide(said)
    assert d.plan is not None and d.plan.calls[-1].reply == f"Opening {opens}."


def test_the_fast_path_never_supplies_an_argument_to_anything_it_starts(tmp_path):
    """The whole justification for opening a terminal: V.O.I.D has no way to hand it something to run.

    Every call the fast path emits is launch_app with a single engine-chosen identity - never a command, never an
    argument list, never anything carried over from what was said.
    """
    fp = _fp(tmp_path, "Terminal", "Command Prompt", "Opera GX Browser")
    for said in ("open terminal", "open command prompt", "open opera gx"):
        for call in fp.decide(said).plan.calls:
            assert call.name == "launch_app"
            assert set(call.arguments) == {"name"}
            assert isinstance(call.arguments["name"], str)


def test_a_catalog_that_cannot_be_built_falls_back_but_aliases_still_work(tmp_path):
    fp = FastPath(AppCatalog(FakeBackend(raise_on={"discover_apps"})))
    assert fp.decide("open spotify").why == "discovery"
    assert fp.decide("open notepad").plan is not None


def test_alias_takes_priority_and_a_unique_catalog_entry_is_only_the_fallback(tmp_path):
    d = _fp(tmp_path, "Notepad").decide("open notepad")
    assert [c.arguments["name"] for c in d.plan.calls][0] == "notepad"
    assert len(d.plan.calls) == 2 and d.plan.calls[1].arguments["name"].startswith("app-")


def test_a_hostile_catalog_name_cannot_inject_into_the_spoken_reply(tmp_path):
    name = "Evil\x1b[31m<script>C:\\x\\y.exe"
    be = FakeBackend(apps=[{"name": name, "kind": "exe", "target": _exe(tmp_path, "e.exe")}])
    d = FastPath(AppCatalog(be)).decide(f"open {name}")
    assert d.plan is None                          # the phrase itself cannot even express it


# --- through the real Assistant --------------------------------------------------------------

@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    launched: list = []
    monkeypatch.setattr(apps.shutil, "which",
                        lambda c: {"notepad": r"C:\fake\notepad.exe", "calc": r"C:\fake\calc.exe"}.get(c))
    monkeypatch.setattr(apps.subprocess, "Popen", lambda argv, *a, **k: launched.append(("popen", tuple(argv))))

    def make(catalog_apps=(), sec=None, fast=True, confirm_fn=None, llm=("ok",)):
        cfg = Config({"app": {"state_dir": ".void"}, "memory": {"enabled": False},
                      "security": {"allowed_roots": [str(home / "ws")], **(sec or {})},
                      "fast_path": {"enabled": fast}})
        a = Assistant(config=cfg, confirm_fn=confirm_fn)
        entries = [a if isinstance(a, dict) else {"name": a[0], "kind": "exe", "target": a[1]}
                   for a in catalog_apps]
        catalog = AppCatalog(FakeBackend(apps=entries))
        fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
        aa = AppActions(fa, catalog=catalog, launcher=lambda kind, target: launched.append((kind, target)))
        a.tools = ToolRegistry()
        a.tools.register_all(aa.tools())
        a._fast = FastPath(catalog) if fast else None
        provider = FakeProvider([LLMResponse(text=t) for t in llm])
        a.providers = ProviderRegistry({"fake": provider}, ["fake"])
        return a, provider, launched, home

    make.launched = launched
    return make


def _no_leak(text):
    for bad in ("\\", ".exe", "launch_app", "Traceback", "app-", "Error", "/"):
        assert bad not in (text or ""), (bad, text)


def test_known_alias_makes_zero_llm_calls_and_answers_briefly(rig):
    a, provider, launched, _ = rig()
    r = a.run("open notepad")
    assert provider.calls == 0
    assert r.status == "completed" and r.result == "Opening Notepad." and r.steps == 1
    assert launched == [("popen", (r"C:\fake\notepad.exe",))]
    _no_leak(r.result)


def test_known_catalog_app_makes_zero_llm_calls(rig, tmp_path):
    target = _exe(tmp_path, "spotify.exe")
    a, provider, launched, _ = rig(catalog_apps=[("Spotify", target)])
    r = a.run("launch spotify please")
    assert provider.calls == 0 and r.result == "Opening Spotify." and launched == [("exe", target)]
    _no_leak(r.result)


def test_it_works_with_no_model_available_at_all(rig):
    a, _provider, launched, _ = rig()
    a.providers = ProviderRegistry({}, [])          # nothing to select: the normal path would raise
    assert a.run("open notepad").result == "Opening Notepad."
    assert len(launched) == 1


def test_the_run_is_recorded_like_any_other_task(rig):
    a, *_ = rig()
    r = a.run("open notepad")
    saved = a.store.load(r.task.id)
    assert saved is not None and saved.status == "completed" and saved.goal == "open notepad"


@pytest.mark.parametrize("said", ["open spotifi", "open sublime text", "open photoshop"])
def test_an_unresolvable_application_launches_nothing_and_is_answered_locally(rig, said):
    """It used to go to the model, which then spent up to 12 calls searching a catalog the resolver had already
    searched - the measured cause of the 3-5 minute "Open Notepad++" (tests/test_missing_app.py)."""
    a, provider, launched, _ = rig()
    r = a.run(said)
    assert launched == [] and provider.calls == 0
    assert r.status == "completed" and "can't find" in r.result.lower()


@pytest.mark.parametrize("said", ["open a program that does not exist", "open my project folder"])
def test_a_phrase_that_is_not_a_product_name_still_goes_to_the_model(rig, said):
    """The local "not installed" answer is only for things that look like applications. Anything pointing at the
    owner's own content is the model's job - it has find_directory and open_path, which the fast path does not."""
    a, provider, launched, _ = rig()
    r = a.run(said)
    assert provider.calls == 1 and launched == [] and r.result == "ok"


def test_an_ambiguous_app_is_asked_about_and_nothing_is_launched(rig, tmp_path):
    """Section 6: never guess between installed applications. Answering here rather than sending the sentence to
    the model is faster, and is the only answer available at all when the cloud is down - which is exactly when
    the owner most needs "open <app>" to keep behaving sensibly."""
    a, provider, launched, _ = rig(catalog_apps=[("Editor", _exe(tmp_path, "a.exe")),
                                                 ("Editor", _exe(tmp_path, "b.exe"))])
    r = a.run("open editor")
    assert launched == [] and provider.calls == 0
    assert r.status == "completed" and "more than one" in r.result.lower() and "which one" in r.result.lower()
    _no_leak(r.result)                                  # no paths, no app_ids, no tool names in what is spoken


@pytest.mark.parametrize("said", [
    r"open C:\Windows\System32\cmd.exe", "open notepad.exe", r"open C:\Users\x\.void\device_key.pem",
    "open notepad && calc", "open notepad; calc", "open $(calc)", "open notepad | calc", "open ../../x",
    r"open \\evil\share\a.exe", "open http://evil.example/a.exe", "open notepad and delete my files",
])
def test_paths_executables_and_shell_strings_never_reach_a_launch_from_the_fast_path(rig, said):
    a, provider, launched, _ = rig()
    a.run(said)
    assert launched == []                          # fast path did nothing (the model, a fake here, launches nothing)
    assert provider.calls == 1                     # ...and the sentence went the ordinary, gated way


def test_an_administration_console_is_never_started_by_the_fast_path_even_when_installed(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Registry Editor", _exe(tmp_path, "regedit.exe"))])
    a.run("open registry editor")
    assert launched == [] and provider.calls == 1


def test_a_catalog_target_inside_a_protected_location_is_refused(rig):
    a, provider, launched, home = rig()
    state = home / ".void"
    state.mkdir(exist_ok=True)
    evil = state / "planted.exe"
    evil.write_text("x")
    a2, provider2, launched2, _ = rig(catalog_apps=[("Tool", str(evil))])
    r = a2.run("open tool")
    assert launched2 == []                         # launch_app refused the protected target
    assert provider2.calls == 1                    # so it fell back to the ordinary path, which is refused the same way


def test_disabled_by_config_sends_everything_through_the_model(rig):
    a, provider, launched, _ = rig(fast=False)
    r = a.run("open notepad")
    assert provider.calls == 1 and launched == []


def test_a_failed_launch_falls_back_instead_of_claiming_success(rig, monkeypatch):
    a, provider, launched, _ = rig()
    monkeypatch.setattr(apps.shutil, "which", lambda c: None)       # the alias exe is not installed
    r = a.run("open notepad")
    assert launched == [] and provider.calls == 1 and r.result == "ok"


# --- kill switch and risk gate --------------------------------------------------------------

def test_kill_switch_engaged_blocks_the_fast_path(rig):
    a, provider, launched, _ = rig()
    a.kill_switch.engage(reason="test")
    r = a.run("open notepad")
    assert launched == [] and provider.calls == 0 and r.status == "paused"


def test_kill_switch_engaged_mid_run_stops_before_the_launch(rig):
    from void.core.agent import Agent
    from void.core.task import TaskStore
    from void.core.fast_path import DirectCall
    a, provider, launched, _ = rig()
    a.kill_switch.engage(reason="test")
    agent = Agent(provider=provider, tools=a.tools, risk_gate=a.risk_gate, kill_switch=a.kill_switch, store=a.store)
    r = agent.run_direct("open notepad", [DirectCall("launch_app", {"name": "notepad"}, "Opening Notepad.")])
    assert r.status == "paused" and launched == [] and provider.calls == 0


def test_every_fast_path_launch_is_authorized_by_the_risk_gate_exactly_once(rig):
    a, provider, launched, _ = rig()
    seen = []
    real = a.risk_gate.authorize
    a.risk_gate.authorize = lambda level, description, owner_decision=None: (
        seen.append((level.name, description)), real(level, description, owner_decision=owner_decision))[1]
    a.run("open notepad")
    assert seen == [("LOW", "launch_app({'name': 'notepad'})")]
    assert len(launched) == 1


def test_a_gate_denial_is_final_and_is_not_retried_through_the_model(rig):
    a, provider, launched, _ = rig()
    a.risk_gate.authorize = lambda *args, **kw: False
    r = a.run("open notepad")
    assert launched == [] and provider.calls == 0
    assert r.status == "failed" and "approval" in r.result
    _no_leak(r.result)


def test_an_action_the_gate_would_confirm_is_left_to_the_normal_loop(rig):
    asked = []
    a, provider, launched, _ = rig(sec={"confirm_at_or_above": "low"}, confirm_fn=lambda d: asked.append(d) or False)
    a.run("open notepad")
    # The fast path neither asked the owner nor ran anything itself; the ordinary agent handled the sentence.
    assert launched == [] and provider.calls == 1


def test_the_fast_path_module_cannot_launch_or_authorize_anything_itself():
    """Static guarantee: the decision module has no route to the risk gate, a process, a shell or the file system."""
    src = Path(fast_path.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported.isdisjoint({"subprocess", "os", "shutil", "webbrowser", "ctypes", "void.security.risk",
                                "void.security", "pathlib"}), imported
    for word in ("Popen", "startfile", "authorize(", "os.system", "shell=True", "open("):
        assert word not in src, word


# --- telemetry ----------------------------------------------------------------------------

def test_telemetry_records_the_route_with_zero_llm_calls_and_no_content(rig, tmp_path):
    path = perf.configure(tmp_path / "perf")
    try:
        a, *_ = rig()
        a.run("open notepad")
        a.run("open spotifi")                       # a miss
    finally:
        perf.shutdown()
    events = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    routes = [e for e in events if e["event"] == "route"]
    hit = next(e for e in routes if e["reason"] == "fast_path")
    assert hit["provider"] == "none" and hit["llm_calls"] == 0 and hit["kind"] == "alias"
    miss = next(e for e in routes if e["reason"] == "fast_path_miss")
    assert miss["why"] == "unknown"
    assert not any(e["event"] == "llm" and e.get("interaction_id") == hit["interaction_id"] for e in events)
    fast = json.dumps([e for e in routes if e["reason"].startswith("fast")])
    for content in ("open notepad", "spotifi", "notepad", "launch_app"):
        assert content not in fast, content


# --- provider failure must not poison the deterministic path (2026-09-23 field report) ------------------
#
# Reported: "explain ARP" failed (Gemini 503), after which "open WhatsApp" and "open Opera GX" also failed, as if a
# failed reasoning task had poisoned the voice loop. These pin the recovery property at both layers, with the failure
# injected deterministically (no real provider, no quota).


class _ProviderError(Exception):
    """Shape of a google-genai transport failure: carries code/status like the SDK's errors."""

    def __init__(self, code=503, status="UNAVAILABLE"):
        super().__init__(f"{code} {status}. This model is currently experiencing high demand.")
        self.code, self.status = code, status


class _AlwaysFails(LLMProvider):
    name = "gemini"

    def __init__(self, exc_factory=_ProviderError):
        self.calls = 0
        self._exc = exc_factory

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        raise self._exc()


@pytest.fixture
def no_backoff(monkeypatch):
    """The agent's bounded retry sleeps between attempts; the delay is not what these tests are about."""
    import void.core.agent as agent_mod
    monkeypatch.setattr(agent_mod.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("code, status", [(503, "UNAVAILABLE"), (429, "RESOURCE_EXHAUSTED"),
                                          (500, "INTERNAL"), (504, "DEADLINE_EXCEEDED")])
def test_a_failed_reasoning_task_does_not_poison_the_next_fast_command(rig, no_backoff, code, status):
    a, _fake, launched, _ = rig()
    prov = _AlwaysFails(lambda: _ProviderError(code, status))
    a.providers = ProviderRegistry({"gemini": prov}, ["gemini"])

    first = a.run("open notepad")                       # A: deterministic, before any failure
    assert first.status == "completed" and prov.calls == 0 and len(launched) == 1

    failed = a.run("explain ARP")                       # B: reasoning -> provider fails
    assert failed.status == "failed" and prov.calls >= 1

    after = a.run("open notepad")                       # C: deterministic again, immediately after
    assert after.status == "completed" and after.result == "Opening Notepad."
    assert len(launched) == 2                           # it really launched
    calls_after_c = prov.calls

    again = a.run("open notepad")                       # D: and again
    assert again.status == "completed" and len(launched) == 3
    assert prov.calls == calls_after_c                  # the fast path never touched the provider


def test_a_provider_failure_is_a_failed_task_not_an_escaping_exception(rig, no_backoff):
    a, _fake, _launched, _ = rig()
    a.providers = ProviderRegistry({"gemini": _AlwaysFails()}, ["gemini"])
    r = a.run("explain ARP")                            # must return, not raise
    assert r.status == "failed" and r.task.error and "503" in r.task.error


def test_a_failed_task_is_left_failed_not_running_and_is_not_reused(rig, no_backoff):
    a, _fake, _launched, _ = rig()
    a.providers = ProviderRegistry({"gemini": _AlwaysFails()}, ["gemini"])
    failed = a.run("explain ARP")
    stored = a.store.load(failed.task.id)
    assert stored is not None and stored.status == "failed" and not stored.pending
    nxt = a.run("open notepad")
    assert nxt.task.id != failed.task.id                # a fresh task, never a resumed/pending one
    assert nxt.status == "completed"


def test_an_unavailable_provider_does_not_stop_a_fast_command(rig):
    """A deterministic launch must not depend on the cloud at all - not even on a provider being selectable."""
    a, _fake, launched, _ = rig()
    a.providers = ProviderRegistry({}, [])              # selection would raise ProviderUnavailable
    r = a.run("open notepad")
    assert r.status == "completed" and len(launched) == 1


def test_the_voice_session_returns_to_idle_after_a_provider_failure_and_still_dispatches(rig, no_backoff):
    """The same sequence through the real VoiceSession state machine: a failure must leave it usable."""
    from void.voice.session import VoiceSession

    class _Cap:
        is_open = False

        def open(self):
            self.is_open = True

        def stop(self):
            self.is_open = False
            return [0.2] * 16000                        # 1 s of "speech" (passes the min-speech guard)

        def close(self):
            self.is_open = False

    class _STT:
        def __init__(self):
            self.queue = []

        def transcribe(self, audio):
            return self.queue.pop(0)

    class _TTS:
        def __init__(self):
            self.spoke = []

        is_speaking = False

        def speak(self, text):
            self.spoke.append(text)

        def stop(self):
            pass

        def close(self):
            pass

    a, _fake, launched, _ = rig()
    a.providers = ProviderRegistry({"gemini": _AlwaysFails()}, ["gemini"])
    cap, stt, tts = _Cap(), _STT(), _TTS()
    session = VoiceSession(a, a.kill_switch, capture=cap, stt=stt, tts=tts,
                           min_speech_ms=0.0)

    states = []
    for said in ("open notepad", "explain ARP", "open notepad", "open notepad"):
        stt.queue.append(said)
        session.on_ptt_press(source="wake")
        session.on_ptt_release()
        session.notify_speech_finished()
        states.append(session.state)
        assert not cap.is_open, f"microphone left open after {said!r}"

    assert states == ["idle"] * 4                        # never stuck in a failed/busy state
    assert len(launched) == 3                            # both launches after the failure still happened
    # A successful launch is silent (tests/test_silent_success.py), so the only thing spoken in this whole
    # sequence is the one genuine failure - and the launches that followed it happened without a word.
    assert tts.spoke == ["That task failed. Please check the command line for details."]


# --- automatic discovery end to end (2026-09-23) ------------------------------------------------------------
#
# The owner must never edit a list of applications. These drive the REAL Assistant with a discovered catalog and
# pin the two things that make that usable: the names people actually say resolve, and none of it costs a model
# call - so it keeps working when Gemini is returning 503s.

WHATSAPP_ID = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"


def _machine(tmp_path):
    """A catalog shaped like a real Windows machine: shortcuts, the SAME shortcut in two Start-Menu folders
    (exactly how Discord is installed on the owner's machine), and a Store app with no path at all."""
    vendor = tmp_path / "Discord Inc"
    vendor.mkdir(exist_ok=True)
    (vendor / "Discord.lnk").write_text("stub")
    return [{"name": "Opera GX Browser", "kind": "lnk", "target": _exe(tmp_path, "opera.lnk")},
            {"name": "Discord", "kind": "lnk", "target": _exe(tmp_path, "Discord.lnk")},
            {"name": "Discord", "kind": "lnk", "target": str(vendor / "Discord.lnk")},
            {"name": "Microsoft Teams", "kind": "lnk", "target": _exe(tmp_path, "teams.lnk")},
            {"name": "WhatsApp", "kind": "uwp", "target": WHATSAPP_ID}]


@pytest.mark.parametrize("said, opens", [
    ("open WhatsApp", "WhatsApp"), ("open whatsapp", "WhatsApp"), ("open Whats App", "WhatsApp"),
    ("hey void, open whats app", "WhatsApp"),
    ("open Opera GX", "Opera GX Browser"), ("open opera gx", "Opera GX Browser"),
    ("open Opera-GX", "Opera GX Browser"), ("open Opera G X", "Opera GX Browser"),
    ("please open the opera gx browser", "Opera GX Browser"),
    ("open Discord", "Discord"), ("launch discord", "Discord"), ("start Discord", "Discord"),
    ("open teams", "Microsoft Teams"), ("open Microsoft Teams", "Microsoft Teams"),
])
def test_a_discovered_application_opens_with_no_model_call(rig, tmp_path, said, opens):
    a, provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    r = a.run(said)
    assert provider.calls == 0, "a plain launch must never reach the model"
    assert r.status == "completed" and r.result == f"Opening {opens}."
    assert len(launched) == 1


def test_a_store_app_launches_from_the_fast_path_by_its_application_id(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    a.run("open whatsapp")
    assert provider.calls == 0 and launched == [("uwp", WHATSAPP_ID)]


def test_the_same_application_found_twice_still_opens_instead_of_becoming_ambiguous(rig, tmp_path):
    """Discord ships a Start-Menu shortcut in two folders. Before de-duplication that made "open Discord" an
    ambiguous exact match, so it silently fell through to the model - and failed outright while Gemini was down."""
    a, provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    r = a.run("open Discord")
    assert provider.calls == 0 and len(launched) == 1 and r.result == "Opening Discord."


def test_an_application_installed_after_startup_opens_without_a_restart(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    assert a.run("open spotify").result != "Opening Spotify."          # not installed yet: the model gets it
    backend = a._fast.catalog._backend
    backend._apps.append({"name": "Spotify", "kind": "exe", "target": _exe(tmp_path, "spotify.exe")})
    a._fast.catalog.invalidate()                                       # what a refresh does; no code change, no restart
    calls_before, launched_before = provider.calls, len(launched)
    r = a.run("open Spotify")
    assert r.result == "Opening Spotify." and provider.calls == calls_before
    assert len(launched) == launched_before + 1


def test_every_named_command_still_works_with_no_model_available_at_all(rig, tmp_path):
    """Section 17: application launching must not be coupled to any provider."""
    a, _provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    a.providers = ProviderRegistry({}, [])
    for said in ("open whats app", "open opera gx", "open discord", "open teams", "open notepad"):
        assert a.run(said).status == "completed", said
    assert len(launched) == 5


def test_an_ambiguous_name_is_answered_even_with_no_model_available(rig, tmp_path):
    a, _provider, launched, _ = rig(catalog_apps=[("Android Studio", _exe(tmp_path, "a.exe")),
                                                  ("Android Studio", _exe(tmp_path, "b.exe"))])
    a.providers = ProviderRegistry({}, [])
    r = a.run("open android studio")
    assert r.status == "completed" and "Android Studio" in r.result and launched == []


def test_a_clarification_leaves_the_session_ready_for_the_next_command(rig, tmp_path):
    """Section 18: ambiguous command -> clarification -> the next command still works."""
    a, provider, launched, _ = rig(catalog_apps=[("Editor", _exe(tmp_path, "a.exe")),
                                                 ("Editor", _exe(tmp_path, "b.exe")),
                                                 ("Opera GX Browser", _exe(tmp_path, "o.exe"))])
    assert a.run("open editor").status == "completed" and launched == []
    assert a.run("open opera gx").result == "Opening Opera GX Browser."
    assert len(launched) == 1 and provider.calls == 0


def test_a_launch_that_fails_leaves_the_next_command_working(rig, tmp_path):
    """Section 16/18: a launch failure is reported, not absorbed, and does not poison what comes next."""
    import os
    records = _machine(tmp_path)
    a, provider, launched, _ = rig(catalog_apps=records)
    gone = [r for r in records if r["name"] == "Opera GX Browser"][0]["target"]
    os.remove(gone)                              # uninstalled between discovery and the command
    r = a.run("open opera gx")
    assert launched == [] and r.result != "Opening Opera GX Browser."   # never claims a success it did not have
    assert a.run("open discord").result == "Opening Discord."           # and the next command is unaffected
    assert len(launched) == 1


@pytest.mark.parametrize("said", [
    "open C:\\Users\\me\\malicious.exe", "open gx browser", "open browser", "open studio",
    "open teams and delete my files", "open ms teams; calc",
])
def test_discovery_never_widens_what_a_sentence_can_express(rig, tmp_path, said):
    """Everything the resolver gained (punctuation, spacing, prefixes, vendor words) must not become a way to name
    an application the owner did not say - nor a way to name something that is not an application at all."""
    a, _provider, launched, _ = rig(catalog_apps=_machine(tmp_path))
    a.run(said)
    assert launched == []
