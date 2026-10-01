"""Windows application & window control (Phase 8A).

Controlled application discovery, running-app/window discovery, window
activation, and GRACEFUL close - all as ordinary V.O.I.D Tools that flow through
the existing KillSwitch -> Tool.effective_risk() -> RiskGate -> ToolRegistry
path. The LLM never calls Windows APIs directly and never supplies an
executable path or command as authority: it works with engine-owned
``app_id`` and opaque ``window_token`` identifiers that resolve to validated,
engine-owned metadata.

There is NO shell/command execution here. Launching is done by apps.py against
a validated catalog target (a real executable or a Start-Menu .lnk), never a
model-supplied command string. Closing is a polite WM_CLOSE only - never
TerminateProcess/force-kill. Windows-specific calls live behind a backend so
non-Windows fails cleanly instead of importing Windows-only modules.
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from void.actions.app_names import (
    MIN_SOUND_KEY, canonical_query, is_prefix, normalise, sound_key, tokens, without_publisher,
    without_qualifier,
)
from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel

_log = logging.getLogger("void.apps")

_MAX_RESULTS = 25
_HARD_MAX = 50


class ComputerBackendError(RuntimeError):
    """Raised when the Windows backend is unavailable or a native call fails."""


# --- backend abstraction ------------------------------------------------
#
# All Windows-specific API access lives behind this interface. Tests inject a
# fake backend to exercise V.O.I.D's higher-level logic; the real backend is
# exercised only by the Windows smoke test (never faked as if it were the SDK).

class WindowsBackend:
    def discover_apps(self) -> list[dict]:
        """Return launchable apps: [{'name','kind'('exe'|'lnk'|'uwp'),'target'}]."""
        raise NotImplementedError

    def discovery_fingerprint(self) -> str | None:
        """A cheap value that CHANGES when the installed applications may have changed, or None.

        The catalog uses it to avoid a full rediscovery it does not need: rebuilding costs a few hundred
        milliseconds, reading this costs a few. None means "no cheap signal available", and the catalog then
        falls back to rebuilding on its ordinary schedule.
        """
        return None

    def shortcut_target(self, path: str) -> str | None:
        """What a Start-Menu shortcut points at, or None when it cannot be determined.

        None means "unknown", never "missing": a backend that cannot read shortcuts must not cause working
        applications to be refused.
        """
        return None

    def launch(self, kind: str, target: str) -> None:
        raise NotImplementedError

    def list_windows(self) -> list[dict]:
        """Visible top-level titled windows: [{'hwnd','pid','title'}]."""
        raise NotImplementedError

    def window_exists(self, hwnd: int) -> bool:
        raise NotImplementedError

    def window_pid(self, hwnd: int) -> int | None:
        raise NotImplementedError

    def window_title(self, hwnd: int) -> str | None:
        raise NotImplementedError

    def process_name(self, pid: int) -> str | None:
        raise NotImplementedError

    def current_pid(self) -> int:
        return os.getpid()

    def activate(self, hwnd: int) -> bool:
        raise NotImplementedError

    def close(self, hwnd: int) -> bool:
        raise NotImplementedError

    def foreground_window(self) -> int | None:
        """The window handle currently in the foreground, or None when it cannot be determined.

        Optional, like ``discovery_fingerprint``: a backend that cannot answer returns None and the
        caller says so, rather than every backend having to implement it.
        """
        return None

    def set_window_state(self, hwnd: int, state: str) -> bool:
        """Minimize, maximize or restore a window. False means "not supported by this backend".

        ``state`` is one of ``minimize``, ``maximize``, ``restore``. All three are reversible, which is
        why this is separate from ``close`` - and why it is not a high-risk operation.
        """
        return False


class NullBackend(WindowsBackend):
    """Fails cleanly on non-Windows: every operation reports unavailability."""
    _MSG = "Windows application/window control is only available on Windows."

    def discover_apps(self):
        raise ComputerBackendError(self._MSG)

    def launch(self, kind, target):
        raise ComputerBackendError(self._MSG)

    def list_windows(self):
        raise ComputerBackendError(self._MSG)

    def window_exists(self, hwnd):
        raise ComputerBackendError(self._MSG)

    def window_pid(self, hwnd):
        raise ComputerBackendError(self._MSG)

    def window_title(self, hwnd):
        raise ComputerBackendError(self._MSG)

    def process_name(self, pid):
        raise ComputerBackendError(self._MSG)

    def activate(self, hwnd):
        raise ComputerBackendError(self._MSG)

    def close(self, hwnd):
        raise ComputerBackendError(self._MSG)


class RealWindowsBackend(WindowsBackend):
    """Real backend over pywin32 + psutil + read-only registry (lazy imports).

    Discovery: read-only App Paths registry + Start Menu .lnk files + PATH for a
    small curated alias set. Registry is READ ONLY - never modified. Launch:
    a real .exe via a no-shell Popen, or a .lnk via os.startfile (Windows
    resolves it) - never a shell/command string.
    """

    def __init__(self):
        self._g = self._p = self._con = self._psutil = None

    def _load(self):
        if self._g is not None:
            return
        try:
            import psutil
            import win32con
            import win32gui
            import win32process
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ComputerBackendError(
                "pywin32 and psutil are required for Windows application "
                "control. Install with: pip install -r requirements.txt"
            ) from exc
        self._g, self._p, self._con, self._psutil = (
            win32gui, win32process, win32con, psutil)

    # -- discovery --
    def discover_apps(self) -> list[dict]:
        apps: list[dict] = []
        apps.extend(self._app_paths())
        apps.extend(self._start_menu())
        apps.extend(self._path_aliases())
        # Store/UWP apps last, and only under a name no path-based source already claimed: they are the apps the
        # scans above CANNOT represent (no .exe, no .lnk - "Open WhatsApp" simply found nothing), but a duplicate
        # name would turn an exact match into an ambiguous one and stop that command fast-pathing.
        apps.extend(self._store_apps({str(a.get("name", "")).strip().lower() for a in apps}))
        return apps

    @staticmethod
    def _store_apps(existing_names: set) -> list[dict]:
        """Store apps from the shell AppsFolder namespace, as {'name','kind':'uwp','target': AppUserModelID}.

        Read-only enumeration through the same pywin32 stack the backend already uses; nothing is installed, changed
        or downloaded. Entries whose target is a plain path (classic apps, which also appear here) are skipped - the
        registry/Start-Menu scans already cover those with a real, stat-able target.
        """
        try:
            import pythoncom
            import win32com.client
        except ImportError:                                  # pragma: no cover - env dependent
            return []
        try:
            pythoncom.CoInitialize()
        except Exception:                                    # noqa: BLE001 - already initialised on this thread
            pass
        out: list[dict] = []
        try:
            folder = win32com.client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
            if folder is None:
                return []
            for item in folder.Items():
                try:
                    name = str(item.Name or "").strip()
                    target = str(item.Path or "").strip()
                except Exception:                            # noqa: BLE001 - one odd shell item must not kill discovery
                    continue
                if not name or not is_app_user_model_id(target):
                    continue
                if name.lower() in existing_names:
                    continue
                existing_names.add(name.lower())
                out.append({"name": name, "kind": "uwp", "target": target})
        except Exception:                                    # noqa: BLE001 - discovery is best-effort
            return out
        return out

    def shortcut_target(self, path: str) -> str | None:
        try:
            import pythoncom            # noqa: F401 - imported for its side effect of initialising COM
            import win32com.client
        except ImportError:                                  # pragma: no cover - env dependent
            return None
        try:
            target = win32com.client.Dispatch("WScript.Shell").CreateShortCut(path).Targetpath
        except Exception:                                    # noqa: BLE001 - unreadable shortcut: simply unknown
            return None
        return str(target).strip() or None

    def discovery_fingerprint(self) -> str | None:
        """Directory mtimes of the Start Menu trees + the App Paths key counts.

        Installing or removing a desktop application writes into one of these, so the pair detects the change
        without re-reading every shortcut. Measured on the owner's machine: ~7 ms, against ~370 ms for a full
        rediscovery. Store apps are not covered (they touch neither), which is why a lookup that finds NOTHING
        also forces a real rebuild - see ``AppCatalog.resolve_name``.
        """
        h = hashlib.sha1()
        try:
            for d in self._start_menu_dirs():
                for root, _dirs, files in os.walk(d):
                    try:
                        h.update(f"{root}|{os.stat(root).st_mtime_ns}|{len(files)}".encode("utf-8", "replace"))
                    except OSError:
                        continue
            import winreg
            for hive, sub_key in ((winreg.HKEY_LOCAL_MACHINE, _APP_PATHS_KEY),
                                  (winreg.HKEY_CURRENT_USER, _APP_PATHS_KEY)):
                try:
                    key = winreg.OpenKey(hive, sub_key)
                except OSError:
                    continue
                try:
                    h.update(f"|{winreg.QueryInfoKey(key)[0]}".encode("ascii"))
                finally:
                    winreg.CloseKey(key)
        except Exception:                                    # noqa: BLE001 - a signal that fails is simply absent
            return None
        return h.hexdigest()

    @staticmethod
    def _start_menu_dirs() -> list[str]:
        return [d for d in (
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(os.environ.get("APPDATA", ""),
                         r"Microsoft\Windows\Start Menu\Programs"),
        ) if d and os.path.isdir(d)]

    @staticmethod
    def _app_paths() -> list[dict]:
        import winreg
        out: list[dict] = []
        roots = [(winreg.HKEY_LOCAL_MACHINE, _APP_PATHS_KEY),
                 (winreg.HKEY_CURRENT_USER, _APP_PATHS_KEY)]
        for hive, sub in roots:
            try:
                key = winreg.OpenKey(hive, sub)   # read-only (KEY_READ default)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        ck = winreg.OpenKey(key, child)
                        val, _ = winreg.QueryValueEx(ck, None)  # default = exe path
                        winreg.CloseKey(ck)
                    except OSError:
                        continue
                    if not val:
                        continue
                    path = str(val).strip().strip('"')
                    if os.path.isfile(path):
                        name = os.path.splitext(child)[0]
                        out.append({"name": name, "kind": "exe", "target": path})
            finally:
                winreg.CloseKey(key)
        return out

    @classmethod
    def _start_menu(cls) -> list[dict]:
        import glob
        out: list[dict] = []
        for d in cls._start_menu_dirs():
            for lnk in glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
                name = os.path.splitext(os.path.basename(lnk))[0]
                out.append({"name": name, "kind": "lnk", "target": lnk})
        return out

    @staticmethod
    def _path_aliases() -> list[dict]:
        import shutil
        aliases = {"notepad": "notepad", "calc": "calc", "explorer": "explorer",
                   "cursor": "cursor", "code": "code", "chrome": "chrome",
                   "msedge": "msedge", "wt": "wt"}
        out: list[dict] = []
        for name, cmd in aliases.items():
            exe = shutil.which(cmd)
            if exe:
                out.append({"name": name, "kind": "exe", "target": exe})
        return out

    def launch(self, kind: str, target: str) -> None:
        if kind == "lnk":
            os.startfile(target)  # type: ignore[attr-defined]  # Windows resolves
        elif kind == "uwp":
            if not is_app_user_model_id(target):             # never reachable from a catalog entry; fail closed anyway
                raise ComputerBackendError("refusing to launch a malformed application id")
            import subprocess
            # The documented way to start a Store app: explorer resolves the AppsFolder item. Two fixed argv
            # elements, no shell, and the id was format-checked above.
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + target])
        else:
            import subprocess
            subprocess.Popen([target])   # no shell, resolved exe path

    # -- windows / processes --
    def list_windows(self) -> list[dict]:
        self._load()
        g, p = self._g, self._p
        out: list[dict] = []

        def cb(hwnd, _):
            if g.IsWindowVisible(hwnd):
                title = g.GetWindowText(hwnd)
                if title:
                    _tid, pid = p.GetWindowThreadProcessId(hwnd)
                    out.append({"hwnd": int(hwnd), "pid": int(pid), "title": title})
            return True

        g.EnumWindows(cb, None)
        return out

    def window_exists(self, hwnd: int) -> bool:
        self._load()
        try:
            return bool(self._g.IsWindow(hwnd))
        except Exception:
            return False

    def window_pid(self, hwnd: int) -> int | None:
        self._load()
        try:
            _tid, pid = self._p.GetWindowThreadProcessId(hwnd)
            return int(pid)
        except Exception:
            return None

    def window_title(self, hwnd: int) -> str | None:
        self._load()
        try:
            return self._g.GetWindowText(hwnd)
        except Exception:
            return None

    def process_name(self, pid: int) -> str | None:
        self._load()
        try:
            return self._psutil.Process(pid).name()
        except Exception:
            return None

    def activate(self, hwnd: int) -> bool:
        self._load()
        try:
            self._g.SetForegroundWindow(hwnd)   # may fail under foreground lock
            return True
        except Exception:
            return False

    def close(self, hwnd: int) -> bool:
        self._load()
        try:
            # Polite close request only. Never TerminateProcess/force-kill.
            self._g.PostMessage(hwnd, self._con.WM_CLOSE, 0, 0)
            return True
        except Exception:
            return False

    def foreground_window(self) -> int | None:
        self._load()
        try:
            hwnd = self._g.GetForegroundWindow()
            return int(hwnd) if hwnd else None
        except Exception:
            return None

    def set_window_state(self, hwnd: int, state: str) -> bool:
        self._load()
        # A fixed mapping, never a caller's value: ``state`` selects a key here and nothing else.
        commands = {"minimize": self._con.SW_MINIMIZE,
                    "maximize": self._con.SW_MAXIMIZE,
                    "restore": self._con.SW_RESTORE}
        command = commands.get(state)
        if command is None:
            return False
        try:
            self._g.ShowWindow(hwnd, command)
            return True
        except Exception:
            return False


_APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"


def make_backend() -> WindowsBackend:
    return RealWindowsBackend() if sys.platform.startswith("win") else NullBackend()


# --- engine-owned application catalog -----------------------------------

@dataclass
class AppEntry:
    app_id: str
    name: str
    kind: str      # 'exe' | 'lnk'
    target: str    # engine-owned validated path; never model-supplied


# A Microsoft Store app has no file path: it is launched by AppUserModelID. Only this exact shape is ever accepted,
# so a target coming out of the shell namespace can never carry a switch, a space-separated second argument, a path
# separator or a quote into the launcher.
_AUMID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}![A-Za-z0-9._-]{1,64}$")


def is_app_user_model_id(target: object) -> bool:
    """True for a well-formed Store AppUserModelID (``Publisher.App_hash!Entry``)."""
    return isinstance(target, str) and bool(_AUMID_RE.match(target))


def _make_app_id(kind: str, target: str) -> str:
    h = hashlib.sha1(f"{kind}|{os.path.normcase(target)}".encode("utf-8")).hexdigest()
    return "app-" + h[:16]


# How much a launch identity is TRUSTED when the same application was found by several sources. A Start-Menu
# shortcut is what Windows itself offers the user - it carries the working directory, arguments and icon the
# publisher intended - so it is preferred over a bare executable found in the registry or on PATH.
_SOURCE_RANK = {"lnk": 0, "uwp": 1, "exe": 2}


def _identity(name: str, kind: str, target: str) -> tuple[str, str]:
    """What makes two discovered records the SAME installed application.

    Same normalised display name AND same program: for a path that is its filename without extension
    (``Discord.lnk`` in two Start-Menu folders, ``Excel.lnk`` beside ``EXCEL.EXE``), for a Store app its
    AppUserModelID. Two different programs that merely share a display name do NOT collapse - they stay a genuine
    ambiguity that nothing is allowed to guess between.
    """
    if kind == "uwp":
        program = target.strip().lower()
    else:
        program = normalise(os.path.splitext(os.path.basename(target))[0])
    return normalise(name), program


def _preference(entry: "AppEntry") -> tuple:
    """Sort key picking one launch identity out of a group. Fully deterministic, never arbitrary."""
    t = os.path.normcase(entry.target)
    # A Startup-folder shortcut is a copy meant for logon (sometimes with extra switches): the ordinary entry wins.
    startup = 1 if (os.sep + "startup" + os.sep) in t else 0
    return (_SOURCE_RANK.get(entry.kind, 9), startup, t.count(os.sep), t)


@dataclass(frozen=True)
class NameMatch:
    """Outcome of resolving a spoken/typed application name against the catalog.

    Exactly one of these is true: ``entry`` is set (one application, safe to launch), or ``candidates`` holds the
    several applications that matched equally well (the caller must ASK, never pick), or neither (no match).
    """
    entry: "AppEntry | None" = None
    candidates: tuple = ()
    tier: str = ""            # exact | spacing | prefix | publisher   ("" when nothing matched)
    reason: str = ""          # "" resolved | ambiguous | unknown


class AppCatalog:
    """Engine-owned map of app_id -> validated application metadata.

    Built from the backend's deterministic discovery and kept fresh automatically: nobody edits a list of
    applications, and an application installed after V.O.I.D started still resolves. The LLM only ever
    sees/uses ``app_id`` (an opaque token); it cannot supply an executable path as authority.
    """

    #: How long a built catalog is trusted without even checking for change.
    DEFAULT_TTL_S = 600.0
    #: A lookup that matched NOTHING may check the cheap change signal, but not more often than this.
    MIN_REBUILD_S = 15.0
    #: ... and may pay for a full rediscovery it has no signal for (a Store install) only this often.
    MIN_BLIND_REBUILD_S = 300.0

    def __init__(self, backend: WindowsBackend, ttl_s: float = DEFAULT_TTL_S,
                 clock=time.monotonic):
        self._backend = backend
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._entries: list[AppEntry] | None = None
        self._by_id: dict[str, AppEntry] = {}
        self._by_norm: dict[str, list[AppEntry]] = {}
        self._by_squash: dict[str, list[AppEntry]] = {}
        self._by_publisher: dict[str, list[AppEntry]] = {}
        self._by_sound: dict[str, list[AppEntry]] = {}
        self._words: list[tuple[tuple[str, ...], AppEntry]] = []
        # Discovery is ~1.6 s here and the startup prewarm runs alongside the first command, so a second
        # caller waits for the build in flight rather than starting its own. Reentrant because _ensure and
        # _rebuild both take it. READS are never guarded - the fast path is 0.02 ms and stays that way.
        self._build_lock = threading.RLock()
        self._built_at = 0.0        # when the content was discovered
        self._checked_at = 0.0      # when freshness was last confirmed
        self._fingerprint: str | None = None
        self.builds = 0                      # observable by tests/telemetry; not used for any decision

    # -- lifecycle ------------------------------------------------------
    def _build(self) -> None:
        """Discover, de-duplicate to one entry per installed application, and index for fast lookup."""
        by_id: dict[str, AppEntry] = {}
        groups: dict[tuple[str, str], list[AppEntry]] = {}
        order: list[tuple[str, str]] = []
        seen: set = set()
        for r in self._backend.discover_apps():
            kind = r.get("kind", "exe")
            target = r.get("target", "")
            name = r.get("name", "")
            if not target or not name or not isinstance(target, str) or not isinstance(name, str):
                continue
            key = (kind, os.path.normcase(target))
            if key in seen:
                continue
            seen.add(key)
            entry = AppEntry(_make_app_id(kind, target), name, kind, target)
            by_id[entry.app_id] = entry     # every discovered record stays resolvable by its own id
            ident = _identity(name, kind, target)
            if ident not in groups:
                groups[ident] = []
                order.append(ident)
            groups[ident].append(entry)

        entries = [min(groups[k], key=_preference) for k in order]
        # Built into locals and published in one step below. A reader that arrives mid-rebuild must never see a
        # complete-looking catalog with an empty index: that lookup would miss an installed application, and a
        # miss is now answered with "I can't find it on this machine" - a wrong answer rather than a slow one.
        by_norm: dict[str, list[AppEntry]] = {}
        by_squash: dict[str, list[AppEntry]] = {}
        by_publisher: dict[str, list[AppEntry]] = {}
        by_sound: dict[str, list[AppEntry]] = {}
        words_index: list[tuple[tuple[str, ...], AppEntry]] = []
        for e in entries:
            words = tokens(e.name)
            if not words:
                continue
            by_norm.setdefault(" ".join(words), []).append(e)
            by_squash.setdefault("".join(words), []).append(e)
            words_index.append((words, e))
            stripped = without_publisher(words)
            if stripped:
                by_publisher.setdefault(" ".join(stripped), []).append(e)
            key = sound_key(e.name)
            if len(key) >= MIN_SOUND_KEY:
                by_sound.setdefault(key, []).append(e)
        self._by_norm, self._by_squash, self._by_publisher = by_norm, by_squash, by_publisher
        self._by_sound, self._words = by_sound, words_index
        self._by_id = by_id
        self._entries = entries                  # published last: a non-None _entries means "indexes are ready"
        self._built_at = self._checked_at = self._clock()
        self.builds += 1

    def _rebuild(self) -> bool:
        """Rediscover. A failure AFTER a good build keeps the previous catalog (whose entries are revalidated
        before launch anyway) rather than leaving V.O.I.D with no applications at all."""
        with self._build_lock:
            return self._rebuild_locked()

    def _rebuild_locked(self) -> bool:
        try:
            self._build()
        except ComputerBackendError:
            if self._entries is None:
                raise                        # nothing to fall back to: the caller must see it
            _log.warning("APP_DISCOVERY_FAILED keeping_previous entries=%d", len(self._entries))
            self._checked_at = self._clock()   # do not retry on every command
            return False
        self._fingerprint = self._safe_fingerprint()
        return True

    def _safe_fingerprint(self) -> str | None:
        try:
            fp = self._backend.discovery_fingerprint()
        except Exception:                    # noqa: BLE001 - an optional signal never breaks a command
            return None
        return fp if isinstance(fp, str) else None

    def _ensure(self, refresh: bool = True) -> None:
        """Build on first use; afterwards, once the catalog is older than the TTL, rebuild only if the cheap
        change signal says the installed applications actually moved.

        The first build is serialised: a caller arriving while one is in flight waits for it and uses the result,
        instead of running a second ~1.6 s discovery of the same machine.
        """
        if self._entries is None:
            with self._build_lock:
                if self._entries is None:     # another thread may have finished it while we waited
                    self._rebuild_locked()
            return
        if not refresh or self._ttl_s <= 0 or (self._clock() - self._checked_at) < self._ttl_s:
            return
        with self._build_lock:
            if (self._clock() - self._checked_at) < self._ttl_s:
                return                        # a concurrent caller already refreshed it
            fp = self._safe_fingerprint()
            if fp is not None and fp == self._fingerprint:
                self._checked_at = self._clock()   # unchanged: keep the catalog, restart the TTL only
                return
            self._rebuild_locked()

    def invalidate(self) -> None:
        """Drop the cache; the next lookup rediscovers. The safe, explicit refresh mechanism."""
        with self._build_lock:
            self._invalidate_locked()

    def _invalidate_locked(self) -> None:
        self._entries = None
        self._by_id, self._by_norm, self._by_squash, self._by_publisher, self._words = {}, {}, {}, {}, []
        self._by_sound = {}
        self._fingerprint = None
        self._built_at = self._checked_at = 0.0

    def _rebuild_for_miss(self) -> bool:
        """A name nothing matched may belong to an application installed since the catalog was built.

        Misses are COMMON - speech-to-text mishears, and an unrecognised phrase lands here - so this must be cheap.
        The change signal is consulted first (~6 ms against ~430 ms for a rediscovery) and settles the desktop
        case. A blind rediscovery, which is the only way to notice a newly installed STORE app, is left to a much
        slower interval: a just-installed Store app is worth one extra rediscovery every few minutes, not one per
        misheard word.
        """
        if self._entries is None:
            return False
        now = self._clock()
        if (now - self._built_at) >= self.MIN_BLIND_REBUILD_S:
            return self._rediscover_and_report()     # content is genuinely old: a Store install could be hiding
        if (now - self._checked_at) < self.MIN_REBUILD_S:
            return False
        fp = self._safe_fingerprint()
        if fp is not None and fp == self._fingerprint:
            self._checked_at = now                   # nothing changed on disk; do not pay for a rediscovery
            return False
        return self._rediscover_and_report()

    def _rediscover_and_report(self) -> bool:
        before = {e.app_id for e in (self._entries or [])}
        if not self._rebuild():
            return False
        return {e.app_id for e in (self._entries or [])} != before

    # -- reading --------------------------------------------------------
    def entries(self) -> list[AppEntry]:
        self._ensure()
        return self._entries or []

    def find(self, query: str) -> list[AppEntry]:
        """Exact case-insensitive name match (glob if * ? [ present). Never
        substring - same deterministic philosophy as find_directory."""
        q = (query or "").strip()
        if not q:
            return []
        is_glob = any(c in q for c in "*?[")
        ql = q.lower()
        out = []
        for e in self.entries():
            n = e.name.lower()
            hit = fnmatch.fnmatch(n, ql) if is_glob else n == ql
            if hit:
                out.append(e)
        return out

    def find_name_prefix(self, query: str) -> list[AppEntry]:
        """Entries whose name STARTS WITH the query's whole words, in order.

        Installed names carry suffixes nobody says out loud: the owner says "Opera GX", the Start Menu entry is
        "Opera GX Browser", and ``find`` (exact) returns nothing. This matches WHOLE WORDS from the start only -
        ["opera", "gx"] is a prefix of ["opera", "gx", "browser"] - so it stays deterministic and order-sensitive:
        never a substring ("code" cannot match "Visual Studio Code"), never a partial word ("oper" cannot match
        "Opera"), never a reordering. ``find`` keeps its exact/glob contract unchanged; a caller decides what to do
        with more than one hit, and the deterministic fast path accepts only a unique one.
        """
        words = tuple(w for w in re.split(r"\s+", (query or "").strip().lower()) if w)
        if not words:
            return []
        out = []
        for e in self.entries():
            name_words = tuple(w for w in re.split(r"\s+", (e.name or "").strip().lower()) if w)
            if is_prefix(words, name_words):
                out.append(e)
        return out

    def resolve_name(self, query: object) -> NameMatch:
        """Resolve a spoken or typed application name through the deterministic matching hierarchy.

        In order, stopping at the first tier that matches anything at all:

        1. ``exact``     - the normalised display name (case, punctuation and spacing already made comparable),
                           after the fixed alias table has had its say;
        2. ``spacing``   - the same name spaced differently ("Whats App" -> "WhatsApp"): an identity comparison,
                           not a partial one;
        3. ``prefix``    - the query is the leading WHOLE words of one installed name ("Opera GX" ->
                           "Opera GX Browser");
        4. ``publisher`` - a vendor word on either side: the installed name's, dropped by the owner ("teams" ->
                           "Microsoft Teams"), or one the owner added that the installed name omits
                           ("windows terminal" -> "Terminal");
        5. ``sound``     - the query sounds exactly like one installed application ("what's up" -> "WhatsApp",
                           "not pad" -> "Notepad", "chat gpd" -> "ChatGPT"). Speech-to-text errors on application
                           names are overwhelmingly of this shape: right sounds, wrong spelling. Still an EQUALITY
                           on a canonical key, not a similarity - see ``sound_key``.

        A tier that matches MORE THAN ONE application ends the search as ``ambiguous``: falling through to a weaker
        tier after a strong one was ambiguous would be guessing. Nothing here is fuzzy - there is no edit distance,
        no substring containment and no scoring - so a name that is merely SIMILAR to an installed one does not
        resolve, it misses.
        """
        words = canonical_query(query)
        if not words:
            return NameMatch(reason="unknown")
        self._ensure()                          # a first-build failure propagates: the caller must see it
        match = self._lookup(words)
        if match.reason == "unknown" and self._rebuild_for_miss():
            match = self._lookup(words)         # the application may have been installed since the last build
        return match

    def _lookup(self, words: tuple) -> NameMatch:
        for tier, hits in (("exact", self._by_norm.get(" ".join(words))),
                           ("spacing", self._by_squash.get("".join(words))),
                           ("prefix", [e for w, e in self._words if is_prefix(words, w)]),
                           ("publisher", self._by_publisher.get(" ".join(words))
                            or self._qualifier_hits(words)),
                           ("sound", self._sound_hits(words))):
            if not hits:
                continue
            if len(hits) == 1:
                return NameMatch(entry=hits[0], tier=tier)
            return NameMatch(candidates=tuple(hits[:_MAX_RESULTS]), tier=tier, reason="ambiguous")
        return NameMatch(reason="unknown")

    def _sound_hits(self, words: tuple) -> list:
        """Applications that sound exactly like this query. Last resort, and fail-closed.

        Deliberately narrow: the key must be long enough to identify a program at all (``MIN_SOUND_KEY``), and a
        key shared by several installed applications ("calc"/"Clock", "Microsoft News"/"Microsoft Teams") returns
        all of them, so the caller asks instead of guessing.
        """
        key = sound_key(" ".join(words))
        if len(key) < MIN_SOUND_KEY:
            return []
        return self._by_sound.get(key) or []

    def _qualifier_hits(self, words: tuple) -> list:
        """Last resort: the owner named a vendor the installed name does not carry ("windows terminal")."""
        stripped = without_qualifier(words)
        if not stripped:
            return []
        key = " ".join(stripped)
        return (self._by_norm.get(key) or self._by_squash.get("".join(stripped))
                or [e for w, e in self._words if is_prefix(stripped, w)])

    def resolve(self, app_id: str) -> AppEntry | None:
        # No staleness refresh here: this runs between deciding to launch and launching, and must stay instant.
        self._ensure(refresh=False)
        return self._by_id.get(app_id)

    @staticmethod
    def revalidate(entry: AppEntry | None) -> bool:
        """Re-check the resolved target is still launchable before launching.

        A Store app has no filesystem path, so there is nothing to stat: what must still hold is that its target is a
        well-formed AppUserModelID (an uninstalled app simply fails to start, which launch_app reports).
        """
        if not entry:
            return False
        if entry.kind == "uwp":
            return is_app_user_model_id(entry.target)
        return os.path.exists(entry.target)

    def validate(self, entry: AppEntry | None) -> bool:
        """``revalidate`` plus, for a Start-Menu shortcut, a check that what it POINTS AT still exists.

        Uninstalling an application often leaves its shortcut behind. Launching one of those makes Windows hunt for
        the missing program and prompt - on this machine "open Discord" produced a UAC dialog and WinError 1223 for
        an application that had been removed. Resolving the shortcut costs about 7 ms, which is affordable once per
        launch but not for all ~110 shortcuts on every rediscovery, so it is done here rather than at build time.

        An unreadable shortcut is UNKNOWN, not missing: the entry is still accepted, exactly as before.
        """
        if not self.revalidate(entry):
            return False
        if entry.kind != "lnk":
            return True
        try:
            target = self._backend.shortcut_target(entry.target)
        except Exception:                                    # noqa: BLE001 - introspection never blocks a launch
            return True
        target = target.strip() if isinstance(target, str) else ""
        return True if not target else os.path.exists(target)


# --- window tokens + computer actions -----------------------------------

@dataclass
class _WinSnap:
    hwnd: int
    pid: int
    process_name: str   # lowercased basename identity at discovery time
    title: str


#: The only window states that can be asked for. A caller's string must be one of these; it then selects a
#: fixed platform constant in the backend rather than being passed through. All three are reversible, which
#: is why they are LOW risk while close_app is HIGH.
_WINDOW_STATES = frozenset({"minimize", "maximize", "restore"})

# Processes never closed by close_app (identity by lowercased image name).
_DEFAULT_PROTECTED = {
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "wt.exe", "conhost.exe", "winlogon.exe",
    "csrss.exe", "services.exe", "lsass.exe", "dwm.exe", "python.exe",
    "pythonw.exe",
}


class ComputerActions:
    def __init__(self, backend: WindowsBackend, catalog: AppCatalog,
                 protected_processes: list[str] | None = None):
        self._b = backend
        self._catalog = catalog
        self._windows: dict[str, _WinSnap] = {}
        self._protected = set(_DEFAULT_PROTECTED)
        for p in (protected_processes or []):
            if isinstance(p, str) and p.strip():
                self._protected.add(p.strip().lower())

    # -- find_app --
    def find_app(self, query: str, max_results: int = _MAX_RESULTS) -> ToolResult:
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = _MAX_RESULTS
        max_results = min(max(1, max_results), _HARD_MAX)
        q = (query or "").strip()
        if not q:
            return ToolResult.failure("No application query given.")
        try:
            matches = self._catalog.find(q)
            if not matches:
                # Exact/glob found nothing. Fall back to the same deterministic hierarchy the fast path uses, so
                # the model is told about "Opera GX Browser" when it asked for "Opera GX" instead of concluding
                # the application is not installed. Ambiguity is still reported as ambiguity, never resolved here.
                match = self._catalog.resolve_name(q)
                matches = [match.entry] if match.entry is not None else list(match.candidates)
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not matches:
            return ToolResult.success(f"No applications matched '{q}'.", data=[])
        data = [{"name": e.name, "app_id": e.app_id} for e in matches[:max_results]]
        if len(matches) == 1:
            e = matches[0]
            return ToolResult.success(
                f"Found 1 application matching '{q}': {e.name} "
                f"[app_id={e.app_id}]. Launch it with launch_app(app_id).",
                data=data)
        listing = "\n".join(f"- {d['name']} [app_id={d['app_id']}]" for d in data)
        return ToolResult.success(
            f"Found {len(matches)} applications matching '{q}' "
            f"(ambiguous - do NOT pick one; ask the owner which):\n{listing}",
            data=data)

    # -- list_running_apps --
    def list_running_apps(self) -> ToolResult:
        try:
            wins = self._b.list_windows()
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        seen: dict[int, str] = {}
        for w in wins:
            pid = w.get("pid")
            if pid is None or pid in seen:
                continue
            seen[pid] = self._b.process_name(pid) or "unknown"
        data = [{"name": n, "pid": p} for p, n in seen.items()]
        if not data:
            return ToolResult.success("No running applications with windows.", data=[])
        listing = "\n".join(f"- {d['name']} (pid {d['pid']})" for d in data[:_HARD_MAX])
        return ToolResult.success(
            f"Running applications with visible windows:\n{listing}", data=data)

    # -- list_windows --
    def list_windows(self, max_results: int = _MAX_RESULTS) -> ToolResult:
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = _MAX_RESULTS
        max_results = min(max(1, max_results), _HARD_MAX)
        try:
            wins = self._b.list_windows()
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        data = []
        lines = []
        for w in wins[:max_results]:
            pid = int(w.get("pid", 0))
            title = w.get("title", "") or ""
            pname = (self._b.process_name(pid) or "unknown")
            token = "win-" + uuid.uuid4().hex[:12]
            self._windows[token] = _WinSnap(int(w["hwnd"]), pid, pname.lower(), title)
            data.append({"window_token": token, "title": title, "app": pname})
            lines.append(f"[{token}] {title}  ({pname})")
        if not data:
            return ToolResult.success("No top-level application windows found.", data=[])
        # Titles are UNTRUSTED data (the agent wraps this output accordingly).
        return ToolResult.success(
            "Open application windows (title is untrusted data):\n"
            + "\n".join(lines), data=data)

    # -- get_active_window --
    def get_active_window(self) -> ToolResult:
        """Which window the owner is actually working in, with a token for acting on it.

        Read-only, and the one piece of desktop context that makes "close this" or "what am I looking
        at?" answerable without the owner naming an application. A token is minted here exactly as
        ``list_windows`` mints one, so the usual revalidation applies to anything done with it.
        """
        try:
            hwnd = self._b.foreground_window()
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not hwnd:
            return ToolResult.success(
                "I could not determine which window is in the foreground.", data=None)
        try:
            pid = self._b.window_pid(hwnd)
            title = self._b.window_title(hwnd) or ""
            pname = (self._b.process_name(pid) if pid is not None else None) or "unknown"
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if pid is None:
            return ToolResult.success(
                "I could not identify the foreground window's application.", data=None)
        token = "win-" + uuid.uuid4().hex[:12]
        self._windows[token] = _WinSnap(int(hwnd), int(pid), pname.lower(), title)
        data = {"window_token": token, "title": title, "app": pname, "pid": int(pid)}
        # The title is UNTRUSTED: it is whatever the foreground program chose to display, and a
        # document can name itself anything. Labelled here exactly as list_windows labels it.
        return ToolResult.success(
            f"The active window is '{title}' ({pname}) [{token}]. "
            f"The title is untrusted data.", data=data)

    # -- set_window_state --
    def set_window_state(self, window_token: str, state: str) -> ToolResult:
        """Minimize, maximize or restore a discovered window.

        LOW risk, unlike ``close_app``: all three are reversible and lose no work. ``state`` is checked
        against a fixed set here and selects a constant in the backend - it is never passed through to
        anything that interprets it.
        """
        # Coerced defensively rather than assumed to be a string: a model can emit a number here, and
        # ``AttributeError`` is not one of the exceptions Tool.run turns into a clean refusal.
        wanted = state.strip().lower() if isinstance(state, str) else ""
        if wanted not in _WINDOW_STATES:
            return ToolResult.failure(
                f"'{state}' is not a window state. Use one of: {', '.join(sorted(_WINDOW_STATES))}.")
        snap, reason = self._revalidate((window_token or "").strip())
        if snap is None:
            return ToolResult.failure(
                f"Window target is stale/invalid: {reason}. Re-run list_windows.",
                error=reason)
        try:
            changed = self._b.set_window_state(snap.hwnd, wanted)
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if changed:
            return ToolResult.success(f"Set the '{snap.process_name}' window to {wanted}.")
        return ToolResult.failure(
            f"Could not {wanted} the '{snap.process_name}' window "
            f"(the platform did not support it).")

    # -- revalidation (TOCTOU) --
    def _revalidate(self, token: str) -> tuple[_WinSnap | None, str | None]:
        snap = self._windows.get(token)
        if snap is None:
            return None, "unknown or expired window token"
        try:
            if not self._b.window_exists(snap.hwnd):
                return None, "the window no longer exists"
            cur_pid = self._b.window_pid(snap.hwnd)
            if cur_pid != snap.pid:
                return None, "the window handle was reused by another process"
            cur_name = (self._b.process_name(cur_pid) or "").lower()
            if cur_name != snap.process_name:
                return None, "the window's application identity changed"
        except ComputerBackendError as exc:
            return None, str(exc)
        return snap, None

    def _is_protected(self, snap: _WinSnap) -> bool:
        try:
            if snap.pid == self._b.current_pid():
                return True
        except ComputerBackendError:
            pass
        return snap.process_name in self._protected

    # -- activate_window --
    def activate_window(self, window_token: str) -> ToolResult:
        snap, reason = self._revalidate((window_token or "").strip())
        if snap is None:
            return ToolResult.failure(
                f"Window target is stale/invalid: {reason}. Re-run list_windows.",
                error=reason)
        try:
            activated = self._b.activate(snap.hwnd)
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if activated:
            return ToolResult.success(
                f"Activated the '{snap.process_name}' window ({window_token}).")
        return ToolResult.failure(
            f"Could not bring the '{snap.process_name}' window to the foreground "
            f"(Windows may be restricting focus changes).")

    # -- close_app --
    def close_app(self, window_token: str) -> ToolResult:
        snap, reason = self._revalidate((window_token or "").strip())
        if snap is None:
            return ToolResult.failure(
                f"Window target is stale/invalid: {reason}. Re-run list_windows.",
                error=reason)
        if self._is_protected(snap):
            return ToolResult.failure(
                f"Refusing to close a protected process ('{snap.process_name}').")
        try:
            closed = self._b.close(snap.hwnd)
        except ComputerBackendError as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if closed:
            return ToolResult.success(
                f"Sent a graceful close request to '{snap.process_name}' "
                f"({window_token}). It may prompt to save unsaved work.")
        return ToolResult.failure(
            f"The '{snap.process_name}' application did not accept the close "
            f"request. Not escalating.")

    # -- registration --
    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="find_app",
                description=(
                    "Find an installed application by name (exact, "
                    "case-insensitive; glob with * ? []). Returns an engine "
                    "app_id to pass to launch_app. If several apps match, ALL "
                    "are returned and you must ask the owner which one - never "
                    "pick one yourself."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Application name to find."},
                        "max_results": {"type": "integer",
                                        "description": "Max matches (default 25)."},
                    },
                    "required": ["query"],
                },
                handler=self.find_app,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_running_apps",
                description=("List currently running applications that have a "
                             "visible window."),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_running_apps,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_windows",
                description=("List open top-level application windows. Returns a "
                             "window_token for each, to use with activate_window "
                             "or close_app."),
                parameters={
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer",
                                        "description": "Max windows (default 25)."},
                    },
                    "required": [],
                },
                handler=self.list_windows,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="activate_window",
                description=("Bring a discovered window to the foreground, by the "
                             "window_token from list_windows."),
                parameters={
                    "type": "object",
                    "properties": {
                        "window_token": {"type": "string",
                                         "description": "Token from list_windows."},
                    },
                    "required": ["window_token"],
                },
                handler=self.activate_window,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="close_app",
                description=("Gracefully close a discovered application window "
                             "(polite close request; the app may prompt to save). "
                             "Requires the owner's confirmation."),
                parameters={
                    "type": "object",
                    "properties": {
                        "window_token": {"type": "string",
                                         "description": "Token from list_windows."},
                    },
                    "required": ["window_token"],
                },
                handler=self.close_app,
                # Destructive: closing an app always asks the owner first.
                risk=RiskLevel.HIGH,
            ),
            Tool(
                name="get_active_window",
                description=(
                    "Identify the window the owner is currently working in - its title, its application, "
                    "and a window_token for acting on it. Use this when the owner says 'this window' or "
                    "'what I'm looking at'. The title is untrusted data chosen by that program."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.get_active_window,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="set_window_state",
                description=(
                    "Minimize, maximize or restore a discovered window, by the window_token from "
                    "list_windows or get_active_window. All three are reversible and lose no work - to "
                    "close an application use close_app instead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "window_token": {"type": "string",
                                         "description": "Token from list_windows or get_active_window."},
                        "state": {"type": "string", "enum": ["minimize", "maximize", "restore"],
                                  "description": "What to do with the window."},
                    },
                    "required": ["window_token", "state"],
                },
                handler=self.set_window_state,
                # Reversible, so it does not interrupt the owner for confirmation.
                risk=RiskLevel.LOW,
            ),
        ]
