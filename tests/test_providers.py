"""Tests for provider selection and message/tool translation."""
import pytest

from void.providers.base import (
    LLMProvider, LLMResponse, ProviderUnavailable, ToolSpec,
)
from void.providers.registry import ProviderRegistry
from void.providers.gemini_provider import GeminiProvider
from void.providers.local_provider import LocalProvider


class _Stub(LLMProvider):
    def __init__(self, name, avail):
        self.name = name
        self._avail = avail

    def available(self):
        return self._avail

    def generate(self, messages, tools=None):
        return LLMResponse(text=self.name)


def test_registry_selects_first_available():
    reg = ProviderRegistry(
        {"gemini": _Stub("gemini", False), "local": _Stub("local", True)},
        order=["gemini", "local"],
    )
    assert reg.select().name == "local"


def test_registry_prefers_primary_when_available():
    reg = ProviderRegistry(
        {"gemini": _Stub("gemini", True), "local": _Stub("local", True)},
        order=["gemini", "local"],
    )
    assert reg.select().name == "gemini"


def test_registry_raises_when_none_available():
    reg = ProviderRegistry({"gemini": _Stub("gemini", False)}, order=["gemini"])
    with pytest.raises(ProviderUnavailable):
        reg.select()


# --- Gemini translation (no network) -----------------------------------

def test_gemini_to_contents_extracts_system():
    g = GeminiProvider()
    messages = [
        {"role": "system", "content": "you are void"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"name": "search_files", "arguments": {"query": "x"}}]},
        {"role": "tool", "name": "search_files", "content": "found 1"},
    ]
    system, contents = g._to_contents(messages)
    assert system == "you are void"
    assert contents[0] == {"role": "user", "parts": [{"text": "hi"}]}
    # assistant tool call -> model role with function_call part
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["function_call"]["name"] == "search_files"
    # tool result -> function_response part
    assert contents[2]["parts"][0]["function_response"]["name"] == "search_files"


def test_gemini_to_tools():
    g = GeminiProvider()
    specs = [ToolSpec("t", "desc", {"type": "object", "properties": {}})]
    out = g._to_tools(specs)
    assert out[0]["function_declarations"][0]["name"] == "t"
    assert g._to_tools(None) is None


class _FakeArgs(dict):
    pass


class _FakeFuncCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakePart:
    def __init__(self, text=None, function_call=None, thought_signature=None):
        self.text = text
        self.function_call = function_call
        self.thought_signature = thought_signature


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, parts):
        self.candidates = [_FakeCandidate(parts)]


def test_gemini_parse_tool_call():
    g = GeminiProvider()
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files",
                                              _FakeArgs(query="cyber")))
    ])
    parsed = g._parse(resp)
    assert parsed.has_tool_calls
    assert parsed.tool_calls[0].name == "search_files"
    assert parsed.tool_calls[0].arguments == {"query": "cyber"}


def test_gemini_parse_text():
    g = GeminiProvider()
    resp = _FakeResponse([_FakePart(text="all done")])
    parsed = g._parse(resp)
    assert not parsed.has_tool_calls
    assert parsed.text == "all done"


# --- Local (Ollama) translation ----------------------------------------

def test_local_parse_tool_calls():
    lp = LocalProvider()
    payload = {"message": {"role": "assistant", "content": "",
                           "tool_calls": [
                               {"function": {"name": "read_file",
                                             "arguments": {"path": "a.txt"}}}]}}
    parsed = lp._parse(payload)
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.tool_calls[0].arguments == {"path": "a.txt"}


def test_local_parse_string_arguments():
    lp = LocalProvider()
    payload = {"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": '{"a": 1}'}}]}}
    parsed = lp._parse(payload)
    assert parsed.tool_calls[0].arguments == {"a": 1}


# --- Gemini thought_signature preservation (3.x tool-calling) ----------

def test_gemini_parse_captures_thought_signature():
    import base64
    g = GeminiProvider()
    sig = b"\x01\x02\x03thought-sig"
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files",
                                              _FakeArgs(query="x")),
                  thought_signature=sig),
    ])
    parsed = g._parse(resp)
    assert parsed.tool_calls[0].signature == base64.b64encode(sig).decode("ascii")


def test_thought_signature_survives_response_to_request_roundtrip():
    """Gemini response -> neutral message -> JSON checkpoint -> Gemini request."""
    import base64
    import json
    g = GeminiProvider()
    sig = b"gemini-3.x-signature-\x00\xff\x10"

    # 1) Gemini response -> neutral ToolCall (as _parse would produce).
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files",
                                              _FakeArgs(query="void_test_note")),
                  thought_signature=sig),
    ])
    tc = g._parse(resp).tool_calls[0]

    # 2) Neutral message exactly as the agent stores it on the task.
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"name": tc.name, "arguments": tc.arguments,
                           "id": tc.id, "signature": tc.signature}]}

    # 3) Checkpoint serialization (task.py persists messages via json).
    msg_roundtripped = json.loads(json.dumps(msg))

    # 4) Rebuild Gemini request contents.
    _system, contents = g._to_contents([msg_roundtripped])
    part = contents[0]["parts"][0]
    assert part["function_call"]["name"] == "search_files"
    # The exact signature bytes are re-attached for the next turn.
    assert part["thought_signature"] == sig


def test_to_contents_without_signature_is_backward_compatible():
    g = GeminiProvider()
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"name": "t", "arguments": {}}]}  # no signature key
    _system, contents = g._to_contents([msg])
    part = contents[0]["parts"][0]
    assert part["function_call"]["name"] == "t"
    assert "thought_signature" not in part


def test_local_to_messages_roles():
    lp = LocalProvider()
    msgs = lp._to_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "tool", "name": "read_file", "content": "r"},
    ])
    assert msgs[0]["role"] == "system"
    assert msgs[2] == {"role": "tool", "content": "r", "name": "read_file"}
