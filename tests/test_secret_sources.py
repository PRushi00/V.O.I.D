"""Where a secret may come from, and - more importantly - where it may not.

A headless machine has no keyring at all, so on a container or a CI runner the choice is not
"keyring or environment", it is "environment or no credential". The fallback exists for that.

The reason these tests are worth their length is the other half: the fallback is a CLOSED list.
The memory key decrypts the owner's private memory store and the stop PIN releases the kill
switch, and an environment variable is readable by every child process this program starts. A
revocable key for somebody else's API is a different class of secret from the key to the owner's
own data, and only the first kind may be taken from the environment. If a later change adds the
second kind, these tests fail.
"""
from __future__ import annotations

import pytest

from void.security import secrets


@pytest.fixture
def no_keyring(monkeypatch):
    """A machine with no keyring at all - a container, a CI runner, a server."""
    def absent():
        raise secrets.SecretStoreError("no keyring on this machine")
    monkeypatch.setattr(secrets, "_keyring", absent)


@pytest.fixture
def empty_keyring(monkeypatch):
    """A keyring that works and simply holds nothing."""
    class _Empty:
        def get_password(self, service, key):
            return None

        def set_password(self, service, key, value):
            return None
    monkeypatch.setattr(secrets, "_keyring", lambda: _Empty())


# --------------------------------------------------------------------------------------------
# The closed list is the point
# --------------------------------------------------------------------------------------------
def test_only_the_api_credential_may_come_from_the_environment():
    assert set(secrets.ENV_FALLBACKS) == {secrets.GEMINI_API_KEY}


def test_the_memory_key_may_never_come_from_the_environment():
    """It decrypts the owner's private memory. The environment is not a safe place for it."""
    assert secrets.MEMORY_KEY not in secrets.ENV_FALLBACKS


def test_the_stop_pin_may_never_come_from_the_environment():
    """It releases the kill switch, which is a safety control, not a convenience."""
    assert secrets.STOP_PIN not in secrets.ENV_FALLBACKS


def test_a_secret_not_on_the_list_ignores_an_environment_variable(monkeypatch, empty_keyring):
    monkeypatch.setenv("VOID_MEMORY_KEY", "should-never-be-read")
    monkeypatch.setenv("MEMORY_KEY", "should-never-be-read")
    assert secrets.get_secret(secrets.MEMORY_KEY) is None


def test_a_secret_not_on_the_list_still_reports_a_broken_store_rather_than_absence(no_keyring):
    """Silently returning None would make a broken keyring look like an unconfigured one."""
    with pytest.raises(secrets.SecretStoreError):
        secrets.get_secret(secrets.MEMORY_KEY)


# --------------------------------------------------------------------------------------------
# The fallback itself
# --------------------------------------------------------------------------------------------
def test_the_api_key_is_read_from_the_environment_when_there_is_no_keyring(monkeypatch, no_keyring):
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "value-from-the-environment")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "value-from-the-environment"


def test_the_conventional_name_is_accepted_too(monkeypatch, no_keyring):
    monkeypatch.delenv("VOID_GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "conventional")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "conventional"


def test_the_prefixed_name_wins_when_both_are_set(monkeypatch, no_keyring):
    """The prefixed one is unambiguously meant for this program."""
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "ours")
    monkeypatch.setenv("GEMINI_API_KEY", "someone-elses")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "ours"


def test_the_keyring_wins_over_the_environment(monkeypatch):
    """An installed desktop must behave exactly as it did before this fallback existed."""
    class _Stocked:
        def get_password(self, service, key):
            return "from-the-keyring"
    monkeypatch.setattr(secrets, "_keyring", lambda: _Stocked())
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "from-the-environment")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "from-the-keyring"


def test_an_empty_keyring_falls_through_to_the_environment(monkeypatch, empty_keyring):
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "fallback")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "fallback"


def test_a_blank_environment_variable_is_not_a_credential(monkeypatch, no_keyring):
    """An exported-but-empty variable is the commonest way to think you set a key when you did not."""
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "   ")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(secrets.SecretStoreError):
        secrets.get_secret(secrets.GEMINI_API_KEY)


def test_surrounding_whitespace_is_stripped(monkeypatch, no_keyring):
    """A key pasted into a settings box usually arrives with a newline on it."""
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "  padded-key\n")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "padded-key"


def test_a_broken_backend_does_not_hide_a_key_the_environment_carries(monkeypatch):
    class _Broken:
        def get_password(self, service, key):
            raise RuntimeError("the backend is wedged")
    monkeypatch.setattr(secrets, "_keyring", lambda: _Broken())
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "still-works")
    assert secrets.get_secret(secrets.GEMINI_API_KEY) == "still-works"


def test_a_broken_backend_with_nothing_in_the_environment_still_reports_itself(monkeypatch):
    class _Broken:
        def get_password(self, service, key):
            raise RuntimeError("the backend is wedged")
    monkeypatch.setattr(secrets, "_keyring", lambda: _Broken())
    monkeypatch.delenv("VOID_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(secrets.SecretStoreError):
        secrets.get_secret(secrets.GEMINI_API_KEY)


# --------------------------------------------------------------------------------------------
# Saying where it came from
# --------------------------------------------------------------------------------------------
def test_the_source_is_named_as_the_environment(monkeypatch, no_keyring):
    monkeypatch.setenv("VOID_GEMINI_API_KEY", "x")
    assert secrets.secret_source(secrets.GEMINI_API_KEY) == secrets.ENVIRONMENT


def test_the_source_is_named_as_the_keyring(monkeypatch):
    class _Stocked:
        def get_password(self, service, key):
            return "k"
    monkeypatch.setattr(secrets, "_keyring", lambda: _Stocked())
    assert secrets.secret_source(secrets.GEMINI_API_KEY) == secrets.KEYRING


def test_absent_means_absent(monkeypatch, no_keyring):
    monkeypatch.delenv("VOID_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert secrets.secret_source(secrets.GEMINI_API_KEY) == secrets.ABSENT


def test_the_source_report_never_raises_on_a_broken_backend(monkeypatch):
    class _Broken:
        def get_password(self, service, key):
            raise RuntimeError("wedged")
    monkeypatch.setattr(secrets, "_keyring", lambda: _Broken())
    monkeypatch.delenv("VOID_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert secrets.secret_source(secrets.GEMINI_API_KEY) == secrets.ABSENT


# --------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------
def test_a_failed_write_names_the_environment_variable_to_use_instead(no_keyring):
    with pytest.raises(secrets.SecretStoreError) as caught:
        secrets.set_secret(secrets.GEMINI_API_KEY, "whatever")
    assert "VOID_GEMINI_API_KEY" in str(caught.value)


def test_a_failed_write_never_puts_the_secret_in_the_message(no_keyring):
    """This text reaches logs and screens. The one thing it must not carry is the value."""
    with pytest.raises(secrets.SecretStoreError) as caught:
        secrets.set_secret(secrets.GEMINI_API_KEY, "super-secret-value")
    assert "super-secret-value" not in str(caught.value)


def test_a_failed_write_of_a_secret_with_no_fallback_suggests_nothing(no_keyring):
    with pytest.raises(secrets.SecretStoreError) as caught:
        secrets.set_secret(secrets.MEMORY_KEY, "whatever")
    assert "environment" not in str(caught.value).lower()


def test_setting_a_secret_never_writes_to_the_environment(monkeypatch, empty_keyring):
    """The environment is a way to READ a credential in, never a place this program stores one."""
    import os
    monkeypatch.delenv("VOID_GEMINI_API_KEY", raising=False)
    secrets.set_secret(secrets.GEMINI_API_KEY, "value")
    assert "VOID_GEMINI_API_KEY" not in os.environ
