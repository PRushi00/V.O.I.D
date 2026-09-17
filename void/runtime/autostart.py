"""Windows login autostart for the persistent V.O.I.D app.

Registers ``pythonw.exe <absolute voice_startup.py>`` under the per-user HKCU
``Run``
key so V.O.I.D starts silently at login - NO console window, NO terminal, NO
administrator rights, NO Windows service, and NO change to Smart App Control
or any security control. Using ``pythonw.exe`` (the windowed Python launcher)
is what makes it console-less. The absolute launcher makes source-tree startup
independent of Windows' login working directory; it delegates to the existing
``void voice`` command and imports no Qt or WebEngine. The QPainter orb
(``app``) and WebGL Blackhole overlay (``singularity``)
remain explicit development/debugging launchers.

install() / remove() / status() are the whole API. The registry access is
behind an injectable backend so the logic is unit-testable without touching
the real registry (tests pass a fake dict-backed backend); the default
backend uses ``winreg`` and is only imported on Windows when actually used.

This module holds no backend authority - it only writes a launch string to
the user's own Run key. It never elevates, never disables security, and
fails safely (a missing key/value is reported, never crashes the app).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VOID"


def default_pythonw_path() -> str:
    """Best-effort path to the windowed (console-less) Python for the current
    interpreter. Falls back to sys.executable if pythonw.exe can't be
    located, so a caller always gets *something* runnable."""
    exe = sys.executable or ""
    folder, name = os.path.split(exe)
    lower = name.lower()
    if lower == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        return candidate if os.path.isfile(candidate) else exe
    return exe


def voice_startup_path() -> str:
    """Absolute source-tree launcher used by the HKCU Run entry.

    Windows Run entries do not provide a reliable project working directory.
    Starting an uninstalled source package with ``-m void`` can therefore fail
    before V.O.I.D reaches its microphone setup.  This launcher resolves the
    repository from its own path and delegates to the existing CLI command.
    """
    return str(Path(__file__).resolve().with_name("voice_startup.py"))


def default_launch_command(pythonw: str | None = None,
                           launcher: str | None = None) -> str:
    """The exact console-less, working-directory-independent Run command."""
    exe = pythonw or default_pythonw_path()
    script = launcher or voice_startup_path()
    return f'"{exe}" "{script}"'


class _WinregBackend:
    """Real HKCU Run-key backend. winreg is imported lazily so importing this
    module never fails on non-Windows / test hosts."""

    def set_value(self, name: str, value: str) -> None:
        import winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 0,
                                 winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        finally:
            winreg.CloseKey(key)

    def get_value(self, name: str) -> str | None:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 0,
                                 winreg.KEY_QUERY_VALUE)
        except FileNotFoundError:
            return None
        try:
            try:
                val, _ = winreg.QueryValueEx(key, name)
                return val
            except FileNotFoundError:
                return None
        finally:
            winreg.CloseKey(key)

    def delete_value(self, name: str) -> bool:
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 0,
                                 winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            return False
        try:
            try:
                winreg.DeleteValue(key, name)
                return True
            except FileNotFoundError:
                return False
        finally:
            winreg.CloseKey(key)


def install(command: str | None = None, *, backend=None,
            value_name: str = VALUE_NAME) -> str:
    """Register V.O.I.D to launch at login. Returns the command written."""
    backend = backend or _WinregBackend()
    cmd = command or default_launch_command()
    backend.set_value(value_name, cmd)
    return cmd


def remove(*, backend=None, value_name: str = VALUE_NAME) -> bool:
    """Remove the login entry. Returns True if one was present and removed."""
    backend = backend or _WinregBackend()
    return backend.delete_value(value_name)


def status(*, backend=None, value_name: str = VALUE_NAME) -> str | None:
    """Return the registered launch command, or None if not installed."""
    backend = backend or _WinregBackend()
    return backend.get_value(value_name)
