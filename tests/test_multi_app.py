"""Several applications (and folders) in ONE command, executed deterministically.

What this pins is the behaviour the milestone specified, and the traps found while building it:

  * each target is independent - a name that is not installed never cancels the ones that are;
  * a successful launch stays SILENT and only the failure is spoken, so "Opened X. Opened Y." is never said;
  * RESOLVE FIRST, SPLIT SECOND - an installed "Command and Conquer" is one application, not two;
  * a split fragment may not reach an installed name by PREFIX alone ("command" -> Command Prompt was a real
    false launch during development);
  * a list containing an instruction ("open chrome and delete my files") is not a list of names, and the WHOLE
    sentence goes to the model rather than being split and half-executed;
  * no model is asked anything: zero provider calls for any of it.
"""
import json
from pathlib import Path

import pytest

from tests.test_computer import FakeBackend, _exe
from void import perf
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.app import Assistant
from void.config import Config
from void.core.fast_path import FastPath, launch_targets, parse_launch
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.protected import EngineProtected

APPS = [
    ("Visual Studio Code", "code.exe"),
    ("WhatsApp", "whatsapp.exe"),
    ("File Explorer", "explorer2.exe"),
    ("ChatGPT", "chatgpt.exe"),
    ("Terminal", "wt.exe"),
    ("Opera GX Browser", "operagx.exe"),
    ("Command Prompt", "cmdish.exe"),
    ("Discord", "discord.exe"),
]


class _Counting(LLMProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text="(the model answered)")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A real Assistant whose catalog is a fixture and whose launches are recorded, not performed.

    ``shutil.which`` is stubbed out: the fixed alias path resolves REAL executables on PATH, so without this the
    tests would start the machine's own VS Code and report a launch this fixture never saw.
    """
    import void.actions.apps as apps_mod
    monkeypatch.setattr(apps_mod.shutil, "which", lambda c: None)

    def make(apps=APPS, folders=None):
        home = tmp_path / "home"
        (home / "ws").mkdir(parents=True, exist_ok=True)
        cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                      "security": {"allowed_roots": [str(home / "ws")]}})
        a = Assistant(config=cfg)
        backend = FakeBackend(apps=[{"name": n, "kind": "exe", "target": _exe(tmp_path, t)} for n, t in apps])
        catalog = AppCatalog(backend)
        launched = []
        fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
        a.tools = ToolRegistry()
        a.tools.register_all(fa.tools())
        a.tools.register_all(AppActions(fa, catalog=catalog,
                                        launcher=lambda k, t: launched.append(t)).tools())
        a._fast = FastPath(catalog, folders=folders)
        gemini, local = _Counting("gemini"), _Counting("local")
        a.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])
        return a, launched, gemini, local
    return make


# --- parsing --------------------------------------------------------------------------------------

@pytest.mark.parametrize("said,expected", [
    ("open vs code and whatsapp", (("vs code",), ("whatsapp",))),
    ("open chrome, vs code and file explorer", (("chrome",), ("vs code",), ("file explorer",))),
    ("open chrome, vs code, file explorer", (("chrome",), ("vs code",), ("file explorer",))),
    ("Open VS Code and WhatsApp.", (("vs code",), ("whatsapp",))),
    ("hey void, please open vs code and whatsapp", (("vs code",), ("whatsapp",))),
    ("could you open discord and terminal please", (("discord",), ("terminal",))),
    ("launch discord and terminal", (("discord",), ("terminal",))),
    ("start discord and terminal", (("discord",), ("terminal",))),
])
def test_a_list_of_names_is_parsed_whatever_the_wording(said, expected):
    """Not one exact sentence format: the polite prefixes, all three verbs, commas and "and" all work."""
    assert launch_targets(said) == expected


def test_the_trailing_noun_belongs_to_the_last_item_only():
    assert launch_targets("open chrome and the calculator app") == (("chrome",), ("calculator", "calculator app"))


def test_one_name_is_still_one_target_and_parse_launch_is_unchanged():
    assert launch_targets("open vs code") == (("vs code",),)
    assert parse_launch("open vs code") == "vs code"
    assert parse_launch("open the calculator app") == "calculator"


@pytest.mark.parametrize("said", [
    "open chrome and delete my files", "open notepad, and remove everything", "open chrome and send an email",
    "open chrome and shut down the pc", "open chrome and install something", "open chrome and play music",
    "open chrome and close discord", "open chrome and search for cats",
])
def test_a_list_carrying_an_instruction_is_not_a_list_of_names(said):
    """The whole sentence goes to the model. Splitting must never drop an instruction by calling it a missing app."""
    assert launch_targets(said) == ()


def test_more_targets_than_the_bound_are_not_split_at_all():
    assert launch_targets("open a, b, c, d, e, f, g") == ()
    assert len(launch_targets("open a, b, c, d, e, f")) == 6


@pytest.mark.parametrize("said", [
    r"open chrome and C:\Windows\System32\cmd.exe", "open chrome and setup.exe", "open chrome and run.ps1",
    "open chrome and notepad | calc", "open chrome and $(calc)", "open chrome and http://evil.example",
    "open chrome and ../../secret", "open chrome and 'notepad'",
])
def test_a_list_cannot_express_a_path_a_script_or_a_shell_string(said):
    """Every item passes the same character and extension rules, so the list cannot widen what is expressible."""
    assert launch_targets(said) == ()


# --- resolve first, split second ------------------------------------------------------------------

def test_an_installed_name_containing_and_is_never_split(rig):
    a, launched, gemini, local = rig(apps=[*APPS, ("Command and Conquer", "cnc.exe")])
    r = a.run("open command and conquer")
    assert r.status == "completed"
    assert len(launched) == 1 and launched[0].endswith("cnc.exe")
    assert gemini.calls == 0 and local.calls == 0


def test_a_split_fragment_may_not_reach_an_installed_name_by_prefix_alone(rig):
    """The real false launch found in development: "open Command and Conquer" started Command Prompt."""
    a, launched, gemini, local = rig()                    # Command and Conquer is NOT installed here
    r = a.run("open command and conquer")
    assert launched == [], "a single split word claimed the start of a different program"
    assert "can't find" in (r.result or "")
    assert gemini.calls == 0 and local.calls == 0


def test_two_leading_words_may_still_prefix_match_in_a_list(rig):
    """"Opera GX" is the leading words of "Opera GX Browser" - evidence, not a guess."""
    a, launched, *_ = rig()
    a.run("open opera gx and discord")
    assert len(launched) == 2


def test_a_whole_single_name_may_still_prefix_match(rig):
    a, launched, *_ = rig()
    a.run("open opera gx")
    assert len(launched) == 1 and launched[0].endswith("operagx.exe")


# --- execution: every target independent ----------------------------------------------------------

def test_every_named_application_is_opened(rig):
    a, launched, gemini, local = rig()
    r = a.run("open vs code and whatsapp")
    assert len(launched) == 2
    assert r.status == "completed" and r.local_action is True        # success is silent
    assert gemini.calls == 0 and local.calls == 0


def test_three_named_applications_are_all_opened(rig):
    a, launched, gemini, local = rig()
    a.run("open chatgpt, vs code and file explorer")
    assert len(launched) == 3
    assert gemini.calls == 0 and local.calls == 0


def test_a_missing_application_does_not_cancel_the_others(rig):
    """The milestone's example: Notepad++ is not installed, and the other two must still open."""
    a, launched, gemini, local = rig()
    r = a.run("open vs code, whatsapp and notepad++")
    assert len(launched) == 2, "a name that is not installed cancelled the ones that are"
    assert r.status == "completed"
    assert gemini.calls == 0 and local.calls == 0


def test_only_the_failure_is_spoken_never_the_successes(rig):
    a, launched, *_ = rig()
    r = a.run("open vs code, whatsapp and notepad++")
    assert "notepad++" in r.result
    assert "Opened" not in r.result and "Opening" not in r.result
    assert r.local_action is False, "a partial result must be HEARD, not suppressed as a silent local action"


def test_a_fully_successful_run_says_nothing_out_loud(rig):
    a, launched, *_ = rig()
    r = a.run("open vs code and whatsapp")
    assert r.local_action is True, "a successful local action is silent"
    assert r.result == "Opening Visual Studio Code and WhatsApp."


def test_every_name_missing_is_reported_in_one_sentence(rig):
    a, launched, gemini, local = rig()
    r = a.run("open notepad++ and someunknownthing")
    assert launched == []
    assert "notepad++" in r.result and "someunknownthing" in r.result
    assert r.result.count("can't find") == 1
    assert r.local_action is False
    assert gemini.calls == 0 and local.calls == 0


def test_one_ambiguous_name_asks_and_launches_nothing(rig):
    """Launching some of the list and then asking would leave the owner unable to tell what happened."""
    a, launched, gemini, local = rig(apps=[("Aurora One", "a1.exe"), ("Aurora Two", "a2.exe"),
                                           ("Discord", "discord.exe")])
    r = a.run("open aurora and discord")
    assert launched == []
    assert "more than one" in (r.result or "")
    assert gemini.calls == 0 and local.calls == 0


def test_a_launch_that_fails_is_reported_and_the_rest_still_open(rig, monkeypatch):
    a, launched, *_ = rig()

    def flaky(kind, target):
        if "whatsapp" in target:
            raise OSError("boom")
        launched.append(target)
    tool = a.tools.get("launch_app")
    monkeypatch.setattr(tool.handler.__self__, "_launch", flaky)
    r = a.run("open vs code and whatsapp")
    assert len(launched) == 1
    assert "WhatsApp" in (r.result or "") and r.local_action is False


# --- security -------------------------------------------------------------------------------------

def test_a_shell_word_anywhere_in_the_list_excludes_the_whole_sentence(rig):
    a, launched, gemini, local = rig(apps=[*APPS, ("Registry Editor", "regedit.exe")])
    r = a.run("open discord and regedit")
    assert launched == []
    assert gemini.calls + local.calls == 1, "the sentence went to the model, and nothing was started here"
    assert r.status in ("completed", "failed")


def test_arguments_are_always_engine_chosen_never_transcript_text(rig):
    a, launched, *_ = rig()
    d = a._fast.decide("open vs code and whatsapp")
    for target in d.plan.targets:
        for call in target.alternatives:
            assert call.name in ("launch_app", "open_path")
            value = next(iter(call.arguments.values()))
            assert value.startswith("app-") or value in ("vscode", "code"), value


def test_confirmation_semantics_are_preserved_for_the_whole_list(rig):
    """If the gate would ask, the fast path executes NOTHING and the ordinary loop owns the confirmation."""
    from void.security.risk import RiskGate
    asked = []
    a, launched, gemini, local = rig()
    a.risk_gate = RiskGate(confirm_at_or_above="low")
    a._confirm_fn = lambda d: asked.append(d) or False
    r = a.run("open vs code and whatsapp")
    assert launched == [], "part of a list was executed before the gate was consulted"
    assert gemini.calls + local.calls == 1
    assert r is not None


def test_a_denied_target_is_reported_and_not_retried_through_another_path(rig):
    a, launched, gemini, local = rig()
    a.risk_gate.authorize = lambda *a_, **k_: False        # authorize returns a bool, not a pair
    r = a.run("open vs code and whatsapp")
    assert launched == []
    assert r is not None and r.status == "failed"
    assert gemini.calls == 0 and local.calls == 0


def test_the_kill_switch_stops_a_list_mid_flight(rig):
    a, launched, *_ = rig()
    a.kill_switch.engage("test")
    r = a.run("open vs code and whatsapp")
    assert launched == []
    assert r is not None


# --- telemetry ------------------------------------------------------------------------------------

def test_telemetry_counts_targets_and_missing_names_and_carries_no_text(rig, tmp_path):
    path = perf.configure(tmp_path / "perf")
    try:
        a, *_ = rig()
        a.run("open vs code, whatsapp and notepad++")
    finally:
        perf.shutdown()
    events = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    route = next(e for e in events if e["event"] == "route" and e["reason"] == "fast_path")
    assert route["kind"] == "multi" and route["targets"] == 2 and route["missing"] == 1
    assert route["llm_calls"] == 0
    blob = json.dumps(events)
    for content in ("notepad++", "whatsapp", "vs code", "WhatsApp"):
        assert content not in blob, content


# --- folders --------------------------------------------------------------------------------------

def test_a_folder_and_an_application_in_one_command(rig, tmp_path):
    from void.actions.folders import FolderCatalog
    home = tmp_path / "home"
    (home / "ws" / "Projects").mkdir(parents=True, exist_ok=True)
    folders = FolderCatalog(roots=(str(home / "ws"),), confine=lambda p: p)
    a, launched, gemini, local = rig(folders=folders)
    opened = []
    a.tools.get("open_path").handler = lambda target: opened.append(target) or _ok()
    r = a.run("open projects and terminal")
    assert opened and opened[0].endswith("Projects")
    assert len(launched) == 1
    assert gemini.calls == 0 and local.calls == 0
    assert r.local_action is True


def _ok():
    from void.actions.base import ToolResult
    return ToolResult.success("opened")


def test_an_installed_application_wins_its_own_name_over_a_folder(rig, tmp_path):
    from void.actions.folders import FolderCatalog
    home = tmp_path / "home"
    (home / "ws" / "Discord").mkdir(parents=True, exist_ok=True)
    folders = FolderCatalog(roots=(str(home / "ws"),), confine=lambda p: p)
    a, launched, *_ = rig(folders=folders)
    a.run("open discord")
    assert len(launched) == 1 and launched[0].endswith("discord.exe")


def test_without_a_folder_index_a_folder_name_goes_to_the_model_exactly_as_before(rig):
    a, launched, gemini, local = rig(folders=None)
    a.run("open my projects folder")
    assert launched == []
    assert gemini.calls + local.calls == 1


def test_nothing_opened_and_a_launch_that_failed_still_falls_back_to_the_ordinary_agent(rig, monkeypatch):
    """The distinction that matters: a name that does not exist is answered here, a launch that FAILED is not.

    With both in one sentence and nothing opened, the ordinary agent must still get the sentence - it may know
    another way to start the program, and reporting "I can't find it" about something that IS installed would be
    wrong. (A mutation that removed this fell through every other test.)
    """
    a, launched, gemini, local = rig()

    def always_fails(kind, target):
        raise OSError("nope")
    monkeypatch.setattr(a.tools.get("launch_app").handler.__self__, "_launch", always_fails)
    r = a.run("open notepad++ and vs code")
    assert launched == []
    assert gemini.calls + local.calls == 1, "the ordinary agent never got the sentence"
    assert "can't find" not in (r.result or ""), "an installed application was reported as missing"


def test_two_folders_with_one_name_are_told_apart_by_their_location(rig, tmp_path):
    """"I found more than one match: Downloads, Downloads" is not a question anyone can answer."""
    from void.actions.folders import FolderCatalog
    ws = tmp_path / "home" / "ws"
    (ws / "alpha" / "Notes").mkdir(parents=True, exist_ok=True)
    (ws / "beta" / "Notes").mkdir(parents=True, exist_ok=True)
    folders = FolderCatalog(roots=(str(ws),), confine=lambda p: p)
    a, launched, gemini, local = rig(folders=folders)
    r = a.run("open notes")
    assert launched == []
    reply = r.result or ""
    assert "alpha" in reply and "beta" in reply, reply
    assert reply.count("Notes in") == 2, reply
    assert gemini.calls == 0 and local.calls == 0
