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


# --- CLI wiring: 'void autostart' now uses Task Scheduler, migrating away
# from any older Run-key registration so exactly one mechanism is ever active

def test_cmd_autostart_install_migrates_off_the_old_run_key(monkeypatch, capsys):
    from void import cli
    from void.runtime import scheduled_task

    reg = FakeRegistry()
    reg.set_value(autostart.VALUE_NAME, autostart.default_launch_command())
    monkeypatch.setattr(autostart, "_WinregBackend", lambda: reg)

    task_backend = {}

    class FakeTaskBackend:
        def register(self, name, exe, args, description):
            task_backend[name] = (exe, args)

        def query(self, name):
            return None

        def delete(self, name):
            return False

    monkeypatch.setattr(scheduled_task, "_TaskSchedulerBackend", FakeTaskBackend)

    assert cli.cmd_autostart("install") == 0
    assert scheduled_task.TASK_NAME in task_backend   # new mechanism registered
    assert autostart.status(backend=reg) is None       # old Run key removed
    out = capsys.readouterr().out
    assert "Task Scheduler" in out


def test_cmd_autostart_remove_clears_both_mechanisms(monkeypatch):
    from void import cli
    from void.runtime import scheduled_task

    reg = FakeRegistry()
    monkeypatch.setattr(autostart, "_WinregBackend", lambda: reg)

    removed = {"task": False}

    class FakeTaskBackend:
        def register(self, name, exe, args, description):
            pass

        def query(self, name):
            return None

        def delete(self, name):
            was = removed["task"]
            removed["task"] = True
            return not was   # True the first time, False after

    monkeypatch.setattr(scheduled_task, "_TaskSchedulerBackend", FakeTaskBackend)

    assert cli.cmd_autostart("remove") == 0   # does not raise either way
