"""Provider hygiene: a configurable Gemini deadline, validated thinking, classified failures (no retry loops, rotation
and fallback preserved), and a correctly shaped Ollama request (think / num_ctx / keep_alive). No network, no model
download, no GPU: every SDK / HTTP boundary is a fake."""
import json

import pytest
import requests

from void.config import Config
from void.providers import gemini_provider as gp
from void.providers.base import ProviderUnavailable
from void.providers.gemini_provider import (
    DEFAULT_TIMEOUT_S,
    GeminiProvider,
    classify_failure,
    normalise_timeout,
    resolve_thinking,
)
from void.providers.local_provider import LocalProvider
from void.providers.registry import ProviderRegistry
from void.security import credentials, secrets

from tests.test_providers import _FakeAPIError, _FakeResponse, _FakePart, _fake_get_secret

K1, K2 = "FAKE_KEY_ONE", "FAKE_KEY_TWO"
PRIMARY = secrets.GEMINI_API_KEY
MSG = [{"role": "user", "content": "hi"}]


# --- Gemini: a recording fake SDK -----------------------------------------------------------------

class _Models:
    def __init__(self, behavior, log):
        self._behavior, self._log = behavior, log

    def generate_content(self, model, contents, config):
        self._log.append({"model": model, "config": config})
        return self._behavior()


class _Client:
    def __init__(self, behavior, log):
        self.models = _Models(behavior, log)


class _Genai:
    def __init__(self, behaviors):
        self._b = behaviors
        self.built: list[tuple[str, object]] = []
        self.log: list[dict] = []

    def Client(self, api_key, http_options=None):
        self.built.append((api_key, http_options))
        return _Client(self._b[api_key], self.log)


def _ok():
    return _FakeResponse([_FakePart(text="ok")])


def _raise(exc):
    def f():
        raise exc
    return f


def _gem(behaviors, names=None, **kw):
    from google.genai import types as real_types
    names = names or [PRIMARY]
    values = dict(zip(names, [K1, K2]))
    pool = credentials.CredentialPool(get_secret=_fake_get_secret(values, names))
    provider = GeminiProvider(credential_pool=pool, **kw)
    genai = _Genai(behaviors)
    provider._sdk = lambda: (genai, real_types)
    return provider, pool, genai


# --- request deadline ------------------------------------------------------------------------------

def test_default_deadline_is_bounded_and_is_sent_to_the_sdk_in_milliseconds():
    p, _pool, g = _gem({K1: _ok})
    assert p.timeout_s == DEFAULT_TIMEOUT_S == 30.0
    p.generate(MSG)
    assert g.built[0][1].timeout == 30000


def test_configured_deadline_reaches_the_client():
    p, _pool, g = _gem({K1: _ok}, timeout_s=7.5)
    p.generate(MSG)
    assert g.built[0][1].timeout == 7500


@pytest.mark.parametrize("raw, expected", [(0, 1.0), (-5, 1.0), (10**6, 300.0), ("12", 12.0), ("abc", 30.0),
                                           (None, 30.0), (float("nan"), 30.0), (float("inf"), 300.0), (True, 1.0)])
def test_deadline_is_clamped_or_defaulted_never_raises(raw, expected):
    assert normalise_timeout(raw) == expected


def test_a_timeout_fails_fast_as_provider_unavailable_and_is_not_retried_or_rotated():
    class ReadTimeout(Exception):          # stands in for httpx.ReadTimeout (matched by class name)
        pass

    p, pool, g = _gem({K1: _raise(ReadTimeout("read timed out")), K2: _ok}, names=[PRIMARY, "gemini_02"],
                      timeout_s=5)
    with pytest.raises(ProviderUnavailable) as e:
        p.generate(MSG)
    assert "5s" in str(e.value)
    assert [k for k, _ in g.built] == [K1]                       # not re-sent, not rotated to the next key
    assert pool.credentials()[0].cooldown_until is None          # a slow answer is not the key's fault


def test_a_deadline_exceeded_status_is_a_timeout():
    p, *_ = _gem({K1: _raise(_FakeAPIError(code=504, status="DEADLINE_EXCEEDED", message="x"))})
    with pytest.raises(ProviderUnavailable):
        p.generate(MSG)


# --- model ------------------------------------------------------------------------------------------

def test_the_configured_model_is_the_one_requested():
    p, _pool, g = _gem({K1: _ok}, model="gemini-3.6-flash")
    p.generate(MSG)
    assert g.log[0]["model"] == "gemini-3.6-flash"


def test_a_missing_model_404_fails_fast_and_names_the_model_without_retry():
    p, pool, g = _gem({K1: _raise(_FakeAPIError(code=404, status="NOT_FOUND", message="models/x is not found"))},
                      model="gemini-nope")
    with pytest.raises(ProviderUnavailable) as e:
        p.generate(MSG)
    assert "gemini-nope" in str(e.value)
    assert len(g.log) == 1 and pool.credentials()[0].cooldown_until is None


# --- thinking: validated, never sent blindly -------------------------------------------------------

def _thinking_sent(model, value):
    p, _pool, g = _gem({K1: _ok}, model=model, thinking=value)
    p.generate(MSG)
    tc = g.log[0]["config"].thinking_config
    return p, (None if tc is None else (tc.thinking_level.value if tc.thinking_level else None, tc.thinking_budget))


@pytest.mark.parametrize("model, value, sent", [
    ("gemini-3.6-flash", "low", ("LOW", None)),
    ("gemini-3.6-flash", "HIGH", ("HIGH", None)),
    ("gemini-3.7-flash", "medium", ("MEDIUM", None)),
    ("gemini-3.1-flash-lite", "minimal", ("MINIMAL", None)),
    ("gemini-2.5-flash", 0, (None, 0)),
    ("gemini-2.5-flash", "1024", (None, 1024)),
    ("gemini-2.5-flash", -1, (None, -1)),
])
def test_a_valid_thinking_value_is_sent_in_the_form_the_model_takes(model, value, sent):
    provider, got = _thinking_sent(model, value)
    assert got == sent and provider.thinking_warning is None


@pytest.mark.parametrize("model, value", [
    ("gemini-3.6-flash", "minimal"),         # documented for the flash-lite line only: never sent blindly
    ("gemini-3.6-flash", "extreme"),
    ("gemini-3.6-flash", 1024),              # a numeric budget is not the 3.x form
    ("gemini-2.5-flash", "low"),             # a level is not the 2.5 form
    ("gemini-2.5-flash", 10**9),             # out of range
    ("gemini-2.5-flash", True),
    ("some-future-model", "low"),            # unrecognised model: nothing is assumed
    ("gemini-3.6-flash", ["low"]),
    ("gemini-3.6-flash", 1.5),
])
def test_an_invalid_or_unsupported_thinking_value_is_not_sent_and_says_why(model, value):
    provider, got = _thinking_sent(model, value)
    assert got is None                       # the request still succeeds, with the model's own default
    assert provider.thinking_warning


@pytest.mark.parametrize("value", [None, "", "  ", "default", "None", "auto", "null"])
def test_unset_thinking_sends_nothing_and_is_not_a_warning(value):
    provider, got = _thinking_sent("gemini-3.6-flash", value)
    assert got is None and provider.thinking_warning is None


def test_resolve_thinking_is_pure_and_names_no_secret():
    setting, warning = resolve_thinking("gemini-3.6-flash", "minimal")
    assert setting is None and "minimal" in warning and "low, medium, high" in warning


def test_an_invalid_thinking_level_rejected_by_the_server_is_a_permanent_error_not_a_retry_loop():
    p, _pool, g = _gem({K1: _raise(_FakeAPIError(code=400, status="INVALID_ARGUMENT", message="thinking_level"))})
    with pytest.raises(ProviderUnavailable):
        p.generate(MSG)
    assert len(g.log) == 1


# --- failure classification --------------------------------------------------------------------------

def _named(name, **attrs):
    return type(name, (Exception,), {})(attrs.get("msg", "x"))


@pytest.mark.parametrize("exc, kind", [
    (_FakeAPIError(code=429, status="RESOURCE_EXHAUSTED"), "rate_limit"),
    (_FakeAPIError(code=401, status="UNAUTHENTICATED"), "auth"),
    (_FakeAPIError(code=403, status="PERMISSION_DENIED"), "auth"),
    (_FakeAPIError(code=404, status="NOT_FOUND"), "not_found"),
    (_FakeAPIError(code=400, status="INVALID_ARGUMENT"), "invalid_request"),
    (_FakeAPIError(code=400, status="FAILED_PRECONDITION"), "invalid_request"),
    (_FakeAPIError(code=500, status="INTERNAL"), "server"),
    (_FakeAPIError(code=503, status="UNAVAILABLE"), "server"),
    (_FakeAPIError(code=504, status="DEADLINE_EXCEEDED"), "timeout"),
    (_FakeAPIError(code=408), "timeout"),
    (TimeoutError("t"), "timeout"),
    (_named("ReadTimeout"), "timeout"),
    (_named("ConnectTimeout"), "timeout"),
    (ConnectionError("down"), "network"),
    (_named("ConnectError"), "network"),
    (_named("RemoteProtocolError"), "network"),
    (ValueError("weird"), "other"),
])
def test_failures_are_classified(exc, kind):
    assert classify_failure(exc) == kind


def test_the_original_rotation_classifier_is_unchanged():
    assert gp._classify_error(_FakeAPIError(code=429)) == "quota"
    assert gp._classify_error(_FakeAPIError(code=403)) == "auth"
    assert gp._classify_error(_FakeAPIError(code=503)) == "other"


def test_a_5xx_is_reraised_unchanged_for_the_agents_bounded_retry_and_the_key_is_kept():
    err = _FakeAPIError(code=503, status="UNAVAILABLE", message="overloaded")
    p, pool, g = _gem({K1: _raise(err), K2: _ok}, names=[PRIMARY, "gemini_02"])
    with pytest.raises(_FakeAPIError):
        p.generate(MSG)
    assert len(g.log) == 1 and pool.credentials()[0].cooldown_until is None


def test_a_network_error_is_reraised_unchanged():
    p, _pool, g = _gem({K1: _raise(ConnectionError("no route"))})
    with pytest.raises(ConnectionError):
        p.generate(MSG)
    assert len(g.log) == 1


def test_429_still_rotates_to_the_next_credential_then_succeeds():
    p, pool, g = _gem({K1: _raise(_FakeAPIError(code=429, status="RESOURCE_EXHAUSTED")), K2: _ok},
                      names=[PRIMARY, "gemini_02"])
    assert p.generate(MSG).text == "ok"
    assert [k for k, _ in g.built] == [K1, K2]
    assert pool.credentials()[0].cooldown_until is not None


def test_401_403_still_rotate_and_never_retry_the_same_key():
    for code, status in ((401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")):
        p, pool, g = _gem({K1: _raise(_FakeAPIError(code=code, status=status)), K2: _ok}, names=[PRIMARY, "gemini_02"])
        assert p.generate(MSG).text == "ok"
        assert [k for k, _ in g.built] == [K1, K2]


def test_all_keys_rate_limited_is_provider_unavailable_after_one_attempt_each():
    e = _FakeAPIError(code=429, status="RESOURCE_EXHAUSTED")
    p, _pool, g = _gem({K1: _raise(e), K2: _raise(e)}, names=[PRIMARY, "gemini_02"])
    with pytest.raises(ProviderUnavailable):
        p.generate(MSG)
    assert [k for k, _ in g.built] == [K1, K2]


def test_failure_messages_never_contain_a_key_or_the_provider_error_text():
    secret_text = f"boom {K1}"
    for exc in (_FakeAPIError(code=404, status="NOT_FOUND", message=secret_text),
                _FakeAPIError(code=400, status="INVALID_ARGUMENT", message=secret_text),
                _FakeAPIError(code=504, status="DEADLINE_EXCEEDED", message=secret_text)):
        p, *_ = _gem({K1: _raise(exc)})
        with pytest.raises(ProviderUnavailable) as e:
            p.generate(MSG)
        assert K1 not in str(e.value) and "boom" not in str(e.value)


def test_a_cooled_down_gemini_makes_the_registry_fall_back_to_the_next_provider():
    """Fallback semantics are unchanged: selection skips a provider that reports it cannot serve requests."""
    p, pool, _g = _gem({K1: _raise(_FakeAPIError(code=429, status="RESOURCE_EXHAUSTED"))})
    with pytest.raises(ProviderUnavailable):
        p.generate(MSG)
    assert p.available() is False

    class _Local:
        name = "local"

        def available(self):
            return True

    assert ProviderRegistry({"gemini": p, "local": _Local()}, ["gemini", "local"]).select().name == "local"


# --- config -> providers --------------------------------------------------------------------------

def test_registry_passes_the_configuration_through():
    cfg = Config({"llm": {"gemini": {"model": "gemini-3.6-flash", "timeout_s": 12, "thinking": "low"},
                          "local": {"model": "qwen3:8b", "think": False, "num_ctx": 4096, "keep_alive": "2m",
                                    "timeout_s": 45}}})
    reg = ProviderRegistry.from_config(cfg)
    g, lo = reg.get("gemini"), reg.get("local")
    assert (g.model, g.timeout_s, g.thinking_setting) == ("gemini-3.6-flash", 12.0, ("level", "low"))
    assert (lo.model, lo.think, lo.num_ctx, lo.keep_alive, lo.timeout) == ("qwen3:8b", False, 4096, "2m", 45)


def test_registry_defaults_are_the_safe_ones():
    reg = ProviderRegistry.from_config(Config({}))
    g, lo = reg.get("gemini"), reg.get("local")
    assert g.timeout_s == DEFAULT_TIMEOUT_S and g.thinking_setting is None
    assert lo.num_ctx == 8192 and lo.keep_alive == "10m" and lo.think is None


def test_the_shipped_default_config_is_valid():
    cfg = Config.load()
    reg = ProviderRegistry.from_config(cfg)
    assert reg.get("gemini").thinking_warning is None
    assert reg.get("local").think is False and reg.get("gemini").timeout_s == 30.0


# --- Ollama --------------------------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, data=None, text="", exc=None, json_exc=None):
        self.status_code, self._data, self.text, self._exc, self._jexc = status, data, text, exc, json_exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        if self._jexc:
            raise self._jexc
        return self._data


_GOOD = {"message": {"role": "assistant", "content": "hello"}}


@pytest.fixture
def http(monkeypatch):
    """Records every request; ``queue`` holds the responses (or exceptions) to return in order."""
    class H:
        posts: list[dict] = []
        gets: list[str] = []
        queue: list = []
    h = H()
    h.posts, h.gets, h.queue = [], [], []

    def post(url, json=None, timeout=None):
        h.posts.append({"url": url, "body": json, "timeout": timeout})
        nxt = h.queue.pop(0) if h.queue else _Resp(200, _GOOD)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(url, timeout=None):
        h.gets.append(url)
        return _Resp(200, {"models": [{"name": "qwen3:8b"}]})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    return h


def _body(http):
    return http.posts[-1]["body"]


def test_ollama_request_has_think_false_explicit_context_and_keep_alive(http):
    lp = LocalProvider(model="qwen3:8b", think=False, num_ctx=8192, keep_alive="10m")
    assert lp.generate(MSG).text == "hello"
    b = _body(http)
    assert b["think"] is False and b["options"]["num_ctx"] == 8192 and b["keep_alive"] == "10m"
    assert b["model"] == "qwen3:8b" and b["stream"] is False and b["options"]["temperature"] == 0.2


def test_ollama_think_is_an_explicit_top_level_field_not_an_option(http):
    LocalProvider(think=False).generate(MSG)
    assert "think" in _body(http) and "think" not in _body(http)["options"]


@pytest.mark.parametrize("setting, present, value", [
    (True, True, True), (False, True, False), (None, False, None), ("false", True, False), ("TRUE", True, True),
    ("off", True, False), ("default", False, None), ("", False, None), ("nonsense", False, None),
])
def test_ollama_think_setting(http, setting, present, value):
    LocalProvider(think=setting).generate(MSG)
    assert ("think" in _body(http)) is present
    if present:
        assert _body(http)["think"] is value


@pytest.mark.parametrize("given, sent", [(4096, 4096), ("2048", 2048), (0, 8192), (-1, 8192), ("abc", 8192),
                                        (None, None)])
def test_ollama_context_window(http, given, sent):
    LocalProvider(num_ctx=given).generate(MSG)
    assert _body(http)["options"].get("num_ctx") == sent


@pytest.mark.parametrize("given, sent", [("30m", "30m"), (0, "0"), ("-1", "-1"), (None, None), ("", None)])
def test_ollama_keep_alive(http, given, sent):
    LocalProvider(keep_alive=given).generate(MSG)
    assert _body(http).get("keep_alive") == sent


def test_ollama_timeout_is_a_connect_and_read_pair(http):
    LocalProvider(timeout=45).generate(MSG)
    assert http.posts[0]["timeout"] == (3.0, 45)


def test_ollama_tools_and_messages_are_still_sent(http):
    from void.providers.base import ToolSpec
    LocalProvider().generate(MSG, tools=[ToolSpec(name="t", description="d", parameters={"type": "object"})])
    assert _body(http)["tools"][0]["function"]["name"] == "t" and _body(http)["messages"][0]["content"] == "hi"


def test_a_model_that_rejects_think_is_retried_exactly_once_without_it_and_remembered(http):
    http.queue = [_Resp(400, text='{"error":"\\"llama3.1:8b\\" does not support thinking"}',
                        exc=requests.exceptions.HTTPError("400"))]
    lp = LocalProvider(model="llama3.1:8b", think=False)
    assert lp.generate(MSG).text == "hello"
    assert len(http.posts) == 2 and "think" in http.posts[0]["body"] and "think" not in http.posts[1]["body"]
    lp.generate(MSG)                                          # remembered: no probe again
    assert len(http.posts) == 3 and "think" not in http.posts[2]["body"]


def test_a_second_rejection_is_not_retried_again(http):
    bad = lambda: _Resp(400, text="think unsupported", exc=requests.exceptions.HTTPError("400"))   # noqa: E731
    http.queue = [bad(), bad(), bad()]
    with pytest.raises(ProviderUnavailable):
        LocalProvider(think=False).generate(MSG)
    assert len(http.posts) == 2                               # one retry, never a loop


def test_a_400_that_is_not_about_think_is_not_retried(http):
    http.queue = [_Resp(400, text="invalid options", exc=requests.exceptions.HTTPError("400"))]
    with pytest.raises(ProviderUnavailable):
        LocalProvider(think=False).generate(MSG)
    assert len(http.posts) == 1


def test_a_missing_model_404_is_provider_unavailable(http):
    http.queue = [_Resp(404, exc=requests.exceptions.HTTPError("404"))]
    with pytest.raises(ProviderUnavailable) as e:
        LocalProvider(model="nope:1b").generate(MSG)
    assert "not installed" in str(e.value) and "nope:1b" in str(e.value)


def test_a_read_timeout_is_provider_unavailable_and_names_the_deadline(http):
    http.queue = [requests.exceptions.ReadTimeout("slow")]
    with pytest.raises(ProviderUnavailable) as e:
        LocalProvider(timeout=9).generate(MSG)
    assert "9s" in str(e.value) and len(http.posts) == 1


def test_ollama_not_running_is_provider_unavailable(http):
    http.queue = [requests.exceptions.ConnectionError("refused")]
    with pytest.raises(ProviderUnavailable):
        LocalProvider().generate(MSG)


def test_a_5xx_is_provider_unavailable(http):
    http.queue = [_Resp(500, exc=requests.exceptions.HTTPError("500"))]
    with pytest.raises(ProviderUnavailable):
        LocalProvider().generate(MSG)


def test_a_malformed_body_propagates_as_before_for_the_agents_bounded_retry(http):
    http.queue = [_Resp(200, json_exc=json.JSONDecodeError("x", "y", 0))]
    with pytest.raises(json.JSONDecodeError):
        LocalProvider().generate(MSG)


def test_a_reply_with_no_message_yields_an_empty_response_not_a_crash(http):
    http.queue = [_Resp(200, {})]
    r = LocalProvider().generate(MSG)
    assert r.text is None and not r.tool_calls


def test_nothing_is_ever_pulled_or_downloaded(http):
    lp = LocalProvider(model="qwen3:8b", think=False)
    assert lp.available() is True
    lp.generate(MSG)
    urls = http.gets + [p["url"] for p in http.posts]
    assert all(u.endswith(("/api/tags", "/api/chat")) for u in urls), urls
    assert not any("pull" in u or "create" in u or "push" in u for u in urls)


def test_needs_no_gpu_assumption_in_the_request(http):
    LocalProvider().generate(MSG)
    assert not any(k in _body(http)["options"] for k in ("num_gpu", "main_gpu", "low_vram", "num_thread"))


# --- Gemini 3.8 Flash as the configured primary cloud brain -------------------------------------------------

def _shipped():
    from pathlib import Path
    return Config.load(local_path=Path("__no_such_local_config__.yaml"))


def test_the_shipped_default_uses_gemini_3_8_flash_as_primary_with_ollama_fallback():
    cfg = _shipped()
    assert cfg.get("llm.primary") == "gemini" and cfg.get("llm.fallback") == ["local"]
    assert cfg.get("llm.gemini.model") == "gemini-3.8-flash"
    reg = ProviderRegistry.from_config(cfg)
    assert reg._order == ["gemini", "local"] and reg.get("gemini").model == "gemini-3.8-flash"


def test_gemini_3_8_keeps_the_existing_credential_pool_and_rotation_untouched():
    g = ProviderRegistry.from_config(_shipped()).get("gemini")
    pool = g._pool()
    assert pool.names()[0] == "gemini_api_key"                 # the existing primary credential name is unchanged
    assert g.timeout_s == DEFAULT_TIMEOUT_S


def test_gemini_3_8_ships_with_thinking_low_by_default_evidence_based_2026_09_22():
    """docs/GEMINI_3_8_FLASH.md: an A/B against the model's own default showed thinking="low" answering in ~2-6s with
    ~0 thinking tokens, vs ~8-11s / 230+ thinking tokens at the default - adopted as the shipped default."""
    g = ProviderRegistry.from_config(_shipped()).get("gemini")
    assert g.thinking_setting == ("level", "low") and g.thinking_warning is None


def test_gemini_3_8_request_carries_the_model_and_no_unsupported_thinking_setting():
    for value, expected in (("low", ("LOW", None)), ("minimal", None), (None, None)):
        p, got = _thinking_sent("gemini-3.8-flash", value)
        assert got == expected


def test_gemini_3_8_is_treated_as_a_cloud_provider_for_memory(tmp_path):
    from void.app import Assistant
    seen = []

    class Mem:
        def build_context(self, goal, for_cloud=False, recent_fallback=False):
            seen.append(for_cloud)
            return None

    a = Assistant(config=Config({"app": {"state_dir": str(tmp_path / "s")}, "memory": {"enabled": False}}))
    a.memory = Mem()
    a._memory_context_fn(GeminiProvider(model="gemini-3.8-flash"))("what do you remember?")
    assert seen == [True]


from tests.test_fast_path import rig  # noqa: E402,F401  (fixture reuse)


def test_a_known_launch_makes_zero_gemini_calls_with_gemini_as_the_primary(rig):
    a, _fake, launched, _ = rig()
    p, _pool, g = _gem({K1: _ok}, model="gemini-3.8-flash")
    a.providers = ProviderRegistry({"gemini": p}, ["gemini"])
    assert a.run("open notepad").result == "Opening Notepad." and len(launched) == 1
    assert g.log == [] and g.built == []                      # no request, no client even built


def test_an_unknown_command_still_reaches_gemini_3_8(rig):
    a, _fake, launched, _ = rig()
    p, _pool, g = _gem({K1: _ok}, model="gemini-3.8-flash")
    a.providers = ProviderRegistry({"gemini": p}, ["gemini"])
    assert a.run("open a program that does not exist").result == "ok"
    assert [c["model"] for c in g.log] == ["gemini-3.8-flash"] and launched == []
