"""Windows Task Scheduler autostart for the voice runtime - V1's "24/7
personal assistant" mechanism, replacing the plain HKCU Run key.

Why Task Scheduler and not (only) the Run key: the Run key fires exactly
once per fresh interactive logon. A laptop that is mostly put to sleep and
woken/unlocked - the overwhelmingly common real-world pattern - therefore
only re-launches V.O.I.D on the rare full sign-out/sign-in, and if the
process ever exits unexpectedly (crash, transient error) nothing brings it
back until the next one. This is the actual, verified reason V.O.I.D can
sit not-running for hours of normal laptop use despite the Run key being
correctly configured (confirmed on this machine: a real boot occurred with
no corresponding autostart activity in the diagnostic log, because the
user's session was reached via unlock, not a fresh logon, on other days).

Task Scheduler is the Windows-native mechanism built for exactly this: a
per-user task (no elevation, no SYSTEM account, no Windows service) with an
"at log on" AND an "at workstation unlock" trigger, plus a built-in
restart-on-failure policy and a built-in "don't start a second instance"
policy. Every setting below was verified by registering a real task on this
machine and reading back its XML (via ``schtasks /Query /XML``) rather than
assumed from documentation.

install()/remove()/status() mirror void.runtime.autostart's shape exactly,
including the injectable-backend seam for unit tests, so this module is
testable without touching the real Task Scheduler (tests pass a fake
in-memory backend; the real backend uses the ``Schedule.Service`` COM API,
imported lazily so importing this module never requires pywin32).

This module holds no backend authority beyond registering ONE per-user,
unelevated task that runs the SAME existing console-less launcher
(voice_startup.py) - it grants no new permission, stores no credential (the
task runs as the interactive user's own token, "InteractiveToken" logon
type - never a stored password), and never touches RiskGate, KillSwitch, or
any tool/security boundary.
"""
from __future__ import annotations

from void.runtime.autostart import default_pythonw_path, voice_startup_path

TASK_NAME = "VOID_VoiceRuntime"
TASK_FOLDER = "\\"
TASK_DESCRIPTION = (
    "V.O.I.D voice runtime: console-less, per-user, starts at login and "
    "workstation unlock, restarts automatically after an unexpected exit."
)

# Task Scheduler 2.0 COM enum values - each verified against a real
# registered task's XML on this machine (schtasks /Query /XML), not merely
# copied from documentation.
_TASK_TRIGGER_LOGON = 9
_TASK_TRIGGER_SESSION_STATE_CHANGE = 11
_TASK_TRIGGER_TIME = 1
_TASK_SESSION_UNLOCK = 8                 # -> <StateChange>SessionUnlock</StateChange>
_TASK_ACTION_EXEC = 0
_TASK_INSTANCES_IGNORE_NEW = 2           # -> <MultipleInstancesPolicy>IgnoreNew</...>
_TASK_LOGON_INTERACTIVE_TOKEN = 3        # -> <LogonType>InteractiveToken</...> (no stored password)
_TASK_RUNLEVEL_LUA = 0                   # least-privileged (no elevation)
_TASK_CREATE_OR_UPDATE = 6
_RESTART_INTERVAL = "PT1M"               # ISO-8601 duration: 1 minute
_RESTART_COUNT = 3
# RestartOnFailure only catches a process that exits ON ITS OWN with a
# non-zero code (exactly what voice_startup.py's own `except Exception:
# return 1` produces for a real crash) - verified LIVE on this machine that
# it does NOT fire for an externally force-killed process (Stop-Process
# -Force / TerminateProcess), a real, documented Task Scheduler limitation,
# not a V.O.I.D defect. This periodic re-check trigger closes that gap using
# only Task Scheduler's own native "repeat every" feature: it re-fires the
# same action every _RECHECK_INTERVAL regardless of exit reason; the
# IgnoreNew policy above makes every firing a harmless no-op while a healthy
# instance is already running, and a real, working restart the moment one
# is not.
_RECHECK_INTERVAL = "PT15M"


def default_launch_action(pythonw: str | None = None,
                          launcher: str | None = None) -> tuple[str, str]:
    """The (executable, quoted-argument) pair the task action runs - the
    SAME console-less, working-directory-independent launcher the old Run
    key used, unchanged."""
    exe = pythonw or default_pythonw_path()
    script = launcher or voice_startup_path()
    return exe, f'"{script}"'


class _TaskSchedulerBackend:
    """Real Task Scheduler backend. win32com is imported lazily so importing
    this module never fails on a non-Windows / test host."""

    def _connect(self):
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        return scheduler

    def register(self, name: str, exe: str, args: str, description: str) -> None:
        import os
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        task_def = scheduler.NewTask(0)
        task_def.RegistrationInfo.Description = description

        logon_trigger = task_def.Triggers.Create(_TASK_TRIGGER_LOGON)
        logon_trigger.UserId = os.environ.get("USERNAME", "")

        unlock_trigger = task_def.Triggers.Create(_TASK_TRIGGER_SESSION_STATE_CHANGE)
        unlock_trigger.StateChange = _TASK_SESSION_UNLOCK
        unlock_trigger.UserId = os.environ.get("USERNAME", "")

        # See _RECHECK_INTERVAL: closes the externally-killed-process gap
        # that RestartOnFailure alone does not cover. A start time already in
        # the past + indefinite repetition means "fire now, then every
        # interval, forever" - verified via a real registered task's XML.
        recheck_trigger = task_def.Triggers.Create(_TASK_TRIGGER_TIME)
        recheck_trigger.StartBoundary = "2020-01-01T00:00:00"
        recheck_trigger.Repetition.Interval = _RECHECK_INTERVAL
        recheck_trigger.Repetition.Duration = ""   # indefinite

        action = task_def.Actions.Create(_TASK_ACTION_EXEC)
        action.Path = exe
        action.Arguments = args

        settings = task_def.Settings
        settings.Enabled = True
        settings.StartWhenAvailable = True
        settings.DisallowStartIfOnBatteries = False
        settings.StopIfGoingOnBatteries = False
        settings.MultipleInstances = _TASK_INSTANCES_IGNORE_NEW
        settings.RestartInterval = _RESTART_INTERVAL
        settings.RestartCount = _RESTART_COUNT
        settings.ExecutionTimeLimit = "PT0S"    # no time limit

        principal = task_def.Principal
        principal.LogonType = _TASK_LOGON_INTERACTIVE_TOKEN
        principal.RunLevel = _TASK_RUNLEVEL_LUA

        folder.RegisterTaskDefinition(
            name, task_def, _TASK_CREATE_OR_UPDATE, "", "",
            _TASK_LOGON_INTERACTIVE_TOKEN)

    def query(self, name: str) -> dict | None:
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        try:
            task = folder.GetTask(name)
        except Exception:
            return None
        action = task.Definition.Actions.Item(1)
        return {
            "path": action.Path,
            "arguments": action.Arguments,
            "enabled": bool(task.Definition.Settings.Enabled),
        }

    def delete(self, name: str) -> bool:
        scheduler = self._connect()
        folder = scheduler.GetFolder(TASK_FOLDER)
        try:
            folder.DeleteTask(name, 0)
            return True
        except Exception:
            return False


def install(*, pythonw: str | None = None, launcher: str | None = None,
           backend=None, task_name: str = TASK_NAME) -> str:
    """Register (or re-register - idempotent) the per-user Task Scheduler
    task. Returns the exact command that will run."""
    backend = backend or _TaskSchedulerBackend()
    exe, args = default_launch_action(pythonw, launcher)
    backend.register(task_name, exe, args, TASK_DESCRIPTION)
    return f'"{exe}" {args}'


def remove(*, backend=None, task_name: str = TASK_NAME) -> bool:
    """Remove the task. Returns True if one was present and removed."""
    backend = backend or _TaskSchedulerBackend()
    return backend.delete(task_name)


def status(*, backend=None, task_name: str = TASK_NAME) -> str | None:
    """Return the registered command, or None if not installed."""
    backend = backend or _TaskSchedulerBackend()
    info = backend.query(task_name)
    if info is None:
        return None
    return f'"{info["path"]}" {info["arguments"]}'
