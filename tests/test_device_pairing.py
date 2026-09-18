"""Tests for the file-backed pairing window (cross-process by design: see
void/device/pairing.py's docstring - `pair-start` and `device serve` are
ordinarily separate process invocations sharing state only via this file,
the same pattern void.core.kill_switch already uses for its stop file)."""
import json
import logging

import pytest

from void.device import pairing as pairing_mod
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


# --- rejection reason codes (diagnostics only - see void/device/gateway.py) --
#
# Real field bug this closes: every PairingError used to log identically
# ("reason=invalid_token"), so a genuine "the gateway never saw a pairing
# window at all" failure (two processes resolving different state
# directories, a permissions problem, a corrupt write) was indistinguishable
# in the log from an ordinary wrong guess or an expired token - completely
# undiagnosable after the fact. These reason codes never change what a
# network caller sees (still a generic invalid_pairing_token - see
# tests/test_device_gateway.py), only what ends up in the log.

def test_no_window_open_has_the_no_window_reason(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    with pytest.raises(PairingError) as exc:
        mgr.redeem("anything", now=1000.0)
    assert exc.value.reason == pairing_mod.NO_WINDOW


def test_expired_token_has_the_expired_reason(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=60)
    token = mgr.begin("My Phone", now=1000.0)
    with pytest.raises(PairingError) as exc:
        mgr.redeem(token.token, now=1000.0 + 61)
    assert exc.value.reason == pairing_mod.EXPIRED


def test_wrong_token_has_the_wrong_token_reason(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    mgr.begin("My Phone", now=1000.0)
    with pytest.raises(PairingError) as exc:
        mgr.redeem("not-the-real-token", now=1001.0)
    assert exc.value.reason == pairing_mod.WRONG_TOKEN


# --- atomicity + diagnosability of the shared file ------------------------

def test_written_file_is_never_left_partial_or_temp(tmp_path):
    mgr = PairingManager(tmp_path, window_seconds=300)
    mgr.begin("My Phone", now=1000.0)
    files = sorted(p.name for p in tmp_path.iterdir())
    # Exactly the final file - no leftover .tmp<pid> file from the atomic
    # write (os.replace always cleans it up, success or not).
    assert files == ["pairing_window.json"]
    # And its content is valid, complete JSON - never partially written.
    data = json.loads((tmp_path / "pairing_window.json").read_text())
    assert set(data) == {"token", "name", "expires_at"}


def test_corrupt_file_is_treated_as_no_window_but_logged(tmp_path, caplog):
    (tmp_path / "pairing_window.json").write_text("{not valid json", encoding="utf-8")
    mgr = PairingManager(tmp_path, window_seconds=300)
    with caplog.at_level(logging.WARNING, logger="void.device.pairing"):
        with pytest.raises(PairingError) as exc:
            mgr.redeem("anything", now=1000.0)
    assert exc.value.reason == pairing_mod.NO_WINDOW
    assert any("PAIRING_FILE_UNREADABLE" in r.message for r in caplog.records)
    # The corrupt content itself is never echoed into the log.
    assert not any("not valid json" in r.message for r in caplog.records)


def test_unexpected_read_error_is_logged_distinctly_from_a_genuine_absence(
        tmp_path, monkeypatch, caplog):
    # Simulates a real-world failure that is NOT "no pairing in progress" -
    # e.g. a permissions error - and proves it is now distinguishable in the
    # log from an ordinary absent/corrupt window instead of collapsing into
    # the same generic outcome.
    mgr = PairingManager(tmp_path, window_seconds=300)
    mgr.begin("My Phone", now=1000.0)

    real_read_text = pairing_mod.Path.read_text

    def _boom(self, *a, **k):
        if self.name == "pairing_window.json":
            raise PermissionError("simulated access denial")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(pairing_mod.Path, "read_text", _boom)
    with caplog.at_level(logging.WARNING, logger="void.device.pairing"):
        with pytest.raises(PairingError) as exc:
            mgr.redeem("anything", now=1001.0)
    assert exc.value.reason == pairing_mod.NO_WINDOW
    assert any("PAIRING_FILE_READ_FAILED" in r.message and "PermissionError" in r.message
              for r in caplog.records)
