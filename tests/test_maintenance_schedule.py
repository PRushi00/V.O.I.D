"""The Windows trigger for the weekly pass: a trigger, and provably nothing more.

The thing most worth protecting here is the division of responsibility. Windows decides *when to ask*;
the database decides *whether anything happens*. If that line ever blurred - if the task carried
``--force``, or if this module grew its own idea of what week it is - then two firings could produce
two passes, which is exactly what the UNIQUE week claim exists to prevent. Several tests below assert
that separation directly rather than trusting it.

The second concern is honesty about authority. A task that runs unattended every Sunday is a standing
grant, so these tests pin down that it is per-user, unelevated, password-less, and that it runs one
existing CLI command and not a shell.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import re

import pytest

from void.maintenance import launcher, schedule


class FakeBackend:
    """In-memory Task Scheduler. Records calls so registration can be asserted without Windows."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.registrations: list[tuple] = []

    def register(self, name, exe, args, description):
        self.registrations.append((name, exe, args, description))
        self.tasks[name] = {"path": exe, "arguments": args, "enabled": True,
                            "description": description, "elevated": False}

    def query(self, name):
        return dict(self.tasks[name]) if name in self.tasks else None

    def delete(self, name):
        return self.tasks.pop(name, None) is not None


@pytest.fixture
def backend():
    return FakeBackend()


# --------------------------------------------------------------------------- registration

def test_installing_registers_one_task_that_runs_the_launcher(backend):
    command = schedule.install(backend=backend)
    assert len(backend.registrations) == 1
    name, exe, args, description = backend.registrations[0]
    assert name == schedule.TASK_NAME
    assert "pythonw" in exe.lower() or exe.lower().endswith("python.exe")
    assert "launcher.py" in args and args.startswith('"') and args.endswith('"')
    assert description == schedule.TASK_DESCRIPTION
    assert exe in command and "launcher.py" in command


def test_installing_twice_leaves_one_task(backend):
    """Re-running install must be a safe repair, not a way to accumulate duplicate triggers."""
    first = schedule.install(backend=backend)
    second = schedule.install(backend=backend)
    assert first == second
    assert len(backend.tasks) == 1


def test_the_registered_argument_is_an_absolute_path(backend):
    """Task Scheduler gives no dependable working directory - a relative launcher would simply fail."""
    schedule.install(backend=backend)
    _, _, args, _ = backend.registrations[0]
    path = args.strip('"')
    assert re.match(r"^[A-Za-z]:[\\/]", path), f"not absolute: {path!r}"
    assert path.endswith("launcher.py")


def test_a_custom_interpreter_and_launcher_are_honoured(backend):
    schedule.install(backend=backend, pythonw="X:/py/pythonw.exe", launcher="X:/void/launcher.py")
    _, exe, args, _ = backend.registrations[0]
    assert exe == "X:/py/pythonw.exe" and args == '"X:/void/launcher.py"'


# --------------------------------------------------------------------------- the voice task is untouched

def test_the_maintenance_task_is_not_the_voice_task():
    """Reusing the voice runtime's task would have meant a misleading description on it."""
    from void.runtime import scheduled_task

    assert schedule.TASK_NAME != scheduled_task.TASK_NAME
    assert schedule.TASK_DESCRIPTION != scheduled_task.TASK_DESCRIPTION


def test_the_description_describes_maintenance_and_promises_no_voice():
    """A standing scheduled task's description is the only explanation the owner gets in the Windows
    UI, so it has to be true: what it looks at, what it does not, and that it is unprivileged."""
    text = schedule.TASK_DESCRIPTION.lower()
    assert "weekly" in text and "sunday" in text
    assert "metadata only" in text
    assert "without elevation" in text
    for absent in ("voice", "microphone", "listen", "wake"):
        assert absent not in text


def test_the_schedule_module_does_not_touch_the_voice_task():
    source = inspect.getsource(schedule)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "void.runtime.scheduled_task" not in imported
    # The one runtime import is a read-only path helper, and nothing from void.voice is reachable.
    assert not any(name.startswith("void.voice") for name in imported)


# --------------------------------------------------------------------------- the database stays in charge

def test_the_scheduled_command_lets_the_database_decide():
    """``--force`` skips the Sunday check. A trigger that fired it every two hours through Sunday
    would be asking for the pass to run repeatedly, so the registered command must be the plain one.
    """
    assert launcher.COMMAND == ("maintenance", "run")
    assert "--force" not in launcher.COMMAND
    assert "--dry-run" not in launcher.COMMAND


def test_the_schedule_module_contains_no_idea_of_when_maintenance_is_due():
    """Weekly semantics live in SQL. A second opinion here is how a duplicate pass would appear."""
    referenced = _identifiers(schedule)
    for forbidden in ("week_key", "is_sunday", "due", "begin_run", "Maintenance", "StateStore",
                      "sqlite3", "datetime"):
        assert forbidden not in referenced, f"the schedule module references {forbidden!r}"


def test_the_launcher_contains_no_maintenance_logic():
    referenced = _identifiers(launcher)
    for forbidden in ("week_key", "is_sunday", "StateStore", "Maintenance", "sqlite3", "Scope",
                      "datetime"):
        assert forbidden not in referenced, f"the launcher references {forbidden!r}"


# --------------------------------------------------------------------------- the trigger

def test_the_trigger_fires_on_sunday_only():
    """``_SUNDAY`` is a bitmask Windows reads literally; 0x01 is Sunday and 0x02 would be Monday."""
    assert schedule._SUNDAY == 0x01
    assert schedule._EVERY_WEEK == 1


def test_the_start_boundary_is_a_sunday_at_the_configured_hour():
    moment = datetime.datetime.fromisoformat(schedule.start_boundary())
    assert moment.weekday() == 6, "2024-01-07 must be a Sunday or the trigger drifts a day"
    assert moment.hour == schedule.MAINTENANCE_HOUR
    assert datetime.datetime.fromisoformat(schedule.start_boundary(3)).hour == 3


def test_the_hour_is_not_the_middle_of_the_night():
    """An 03:00 trigger would be missed every week by a machine that sleeps overnight, and
    ``StartWhenAvailable`` cannot rescue it: Windows would retry on Monday, where due() declines."""
    assert 8 <= schedule.MAINTENANCE_HOUR <= 20


def test_the_trigger_repeats_through_sunday_but_not_beyond():
    """The repetition is what makes the pass survive a machine switched off at the nominal hour. It
    must not outlast the day, or firings would land on Monday where they can only be no-ops."""
    assert schedule.REPEAT_DURATION == "P1D"
    assert re.fullmatch(r"PT([1-9]|1[0-2])H", schedule.REPEAT_INTERVAL)


# --------------------------------------------------------------------------- authority

def test_the_task_asks_for_no_elevation_and_stores_no_password():
    assert schedule._TASK_RUNLEVEL_LUA == 0
    assert schedule._TASK_LOGON_INTERACTIVE_TOKEN == 3


def test_a_second_firing_while_one_runs_is_dropped_by_windows_too():
    """Belt and braces over the database's own claim: the cheapest place to stop a duplicate pass is
    before Python starts."""
    assert schedule._TASK_INSTANCES_IGNORE_NEW == 2


#: Identifiers that would mean this layer had grown authority it must not have.
FORBIDDEN_AUTHORITY = ("subprocess", "system", "eval", "exec", "RiskGate", "kill_switch",
                       "authorize", "Popen", "keyring", "winreg", "ctypes", "ShellExecute")


def test_the_schedule_module_grants_nothing_and_runs_no_shell():
    referenced = _identifiers(schedule)
    for forbidden in FORBIDDEN_AUTHORITY:
        assert forbidden not in referenced, f"the schedule module references {forbidden!r}"


def test_the_launcher_runs_no_shell_and_grants_nothing():
    referenced = _identifiers(launcher)
    for forbidden in FORBIDDEN_AUTHORITY:
        assert forbidden not in referenced, f"the launcher references {forbidden!r}"


# --------------------------------------------------------------------------- status and removal

def test_status_is_none_when_nothing_is_registered(backend):
    assert schedule.status(backend=backend) is None


def test_status_reports_the_command_and_the_authority(backend):
    schedule.install(backend=backend)
    info = schedule.status(backend=backend)
    assert info["enabled"] is True
    assert info["elevated"] is False
    assert "launcher.py" in info["command"]
    assert info["runs_maintenance"] is True


def test_a_task_under_this_name_that_runs_something_else_is_reported(backend):
    """Worse than no task, because the owner would believe scheduling was handled."""
    backend.register(schedule.TASK_NAME, "pythonw.exe", '"C:/other/thing.py"', "something else")
    assert schedule.status(backend=backend)["runs_maintenance"] is False


def test_removal_is_truthful_about_whether_anything_was_there(backend):
    assert schedule.remove(backend=backend) is False
    schedule.install(backend=backend)
    assert schedule.remove(backend=backend) is True
    assert schedule.status(backend=backend) is None


# --------------------------------------------------------------------------- the launcher

def test_the_launcher_delegates_to_the_existing_cli():
    seen = []

    def fake_cli(argv):
        seen.append(list(argv))
        return 0

    assert launcher.main(cli_main=fake_cli) == 0
    assert seen == [["maintenance", "run"]]


def test_a_crashing_run_becomes_an_exit_code_rather_than_a_traceback():
    """Task Scheduler records a "last result" number, not a stack trace. Raising out of the launcher
    would leave the owner with an opaque failure code and nothing to read."""
    def exploding(argv):
        raise RuntimeError("collector blew up")

    assert launcher.main(cli_main=exploding) == 1


def test_a_non_zero_cli_result_is_passed_through():
    assert launcher.main(cli_main=lambda argv: 3) == 3


def test_the_launcher_puts_the_repository_on_the_path():
    """The whole reason this file exists: Task Scheduler supplies no useful working directory."""
    import os
    from pathlib import Path

    root = Path(launcher.__file__).resolve().parents[2]
    assert (root / "void" / "__init__.py").is_file(), f"wrong repository root: {root}"
    assert os.path.isfile(schedule.launcher_path())


def _identifiers(module) -> set[str]:
    """Every name, attribute and imported module the source actually references.

    Scanning identifiers rather than source text, because text matching flags a module for its own
    honest prose. The first version of these tests asserted that ``schedule`` never mentions
    "credential" and failed on ``TASK_DESCRIPTION``, which promises the owner that the task reads no
    credentials - the scan matching the very sentence that makes the guarantee. Stripping docstrings
    was not enough: that sentence is a module constant, not a docstring.

    Identifiers are also the right question. A forbidden *string* cannot do anything; a forbidden
    *call* can. ``"subprocess"`` in a sentence is documentation, ``subprocess.run`` is behaviour.
    """
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(part for part in node.module.split("."))
            found.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.update(part for part in alias.name.split("."))
                if alias.asname:
                    found.add(alias.asname)
    return found
