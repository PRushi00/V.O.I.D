"""Unit tests for the Gemini CredentialPool.

All tests inject a FAKE get_secret; the real OS keyring is never touched and no
real Gemini key is ever used. Secret *values* used here are obvious fakes.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from void.security import secrets
from void.security.credentials import (
    Credential,
    CredentialMissing,
    CredentialPool,
    CredentialsExhausted,
    MANIFEST_KEY,
)

PRIMARY = secrets.GEMINI_API_KEY  # "gemini_api_key"
SENTINEL = "FAKE-SECRET-VALUE-DO-NOT-DISCLOSE"


def make_store(values=None, manifest=None):
    """Build a fake keyring: a dict + a get_secret callable over it."""
    data = dict(values or {})
    if manifest is not None:
        data[MANIFEST_KEY] = json.dumps(manifest)
    def get_secret(name):
        return data.get(name)
    return data, get_secret


# A. Single-key backward compatibility ----------------------------------

def test_single_key_backward_compat_no_manifest():
    _data, gs = make_store({PRIMARY: SENTINEL})  # only the primary, no manifest
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY]
    assert len(pool) == 1
    assert pool.get_next_available().name == PRIMARY


def test_single_key_even_if_primary_value_absent_is_listed():
    # The pool tracks references; missing values are a retrieval-time concern.
    _data, gs = make_store({})  # nothing stored at all
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY]


# B. Multiple credential ordering ---------------------------------------

def test_multiple_ordering_primary_first():
    _data, gs = make_store(
        {PRIMARY: SENTINEL, "gemini_api_key_2": "b", "gemini_api_key_3": "c"},
        manifest=[PRIMARY, "gemini_api_key_2", "gemini_api_key_3"],
    )
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY, "gemini_api_key_2", "gemini_api_key_3"]


def test_primary_forced_first_even_if_manifest_lists_it_later():
    _data, gs = make_store(manifest=["gemini_api_key_2", PRIMARY, "gemini_api_key_3"])
    pool = CredentialPool(get_secret=gs)
    assert pool.names()[0] == PRIMARY
    assert pool.names() == [PRIMARY, "gemini_api_key_2", "gemini_api_key_3"]


def test_primary_prepended_when_manifest_omits_it():
    _data, gs = make_store(manifest=["gemini_api_key_2", "gemini_api_key_3"])
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY, "gemini_api_key_2", "gemini_api_key_3"]


# C. Manifest handling --------------------------------------------------

def test_missing_manifest_falls_back_to_primary():
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY]


def test_valid_manifest():
    _data, gs = make_store(manifest=[PRIMARY, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY, "gemini_api_key_2"]


def test_duplicate_names_are_deduped_deterministically():
    _data, gs = make_store(
        manifest=["gemini_api_key_2", "gemini_api_key_2", PRIMARY, PRIMARY])
    pool = CredentialPool(get_secret=gs)
    assert pool.names() == [PRIMARY, "gemini_api_key_2"]


def test_empty_and_invalid_names_are_filtered():
    _data, gs = make_store(
        manifest=["", "   ", None, 123, {"x": 1}, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    # Junk dropped; primary guaranteed first; whitespace not treated as a name.
    assert pool.names() == [PRIMARY, "gemini_api_key_2"]


def test_invalid_json_manifest_falls_back_to_primary():
    data = {PRIMARY: SENTINEL, MANIFEST_KEY: "this is not json"}
    pool = CredentialPool(get_secret=lambda n: data.get(n))
    assert pool.names() == [PRIMARY]


def test_non_list_json_manifest_falls_back_to_primary():
    data = {PRIMARY: SENTINEL, MANIFEST_KEY: json.dumps({"a": "b"})}
    pool = CredentialPool(get_secret=lambda n: data.get(n))
    assert pool.names() == [PRIMARY]


# D. Availability -------------------------------------------------------

def test_available_credential_returned():
    _data, gs = make_store(manifest=[PRIMARY, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    assert pool.get_next_available().name == PRIMARY


def test_unavailable_is_skipped_next_is_selected():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _data, gs = make_store(manifest=[PRIMARY, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    pool.mark_unavailable(PRIMARY, now + timedelta(hours=1))
    assert pool.get_next_available(now=now).name == "gemini_api_key_2"


def test_mark_unavailable_unknown_alias_raises():
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    with pytest.raises(ValueError):
        pool.mark_unavailable("nope", datetime.now(timezone.utc))


def test_mark_unavailable_requires_tzaware():
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    with pytest.raises(ValueError):
        pool.mark_unavailable(PRIMARY, datetime(2026, 1, 1))  # naive


def test_cooldown_stored_as_utc():
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    est = timezone(timedelta(hours=-5))
    pool.mark_unavailable(PRIMARY, datetime(2026, 1, 1, 12, tzinfo=est))
    cred = pool.credentials()[0]
    assert cred.cooldown_until.tzinfo == timezone.utc


# E. Cooldown expiry ----------------------------------------------------

def test_expired_cooldown_becomes_available_again():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _data, gs = make_store(manifest=[PRIMARY, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    pool.mark_unavailable(PRIMARY, now + timedelta(minutes=10))
    # Still cooling: second key is chosen.
    assert pool.get_next_available(now=now).name == "gemini_api_key_2"
    # After the cooldown passes, the primary is available again (order restored).
    later = now + timedelta(minutes=11)
    assert pool.get_next_available(now=later).name == PRIMARY


def test_clear_cooldown_restores_availability():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    pool.mark_unavailable(PRIMARY, now + timedelta(days=1))
    assert not pool.credentials()[0].is_available(now)
    pool.clear_cooldown(PRIMARY)
    assert pool.credentials()[0].is_available(now)


# F. All credentials unavailable ----------------------------------------

def test_all_unavailable_raises_exhaustion():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _data, gs = make_store(manifest=[PRIMARY, "gemini_api_key_2"])
    pool = CredentialPool(get_secret=gs)
    pool.mark_unavailable(PRIMARY, now + timedelta(hours=1))
    pool.mark_unavailable("gemini_api_key_2", now + timedelta(hours=1))
    with pytest.raises(CredentialsExhausted):
        pool.get_next_available(now=now)


# G. Keyring interaction (fake) -----------------------------------------

def test_get_value_reads_on_demand():
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    assert pool.get_value(pool.get_next_available()) == SENTINEL
    assert pool.get_value(PRIMARY) == SENTINEL  # by name too


def test_get_value_missing_raises_without_value():
    _data, gs = make_store({})  # no value stored
    pool = CredentialPool(get_secret=gs)
    with pytest.raises(CredentialMissing) as exc:
        pool.get_value(PRIMARY)
    assert SENTINEL not in str(exc.value)


def test_manifest_backend_error_falls_back_to_primary():
    # A keyring failure while reading the manifest must not break single-key.
    def failing_get_secret(name):
        if name == MANIFEST_KEY:
            raise secrets.SecretStoreError("backend unavailable")
        return SENTINEL if name == PRIMARY else None
    pool = CredentialPool(get_secret=failing_get_secret)
    assert pool.names() == [PRIMARY]


# H. Secret non-disclosure ----------------------------------------------

def test_value_not_in_pool_or_credential_representations():
    _data, gs = make_store(
        {PRIMARY: SENTINEL, "gemini_api_key_2": SENTINEL},
        manifest=[PRIMARY, "gemini_api_key_2"],
    )
    pool = CredentialPool(get_secret=gs)
    cred = pool.get_next_available()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool.mark_unavailable(PRIMARY, now + timedelta(hours=1))

    blobs = [repr(pool), str(pool), repr(cred), str(cred),
             repr(pool.credentials()), repr(pool.names())]
    for blob in blobs:
        assert SENTINEL not in blob


def test_credential_object_holds_no_value_attribute():
    cred = Credential(PRIMARY)
    # Only name + cooldown_until; no field carries secret material.
    assert set(vars(cred)) == {"name", "cooldown_until"}


def test_exhaustion_and_missing_errors_contain_no_value():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _data, gs = make_store({PRIMARY: SENTINEL})
    pool = CredentialPool(get_secret=gs)
    pool.mark_unavailable(PRIMARY, now + timedelta(hours=1))
    with pytest.raises(CredentialsExhausted) as exc:
        pool.get_next_available(now=now)
    assert SENTINEL not in str(exc.value)
