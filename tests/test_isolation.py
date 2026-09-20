"""The hermetic-isolation fixture itself (T0.1): ordinary tests cannot reach the
real credential store, ``~/.void`` or the production log."""
import os
from pathlib import Path

import pytest

keyring = pytest.importorskip("keyring")

from void.config import Config
from void.security import secrets

_LEAK_PROBE = {"seen": None}


def test_keyring_backend_is_in_memory_not_windows_vault():
    backend = keyring.get_keyring()
    assert type(backend).__name__ == "InMemoryKeyring"
    assert "WinVault" not in type(backend).__module__ + type(backend).__name__


def test_secret_roundtrip_stays_in_process():
    secrets.set_secret("isolation_probe", "value-1")
    assert secrets.get_secret("isolation_probe") == "value-1"
    assert secrets.delete_secret("isolation_probe") is True
    _LEAK_PROBE["seen"] = None


def test_writes_do_not_leak_into_the_next_test_part_1():
    secrets.set_secret("device_secret:leak-canary", "x")
    _LEAK_PROBE["seen"] = "set"


def test_writes_do_not_leak_into_the_next_test_part_2():
    # Runs after part_1 in file order; the store must have been emptied between tests.
    assert _LEAK_PROBE["seen"] == "set"
    assert secrets.get_secret("device_secret:leak-canary") is None


def test_home_and_state_dir_are_sandboxed(real_home, tmp_path):
    state = Config({}).state_dir()            # default app.state_dir ".void"
    assert str(state).lower() != str(Path(real_home, ".void")).lower()
    assert not str(state).lower().startswith(str(Path(real_home, ".void")).lower())
    assert Path(os.path.expanduser("~")) != Path(real_home)
    assert Path.home() != Path(real_home)


def test_network_model_downloads_are_disabled():
    # Sandboxing HOME hides the real Hugging Face cache; offline mode stops a test
    # from downloading a multi-hundred-MB model into the sandbox instead.
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_state_dir_creation_touches_only_the_sandbox(real_home):
    before = Path(real_home, ".void").exists()
    Config({}).state_dir()
    assert Path(real_home, ".void").exists() == before   # unchanged by the test


class _FakeItem:
    def __init__(self, keywords):
        self.keywords = dict.fromkeys(keywords, True)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def test_real_keyring_marker_is_skipped_by_default_and_only_opt_in_runs_it(monkeypatch):
    """The hook is exercised on stand-in items so the suite itself carries no skipped test."""
    from tests.conftest import pytest_collection_modifyitems

    monkeypatch.delenv("VOID_ALLOW_REAL_KEYRING", raising=False)
    risky, safe = _FakeItem(["real_keyring"]), _FakeItem(["real_socket"])
    pytest_collection_modifyitems(None, [risky, safe])
    assert [m.name for m in risky.markers] == ["skip"] and safe.markers == []

    monkeypatch.setenv("VOID_ALLOW_REAL_KEYRING", "1")
    opted_in = _FakeItem(["real_keyring"])
    pytest_collection_modifyitems(None, [opted_in])
    assert opted_in.markers == []
