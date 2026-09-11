"""Tests for provider selection and message/tool translation."""
import base64
import json

import pytest
import requests

from void.providers.base import (
    LLMProvider, LLMResponse, ProviderUnavailable, ToolCall, ToolSpec,
)
from void.providers.registry import ProviderRegistry
from void.providers.gemini_provider import GeminiProvider
from void.providers.local_provider import LocalProvider
from void.security import credentials, secrets


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


# --- LocalProvider HTTP behavior (mocked; no real Ollama, no network) ---
#
# LocalProvider._requests() does a live `import requests` and returns the
# real module, so monkeypatching requests.get/requests.post intercepts every
# call LocalProvider makes without touching LocalProvider itself. Fake
# response objects expose ONLY .status_code / .raise_for_status() / .json() -
# exactly what the production code actually calls, nothing invented.

class _FakeHTTPResponse:
    def __init__(self, status_code=200, json_data=None, json_exc=None,
                 raise_exc=None):
        self.status_code = status_code
        self._json_data = json_data
        self._json_exc = json_exc
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data


# --- available(): A) server reachable -----------------------------------

def test_local_available_true_when_tags_returns_200(monkeypatch):
    lp = LocalProvider()
    calls = []

    def fake_get(url, timeout=None):
        calls.append((url, timeout))
        return _FakeHTTPResponse(status_code=200)

    monkeypatch.setattr(requests, "get", fake_get)
    assert lp.available() is True
    assert calls == [(f"{lp.base_url}/api/tags", 2.0)]


# --- available(): B) connection failure ----------------------------------

def test_local_available_false_on_connection_error(monkeypatch):
    lp = LocalProvider()

    def fake_get(url, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", fake_get)
    assert lp.available() is False


# --- available(): C) non-200 response -------------------------------------

def test_local_available_false_on_non_200(monkeypatch):
    lp = LocalProvider()
    monkeypatch.setattr(
        requests, "get", lambda url, timeout=None: _FakeHTTPResponse(status_code=500))
    assert lp.available() is False


# --- available(): D) response body is never inspected ---------------------

def test_local_available_ignores_response_body_entirely(monkeypatch):
    # available() never calls resp.json() - only resp.status_code. A response
    # whose body would fail to parse (or means anything at all) must not
    # affect the result. json() is wired to raise if it is ever called, which
    # proves the body is never read rather than merely asserting the outcome.
    lp = LocalProvider()

    def _boom():
        raise AssertionError("available() must never call resp.json()")

    resp = _FakeHTTPResponse(status_code=200)
    resp.json = _boom
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: resp)
    assert lp.available() is True


# --- configured-model presence: KNOWN, ACCEPTED non-blocking gap --------

def test_local_available_does_not_verify_configured_model_is_present(monkeypatch):
    """Documents a known, non-blocking robustness gap identified in the
    provider audit: available() checks only server reachability (HTTP 200 on
    /api/tags) and never whether the CONFIGURED model (self.model) actually
    appears in the returned model list - it never reads the list at all. This
    test shows the CURRENT behavior (True, even with the model absent/
    unverifiable), not an assumed or ideal one. Not a security defect:
    generate() still fails safely (raises ProviderUnavailable, see the
    model-not-found case below) if the model turns out to be missing; this is
    an inaccurate-but-fail-safe availability signal, never a bypass.
    """
    lp = LocalProvider(model="qwen3:8b")

    def _boom():
        raise AssertionError(
            "available() would need to read the model list to answer this "
            "question - if this executes, the known gap has been fixed "
            "without updating this test.")

    resp = _FakeHTTPResponse(status_code=200)
    resp.json = _boom
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: resp)

    assert lp.available() is True   # <-- current, inaccurate-but-safe behavior


# --- generate(): A) successful text response ------------------------------

def test_local_generate_success_parses_text(monkeypatch):
    lp = LocalProvider(base_url="http://localhost:11434", model="qwen3:8b")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeHTTPResponse(status_code=200, json_data={
            "message": {"role": "assistant", "content": "The answer is 4."}})

    monkeypatch.setattr(requests, "post", fake_post)
    resp = lp.generate([{"role": "user", "content": "What is 2+2?"}])

    assert resp.text == "The answer is 4."
    assert resp.tool_calls == []
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["model"] == "qwen3:8b"
    assert captured["json"]["stream"] is False


# --- generate(): B) tool-call response ------------------------------------

def test_local_generate_parses_tool_call_through_http(monkeypatch):
    lp = LocalProvider()
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=200, json_data={
                "message": {"role": "assistant", "content": "",
                           "tool_calls": [{"function": {
                               "name": "read_file",
                               "arguments": {"path": "a.txt"}}}]}}))

    resp = lp.generate([{"role": "user", "content": "read a.txt"}])

    assert resp.has_tool_calls
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "a.txt"}


# --- generate(): C) malformed JSON response body --------------------------

def test_local_generate_malformed_json_body_is_not_wrapped(monkeypatch):
    # resp.json() is called OUTSIDE generate()'s try/except (only the POST
    # call and raise_for_status() are guarded), so a body that fails to parse
    # propagates as the RAW underlying exception, NOT ProviderUnavailable.
    # This asserts the actual current behavior, not an invented expectation.
    lp = LocalProvider()
    broken = json.JSONDecodeError("Expecting value", "not json", 0)
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=200, json_exc=broken))

    with pytest.raises(json.JSONDecodeError):
        lp.generate([{"role": "user", "content": "hi"}])


# --- generate(): D) HTTP non-200 ------------------------------------------

def test_local_generate_http_error_raises_provider_unavailable(monkeypatch):
    lp = LocalProvider()
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=500,
            raise_exc=requests.exceptions.HTTPError("500 Server Error")))

    with pytest.raises(ProviderUnavailable) as exc_info:
        lp.generate([{"role": "user", "content": "hi"}])
    assert "Local model request failed" in str(exc_info.value)


# --- generate(): E) connection failure -------------------------------------

def test_local_generate_connection_error_raises_provider_unavailable(monkeypatch):
    lp = LocalProvider()

    def fake_post(url, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(ProviderUnavailable) as exc_info:
        lp.generate([{"role": "user", "content": "hi"}])
    assert "Local model request failed" in str(exc_info.value)


# --- generate(): F) model-not-found / 404 ----------------------------------

def test_local_generate_model_not_found_raises_provider_unavailable(monkeypatch):
    # Same code path as D (raise_for_status), exercised with the realistic
    # "model not pulled" status Ollama returns for an unknown model name.
    lp = LocalProvider(model="qwen3:8b")
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=404,
            raise_exc=requests.exceptions.HTTPError("404 model not found")))

    with pytest.raises(ProviderUnavailable) as exc_info:
        lp.generate([{"role": "user", "content": "hi"}])
    assert "Local model request failed" in str(exc_info.value)


# --- security boundary: generate() is a pure provider layer --------------

def test_local_generate_returns_data_only_never_executes_a_tool(monkeypatch):
    """generate() must return an LLMResponse (data) and never itself invoke a
    tool. LocalProvider holds no reference to a ToolRegistry/Agent/RiskGate/
    KillSwitch, so there is structurally nothing for it to execute; a "tool
    call" in the response is nothing more than an inert ToolCall dataclass
    the CALLER (Agent) later decides whether to authorize and run."""
    lp = LocalProvider()
    assert not any(hasattr(lp, attr) for attr in
                   ("tools", "_tools", "registry", "_registry",
                    "risk_gate", "_risk_gate", "kill_switch", "_kill_switch",
                    "agent", "_agent", "execute"))

    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=200, json_data={
                "message": {"content": "", "tool_calls": [{"function": {
                    "name": "delete_file",
                    "arguments": {"path": "C:\\danger.txt"}}}]}}))

    resp = lp.generate([{"role": "user", "content": "delete danger.txt"}])

    assert isinstance(resp, LLMResponse)
    assert isinstance(resp.tool_calls[0], ToolCall)
    assert resp.tool_calls[0].name == "delete_file"
    # The proposal is inert data only: generate() never touches a filesystem,
    # a ToolRegistry, RiskGate, or KillSwitch - nothing was, or could be, run.


# --- Qwen3 thinking output: current parser behavior (robustness only) ----

def test_local_generate_ignores_a_separate_thinking_field(monkeypatch):
    """Ollama MAY return a separate message.thinking field alongside a clean
    message.content for thinking models. _parse() reads only content/
    tool_calls, so a separate thinking field is correctly ignored - it never
    leaks into LLMResponse.text."""
    lp = LocalProvider(model="qwen3:8b")
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=200, json_data={
                "message": {"content": "The answer is 4.",
                           "thinking": "Let me reason step by step..."}}))

    resp = lp.generate([{"role": "user", "content": "What is 2+2?"}])

    assert resp.text == "The answer is 4."
    assert "reason step by step" not in (resp.text or "")


def test_local_generate_does_not_strip_inline_think_tags(monkeypatch):
    """Documents a KNOWN, NON-SECURITY robustness/presentation gap: if a
    template inlines reasoning into message.content as <think>...</think>
    instead of using a separate field, _parse() does not strip it - the raw
    tags flow through verbatim into LLMResponse.text. This can never become a
    tool call: tool_calls is a structurally separate field Ollama populates
    itself, never derived by scanning content text, so this is
    presentation-only, never a security issue (see the isolated assertion
    below)."""
    lp = LocalProvider(model="qwen3:8b")
    raw_content = "<think>reasoning about 2+2</think>The answer is 4."
    monkeypatch.setattr(
        requests, "post",
        lambda url, json=None, timeout=None: _FakeHTTPResponse(
            status_code=200, json_data={"message": {"content": raw_content}}))

    resp = lp.generate([{"role": "user", "content": "What is 2+2?"}])

    assert resp.text == raw_content        # unstripped - current behavior
    assert resp.tool_calls == []           # never misinterpreted as a tool call


# --- Gemini credential rotation (no network, no real keys) -------------
#
# The SDK/client boundary is mocked. Key VALUES are obvious fakes and must
# never appear in exceptions/reprs/task state.

PRIMARY = secrets.GEMINI_API_KEY
K1, K2, K3 = "FAKE_KEY_ONE", "FAKE_KEY_TWO", "FAKE_KEY_THREE"


class _FakeAPIError(Exception):
    """Duck-types google.genai.errors.APIError (code/status/message/details)."""
    def __init__(self, code=None, status=None, message="", details=None):
        super().__init__(message or status or str(code))
        self.code = code
        self.status = status
        self.message = message
        self.details = details


def _err_429(details=None):
    return _FakeAPIError(code=429, status="RESOURCE_EXHAUSTED",
                         message="quota exceeded", details=details)


def _err_auth():
    return _FakeAPIError(code=403, status="PERMISSION_DENIED",
                         message="invalid credential")


def _err_other():
    return _FakeAPIError(code=400, status="INVALID_ARGUMENT", message="bad request")


def _ok():
    return _FakeResponse([_FakePart(text="ok")])


class _FakeModels:
    def __init__(self, behavior):
        self._behavior = behavior

    def generate_content(self, model, contents, config):
        return self._behavior()


class _FakeClient:
    def __init__(self, behavior):
        self.models = _FakeModels(behavior)


class _FakeGenai:
    """Stands in for the google.genai module; records the keys it built."""
    def __init__(self, behavior_by_key):
        self._behavior_by_key = behavior_by_key
        self.built_keys: list[str] = []

    def Client(self, api_key):
        self.built_keys.append(api_key)
        return _FakeClient(self._behavior_by_key[api_key])


def _fake_get_secret(values, names):
    data = dict(values)
    if len(names) > 1:
        data[credentials.MANIFEST_KEY] = json.dumps(names)
    return lambda k: data.get(k)


def _build(names, values, behavior_by_key):
    """A GeminiProvider whose SDK boundary is a fake; real types for translation."""
    from google.genai import types as real_types
    pool = credentials.CredentialPool(get_secret=_fake_get_secret(values, names))
    provider = GeminiProvider(credential_pool=pool)
    fake_genai = _FakeGenai(behavior_by_key)
    provider._sdk = lambda: (fake_genai, real_types)
    return provider, pool, fake_genai


_MSG = [{"role": "user", "content": "hi"}]


def _never():
    raise AssertionError("this credential should not have been used")


# A. single credential success ------------------------------------------

def test_rotation_single_credential_success():
    provider, pool, fg = _build([PRIMARY], {PRIMARY: K1}, {K1: _ok})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K1]


# B. multiple credentials, first succeeds -------------------------------

def test_rotation_first_of_many_succeeds():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: _ok, K2: _never})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K1]  # second credential never used


# C. 429 rotation -------------------------------------------------------

def test_rotation_on_429_moves_to_next_and_succeeds():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: lambda: (_ for _ in ()).throw(_err_429()), K2: _ok})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K1, K2]              # rebuilt for the next cred
    assert len(fg.built_keys) == len(set(fg.built_keys))  # no key reused
    primary = pool.credentials()[0]
    assert primary.name == PRIMARY
    assert primary.cooldown_until is not None      # cooled after 429


# D. multiple consecutive failures then success -------------------------

def test_rotation_two_429s_then_success_in_order():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02", "gemini_03"],
        {PRIMARY: K1, "gemini_02": K2, "gemini_03": K3},
        {K1: lambda: (_ for _ in ()).throw(_err_429()),
         K2: lambda: (_ for _ in ()).throw(_err_429()),
         K3: _ok})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K1, K2, K3]           # ordered rotation


# E. all credentials exhausted ------------------------------------------

def test_rotation_all_429_raises_provider_unavailable():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02", "gemini_03"],
        {PRIMARY: K1, "gemini_02": K2, "gemini_03": K3},
        {K1: lambda: (_ for _ in ()).throw(_err_429()),
         K2: lambda: (_ for _ in ()).throw(_err_429()),
         K3: lambda: (_ for _ in ()).throw(_err_429())})
    with pytest.raises(ProviderUnavailable) as exc:
        provider.generate(_MSG)
    assert fg.built_keys == [K1, K2, K3]           # each tried exactly once
    for k in (K1, K2, K3):                         # J: no secret in the error
        assert k not in str(exc.value)


# F. credential missing value -------------------------------------------

def test_rotation_skips_missing_value():
    # Primary has NO stored value; second one works.
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {"gemini_02": K2}, {K2: _ok})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K2]                   # no client built for primary
    assert pool.credentials()[0].cooldown_until is not None


# G. authentication failure ---------------------------------------------

def test_rotation_on_auth_error_does_not_retry_same_credential():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: lambda: (_ for _ in ()).throw(_err_auth()), K2: _ok})
    resp = provider.generate(_MSG)
    assert resp.text == "ok"
    assert fg.built_keys == [K1, K2]               # primary tried once, not hammered


def test_rotation_all_auth_raises_provider_unavailable():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: lambda: (_ for _ in ()).throw(_err_auth()),
         K2: lambda: (_ for _ in ()).throw(_err_auth())})
    with pytest.raises(ProviderUnavailable):
        provider.generate(_MSG)
    assert fg.built_keys == [K1, K2]               # each once, no repeats


# H. non-429 provider error is re-raised (existing behavior preserved) --

def test_non_quota_error_is_reraised_not_rotated():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: lambda: (_ for _ in ()).throw(_err_other()), K2: _never})
    with pytest.raises(_FakeAPIError):
        provider.generate(_MSG)
    assert fg.built_keys == [K1]                   # not rotated
    assert pool.credentials()[0].cooldown_until is None  # not cooled


# I. agent retry interaction --------------------------------------------

def test_agent_does_not_retry_when_provider_signals_exhaustion(tmp_path):
    from void.core.agent import Agent
    from void.actions.registry import ToolRegistry
    from void.core.kill_switch import KillSwitch
    from void.core.task import Status, TaskStore
    from void.security.risk import RiskGate

    calls = {"n": 0}

    class _Exhausted(LLMProvider):
        name = "gemini"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            calls["n"] += 1
            raise ProviderUnavailable("All Gemini credentials are exhausted")

    agent = Agent(_Exhausted(), ToolRegistry(),
                  RiskGate(confirm_at_or_above="high"), KillSwitch(),
                  TaskStore(tmp_path / "t.sqlite"), max_retries=2)
    result = agent.run("hi")
    assert result.status == Status.FAILED
    assert calls["n"] == 1                          # NOT retried on exhaustion


# J & K. non-disclosure + client recreation -----------------------------

def test_rotation_keys_absent_from_reprs_and_errors():
    provider, pool, fg = _build(
        [PRIMARY, "gemini_02"], {PRIMARY: K1, "gemini_02": K2},
        {K1: lambda: (_ for _ in ()).throw(_err_429()),
         K2: lambda: (_ for _ in ()).throw(_err_429())})
    with pytest.raises(ProviderUnavailable) as exc:
        provider.generate(_MSG)
    blobs = [str(exc.value), repr(pool), repr(pool.credentials())]
    for blob in blobs:
        assert K1 not in blob and K2 not in blob


def test_classify_error_uses_structured_fields():
    from void.providers.gemini_provider import _classify_error
    assert _classify_error(_err_429()) == "quota"
    assert _classify_error(_err_auth()) == "auth"
    assert _classify_error(_err_other()) == "other"
    # message fallback still works when structured fields are absent
    assert _classify_error(Exception("boom RESOURCE_EXHAUSTED")) == "quota"


def test_cooldown_honors_longer_retry_delay_only():
    from datetime import datetime, timezone
    from void.providers.gemini_provider import _cooldown_until, _QUOTA_COOLDOWN_S
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # A short retry delay does not shorten the conservative floor.
    short = _err_429(details=[{"retryDelay": "9s"}])
    assert (_cooldown_until(now, _QUOTA_COOLDOWN_S, short) - now).total_seconds() \
        == _QUOTA_COOLDOWN_S
    # A longer explicit delay is honored.
    long = _err_429(details=[{"retryDelay": "7200s"}])
    assert (_cooldown_until(now, _QUOTA_COOLDOWN_S, long) - now).total_seconds() \
        == 7200
