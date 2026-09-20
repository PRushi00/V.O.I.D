"""T0.7: one ``interaction_id`` correlates activation -> endpoint -> STT -> route -> LLM ->
tool -> completion -> response -> speech, with NO content in the telemetry.

Uses a REAL Assistant + VoiceSession (fakes only for the mic, STT, TTS and LLM)."""
import json
from pathlib import Path

import pytest

from tests.helpers import FakeProvider, tool_call
from void import perf
from void.actions.base import Tool, ToolResult
from void.app import Assistant
from void.config import Config
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.risk import RiskLevel
from void.voice.adapters import STT, TTS
from void.voice.session import VoiceSession

GOAL_CANARY = "CANARY-GOAL open my secret resume"
ANSWER_CANARY = "CANARY-ANSWER the resume has 3 pages"


class _RecTTS(TTS):
    def __init__(self):
        self.spoke = []

    @property
    def is_speaking(self):
        return False

    def speak(self, text):
        self.spoke.append(text)

    def stop(self):
        pass


class _NullCapture:
    is_open = False

    def open(self): pass
    def stop(self): return []
    def close(self): pass


class _CanarySTT(STT):
    def transcribe(self, audio):
        return GOAL_CANARY


@pytest.fixture
def assistant(tmp_path):
    cfg = Config({"app": {"state_dir": str(tmp_path / "state")}, "security": {"allowed_roots": []}})
    a = Assistant(config=cfg)
    a.tools.register(Tool(name="echo", description="d", parameters={"type": "object"},
                          handler=lambda **kw: ToolResult.success("SECRET-TOOL-OUTPUT"), risk=RiskLevel.LOW))
    a.providers = ProviderRegistry(
        {"fake": FakeProvider([LLMResponse(tool_calls=[tool_call("echo", secret="CANARY-ARG")]),
                               LLMResponse(text=ANSWER_CANARY)])}, ["fake"])
    return a


@pytest.fixture
def sink(tmp_path):
    path = perf.configure(tmp_path / "perf")
    yield path
    perf.shutdown()


def _events(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def test_voice_command_produces_one_ordered_content_free_chain(assistant, sink):
    tts = _RecTTS()
    session = VoiceSession(assistant, assistant.kill_switch, capture=_NullCapture(),
                           stt=_CanarySTT(), tts=tts)
    session.on_ptt_press(source="wake")
    session.on_ptt_release()
    session.notify_speech_finished()
    assert tts.spoke == [ANSWER_CANARY]                          # the command really ran end to end

    events = _events(sink)
    names = [e["event"] for e in events]
    assert names == ["activation", "endpoint", "stt", "route", "llm", "tool", "llm",
                     "complete", "respond", "speak", "speak"], names
    ids = {e["interaction_id"] for e in events}
    assert len(ids) == 1, f"chain was split across interactions: {ids}"
    stamps = [e["ts"] for e in events]
    assert stamps == sorted(stamps)

    by = {e["event"]: e for e in events}
    assert by["activation"]["source"] == "wake"
    assert by["endpoint"]["reason"] == "ptt_release"
    assert by["stt"]["empty"] is False and by["stt"]["backend"] == "_CanarySTT"
    assert by["route"]["provider"] == "fake"
    assert by["tool"]["name"] == "echo" and by["tool"]["risk"] == "LOW" and by["tool"]["ok"] is True
    assert by["complete"]["status"] == "completed" and by["complete"]["steps"] >= 1
    assert by["respond"]["kind"] == "llm_text"

    blob = Path(sink).read_text(encoding="utf-8")
    for canary in (GOAL_CANARY, ANSWER_CANARY, "CANARY", "SECRET-TOOL-OUTPUT", "secret resume"):
        assert canary not in blob, f"content leaked into telemetry: {canary}"


def test_cli_run_gets_its_own_interaction_with_an_activation(assistant, sink):
    assistant.run(GOAL_CANARY)
    events = _events(sink)
    names = [e["event"] for e in events]
    assert names[0] == "activation" and events[0]["source"] == "cli"
    assert names[-1] == "complete"
    assert len({e["interaction_id"] for e in events}) == 1
    assert GOAL_CANARY not in Path(sink).read_text(encoding="utf-8")


def test_two_commands_are_two_distinct_interactions(assistant, sink):
    assistant.run("first")
    assistant.providers = ProviderRegistry({"fake": FakeProvider([LLMResponse(text="ok")])}, ["fake"])
    assistant.run("second")
    ids = [e["interaction_id"] for e in _events(sink) if e["event"] == "activation"]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_voice_command_joins_the_session_id_instead_of_starting_a_second_activation(assistant, sink):
    session = VoiceSession(assistant, assistant.kill_switch, capture=_NullCapture(),
                           stt=_CanarySTT(), tts=_RecTTS())
    session.on_ptt_press()                                       # plain PTT
    session.on_ptt_release()
    assert [e["event"] for e in _events(sink)].count("activation") == 1


def test_a_failing_provider_is_recorded_without_the_error_text(assistant, sink, monkeypatch):
    class _Boom(LLMProvider):
        name = "gemini"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            raise ConnectionError("CANARY-ERROR-TEXT with secrets")

    monkeypatch.setattr("void.core.agent.time.sleep", lambda s: None)
    assistant.providers = ProviderRegistry({"gemini": _Boom()}, ["gemini"])
    result = assistant.run("x")
    assert result.status == "failed"
    events = _events(sink)
    llm = [e for e in events if e["event"] == "llm"]
    assert len(llm) == 3 and all(e["ok"] is False and e["error_class"] == "ConnectionError" for e in llm)
    assert events[-1]["event"] == "complete" and events[-1]["status"] == "failed"
    assert "CANARY-ERROR-TEXT" not in Path(sink).read_text(encoding="utf-8")


def test_telemetry_is_inert_when_not_configured(assistant):
    perf.shutdown()
    result = assistant.run("x")                                  # must behave exactly as before
    assert result.status == "completed"
