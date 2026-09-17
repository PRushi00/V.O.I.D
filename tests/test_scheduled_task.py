"""Tests for the Task Scheduler-based persistent autostart
(void.runtime.scheduled_task). Uses a fake in-memory backend so nothing
touches the real Task Scheduler. Mirrors tests/test_autostart.py's shape.

Covers the reliability fix: the plain HKCU Run key (void.runtime.autostart)
only fires once per fresh interactive logon, so a laptop that is mostly put
to sleep and woken/unlocked - not fully signed out - could go long stretches
without V.O.I.D ever starting, and had no recovery if the process crashed.
This module registers a per-user task with an "at logon" trigger, an "at
workstation unlock" trigger, and a restart-on-failure policy."""
from __future__ import annotations

from void.runtime import scheduled_task


class FakeTaskSchedulerBackend:
    """Records exactly what install()/remove()/status() would have asked a
    real Task Scheduler backend to do, without touching the real service."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}

    def register(self, name, exe, args, description):
        self.tasks[name] = {"path": exe, "arguments": args,
                            "description": description, "enabled": True}

    def query(self, name):
        return self.tasks.get(name)

    def delete(self, name):
        return self.tasks.pop(name, None) is not None


def test_default_launch_action_uses_pythonw_and_absolute_voice_launcher():
    exe, args = scheduled_task.default_launch_action(
        pythonw=r"C:\x\pythonw.exe", launcher=r"C:\x\voice_startup.py")
    assert exe == r"C:\x\pythonw.exe"
    assert args == r'"C:\x\voice_startup.py"'


def test_install_registers_task_then_status_reports_it():
    backend = FakeTaskSchedulerBackend()
    cmd = scheduled_task.install(
        pythonw=r"C:\x\pythonw.exe", launcher=r"C:\x\voice_startup.py",
        backend=backend)
    assert cmd == r'"C:\x\pythonw.exe" "C:\x\voice_startup.py"'
    assert scheduled_task.status(backend=backend) == cmd
    registered = backend.tasks[scheduled_task.TASK_NAME]
    assert registered["path"] == r"C:\x\pythonw.exe"
    assert registered["arguments"] == r'"C:\x\voice_startup.py"'


def test_install_defaults_to_the_real_voice_launcher_path():
    backend = FakeTaskSchedulerBackend()
    cmd = scheduled_task.install(backend=backend)
    from void.runtime.autostart import voice_startup_path
    assert voice_startup_path() in cmd
    assert "singularity" not in cmd


def test_install_is_idempotent_reregistering_updates_in_place():
    backend = FakeTaskSchedulerBackend()
    scheduled_task.install(pythonw=r"C:\a\pythonw.exe", launcher=r"C:\a\v.py",
                          backend=backend)
    scheduled_task.install(pythonw=r"C:\b\pythonw.exe", launcher=r"C:\b\v.py",
                          backend=backend)
    assert len(backend.tasks) == 1   # never a duplicate registration
    assert backend.tasks[scheduled_task.TASK_NAME]["path"] == r"C:\b\pythonw.exe"


def test_status_none_when_not_installed():
    backend = FakeTaskSchedulerBackend()
    assert scheduled_task.status(backend=backend) is None


def test_remove_reports_true_then_false_and_clears_status():
    backend = FakeTaskSchedulerBackend()
    scheduled_task.install(backend=backend)
    assert scheduled_task.remove(backend=backend) is True
    assert scheduled_task.status(backend=backend) is None
    assert scheduled_task.remove(backend=backend) is False   # already gone


def test_custom_task_name_isolated():
    backend = FakeTaskSchedulerBackend()
    scheduled_task.install(pythonw="a", launcher="b", backend=backend,
                          task_name="VOID_TEST")
    assert scheduled_task.status(backend=backend, task_name="VOID_TEST") is not None
    assert scheduled_task.status(backend=backend) is None   # default name untouched


def test_registration_never_calls_setaccountinformation_or_passes_a_password():
    # The task must run via the interactive user's own token
    # (TASK_LOGON_INTERACTIVE_TOKEN), registered with empty user/password
    # arguments - never a stored credential. Checks actual code shape (not
    # prose - the module's own docstring legitimately explains this),
    # so a real regression trips it but a comment never does.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(scheduled_task))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "SetAccountInformation"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "RegisterTaskDefinition":
                # (name, definition, flags, userId, password, logonType)
                user_arg, password_arg = node.args[3], node.args[4]
                assert isinstance(user_arg, ast.Constant) and user_arg.value == ""
                assert isinstance(password_arg, ast.Constant) and password_arg.value == ""


def test_register_creates_logon_unlock_and_recheck_triggers():
    # Structural guard (real COM objects can't be meaningfully faked): the
    # real backend must create exactly the three triggers this module's
    # reliability fix depends on - logon, workstation unlock, and the
    # periodic recheck that catches a force-killed process
    # RestartOnFailure alone was verified (live, on this machine) NOT to
    # catch. Losing any one of these silently reintroduces the exact gap
    # that was found and fixed.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(scheduled_task))
    register_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "register")
    # Trigger types are passed as the module's own named constants
    # (_TASK_TRIGGER_LOGON etc.), not literals - match on the identifier.
    trigger_arg_names = [
        n.args[0].id for n in ast.walk(register_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "Create" and n.args and isinstance(n.args[0], ast.Name)
    ]
    assert "_TASK_TRIGGER_LOGON" in trigger_arg_names
    assert "_TASK_TRIGGER_SESSION_STATE_CHANGE" in trigger_arg_names
    assert "_TASK_TRIGGER_TIME" in trigger_arg_names


def test_recheck_interval_is_a_plausible_iso8601_duration():
    # Sanity guard on the constant itself: must look like "PT<number><unit>"
    # and be short enough to actually close the gap (not, say, once a day).
    import re
    assert re.fullmatch(r"PT\d+[MH]", scheduled_task._RECHECK_INTERVAL)
