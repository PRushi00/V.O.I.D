"""CLI wiring tests for `python -m void device ...` (owner-only pairing and
capability management). A fake in-memory keyring (same pattern as
tests/test_cli.py) backs secret storage; ``cli._device_state_dir`` is
monkeypatched to an isolated tmp_path so nothing touches the real
``~/.void``."""
import pytest

from void import cli
from void.device.identity import DeviceRegistry
from void.security import secrets


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
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
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_device_state_dir", lambda: tmp_path)
    return tmp_path


def test_pair_start_prints_a_token_and_opens_a_window(state_dir, capsys):
    rc = cli.cmd_device_pair_start("My Phone", 5.0)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Pairing token" in out
    assert "fingerprint" in out.lower()

    from void.device.pairing import PairingManager
    mgr = PairingManager(state_dir)
    # The token just printed must actually be redeemable.
    assert mgr.redeem(out.split("Pairing token:")[1].splitlines()[0].strip()) == "My Phone"


def _pair_start_fields(capsys, monkeypatch, ip_hint="10.192.243.47", name="My Android Phone"):
    """Run pair-start and parse each printed field's VALUE (not just its
    label - a label-only check passes even when the value after it is
    blank). Returns a dict; the token is returned so tests can test it, but
    no assertion below ever puts it in a failure message."""
    monkeypatch.setattr(cli, "_local_ip_hint", lambda: ip_hint)
    assert cli.cmd_device_pair_start(name, 5.0) == 0
    out = capsys.readouterr().out
    fields = {}
    for line in out.splitlines():
        for label, key in (("Pairing token:", "token"), ("Port:", "port"),
                          ("Certificate fingerprint:", "fingerprint"),
                          ("Device name:", "name"),
                          ("This laptop's address (best guess):", "address")):
            if line.strip().startswith(label):
                fields[key] = line.strip()[len(label):].strip()
    return fields


def test_pair_start_prints_a_nonblank_value_for_every_field(state_dir, capsys, monkeypatch):
    fields = _pair_start_fields(capsys, monkeypatch)
    # Booleans only: pytest echoes the operands of a failed `==`, which would
    # leak the pairing token into test output.
    assert bool(fields.get("token")), "pairing token value is blank"
    assert len(fields["token"].split()) == 1, "token line should hold exactly one value"
    assert bool(fields.get("fingerprint")), "fingerprint value is blank"
    assert bool(fields.get("port")), "port value is blank"
    assert fields["name"] == "My Android Phone"
    assert fields["address"] == "10.192.243.47"


def test_pair_start_fingerprint_is_the_real_certificates_sha256(state_dir, capsys, monkeypatch):
    import re

    from void.device import cert
    fields = _pair_start_fields(capsys, monkeypatch)
    printed = fields["fingerprint"]
    assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", printed) is not None
    # The very certificate the gateway will serve from this state dir.
    assert (printed == cert.fingerprint(state_dir / "device_cert.pem")) is True


def test_pair_start_token_is_the_one_stored_in_the_pairing_window(state_dir, capsys, monkeypatch):
    import json
    fields = _pair_start_fields(capsys, monkeypatch)
    stored = json.loads((state_dir / "pairing_window.json").read_text())["token"]
    assert len(fields["token"]) >= 8
    assert (fields["token"] == stored) is True


def test_pair_start_reports_the_running_gateways_actual_port(state_dir, capsys, monkeypatch):
    (state_dir / "gateway_address.json").write_text('{"port": 9123}')
    fields = _pair_start_fields(capsys, monkeypatch)
    assert fields["port"] == "9123"


def test_pair_start_falls_back_to_the_configured_port_when_no_gateway(state_dir, capsys, monkeypatch):
    fields = _pair_start_fields(capsys, monkeypatch)
    assert fields["port"].split()[0] == "8765"


def test_pair_start_without_an_address_guess_says_so_instead_of_blank(state_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_local_ip_hint", lambda: None)
    assert cli.cmd_device_pair_start("P", 5.0) == 0
    assert "Could not guess this laptop's address" in capsys.readouterr().out


def test_pair_start_never_touches_an_existing_device_registry(state_dir, capsys, monkeypatch):
    reg = DeviceRegistry(state_dir / "devices.json")
    device, _ = reg.pair(name="Existing Phone")
    before = (state_dir / "devices.json").read_bytes()
    _pair_start_fields(capsys, monkeypatch)
    assert (state_dir / "devices.json").read_bytes() == before
    assert reg.get(device.device_id) is not None


def test_list_reports_no_devices_initially(state_dir, capsys):
    rc = cli.cmd_device_list()
    assert rc == 0
    assert "No paired devices" in capsys.readouterr().out


def test_list_shows_a_paired_device(state_dir, capsys):
    reg = DeviceRegistry(state_dir / "devices.json")
    device, _ = reg.pair(name="My Phone")
    rc = cli.cmd_device_list()
    out = capsys.readouterr().out
    assert rc == 0
    assert device.device_id in out
    assert "My Phone" in out


def test_grant_unknown_capability_rejected(state_dir, capsys):
    reg = DeviceRegistry(state_dir / "devices.json")
    device, _ = reg.pair(name="My Phone")
    rc = cli.cmd_device_grant(device.device_id, "execute_shell")
    assert rc == 1
    assert "Unknown capability" in capsys.readouterr().out


def test_grant_and_revoke_known_capability(state_dir, capsys):
    reg = DeviceRegistry(state_dir / "devices.json")
    device, _ = reg.pair(name="My Phone")

    rc = cli.cmd_device_grant(device.device_id, "launch_app")
    assert rc == 0
    # cmd_device_grant opens its own DeviceRegistry (a separate process would
    # too) - re-read from disk rather than trusting this stale in-memory copy.
    reloaded = DeviceRegistry(state_dir / "devices.json")
    assert "launch_app" in reloaded.get(device.device_id).capabilities

    rc = cli.cmd_device_revoke(device.device_id, "launch_app")
    assert rc == 0
    reloaded = DeviceRegistry(state_dir / "devices.json")
    assert "launch_app" not in reloaded.get(device.device_id).capabilities


def test_grant_unknown_device_reports_error(state_dir, capsys):
    rc = cli.cmd_device_grant("nonexistent", "launch_app")
    assert rc == 1
    assert "Unknown device" in capsys.readouterr().out


def test_forget_removes_the_device_and_its_secret(state_dir, capsys):
    reg = DeviceRegistry(state_dir / "devices.json")
    device, secret = reg.pair(name="My Phone")
    assert secrets.get_secret(f"device_secret:{device.device_id}") == secret

    rc = cli.cmd_device_forget(device.device_id)
    assert rc == 0
    assert "Unpaired" in capsys.readouterr().out
    assert secrets.get_secret(f"device_secret:{device.device_id}") is None
    fresh = DeviceRegistry(state_dir / "devices.json")
    assert fresh.get(device.device_id) is None


def test_forget_unknown_device_reports_not_found(state_dir, capsys):
    rc = cli.cmd_device_forget("nonexistent")
    assert rc == 0
    assert "No such paired device" in capsys.readouterr().out


def test_argparse_wires_device_subcommands():
    parser = cli.build_parser()
    args = parser.parse_args(["device", "pair-start", "--name", "X", "--minutes", "2"])
    assert args.command == "device"
    assert args.device_action == "pair-start"
    assert args.name == "X"
    assert args.minutes == 2.0

    args = parser.parse_args(["device", "grant", "dev123", "launch_app"])
    assert args.device_action == "grant"
    assert args.device_id == "dev123"
    assert args.capability == "launch_app"


def test_main_dispatches_device_list(monkeypatch, state_dir, capsys):
    calls = []
    monkeypatch.setattr(cli, "cmd_device_list", lambda: calls.append(1) or 0)
    rc = cli.main(["device", "list"])
    assert rc == 0
    assert calls == [1]
