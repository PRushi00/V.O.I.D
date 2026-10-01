"""Failing over must not widen what memory a run may carry.

Memory is filtered per destination: anything the owner marked as not-for-cloud is withheld when the provider is
not the local model. That filter is applied ONCE, when the run is built. Since a run can now change provider
half-way through, the filter has to be decided for every provider the run might reach - not just the first - or a
cloud fallback would receive memory that was only ever cleared for the local model.
"""
from void.app import Assistant
from void.config import Config


class _P:
    def __init__(self, name):
        self.name = name

    def available(self):
        return True


def _assistant(tmp_path):
    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    return Assistant(config=Config({"app": {"state_dir": str(tmp_path / ".void")},
                                    "memory": {"enabled": True},
                                    "security": {"allowed_roots": [str(home / "ws")]}}))


def _for_cloud(assistant, chain, monkeypatch):
    seen = {}

    def build_context(goal, for_cloud=True, recent_fallback=False):
        seen["for_cloud"] = for_cloud
        return None

    monkeypatch.setattr(assistant.memory, "build_context", build_context)
    fn = assistant._memory_context_fn(chain)
    fn("anything")
    return seen["for_cloud"]


def test_a_local_only_run_may_still_carry_private_memory(tmp_path, monkeypatch):
    a = _assistant(tmp_path)
    assert _for_cloud(a, [_P("local")], monkeypatch) is False


def test_a_run_that_starts_local_but_could_reach_the_cloud_is_treated_as_cloud(tmp_path, monkeypatch):
    """The dangerous direction: built permissively, then handed to a cloud provider."""
    a = _assistant(tmp_path)
    assert _for_cloud(a, [_P("local"), _P("gemini")], monkeypatch) is True


def test_a_cloud_run_that_falls_back_to_local_stays_cloud_filtered(tmp_path, monkeypatch):
    a = _assistant(tmp_path)
    assert _for_cloud(a, [_P("gemini"), _P("local")], monkeypatch) is True


def test_an_unknown_provider_counts_as_cloud(tmp_path, monkeypatch):
    a = _assistant(tmp_path)
    assert _for_cloud(a, [_P("something-new")], monkeypatch) is True
