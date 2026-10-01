"""Scratch-only pytest plugin: keep the V.O.I.D suite off the machine's real state.

Swaps the OS keyring for an in-memory backend BEFORE any test imports
void.security.secrets, so pairing tests that use the default DeviceRegistry
cannot write to (or delete from) the real Windows Credential Manager.
Not part of the repo.
"""
import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class InMemoryKeyring(KeyringBackend):
    priority = 100

    def __init__(self):
        super().__init__()
        self._store = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found")


keyring.set_keyring(InMemoryKeyring())


def pytest_report_header(config):
    return f"isolation: keyring backend = {type(keyring.get_keyring()).__name__}"
