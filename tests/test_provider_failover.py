"""Bounded, policy-aware failover from the cloud brain to the local one.

Before this, every provider failure was treated identically: three attempts on the same provider with exponential
backoff, then the task failed. A transient Gemini 503 therefore turned "explain ARP" into a 14-16 second error
message while Ollama sat idle - and Ollama was in fact unreachable anyway, because the configured model was not
the one installed.

Two things are pinned here: that a failure is classified before it is acted on, and that the classification
actually changes what happens.
"""
import pytest

from void.config import Config
from void.providers import failures
from void.providers.base import LLMProvider, LLMResponse, ProviderUnavailable
from void.providers.registry import ProviderRegistry


class _Fake(LLMProvider):
    def __init__(self, name, exc=None, text="answered", succeed_after=None):
        self.name, self._exc, self._text = name, exc, text
        self._succeed_after = succeed_after
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        if self._succeed_after is not None and self.calls > self._succeed_after:
            return LLMResponse(text=self._text)
        if self._exc is not None:
            raise self._exc
        return LLMResponse(text=self._text)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(failures, "backoff_s", lambda _a: 0.0)
    import void.core.agent as agent_mod
    monkeypatch.setattr(agent_mod.time, "sleep", lambda _s: None)


def _assistant(tmp_path, providers, order):
    from void.app import Assistant
    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    a = Assistant(config=Config({"app": {"state_dir": str(tmp_path / ".void")},
                                 "memory": {"enabled": False},
                                 "security": {"allowed_roots": [str(home / "ws")]}}))
    a.providers = ProviderRegistry(providers, order)
    return a


# --- classification --------------------------------------------------------------------------

@pytest.mark.parametrize("exc, category", [
    (RuntimeError("503 UNAVAILABLE. This model is experiencing high demand"), "server"),
    (RuntimeError("500 INTERNAL"), "server"),
    (RuntimeError("The service is currently unavailable"), "server"),
    (RuntimeError("429 RESOURCE_EXHAUSTED"), "rate_limit"),
    (RuntimeError("Too Many Requests"), "rate_limit"),
    (RuntimeError("You exceeded your current quota"), "quota"),
    (TimeoutError("deadline exceeded"), "timeout"),
    (RuntimeError("504 Gateway Timeout"), "timeout"),
    (ConnectionError("connection refused"), "network"),
    (RuntimeError("401 Unauthorized"), "auth"),
    (RuntimeError("API key not valid"), "auth"),
    (RuntimeError("403 forbidden"), "auth"),
    (RuntimeError("model 'llama3.1:8b' is not installed in Ollama"), "unsupported_model"),
    (RuntimeError("400 invalid argument: bad tool schema"), "invalid_request"),
    (ProviderUnavailable("every credential is cooled"), "unavailable"),
    (ValueError("something nobody anticipated"), "other"),
])
def test_a_failure_is_classified_before_anything_is_decided(exc, category):
    assert failures.classify(exc) == category


def test_every_category_has_a_bounded_policy():
    for category, (attempts, _failover) in failures.POLICY.items():
        assert 1 <= attempts <= 3, category
    assert failures.policy("a category that does not exist") == failures.POLICY["other"]


def test_backoff_is_short_and_bounded():
    """Waiting is the alternative to switching, and switching is usually faster."""
    assert [failures.backoff_s(i) for i in range(5)] == [1.0, 2.0, 2.0, 2.0, 2.0]


def test_the_categories_that_cannot_be_fixed_by_waiting_are_not_retried():
    for category in ("auth", "quota", "rate_limit", "unavailable", "unsupported_model"):
        assert failures.policy(category)[0] == 1, category


def test_an_invalid_request_is_the_one_thing_that_never_fails_over():
    """Another provider would reject it identically; the real problem has to surface."""
    assert failures.policy("invalid_request") == (1, False)
    assert all(failures.policy(c)[1] for c in failures.POLICY if c != "invalid_request")


# --- what actually happens -------------------------------------------------------------------

def test_a_transient_cloud_failure_is_answered_by_the_local_model(tmp_path, no_backoff):
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE. This model is experiencing high demand"))
    local = _Fake("local", text="ARP maps an IP address to a MAC address.")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "completed" and "ARP" in r.result
    assert gem.calls == 2 and local.calls == 1          # one bounded retry, then hand over


@pytest.mark.parametrize("exc", [
    RuntimeError("429 RESOURCE_EXHAUSTED"),
    RuntimeError("You exceeded your current quota"),
    RuntimeError("401 API key not valid"),
    ProviderUnavailable("every credential is cooled"),
])
def test_a_failure_that_waiting_cannot_fix_hands_over_immediately(tmp_path, no_backoff, exc):
    gem = _Fake("gemini", exc=exc)
    local = _Fake("local", text="answered locally")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "completed" and gem.calls == 1 and local.calls == 1


def test_an_invalid_request_surfaces_instead_of_being_re_sent_elsewhere(tmp_path, no_backoff):
    gem = _Fake("gemini", exc=RuntimeError("400 invalid argument: bad tool schema"))
    local = _Fake("local", text="should never be asked")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "failed" and gem.calls == 1 and local.calls == 0


def test_a_provider_that_recovers_on_its_own_is_never_handed_over(tmp_path, no_backoff):
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"), succeed_after=1, text="cloud answer")
    local = _Fake("local", text="local answer")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.result == "cloud answer" and local.calls == 0


def test_with_nowhere_to_fail_over_the_full_retry_budget_is_still_used(tmp_path, no_backoff):
    """Shortening the retries is a good trade only because switching is available. Alone, resilience wins."""
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"), succeed_after=2, text="cloud answer")
    a = _assistant(tmp_path, {"gemini": gem}, ["gemini"])
    r = a.run("explain ARP")
    assert r.status == "completed" and gem.calls == 3


def test_both_providers_failing_is_bounded_and_terminal(tmp_path, no_backoff):
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"))
    local = _Fake("local", exc=ConnectionError("connection refused"))
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "failed"
    assert gem.calls == 2 and local.calls == 3          # last in the chain keeps the full budget
    assert gem.calls + local.calls <= 8                 # never an unbounded loop


def test_failing_over_does_not_start_the_task_again(tmp_path, no_backoff):
    """The replacement provider continues the SAME conversation - it does not re-run tools or re-plan."""
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"))
    local = _Fake("local", text="done")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "completed" and r.steps == 1


def test_a_failover_is_recorded_in_telemetry_without_any_content(tmp_path, no_backoff, monkeypatch):
    from void import perf
    events = []
    monkeypatch.setattr(perf, "emit", lambda ev, **f: events.append((ev, f)))
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"))
    local = _Fake("local", text="answered")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    a.run("explain ARP")
    routes = [f for ev, f in events if ev == "route"]
    assert any(f.get("reason") == "failover" and f.get("provider") == "local" for f in routes)
    for _ev, fields in events:
        assert "ARP" not in repr(fields)                # never the goal, never the error text


# --- the fast path is untouched by any of this ------------------------------------------------

def test_an_application_command_never_reaches_either_provider(tmp_path, no_backoff):
    gem = _Fake("gemini", exc=RuntimeError("503 UNAVAILABLE"))
    local = _Fake("local", text="should never be asked")
    a = _assistant(tmp_path, {"gemini": gem, "local": local}, ["gemini", "local"])
    a.run("open notepad")
    assert gem.calls == 0 and local.calls == 0


def test_the_local_model_named_in_config_is_one_ollama_actually_has():
    """The fallback brain was unreachable because config named a model that was not installed."""
    cfg = Config.load()
    assert cfg.get("llm.local.model", "") == "qwen3:8b"
    assert cfg.get("llm.fallback", []) == ["local"]
