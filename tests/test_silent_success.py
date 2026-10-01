"""A local action that succeeded is silent; anything else still speaks.

Opening an application is its own confirmation - the window appears - so announcing "Opening VS Code." afterwards
added roughly 1.7-2.4 s of talking to a command that finished in about 10 ms. Failures say nothing visible
happened, so they must still be heard.

The classification is the ENGINE's, not the session's: ``AgentResult.local_action`` is set only where a task
COMPLETED by performing a local action - ``run_direct`` (the deterministic fast path) and
``_deterministic_completion`` (a first-step ``terminal_on_success`` tool that succeeded). The session honours that
flag and nothing else, which is why a failure, a clarification, a status phrase or any model-written answer is
unaffected.
"""
import pytest

from void.app import Assistant
from void.config import Config
from void.core.agent import AgentResult
from void.core.task import Status, Task
from void.providers.base import LLMProvider, LLMResponse, ToolCall
from void.providers.registry import ProviderRegistry
from void.voice.session import VoiceSession

from tests.test_computer import _exe


class _TTS:
    is_speaking = False

    def __init__(self):
        self.spoke = []

    def speak(self, text):
        self.spoke.append(text)

    def stop(self):
        pass

    def close(self):
        pass


class _Capture:
    is_open = False

    def open(self):
        self.is_open = True

    def stop(self):
        self.is_open = False
        return [0.2] * 16000          # 1 s of "speech" so the pre-STT guard passes

    def close(self):
        self.is_open = False


class _STT:
    def __init__(self, *texts):
        self.queue = list(texts)

    def transcribe(self, audio):
        return self.queue.pop(0)


class _Model(LLMProvider):
    name = "gemini"

    def __init__(self, text="A protocol that maps addresses."):
        self.calls = 0
        self._text = text

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text=self._text)


def _say(assistant, *transcripts, speak_successful_actions=False):
    """Drive the real session once per transcript; return (tts, session)."""
    tts, cap = _TTS(), _Capture()
    session = VoiceSession(assistant, assistant.kill_switch, capture=cap, stt=_STT(*transcripts), tts=tts,
                           min_speech_ms=0.0, speak_successful_actions=speak_successful_actions)
    for _ in transcripts:
        session.on_ptt_press(source="wake")
        session.on_ptt_release()
        session.notify_speech_finished()
        assert not cap.is_open, "the microphone was left open"
    return tts, session


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A real Assistant whose launches are recorded instead of performed."""
    import void.actions.apps as apps_mod
    from void.actions.apps import AppActions
    from void.actions.computer import AppCatalog
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.core.fast_path import FastPath
    from void.security.protected import EngineProtected
    from tests.test_computer import FakeBackend

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    launched: list = []
    monkeypatch.setattr(apps_mod.shutil, "which", lambda c: None)

    def make(apps=(("Opera GX Browser", "opera.lnk"), ("Notepad", "notepad.exe")), model=None):
        cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                      "security": {"allowed_roots": [str(home / "ws")]}})
        a = Assistant(config=cfg)
        rows = [{"name": n, "kind": "lnk" if t.endswith(".lnk") else "exe", "target": _exe(tmp_path, t)}
                for n, t in apps]
        catalog = AppCatalog(FakeBackend(apps=rows))
        fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
        aa = AppActions(fa, catalog=catalog, launcher=lambda kind, target: launched.append((kind, target)))
        a.tools = ToolRegistry()
        a.tools.register_all(aa.tools())
        a._fast = FastPath(catalog)
        provider = model or _Model()
        a.providers = ProviderRegistry({"gemini": provider}, ["gemini"])
        return a, provider, launched

    return make


# --- a successful local action is silent ---------------------------------------------------------

@pytest.mark.parametrize("said, opens", [
    ("open opera gx", "Opening Opera GX Browser."),
    ("Open Opera GX.", "Opening Opera GX Browser."),
    ("hey void, open opera gx", "Opening Opera GX Browser."),
    ("open notepad", "Opening Notepad."),
])
def test_a_successful_launch_says_nothing(rig, said, opens):
    a, provider, launched = rig()
    tts, session = _say(a, said)
    assert session._last_result.result == opens       # it did happen, and reports it internally
    assert len(launched) == 1                         # the action really ran
    assert tts.spoke == []                            # and was not announced
    assert provider.calls == 0


def test_the_action_is_what_is_suppressed_not_the_result(rig):
    """Suppressing speech must not change execution, status, or what the engine recorded."""
    a, _provider, launched = rig()
    _tts, session = _say(a, "open opera gx")
    result = session._last_result
    assert result.status == Status.COMPLETED and result.local_action is True
    assert result.result == "Opening Opera GX Browser." and len(launched) == 1


def test_several_launches_in_a_row_stay_silent_and_all_run(rig):
    a, _provider, launched = rig()
    tts, _session = _say(a, "open opera gx", "open notepad", "open opera gx")
    assert len(launched) == 3 and tts.spoke == []


# --- everything else still speaks ----------------------------------------------------------------

@pytest.mark.parametrize("said", ["Open Notepad++", "open photoshop", "launch sublime text"])
def test_an_application_that_cannot_be_found_is_spoken(rig, said):
    """Nothing visible happened, so silence would leave the owner with no idea why."""
    a, provider, launched = rig()
    tts, _session = _say(a, said)
    assert launched == [] and provider.calls == 0
    assert len(tts.spoke) == 1 and "can't find" in tts.spoke[0].lower()


def test_an_ambiguous_name_is_spoken(rig, tmp_path):
    a, provider, launched = rig(apps=(("Editor", "a.exe"), ("Editor", "b.exe")))
    tts, _session = _say(a, "open editor")
    assert launched == [] and len(tts.spoke) == 1
    assert "more than one" in tts.spoke[0].lower()


@pytest.mark.parametrize("said", ["Explain ARP", "What is DNS?", "what time is it"])
def test_an_informational_answer_is_spoken(rig, said):
    a, provider, launched = rig()
    tts, _session = _say(a, said)
    assert provider.calls == 1 and launched == []
    assert len(tts.spoke) == 1


def test_a_failed_launch_is_spoken(rig, tmp_path):
    """The shortcut resolves but the target is gone: the task fails, and a failure must be heard."""
    import os
    a, provider, launched = rig(apps=(("Opera GX Browser", "opera.lnk"),))
    os.remove(_exe(tmp_path, "opera.lnk"))
    tts, session = _say(a, "open opera gx")
    assert launched == []
    assert session._last_result.local_action is False
    assert len(tts.spoke) == 1


def test_a_launch_the_risk_gate_refuses_is_spoken(rig, monkeypatch):
    """A refusal is a failure, however deterministic the path that reached it."""
    a, _provider, launched = rig()
    monkeypatch.setattr(a.risk_gate, "authorize", lambda *args, **kw: False)
    tts, session = _say(a, "open opera gx")
    assert launched == []
    assert session._last_result is None or session._last_result.local_action is False
    assert len(tts.spoke) == 1


# --- the flag itself ------------------------------------------------------------------------------

def test_the_flag_defaults_to_spoken():
    """Every other construction site keeps the old behaviour without being touched."""
    r = AgentResult(Task(goal="x"), Status.COMPLETED, "hello", 1)
    assert r.local_action is False


def test_a_status_phrase_is_never_silenced(rig):
    """Phrases like "That task needs your approval" exist because the owner is needed: always spoken."""
    a, _provider, _launched = rig()
    tts, _session = _say(a, "open opera gx")
    assert tts.spoke == []                       # baseline: the launch was silent
    # A result that both carries a status needing the owner AND claims to be a local action must still speak.
    tts2, cap = _TTS(), _Capture()
    session = VoiceSession(a, a.kill_switch, capture=cap, stt=_STT("anything"), tts=tts2, min_speech_ms=0.0)
    paused = AgentResult(Task(goal="x", status=Status.AWAITING_CONFIRMATION), Status.AWAITING_CONFIRMATION,
                         "ignored", 1, local_action=True)
    a.run = lambda _goal: paused
    session.on_ptt_press(source="wake")
    session.on_ptt_release()
    session.notify_speech_finished()
    assert len(tts2.spoke) == 1


def test_the_confirmation_can_be_restored_by_configuration(rig):
    a, _provider, launched = rig()
    tts, _session = _say(a, "open opera gx", speak_successful_actions=True)
    assert len(launched) == 1 and tts.spoke == ["Opening Opera GX Browser."]


def test_the_shipped_default_is_silent_success():
    assert Config.load().get("voice.speak_successful_actions", None) is False


def test_suppressing_speech_executes_nothing_extra(rig):
    """No action may happen merely because its confirmation was suppressed."""
    a, provider, launched = rig()
    tts, _session = _say(a, "Explain ARP")
    assert launched == [] and provider.calls == 1 and len(tts.spoke) == 1

def test_a_launch_the_model_performed_is_also_silent(tmp_path, monkeypatch):
    """The fast path is not the only way an application opens.

    When the model chooses launch_app and it succeeds on the first step, the tool's own outcome line becomes the
    answer with no second model call (_deterministic_completion). That is a successful local action too.
    """
    import void.actions.apps as apps_mod
    from void.actions.apps import AppActions
    from void.actions.computer import AppCatalog
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.security.protected import EngineProtected
    from tests.helpers import FakeProvider
    from tests.test_computer import FakeBackend

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(apps_mod.shutil, "which", lambda c: None)
    launched: list = []

    cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(home / "ws")]},
                  "fast_path": {"enabled": False}})          # force the model to do it
    a = Assistant(config=cfg)
    backend = FakeBackend(apps=[{"name": "Opera GX Browser", "kind": "lnk",
                                 "target": _exe(tmp_path, "opera.lnk")}])
    catalog = AppCatalog(backend)
    fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    aa = AppActions(fa, catalog=catalog, launcher=lambda kind, target: launched.append((kind, target)))
    a.tools = ToolRegistry()
    a.tools.register_all(aa.tools())
    assert a._fast is None                                   # the fast path really is out of the picture

    app_id = catalog.resolve_name("opera gx").entry.app_id
    provider = FakeProvider([LLMResponse(tool_calls=[
        ToolCall(name="launch_app", arguments={"name": app_id})])])
    a.providers = ProviderRegistry({"gemini": provider}, ["gemini"])

    tts, session = _say(a, "open opera gx")
    assert len(launched) == 1                                # the model's launch really ran
    assert session._last_result.local_action is True
    assert tts.spoke == []                                   # and was not announced


def test_a_tool_failure_on_the_model_path_is_still_spoken(tmp_path, monkeypatch):
    """Same path, unsuccessful: the tool fails, the task does not complete on it, and the owner hears why."""
    import void.actions.apps as apps_mod
    from void.actions.apps import AppActions
    from void.actions.computer import AppCatalog
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.security.protected import EngineProtected
    from tests.helpers import FakeProvider
    from tests.test_computer import FakeBackend

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(apps_mod.shutil, "which", lambda c: None)

    cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(home / "ws")]},
                  "fast_path": {"enabled": False}})
    a = Assistant(config=cfg)
    catalog = AppCatalog(FakeBackend(apps=[]))
    fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    a.tools = ToolRegistry()
    a.tools.register_all(AppActions(fa, catalog=catalog).tools())
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(name="launch_app", arguments={"name": "app-nope"})]),
        LLMResponse(text="I could not open that.")])
    a.providers = ProviderRegistry({"gemini": provider}, ["gemini"])

    tts, session = _say(a, "open something")
    assert session._last_result.local_action is False
    assert len(tts.spoke) == 1
