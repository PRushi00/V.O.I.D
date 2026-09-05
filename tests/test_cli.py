"""Deterministic tests for the Gemini credential-management CLI.

A FAKE in-memory keyring is injected via monkeypatch; the real Windows
Credential Manager is never touched, and only obvious fake sentinel values are
used (never anything resembling a real Gemini API key).
"""
import getpass as _getpass_mod
import json

import pytest

from void import cli
from void.security import credentials, secrets

PRIMARY = secrets.GEMINI_API_KEY        # "gemini_api_key"
MANIFEST = credentials.MANIFEST_KEY     # "gemini_api_key_names"

FAKE_PRIMARY = "FAKE_GEMINI_KEY_PRIMARY"
FAKE_02 = "FAKE_GEMINI_KEY_02"
FAKE_03 = "FAKE_GEMINI_KEY_03"


@pytest.fixture
def store(monkeypatch):
    """In-memory fake keyring backing secrets.{get,set,delete}_secret."""
    data = {}
    monkeypatch.setattr(secrets, "set_secret", lambda k, v: data.__setitem__(k, v))
    monkeypatch.setattr(secrets, "get_secret", lambda k: data.get(k))

    def _delete(k):
        existed = k in data
        data.pop(k, None)
        return existed

    monkeypatch.setattr(secrets, "delete_secret", _delete)
    return data


@pytest.fixture
def enter_key(monkeypatch):
    """Queue hidden inputs; record getpass use and forbid plain input()."""
    calls = {"getpass": 0}
    queue: list[str] = []

    def fake_getpass(prompt=""):
        calls["getpass"] += 1
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(_getpass_mod, "getpass", fake_getpass)

    def _boom(*a, **k):
        raise AssertionError("input() must never be used to read a secret")

    monkeypatch.setattr("builtins.input", _boom)

    def feed(*values):
        queue.extend(values)

    return calls, feed


def _run(*argv):
    return cli.main(list(argv))


# 1. Existing primary command unchanged ---------------------------------

def test_set_key_primary_unchanged(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    assert _run("set-key", "gemini") == 0
    assert store[PRIMARY] == FAKE_PRIMARY
    assert MANIFEST not in store          # no manifest for primary-only
    assert calls["getpass"] == 1          # hidden input used


# 2. Named credential ---------------------------------------------------

def test_set_key_named_stores_under_alias_and_updates_manifest(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    assert _run("set-key", "gemini", "--name", "gemini_02") == 0
    assert store["gemini_02"] == FAKE_02
    names = json.loads(store[MANIFEST])
    assert "gemini_02" in names
    assert names[0] == PRIMARY


# 3. Hidden input -------------------------------------------------------

def test_hidden_input_used_not_plain_input(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    # builtins.input is patched to raise; using it would fail the test.
    assert _run("set-key", "gemini", "--name", "gemini_02") == 0
    assert calls["getpass"] == 1


# 4 & 5. Manifest + ordering --------------------------------------------

def test_manifest_ordering_primary_first(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_03)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_03")
    assert json.loads(store[MANIFEST]) == [PRIMARY, "gemini_02", "gemini_03"]


# 6. Duplicate alias ----------------------------------------------------

def test_duplicate_alias_no_duplicate_manifest_entry(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_02")
    names = json.loads(store[MANIFEST])
    assert names.count("gemini_02") == 1


# 7. Updating an existing alias replaces its secret ---------------------

def test_update_existing_alias_replaces_secret(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, "FAKE_GEMINI_KEY_02_ROTATED")
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_02")
    assert store["gemini_02"] == "FAKE_GEMINI_KEY_02_ROTATED"
    assert json.loads(store[MANIFEST]).count("gemini_02") == 1


# 8. list-keys shows names only -----------------------------------------

def test_list_keys_shows_names_only(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_PRIMARY, FAKE_02)
    _run("set-key", "gemini")
    _run("set-key", "gemini", "--name", "gemini_02")
    capsys.readouterr()  # clear prior output
    assert _run("list-keys") == 0
    out = capsys.readouterr().out
    assert PRIMARY in out and "gemini_02" in out
    assert FAKE_PRIMARY not in out           # never show values
    assert FAKE_02 not in out


# 9. remove-key ---------------------------------------------------------

def test_remove_key_deletes_secret_and_manifest_entry(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    assert "gemini_02" in store
    assert _run("remove-key", "gemini_02") == 0
    assert "gemini_02" not in store
    # Only primary remains -> manifest removed (pristine single-key state).
    assert MANIFEST not in store


def test_remove_key_keeps_other_additional(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_03)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_03")
    assert _run("remove-key", "gemini_02") == 0
    assert "gemini_02" not in store and "gemini_03" in store
    assert json.loads(store[MANIFEST]) == [PRIMARY, "gemini_03"]


def test_remove_nonexistent_alias_is_clean(store):
    assert _run("remove-key", "ghost") == 1


# 10. Primary protection ------------------------------------------------

def test_primary_cannot_be_removed_via_remove_key(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    _run("set-key", "gemini")
    assert _run("remove-key", PRIMARY) == 1
    assert store[PRIMARY] == FAKE_PRIMARY   # still present


def test_remove_key_will_not_delete_stop_pin_or_manifest(store, enter_key):
    # Reserved/unrelated names are not "additional credentials" and are safe.
    store[secrets.STOP_PIN] = "FAKE_PIN"
    assert _run("remove-key", secrets.STOP_PIN) == 1
    assert store[secrets.STOP_PIN] == "FAKE_PIN"


# 11. Invalid aliases ---------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", "   ", "bad name", "gemini/02", "gemini.02", "a b",
    PRIMARY, MANIFEST, "stop_pin",
])
def test_invalid_alias_rejected_before_prompting(store, enter_key, bad):
    calls, feed = enter_key
    assert _run("set-key", "gemini", "--name", bad) == 1
    assert bad not in store            # nothing stored
    assert MANIFEST not in store       # manifest untouched
    assert calls["getpass"] == 0       # never even prompted


# 12. Keyring failures --------------------------------------------------

def test_set_key_keyring_failure_is_clean(store, enter_key, monkeypatch, capsys):
    calls, feed = enter_key
    feed(FAKE_02)

    def boom(k, v):
        raise secrets.SecretStoreError("raw backend text should not surface")

    monkeypatch.setattr(secrets, "set_secret", boom)
    assert _run("set-key", "gemini", "--name", "gemini_02") == 1
    out = capsys.readouterr().out
    assert FAKE_02 not in out
    assert "raw backend text" not in out


# 13. Backward compatibility --------------------------------------------

def test_backward_compat_only_primary(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    _run("set-key", "gemini")
    capsys.readouterr()
    assert _run("list-keys") == 0
    out = capsys.readouterr().out
    assert PRIMARY in out
    assert "gemini_02" not in out
    assert MANIFEST not in store


# Secret non-disclosure on success paths --------------------------------

def test_set_key_success_output_has_no_value(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    out = capsys.readouterr().out
    assert FAKE_02 not in out
    assert "gemini_02" in out  # the alias name is fine to show
