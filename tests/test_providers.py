"""Tests for provider selection and message/tool translation."""
import base64
import json

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


# --- google-genai import / init ----------------------------------------

def test_google_genai_imports():
    from google import genai
    from google.genai import types
    assert genai.Client is not None
    assert types.Part is not None
    assert types.FunctionCall is not None
    assert types.GenerateContentConfig is not None


def test_gemini_provider_initializes_with_google_genai():
    g = GeminiProvider(model="gemini-3.6-flash")
    assert g.name == "gemini"
    assert g.model == "gemini-3.6-flash"
    genai, types = g._sdk()
    assert genai.__name__ == "google.genai"
    assert types.Part is not None
    # No client until a key is loaded — translation does not need the API.
    assert g._client is None


# --- Gemini translation (no network) -----------------------------------

def test_gemini_to_contents_extracts_system():
    from google.genai import types
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
    assert isinstance(contents[0], types.Content)
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "hi"
    assert contents[1].role == "model"
    assert contents[1].parts[0].function_call.name == "search_files"
    assert contents[1].parts[0].function_call.args == {"query": "x"}
    fr = contents[2].parts[0].function_response
    assert fr.name == "search_files"
    assert fr.response == {"result": "found 1"}


def test_gemini_to_tools():
    g = GeminiProvider()
    schema = {"type": "object", "properties": {"q": {"type": "string"}},
              "required": ["q"]}
    specs = [ToolSpec("search_files", "desc", schema)]
    out = g._to_tools(specs)
    decl = out[0].function_declarations[0]
    assert decl.name == "search_files"
    assert decl.description == "desc"
    assert decl.parameters_json_schema == schema
    assert g._to_tools(None) is None


def test_automatic_function_calling_is_disabled():
    g = GeminiProvider()
    specs = [ToolSpec("t", "desc", {"type": "object", "properties": {}})]
    cfg = g._generate_config("sys", specs)
    assert cfg.automatic_function_calling is not None
    assert cfg.automatic_function_calling.disable is True
    # Declarations only — no Python callables for the SDK to execute.
    assert cfg.tools[0].function_declarations[0].name == "t"
    assert cfg.system_instruction == "sys"


# Duck-typed doubles matching google.genai attribute shape (not live responses).

class _FakeFuncCall:
    def __init__(self, name, args, id=None, thought_signature=None):
        self.name = name
        self.args = args
        self.id = id
        self.thought_signature = thought_signature


class _FakePart:
    def __init__(self, text=None, function_call=None, thought_signature=None,
                 thought=False):
        self.text = text
        self.function_call = function_call
        self.thought_signature = thought_signature
        self.thought = thought


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
        _FakePart(function_call=_FakeFuncCall("search_files", {"query": "cyber"}))
    ])
    parsed = g._parse(resp)
    assert parsed.has_tool_calls
    assert parsed.tool_calls[0].name == "search_files"
    assert parsed.tool_calls[0].arguments == {"query": "cyber"}
    assert parsed.raw is resp


def test_gemini_parse_text():
    g = GeminiProvider()
    resp = _FakeResponse([_FakePart(text="all done")])
    parsed = g._parse(resp)
    assert not parsed.has_tool_calls
    assert parsed.text == "all done"


def test_gemini_parse_preserves_function_call_id():
    g = GeminiProvider()
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall(
            "search_files", {"query": "x"}, id="fc-1"))
    ])
    parsed = g._parse(resp)
    assert parsed.tool_calls[0].id == "fc-1"


def test_function_call_id_roundtrip_onto_function_response():
    g = GeminiProvider()
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall(
            "search_files", {"query": "x"}, id="fc-99"))
    ])
    tc = g._parse(resp).tool_calls[0]
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"name": tc.name, "arguments": tc.arguments,
                         "id": tc.id, "signature": tc.signature}]},
        {"role": "tool", "name": "search_files", "content": "found 1"},
    ]
    _system, contents = g._to_contents(messages)
    assert contents[0].parts[0].function_call.id == "fc-99"
    assert contents[1].parts[0].function_response.id == "fc-99"


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


# --- thought_signature preservation (shape-level; not live Gemini) -----

def test_gemini_parse_captures_thought_signature():
    g = GeminiProvider()
    payload = b"\x01\x02\x03shape-only"
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files", {"query": "x"}),
                  thought_signature=payload),
    ])
    parsed = g._parse(resp)
    assert parsed.tool_calls[0].signature == base64.b64encode(payload).decode("ascii")


def test_thought_signature_survives_response_to_request_roundtrip():
    """Parse -> agent message -> JSON checkpoint -> typed model Part."""
    g = GeminiProvider()
    payload = b"opaque-bytes-\x00\xff\x10"

    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files",
                                              {"query": "void_test_note"}),
                  thought_signature=payload),
    ])
    tc = g._parse(resp).tool_calls[0]

    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"name": tc.name, "arguments": tc.arguments,
                           "id": tc.id, "signature": tc.signature}]}
    msg_roundtripped = json.loads(json.dumps(msg))

    _system, contents = g._to_contents([msg_roundtripped])
    part = contents[0].parts[0]
    assert part.function_call.name == "search_files"
    # Same bytes, on the original function-call Part (not a neighbour).
    assert part.thought_signature == payload
    assert len(contents[0].parts) == 1


def test_typed_part_signature_stays_on_original_part_position():
    """google.genai.types.Part shape: signature stays on that Part index."""
    from google.genai import types
    g = GeminiProvider()
    payload = b"\xaa\xbb"
    typed = types.Part(
        function_call=types.FunctionCall(name="search_files", args={"q": "1"}),
        thought_signature=payload,
    )
    parsed = g._parse(_FakeResponse([typed]))
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"name": parsed.tool_calls[0].name,
                           "arguments": parsed.tool_calls[0].arguments,
                           "id": parsed.tool_calls[0].id,
                           "signature": parsed.tool_calls[0].signature}]}
    _system, contents = g._to_contents([msg])
    part = contents[0].parts[0]
    assert isinstance(part, types.Part)
    assert part.thought_signature == payload
    assert part.function_call.name == "search_files"


def test_to_contents_without_signature_is_backward_compatible():
    g = GeminiProvider()
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"name": "t", "arguments": {}}]}  # no signature key
    _system, contents = g._to_contents([msg])
    part = contents[0].parts[0]
    assert part.function_call.name == "t"
    assert part.thought_signature is None


def test_multiple_function_calls_keep_first_signature():
    """Parallel calls: signature on the first function_call Part only."""
    g = GeminiProvider()
    first = b"\x11first"
    resp = _FakeResponse([
        _FakePart(function_call=_FakeFuncCall("search_files", {"query": "a"}),
                  thought_signature=first),
        _FakePart(function_call=_FakeFuncCall("read_file", {"path": "a.txt"})),
    ])
    parsed = g._parse(resp)
    assert parsed.tool_calls[0].signature == base64.b64encode(first).decode("ascii")
    assert parsed.tool_calls[1].signature is None

    msg = {"role": "assistant", "content": None,
           "tool_calls": [
               {"name": parsed.tool_calls[0].name,
                "arguments": parsed.tool_calls[0].arguments,
                "id": parsed.tool_calls[0].id,
                "signature": parsed.tool_calls[0].signature},
               {"name": parsed.tool_calls[1].name,
                "arguments": parsed.tool_calls[1].arguments,
                "id": parsed.tool_calls[1].id,
                "signature": parsed.tool_calls[1].signature},
           ]}
    _system, contents = g._to_contents([msg])
    parts = contents[0].parts
    assert len(parts) == 2
    assert parts[0].function_call.name == "search_files"
    assert parts[0].thought_signature == first
    assert parts[1].function_call.name == "read_file"
    assert parts[1].thought_signature is None


def test_text_and_function_calls_stay_separate_parts():
    g = GeminiProvider()
    payload = b"\x22sig"
    msg = {
        "role": "assistant",
        "content": "looking it up",
        "tool_calls": [{
            "name": "search_files",
            "arguments": {"query": "x"},
            "signature": base64.b64encode(payload).decode("ascii"),
        }],
    }
    _system, contents = g._to_contents([msg])
    parts = contents[0].parts
    assert parts[0].text == "looking it up"
    assert parts[0].function_call is None
    assert parts[0].thought_signature is None
    assert parts[1].function_call.name == "search_files"
    assert parts[1].thought_signature == payload


def test_local_to_messages_roles():
    lp = LocalProvider()
    msgs = lp._to_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "tool", "name": "read_file", "content": "r"},
    ])
    assert msgs[0]["role"] == "system"
    assert msgs[2] == {"role": "tool", "content": "r", "name": "read_file"}
