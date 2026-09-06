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
import os
import sys
import uuid
from dataclasses import dataclass

from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel

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
        """Return launchable apps: [{'name','kind'('exe'|'lnk'),'target'}]."""
        raise NotImplementedError

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
        return apps

    @staticmethod
    def _app_paths() -> list[dict]:
        import winreg
        out: list[dict] = []
        roots = [(winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
                 (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths")]
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

    @staticmethod
    def _start_menu() -> list[dict]:
        import glob
        dirs = [
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(os.environ.get("APPDATA", ""),
                         r"Microsoft\Windows\Start Menu\Programs"),
        ]
        out: list[dict] = []
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
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


def make_backend() -> WindowsBackend:
    return RealWindowsBackend() if sys.platform.startswith("win") else NullBackend()


# --- engine-owned application catalog -----------------------------------

@dataclass
class AppEntry:
    app_id: str
    name: str
    kind: str      # 'exe' | 'lnk'
    target: str    # engine-owned validated path; never model-supplied


def _make_app_id(kind: str, target: str) -> str:
    h = hashlib.sha1(f"{kind}|{os.path.normcase(target)}".encode("utf-8")).hexdigest()
    return "app-" + h[:16]


class AppCatalog:
    """Engine-owned map of app_id -> validated application metadata.

    Built lazily from the backend's deterministic discovery. The LLM only ever
    sees/uses ``app_id`` (an opaque token); it cannot supply an executable path
    as authority.
    """

    def __init__(self, backend: WindowsBackend):
        self._backend = backend
        self._entries: list[AppEntry] | None = None
        self._by_id: dict[str, AppEntry] = {}

    def _build(self) -> None:
        entries: list[AppEntry] = []
        by_id: dict[str, AppEntry] = {}
        seen: set = set()
        for r in self._backend.discover_apps():
            kind = r.get("kind", "exe")
            target = r.get("target", "")
            name = r.get("name", "")
            if not target or not name:
                continue
            key = (kind, os.path.normcase(target))
            if key in seen:
                continue
            seen.add(key)
            aid = _make_app_id(kind, target)
            entry = AppEntry(aid, name, kind, target)
            entries.append(entry)
            by_id[aid] = entry
        self._entries = entries
        self._by_id = by_id

    def entries(self) -> list[AppEntry]:
        if self._entries is None:
            self._build()
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

    def resolve(self, app_id: str) -> AppEntry | None:
        if self._entries is None:
            self._build()
        return self._by_id.get(app_id)

    @staticmethod
    def revalidate(entry: AppEntry | None) -> bool:
        """Re-check the resolved target still exists before launching."""
        return bool(entry) and os.path.exists(entry.target)


# --- window tokens + computer actions -----------------------------------

@dataclass
class _WinSnap:
    hwnd: int
    pid: int
    process_name: str   # lowercased basename identity at discovery time
    title: str


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
        ]
