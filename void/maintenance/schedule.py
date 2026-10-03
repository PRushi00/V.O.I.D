"""Registering the weekly pass with Windows Task Scheduler.

This is a *trigger*, not a scheduler. Everything that makes the weekly pass correct already lives in
SQL: :meth:`void.maintenance.Maintenance.due` requires it to be Sunday, the week is claimed with a
UNIQUE insert so two firings cannot produce two passes, and a crashed claim is released after an
hour. So this module's entire job is to make Windows ask - and because the database is authoritative,
asking too often is harmless and asking at the wrong time does nothing.

**Why a separate task rather than the voice runtime's.**
:mod:`void.runtime.scheduled_task` already registers a per-user task, and reusing it was considered
and rejected: its triggers (at logon, at unlock, re-check every 15 minutes) and its description are
specific to keeping a long-lived voice process alive, and the weekly pass needs none of them.
Registering maintenance under that task would have meant either a misleading description on the
voice task or a second action smuggled into it. Two tasks with honest descriptions is the correct
answer, and the voice task is not touched.

**The trigger.** Weekly, Sunday, with Task Scheduler's own repetition for the rest of that day::

    weekly(Sunday) at MAINTENANCE_HOUR, repeating every REPEAT_INTERVAL for REPEAT_DURATION

A single Sunday-morning firing would be missed by anyone whose machine is off at that hour, and
``StartWhenAvailable`` does not rescue it: Windows would run the missed task on Monday, where
``due()`` correctly answers "today is not Sunday" and nothing happens. Repeating through Sunday is
what actually closes that gap - the pass runs at the first firing after the machine is on, whenever
during Sunday that is. Non-Sunday days get no firing at all, so the robustness costs nothing.

A machine that is off for the whole of Sunday still misses that week. That is a deliberate,
documented consequence of the requirement being *Sunday* maintenance rather than *weekly-ish*
maintenance: relaxing it belongs in ``due()``, as an owner decision, not here.

**Authority.** One per-user task, least-privileged (``RunLevel`` LUA), run with the interactive
user's own token so no password is stored, running one existing CLI command. It grants no permission,
reads no credential, and never touches RiskGate, the kill switch or any tool boundary. The action it
registers cannot do more than ``python -m void maintenance run`` can do when the owner types it.
"""
from __future__ import annotations

from pathlib import Path

from void.maintenance.launcher import COMMAND
from void.runtime.autostart import default_pythonw_path

TASK_NAME = "VOID_SundayMaintenance"
#: The Task Scheduler root folder - a single backslash. Spelled with ``chr`` so that no escaping
#: question arises for anyone reading or editing this line.
TASK_FOLDER = chr(92)
TASK_DESCRIPTION = (
    "V.O.I.D weekly system pass: looks at installed applications, devices and system settings on "
    "Sunday and records what changed. Metadata only - no file contents, messages or credentials. "
    "Runs as the logged-in user without elevation; harmless if it fires more than once."
)

#: Local hour the Sunday trigger starts at. Late morning rather than 03:00 on purpose: a desktop that
#: is asleep overnight would miss an early trigger every single week, and the pass is cheap enough
#: that running it while the owner is using the machine is not disruptive.
MAINTENANCE_HOUR = 11
#: Re-ask through the rest of Sunday. Each extra firing is a no-op once the week has been claimed.
REPEAT_INTERVAL = "PT2H"
REPEAT_DURATION = "P1D"

# Task Scheduler 2.0 COM enum values. Verified against the XML of a real task registered on this
# machine (``schtasks /Query /XML``) rather than copied from documentation - the same standard the
# voice task's constants were held to.
_TASK_TRIGGER_WEEKLY = 3
_SUNDAY = 0x01                      # DaysOfWeek bitmask -> <DaysOfWeek><Sunday /></DaysOfWeek>
_EVERY_WEEK = 1                     # WeeksInterval
_TASK_ACTION_EXEC = 0
_TASK_INSTANCES_IGNORE_NEW = 2      # a second firing while one runs is dropped by Windows too
_TASK_LOGON_INTERACTIVE_TOKEN = 3   # never a stored password
_TASK_RUNLEVEL_LUA = 0              # least-privileged: no elevation
_TASK_CREATE_OR_UPDATE = 6
#: An hour is far longer than a real pass (measured in seconds) and short enough that a wedged run
#: cannot sit in the task list forever. The database's own stale-claim release is the real recovery.
_EXECUTION_TIME_LIMIT = "PT1H"


def launcher_path() -> str:
    """Absolute path to the console-less launcher the task runs."""
    return str(Path(__file__).resolve().with_name("launcher.py"))


def default_maintenance_action(pythonw: str | None = None,
                               launcher: str | None = None) -> tuple[str, str]:
    """The (executable, quoted-argument) pair the task action runs.

    ``pythonw.exe`` so a Sunday firing never flashes a console window over whatever the owner is
    doing, and an absolute launcher so the action does not depend on a working directory.
    """
    exe = pythonw or default_pythonw_path()
    script = launcher or launcher_path()
    return exe, '"' + script + '"'


def start_boundary(hour: int = MAINTENANCE_HOUR) -> str:
    """A fixed past Sunday at ``hour`` local time.

    Task Scheduler reads a weekly trigger's StartBoundary for its time-of-day and day alignment, not
    as "first run"; a boundary in the past means the schedule is already live. 2024-01-07 was a
    Sunday - the date is only there to make the local time unambiguous.
    """
    return "2024-01-07T{:02d}:00:00".format(int(hour) % 24)


class _TaskSchedulerBackend:
    """Real Task Scheduler backend. ``win32com`` is imported lazily so importing this module never
    requires pywin32 (tests inject a fake, and a non-Windows host can still import it)."""

    def _connect(self):
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        return scheduler

    def register(self, name: str, exe: str, args: str, description: str) -> None:
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        task_def = scheduler.NewTask(0)
        task_def.RegistrationInfo.Description = description

        trigger = task_def.Triggers.Create(_TASK_TRIGGER_WEEKLY)
        trigger.StartBoundary = start_boundary()
        trigger.DaysOfWeek = _SUNDAY
        trigger.WeeksInterval = _EVERY_WEEK
        # See the module docstring: repeating through Sunday is what makes the pass survive a machine
        # that happened to be switched off at the nominal hour.
        trigger.Repetition.Interval = REPEAT_INTERVAL
        trigger.Repetition.Duration = REPEAT_DURATION
        trigger.Enabled = True

        action = task_def.Actions.Create(_TASK_ACTION_EXEC)
        action.Path = exe
        action.Arguments = args

        settings = task_def.Settings
        settings.Enabled = True
        # A Sunday the machine slept through should still run the pass when it wakes - and if that
        # wake is Monday, due() declines, which is the intended behaviour rather than a failure.
        settings.StartWhenAvailable = True
        settings.DisallowStartIfOnBatteries = False
        settings.StopIfGoingOnBatteries = False
        settings.MultipleInstances = _TASK_INSTANCES_IGNORE_NEW
        settings.ExecutionTimeLimit = _EXECUTION_TIME_LIMIT

        principal = task_def.Principal
        principal.LogonType = _TASK_LOGON_INTERACTIVE_TOKEN
        principal.RunLevel = _TASK_RUNLEVEL_LUA

        folder.RegisterTaskDefinition(
            name, task_def, _TASK_CREATE_OR_UPDATE, "", "", _TASK_LOGON_INTERACTIVE_TOKEN)

    def query(self, name: str) -> dict | None:
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        try:
            task = folder.GetTask(name)
        except Exception:                                   # noqa: BLE001 - absent is not an error
            return None
        definition = task.Definition
        action = definition.Actions.Item(1)
        return {
            "path": action.Path,
            "arguments": action.Arguments,
            "enabled": bool(definition.Settings.Enabled),
            "description": definition.RegistrationInfo.Description or "",
            "elevated": int(definition.Principal.RunLevel) != _TASK_RUNLEVEL_LUA,
        }

    def delete(self, name: str) -> bool:
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        try:
            folder.DeleteTask(name, 0)
            return True
        except Exception:                                   # noqa: BLE001
            return False


def install(*, pythonw: str | None = None, launcher: str | None = None,
            backend=None, task_name: str = TASK_NAME) -> str:
    """Register (or re-register - idempotent) the weekly task. Returns the command it will run."""
    backend = backend or _TaskSchedulerBackend()
    exe, args = default_maintenance_action(pythonw, launcher)
    backend.register(task_name, exe, args, TASK_DESCRIPTION)
    return '"' + exe + '" ' + args


def remove(*, backend=None, task_name: str = TASK_NAME) -> bool:
    """Remove the task. True if one was present and removed."""
    backend = backend or _TaskSchedulerBackend()
    return backend.delete(task_name)


def status(*, backend=None, task_name: str = TASK_NAME) -> dict | None:
    """What is registered, or None if nothing is.

    Returns the whole record rather than a command string: "is it enabled?" and "is it elevated?" are
    the questions worth asking about a task that runs unattended.
    """
    backend = backend or _TaskSchedulerBackend()
    info = backend.query(task_name)
    if info is None:
        return None
    command = '"' + str(info.get("path", "")) + '" ' + str(info.get("arguments", ""))
    return {"command": command.strip(),
            "enabled": bool(info.get("enabled", False)),
            "elevated": bool(info.get("elevated", False)),
            "description": str(info.get("description", "")),
            "runs_maintenance": _runs_maintenance(info)}


def _runs_maintenance(info: dict) -> bool:
    """Does the registered action actually point at the maintenance launcher?

    Worth checking separately from "a task exists": a task left over from an earlier layout, or one
    whose name was reused, would otherwise be reported as working scheduling when it runs something
    else entirely.
    """
    argument = str(info.get("arguments", "")).lower()
    return "launcher.py" in argument and "maintenance" in argument
