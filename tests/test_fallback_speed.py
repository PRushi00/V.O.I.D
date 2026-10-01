"""How quickly V.O.I.D reaches the local model when the cloud one fails.

Two things were making the fallback slow, and neither was the retry policy:

1. **"localhost" cost 2 seconds per call.** It resolves to the IPv6 ``::1`` before IPv4 on a default Windows
   install and Ollama listens only on IPv4, so every request waited for that connection to fail first. Measured
   2026-09-24: readiness probe 2110 ms -> 16 ms, generation 4.32 s -> 2.17 s.

2. **The readiness probe ran on every request.** ``available_order`` asked every provider whether it was ready
   before each run, so even a request Gemini answered perfectly well paid the 2 s Ollama probe.

Together: a Gemini-503 fallback went from 7.9-9.5 s to 3.9-4.1 s, and every successful Gemini request stopped
paying a 2 s tax it never needed. See docs/PROVIDER_FALLBACK_2026-09-24.md.
"""
import pytest

from void.providers.base import LLMProvider, LLMResponse, ProviderUnavailable
from void.providers.local_provider import LocalProvider
from void.providers.registry import ProviderRegistry


# --- the host that cost two seconds ------------------------------------------------------------

@pytest.mark.parametrize("configured, expected", [
    ("http://localhost:11434", "http://127.0.0.1:11434"),
    ("http://LOCALHOST:11434", "http://127.0.0.1:11434"),
    ("http://localhost:11434/", "http://127.0.0.1:11434"),
    ("http://localhost", "http://127.0.0.1"),
])
def test_localhost_is_pinned_to_ipv4(configured, expected):
    """The whole fix: naming the interface instead of asking the resolver which one to try first."""
    assert LocalProvider(base_url=configured).base_url == expected


@pytest.mark.parametrize("configured", [
    "http://127.0.0.1:11434",          # already explicit
    "http://[::1]:11434",              # a deliberate IPv6 choice is honoured
    "http://ollama.lan:11434",         # a real host on the network
    "http://192.168.1.50:11434",
    "https://ollama.example.com",
])
def test_nothing_else_is_ever_rewritten(configured):
    """Redirecting someone's traffic somewhere they did not ask for would be far worse than a slow probe."""
    assert LocalProvider(base_url=configured).base_url == configured.rstrip("/")


def test_credentials_in_the_url_survive_the_rewrite():
    p = LocalProvider(base_url="http://user:pw@localhost:11434")
    assert p.base_url == "http://user:pw@127.0.0.1:11434"


def test_an_unparseable_url_is_left_alone_rather_than_mangled():
    weird = "not a url at all"
    assert LocalProvider(base_url=weird).base_url == weird


def test_the_shipped_configuration_does_not_rely_on_the_resolver():
    from void.config import Config
    assert "localhost" not in Config.load().get("llm.local.base_url", "")


# --- who gets asked whether they are ready -------------------------------------------------------

class _Probe(LLMProvider):
    def __init__(self, name, available=True):
        self.name, self._available = name, available
        self.probes = 0

    def available(self):
        self.probes += 1
        return self._available

    def generate(self, messages, tools=None):
        return LLMResponse(text=f"{self.name} answered")


def test_a_fallback_is_not_asked_whether_it_is_ready_until_it_is_needed():
    """This probe was a 2 s round trip on this machine, paid by every request Gemini answered fine."""
    gemini, local = _Probe("gemini"), _Probe("local")
    chain = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"]).available_order()
    assert [p.name for p in chain] == ["gemini", "local"]      # still offered as the fallback
    assert gemini.probes == 1 and local.probes == 0            # but never asked


def test_an_unavailable_primary_is_skipped_and_the_next_one_is_checked():
    gemini, local = _Probe("gemini", available=False), _Probe("local")
    chain = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"]).available_order()
    assert [p.name for p in chain] == ["local"]
    assert gemini.probes == 1 and local.probes == 1


def test_nothing_available_is_an_empty_chain_not_a_crash():
    gemini, local = _Probe("gemini", available=False), _Probe("local", available=False)
    assert ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"]).available_order() == []


def test_a_fallback_that_turns_out_to_be_dead_is_handled_by_the_normal_failure_path(tmp_path, monkeypatch):
    """Not probing a fallback is only safe because an unavailable one raises and is classified like anything else."""
    from void.app import Assistant
    from void.config import Config
    from void.providers import failures
    monkeypatch.setattr(failures, "backoff_s", lambda _a: 0.0)

    class _Dead(LLMProvider):
        name = "local"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            raise ProviderUnavailable("Ollama is not running")

    class _Fail(LLMProvider):
        name = "gemini"
        calls = 0

        def available(self):
            return True

        def generate(self, messages, tools=None):
            _Fail.calls += 1
            raise RuntimeError("503 UNAVAILABLE")

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    a = Assistant(config=Config({"app": {"state_dir": str(tmp_path / ".void")},
                                 "memory": {"enabled": False},
                                 "security": {"allowed_roots": [str(home / "ws")]}}))
    dead = _Dead()
    a.providers = ProviderRegistry({"gemini": _Fail(), "local": dead}, ["gemini", "local"])
    r = a.run("explain ARP")
    assert r.status == "failed"                    # honest failure, not a hang
    assert _Fail.calls == 2                        # bounded: it did not keep trying


def test_the_number_of_provider_calls_is_bounded_whatever_fails(tmp_path, monkeypatch):
    from void.app import Assistant
    from void.config import Config
    from void.providers import failures
    monkeypatch.setattr(failures, "backoff_s", lambda _a: 0.0)
    seen = []

    class _Fail(LLMProvider):
        def __init__(self, name):
            self.name = name

        def available(self):
            return True

        def generate(self, messages, tools=None):
            seen.append(self.name)
            raise RuntimeError("503 UNAVAILABLE")

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    a = Assistant(config=Config({"app": {"state_dir": str(tmp_path / ".void")},
                                 "memory": {"enabled": False},
                                 "security": {"allowed_roots": [str(home / "ws")]}}))
    a.providers = ProviderRegistry({"gemini": _Fail("gemini"), "local": _Fail("local")},
                                   ["gemini", "local"])
    a.run("explain ARP")
    assert len(seen) <= 6, f"unbounded retrying: {seen}"
    assert seen.count("gemini") == 2 and seen.count("local") == 3
