"""Tests for the file-backed pairing window (cross-process by design: see
void/device/pairing.py's docstring - `pair-start` and `device serve` are
ordinarily separate process invocations sharing state only via this file,
the same pattern void.core.kill_switch already uses for its stop file)."""
import pytest

from void.device.pairing import PairingError, PairingManager


def test_begin_then_redeem_happy_path(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    token = mgr.begin("My Phone", now=1000.0)
    name = mgr.redeem(token.token, now=1001.0)
    assert name == "My Phone"


def test_redeem_is_single_use(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    token = mgr.begin("My Phone", now=1000.0)
    mgr.redeem(token.token, now=1001.0)
    with pytest.raises(PairingError):
        mgr.redeem(token.token, now=1002.0)


def test_redeem_wrong_token_rejected_and_window_stays_open(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    token = mgr.begin("My Phone", now=1000.0)
    with pytest.raises(PairingError):
        mgr.redeem("totally-wrong", now=1001.0)
    # The real token still works - a wrong guess doesn't burn the window.
    assert mgr.redeem(token.token, now=1002.0) == "My Phone"


def test_redeem_expired_token_rejected(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=60)
    token = mgr.begin("My Phone", now=1000.0)
    with pytest.raises(PairingError):
        mgr.redeem(token.token, now=1000.0 + 61)


def test_redeem_with_no_window_open_rejected(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    with pytest.raises(PairingError):
        mgr.redeem("anything", now=1000.0)


def test_begin_replaces_a_prior_unused_token(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    first = mgr.begin("First", now=1000.0)
    mgr.begin("Second", now=1001.0)
    with pytest.raises(PairingError):
        mgr.redeem(first.token, now=1002.0)


def test_cancel_closes_the_window(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    token = mgr.begin("My Phone", now=1000.0)
    mgr.cancel()
    with pytest.raises(PairingError):
        mgr.redeem(token.token, now=1001.0)


def test_state_is_visible_across_separate_manager_instances(tmp_path):
    """The whole point of file-backing: a `pair-start` CLI process and a
    `device serve` server process are different Python processes, modeled
    here as two independent PairingManager objects over the same directory."""
    starter = PairingManager(tmp_path, window_seconds=300)
    server = PairingManager(tmp_path, window_seconds=300)
    token = starter.begin("My Phone", now=1000.0)
    assert server.redeem(token.token, now=1001.0) == "My Phone"
