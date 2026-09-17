"""Tests for the paired-device registry. A fake in-memory keyring stands in
for the OS secret store - identical pattern to tests/test_cli.py - so no
real credential is ever touched."""
import pytest

from void.device.identity import DeviceRegistry


@pytest.fixture
def fake_keyring():
    data = {}
    return {
        "get": lambda k: data.get(k),
        "set": lambda k, v: data.__setitem__(k, v),
        "delete": lambda k: (data.pop(k, None) is not None) if True else False,
        "data": data,
    }


@pytest.fixture
def registry(tmp_path, fake_keyring):
    def _delete(k):
        existed = k in fake_keyring["data"]
        fake_keyring["data"].pop(k, None)
        return existed

    return DeviceRegistry(
        tmp_path / "devices.json",
        get_secret=fake_keyring["get"],
        set_secret=fake_keyring["set"],
        delete_secret=_delete,
    ), fake_keyring


def test_pair_creates_device_with_default_capability_and_a_secret(registry):
    reg, kr = registry
    device, secret = reg.pair(name="Test Phone")
    assert device.name == "Test Phone"
    assert device.capabilities == ["get_status"]
    assert secret and isinstance(secret, str)
    assert kr["data"][f"device_secret:{device.device_id}"] == secret


def test_pair_never_stores_the_secret_value_on_the_device_record(registry):
    reg, _ = registry
    device, secret = reg.pair(name="Test Phone")
    assert secret not in device.to_dict().values()
    assert "secret" not in device.to_dict()


def test_get_persists_across_a_fresh_registry_instance(tmp_path, fake_keyring):
    def _delete(k):
        existed = k in fake_keyring["data"]
        fake_keyring["data"].pop(k, None)
        return existed

    path = tmp_path / "devices.json"
    reg1 = DeviceRegistry(path, get_secret=fake_keyring["get"],
                          set_secret=fake_keyring["set"], delete_secret=_delete)
    device, secret = reg1.pair(name="Test Phone")

    reg2 = DeviceRegistry(path, get_secret=fake_keyring["get"],
                          set_secret=fake_keyring["set"], delete_secret=_delete)
    found = reg2.get(device.device_id)
    assert found is not None
    assert found.name == "Test Phone"
    assert reg2.get_secret_value(device.device_id) == secret


def test_grant_adds_a_capability_idempotently(registry):
    reg, _ = registry
    device, _ = reg.pair(name="Phone")
    reg.grant(device.device_id, "launch_app")
    reg.grant(device.device_id, "launch_app")  # idempotent
    assert reg.get(device.device_id).capabilities.count("launch_app") == 1


def test_grant_unknown_device_raises(registry):
    reg, _ = registry
    with pytest.raises(KeyError):
        reg.grant("nonexistent", "launch_app")


def test_revoke_capability_removes_it(registry):
    reg, _ = registry
    device, _ = reg.pair(name="Phone")
    reg.grant(device.device_id, "launch_app")
    reg.revoke_capability(device.device_id, "launch_app")
    assert "launch_app" not in reg.get(device.device_id).capabilities
    assert "get_status" in reg.get(device.device_id).capabilities


def test_forget_removes_device_and_deletes_secret(registry):
    reg, kr = registry
    device, _ = reg.pair(name="Phone")
    assert reg.forget(device.device_id) is True
    assert reg.get(device.device_id) is None
    assert kr["data"].get(f"device_secret:{device.device_id}") is None
    assert reg.get_secret_value(device.device_id) is None


def test_forget_unknown_device_returns_false(registry):
    reg, _ = registry
    assert reg.forget("nonexistent") is False


def test_list_orders_by_paired_at(registry, monkeypatch):
    import time as time_mod

    reg, _ = registry
    times = iter([100.0, 200.0, 50.0])
    monkeypatch.setattr(time_mod, "time", lambda: next(times))
    a, _ = reg.pair(name="A")
    b, _ = reg.pair(name="B")
    c, _ = reg.pair(name="C")
    ordered = [d.name for d in reg.list()]
    assert ordered == ["C", "A", "B"]


def test_touch_updates_last_seen(registry, monkeypatch):
    import time as time_mod

    reg, _ = registry
    device, _ = reg.pair(name="Phone")
    assert reg.get(device.device_id).last_seen is None
    monkeypatch.setattr(time_mod, "time", lambda: 999.0)
    reg.touch(device.device_id)
    assert reg.get(device.device_id).last_seen == 999.0
