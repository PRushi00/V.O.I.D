"""Tests for HMAC request signing and replay/rate abuse protection."""
from void.device import auth


def test_sign_and_verify_round_trip():
    sig = auth.sign("secret123", b'{"a":1}')
    assert auth.verify("secret123", b'{"a":1}', sig)


def test_verify_fails_on_wrong_secret():
    sig = auth.sign("secret123", b'{"a":1}')
    assert not auth.verify("wrong-secret", b'{"a":1}', sig)


def test_verify_fails_on_tampered_body():
    sig = auth.sign("secret123", b'{"a":1}')
    assert not auth.verify("secret123", b'{"a":2}', sig)


def test_verify_fails_on_missing_signature():
    assert not auth.verify("secret123", b'{"a":1}', "")
    assert not auth.verify("secret123", b'{"a":1}', None)


def test_replay_guard_allows_first_use_then_rejects_replay():
    guard = auth.ReplayGuard()
    assert guard.check_and_record("dev-1", "req-1", now=1000.0) is True
    assert guard.check_and_record("dev-1", "req-1", now=1001.0) is False


def test_replay_guard_distinguishes_devices_and_ids():
    guard = auth.ReplayGuard()
    assert guard.check_and_record("dev-1", "req-1", now=1000.0) is True
    assert guard.check_and_record("dev-2", "req-1", now=1000.0) is True   # diff device
    assert guard.check_and_record("dev-1", "req-2", now=1000.0) is True   # diff id


def test_replay_guard_forgets_after_the_window():
    guard = auth.ReplayGuard(window_seconds=10)
    assert guard.check_and_record("dev-1", "req-1", now=1000.0) is True
    # Same id, well past the window: allowed again (and staleness would have
    # already rejected an actual replay attempt at the protocol layer).
    assert guard.check_and_record("dev-1", "req-1", now=1011.0) is True


def test_rate_limiter_allows_up_to_the_cap_then_denies():
    limiter = auth.RateLimiter(max_events=3, per_seconds=60)
    now = 1000.0
    assert limiter.allow("dev-1", now=now) is True
    assert limiter.allow("dev-1", now=now) is True
    assert limiter.allow("dev-1", now=now) is True
    assert limiter.allow("dev-1", now=now) is False


def test_rate_limiter_recovers_after_the_window_slides():
    limiter = auth.RateLimiter(max_events=1, per_seconds=10)
    assert limiter.allow("dev-1", now=1000.0) is True
    assert limiter.allow("dev-1", now=1005.0) is False
    assert limiter.allow("dev-1", now=1011.0) is True


def test_rate_limiter_keys_are_independent():
    limiter = auth.RateLimiter(max_events=1, per_seconds=60)
    assert limiter.allow("dev-1", now=1000.0) is True
    assert limiter.allow("dev-2", now=1000.0) is True


def test_is_stale_within_skew_is_not_stale():
    assert auth.is_stale(1000.0, now=1000.0 + auth.MAX_CLOCK_SKEW_SECONDS - 1) is False


def test_is_stale_beyond_skew_is_stale():
    assert auth.is_stale(1000.0, now=1000.0 + auth.MAX_CLOCK_SKEW_SECONDS + 1) is True


def test_is_stale_future_timestamp_also_rejected():
    assert auth.is_stale(1000.0, now=1000.0 - auth.MAX_CLOCK_SKEW_SECONDS - 1) is True
