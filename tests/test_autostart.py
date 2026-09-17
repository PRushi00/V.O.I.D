"""Tests for silent login autostart (void.runtime.autostart). Uses a fake
dict-backed registry so nothing touches the real HKCU Run key. Covers
milestone item 15: the background runtime can be registered to start
without a terminal."""
from __future__ import annotations

from void.runtime import autostart


class FakeRegistry:
    def __init__(self):
        self.values: dict[str, str] = {}

    def set_value(self, name, value):
        self.values[name] = value

    def get_value(self, name):
        return self.values.get(name)

    def delete_value(self, name):
        return self.values.pop(name, None) is not None


def test_default_launch_command_uses_pythonw_and_absolute_voice_launcher():
    cmd = autostart.default_launch_command(
        pythonw=r"C:\x\pythonw.exe", launcher=r"C:\x\voice_startup.py")
    assert cmd == r'"C:\x\pythonw.exe" "C:\x\voice_startup.py"'
    assert "voice_startup.py" in cmd
    assert "singularity" not in cmd and " app" not in cmd


def test_default_pythonw_path_is_a_string():
    p = autostart.default_pythonw_path()
    assert isinstance(p, str) and p


def test_install_writes_run_key_then_status_reports_it():
    reg = FakeRegistry()
    cmd = autostart.install(command=r'"py.exe" -m void app', backend=reg)
    assert cmd == r'"py.exe" -m void app'
    assert autostart.status(backend=reg) == r'"py.exe" -m void app'
    assert reg.values[autostart.VALUE_NAME] == r'"py.exe" -m void app'


def test_install_defaults_to_generated_command():
    reg = FakeRegistry()
    cmd = autostart.install(backend=reg)
    assert autostart.voice_startup_path() in cmd
    assert "singularity" not in cmd
    assert autostart.status(backend=reg) == cmd


def test_status_none_when_not_installed():
    reg = FakeRegistry()
    assert autostart.status(backend=reg) is None


def test_remove_reports_true_then_false_and_clears_status():
    reg = FakeRegistry()
    autostart.install(backend=reg)
    assert autostart.remove(backend=reg) is True
    assert autostart.status(backend=reg) is None
    assert autostart.remove(backend=reg) is False   # already gone


def test_custom_value_name_isolated():
    reg = FakeRegistry()
    autostart.install(command="a", backend=reg, value_name="VOID_TEST")
    assert autostart.status(backend=reg, value_name="VOID_TEST") == "a"
    assert autostart.status(backend=reg) is None     # default name untouched
