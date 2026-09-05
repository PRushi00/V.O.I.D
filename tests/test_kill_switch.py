"""Tests for the emergency kill switch."""
import pytest

from void.core.kill_switch import KillSwitch, StopRequested


def test_engage_and_raise():
    ks = KillSwitch()
    assert not ks.engaged
    ks.raise_if_engaged()  # no-op
    assert ks.engage()
    assert ks.engaged
    with pytest.raises(StopRequested):
        ks.raise_if_engaged()


def test_reset():
    ks = KillSwitch()
    ks.engage()
    ks.reset()
    assert not ks.engaged


def test_handle_command_phrase_match():
    ks = KillSwitch(phrase="VOID, STOP EVERYTHING")
    assert not ks.handle_command("hello")
    assert ks.handle_command("void, stop everything")  # case-insensitive
    assert ks.engaged


def test_file_based_stop_cross_process(tmp_path):
    stop_file = tmp_path / "STOP"
    ks = KillSwitch(stop_file=stop_file)
    assert not ks.engaged
    # Simulate another process writing the stop file.
    stop_file.write_text("stop from elsewhere")
    assert ks.engaged
    assert "elsewhere" in (ks.reason or "")
    ks.reset()
    assert not stop_file.exists()


def test_engage_writes_stop_file(tmp_path):
    stop_file = tmp_path / "STOP"
    ks = KillSwitch(stop_file=stop_file)
    ks.engage(reason="button")
    assert stop_file.exists()


def test_pin_auth(monkeypatch):
    ks = KillSwitch(require_pin=True)
    # Patch the stored PIN.
    from void.security import secrets
    monkeypatch.setattr(secrets, "get_secret", lambda key: "1234")
    assert not ks.engage(pin="wrong")
    assert not ks.engaged
    assert ks.engage(pin="1234")
    assert ks.engaged
