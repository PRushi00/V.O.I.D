"""Doing something about a slow machine, within hard limits.

V2 could already answer "why is my machine slow?" through ``diagnose_slowness``. This is the smallest
honest step from diagnosis to remedy, and its boundaries are set by what the brief rules out rather than by
what psutil can do. Unrestricted process manipulation is not on the table, so:

**It can only lower priority, and only back to normal.** Two operations: ease a process off the CPU, and
undo that. There is no raise-above-normal, because raising one process's priority is how you starve
everything else, including V.O.I.D's own voice loop. There is no kill, no suspend, no terminate - those are
destructive and recoverable only by the owner restarting the program, which makes them the owner's call,
not V.O.I.D's.

**It will not touch a process that is not the owner's.** Service and system processes are refused: their
priority is set by whoever configured the service, and lowering it can break something no one connects back
to V.O.I.D. The engine's protected list (the shell, the security processes, V.O.I.D itself) is refused on
top of that, reusing V2's list rather than keeping a second copy.

**It is deny-by-default** behind ``resources.enabled``, and every change is reversible by construction: the
original priority is recorded so ``restore`` is exact rather than a guess at what "normal" was.

What this deliberately is not: a process manager, a service manager, a way to run anything, or a way to
change anything outside a process's scheduling priority. There is no code path here that executes a
command.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


class ResourceError(RuntimeError):
    """A resource change could not be made. The message is safe to say to the owner."""


class ProtectedProcess(ResourceError):
    """The process is one V.O.I.D must not touch."""


@dataclass(frozen=True)
class ResourcePolicy:
    """What the owner's configuration permits."""

    enabled: bool = False
    #: Extra process image names never adjusted, on top of the engine defaults.
    protected_processes: tuple[str, ...] = ()
    #: Lowest CPU share a process may be eased to. "idle" only runs it when nothing else wants the CPU,
    #: which is the point of the capability; "below_normal" is the gentler option.
    floor: str = "below_normal"

    @classmethod
    def from_config(cls, config) -> "ResourcePolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default
        extra = get("security.protected_processes", []) or []
        names = tuple(str(name).strip().lower() for name in extra
                      if isinstance(name, str) and name.strip())
        floor = str(get("resources.floor", "below_normal")).strip().lower()
        if floor not in ("below_normal", "idle"):
            floor = "below_normal"
        return cls(enabled=bool(get("resources.enabled", False)),
                   protected_processes=names, floor=floor)


@dataclass
class Adjustment:
    """One priority change, with what it was before, so it can be undone exactly."""

    pid: int
    name: str
    was: object = None
    now: object = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"pid": self.pid, "name": self.name, "at": self.at}


def _psutil():
    try:
        import psutil
    except ImportError as exc:                                  # pragma: no cover - dependency present
        raise ResourceError("Process control is unavailable on this machine.") from exc
    return psutil


def _protected_names(policy: ResourcePolicy) -> frozenset[str]:
    """The engine's protected process list plus the owner's additions.

    Imported from V2's computer actions so there is exactly one list of processes V.O.I.D will not touch,
    shared by window control, UI Automation and this module. ``protected_processes`` can only ADD.
    """
    base: set[str] = set()
    try:
        from void.actions.computer import _DEFAULT_PROTECTED
        base.update(name.lower() for name in _DEFAULT_PROTECTED)
    except Exception:                                           # noqa: BLE001
        # Fail CLOSED on the engine list: if it cannot be read, refuse the processes most likely to matter
        # rather than proceeding with an empty protected set.
        base.update({"explorer.exe", "lsass.exe", "csrss.exe", "winlogon.exe", "services.exe",
                     "smss.exe", "wininit.exe", "svchost.exe", "python.exe", "pythonw.exe"})
    base.update(policy.protected_processes)
    return frozenset(base)


class ResourceManager:
    """Eases a greedy process off the CPU, reversibly.

    Holds the record of what it changed, so ``restore`` and ``restore_all`` put things back exactly. The
    record is in memory only: V.O.I.D does not persist priority changes, because a priority that outlived
    the process that set it would be a change nobody remembers making.
    """

    def __init__(self, policy: ResourcePolicy | None = None):
        self._policy = policy or ResourcePolicy()
        self._adjusted: dict[int, Adjustment] = {}

    @property
    def policy(self) -> ResourcePolicy:
        return self._policy

    def available(self) -> bool:
        return bool(self._policy.enabled)

    def _require(self):
        if not self._policy.enabled:
            raise ResourceError(
                "Adjusting process priority is switched off in V.O.I.D's configuration. "
                "You can enable resources.enabled in your local config.")
        return _psutil()

    def _process(self, pid: int):
        """Resolve a pid to a process, after every refusal check.

        The checks are re-run here, at the point of use, rather than trusted from an earlier listing - a
        pid can be reused by a different program between a listing and an action.
        """
        psutil = self._require()
        try:
            number = int(pid)
        except (TypeError, ValueError) as exc:
            raise ResourceError("That is not a process id.") from exc
        if number <= 0:
            raise ResourceError("That is not a process id.")
        try:
            process = psutil.Process(number)
            name = (process.name() or "").lower()
        except Exception as exc:                                # noqa: BLE001
            raise ResourceError("That process is not running.") from exc

        if name in _protected_names(self._policy):
            raise ProtectedProcess(f"{name} is protected; I will not change its priority.")
        if number == os.getpid():
            raise ProtectedProcess("That is V.O.I.D itself; I will not change its priority.")
        if not self._is_owners(process):
            raise ProtectedProcess(
                f"{name} is not running as you, so changing it could affect the whole machine.")
        return process, name

    def _is_owners(self, process) -> bool:
        """True only when the process runs as the current user.

        Fails CLOSED: if the owner cannot be determined - which is itself what happens for most service
        and system processes - the answer is no.
        """
        try:
            username = process.username()
        except Exception:                                       # noqa: BLE001
            return False
        if not username:
            return False
        try:
            me = os.getlogin()
        except OSError:
            me = os.environ.get("USERNAME") or ""
        if not me:
            return False
        # Windows reports "MACHINE\\user"; compare the account part only.
        return username.split("\\")[-1].strip().lower() == me.strip().lower()

    def ease(self, pid: int) -> Adjustment:
        """Lower a process's CPU priority to the configured floor. Reversible."""
        psutil = self._require()
        process, name = self._process(pid)
        target = (psutil.IDLE_PRIORITY_CLASS if self._policy.floor == "idle"
                  else psutil.BELOW_NORMAL_PRIORITY_CLASS)
        try:
            was = process.nice()
            process.nice(target)
            now = process.nice()
        except psutil.AccessDenied as exc:
            raise ResourceError(f"Windows would not let me change {name}'s priority.") from exc
        except Exception as exc:                                # noqa: BLE001
            raise ResourceError(f"{name}'s priority could not be changed "
                                f"({type(exc).__name__}).") from exc
        adjustment = Adjustment(pid=process.pid, name=name, was=was, now=now)
        self._adjusted[process.pid] = adjustment
        _log.info("RESOURCE_EASED pid=%d floor=%s", process.pid, self._policy.floor)
        return adjustment

    def restore(self, pid: int) -> Adjustment:
        """Put a process's priority back to exactly what it was before V.O.I.D changed it."""
        psutil = self._require()
        record = self._adjusted.get(int(pid))
        if record is None:
            raise ResourceError("I have not changed that process's priority.")
        process, name = self._process(pid)
        try:
            process.nice(record.was if record.was is not None
                         else psutil.NORMAL_PRIORITY_CLASS)
        except Exception as exc:                                # noqa: BLE001
            raise ResourceError(f"{name}'s priority could not be restored "
                                f"({type(exc).__name__}).") from exc
        self._adjusted.pop(int(pid), None)
        _log.info("RESOURCE_RESTORED pid=%d", int(pid))
        return record

    def restore_all(self) -> list[Adjustment]:
        """Undo every change made this session. Used on shutdown and by the owner saying "put it back"."""
        restored: list[Adjustment] = []
        for pid in list(self._adjusted):
            try:
                restored.append(self.restore(pid))
            except ResourceError:
                # A process that has since exited needs no restoring; drop the record and continue.
                self._adjusted.pop(pid, None)
        return restored

    def adjustments(self) -> list[Adjustment]:
        return list(self._adjusted.values())
