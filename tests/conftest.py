"""Pytest configuration: make the repo root importable and make every test HERMETIC.

Hermetic isolation (V2.0 T0.1). Ordinary tests must never touch:

  * the real Windows Credential Manager (service ``void``) - it is replaced by an
    in-memory keyring backend, freshly emptied before every test;
  * the real ``~/.void`` state directory / production log - ``HOME`` and
    ``USERPROFILE`` are pointed at a per-test temp directory, so anything that
    resolves ``~`` (``Config.state_dir()``, ``Path.home()``) lands in a sandbox.

Before this existed, ``tests/test_device_gateway.py`` paired devices through the
default ``DeviceRegistry`` and left ~17 ``device_secret:*`` credentials in the
owner's real store on every run (defect D-14).

Markers (registered here; there is no pytest.ini):

  real_socket   opens real loopback sockets (still runs by default).
  hardware      needs a real device (mic/GPU/phone); skip in CI, opt in locally.
  real_keyring  RESERVED. Would need the real credential store; skipped unless
                VOID_ALLOW_REAL_KEYRING=1. No test uses it.
"""
import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Captured BEFORE any test redirects the environment, so tests can prove they
# are NOT touching it. Never used to read or write anything.
REAL_HOME = os.path.expanduser("~")

try:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError
except ImportError:  # keyring is a declared dependency; degrade rather than crash
    keyring = None


if keyring is not None:
    class InMemoryKeyring(KeyringBackend):
        """A keyring that lives and dies in process memory."""
        priority = 100

        def __init__(self):
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            try:
                del self._store[(service, username)]
            except KeyError:
                raise PasswordDeleteError("not found")

    # Installed at import time as well, so a collection-time import of
    # void.security.secrets can never reach the real backend.
    keyring.set_keyring(InMemoryKeyring())


def pytest_configure(config):
    for name, doc in (
        ("real_socket", "opens real loopback sockets"),
        ("hardware", "needs a real device (mic/GPU/phone); skipped unless opted in"),
        ("real_keyring", "RESERVED: needs the real credential store; skipped by default"),
    ):
        config.addinivalue_line("markers", f"{name}: {doc}")


def pytest_collection_modifyitems(config, items):
    skip_real = pytest.mark.skip(
        reason="real_keyring tests need VOID_ALLOW_REAL_KEYRING=1 (never set in CI)")
    if os.environ.get("VOID_ALLOW_REAL_KEYRING") != "1":
        for item in items:
            if "real_keyring" in item.keywords:
                item.add_marker(skip_real)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path_factory):
    """Fresh in-memory credential store + sandboxed home for EVERY test."""
    if keyring is not None:
        keyring.set_keyring(InMemoryKeyring())
    sandbox = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("USERPROFILE", str(sandbox))
    monkeypatch.setenv("HOME", str(sandbox))
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    # The Hugging Face cache lives under ~, so sandboxing HOME also hides the real
    # model cache; without this an STT test would try to DOWNLOAD ~460 MB into the
    # sandbox (observed while validating T0.1). Tests must never use the network.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    yield sandbox


@pytest.fixture
def real_home():
    """The genuine home directory (path only) - for tests that assert isolation."""
    return REAL_HOME
