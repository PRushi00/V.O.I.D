"""Console-less, working-directory-independent V.O.I.D voice launcher.

Windows executes HKCU Run entries without a dependable project working
directory.  This tiny bootstrapper derives the repository from its own absolute
path, then delegates to the existing ``void voice`` command.  It owns no voice,
audio, wakeword, UI, or security policy.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable


def _ensure_repo_on_path() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


def _install_background_logging() -> None:
    """Write privacy-safe lifecycle diagnostics (void.log) for a ``pythonw``
    process. Shared with the other launchers - see void.runtime.diagnostics."""
    _ensure_repo_on_path()
    from void.runtime.diagnostics import install_background_logging
    install_background_logging()


_INSTANCE_MUTEX_NAME = "VOID_VoiceRuntimeMutex"
# Module-level: a local variable's handle would be garbage-collected (and the
# mutex released) as soon as _acquire_single_instance_lock() returns, which
# would silently defeat the whole guard for the rest of the process.
_instance_mutex_handle = None


def _acquire_single_instance_lock() -> bool:
    """Race-free, self-releasing single-instance guard: a named Win32 mutex.

    Returns True if THIS process now owns the lock (safe to proceed), False
    if another voice-runtime instance already holds it. Deliberately NOT a
    lock file: Windows automatically releases a mutex when its owning
    process exits for ANY reason (clean shutdown or a crash), so a dead
    instance can never leave a stale lock behind and block a legitimate
    restart (Task Scheduler's own recovery, a fresh login/unlock trigger
    firing again, or a manual relaunch). Fails OPEN (returns True) if
    pywin32 is unavailable, so a missing optional dependency can never by
    itself prevent voice from starting - the existing AudioCaptureBroker
    remains the actual single-microphone-owner guarantee regardless.
    """
    global _instance_mutex_handle
    try:
        import win32api
        import win32event
        import winerror
    except ImportError:
        return True
    handle = win32event.CreateMutex(None, False, _INSTANCE_MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if already_running:
        return False
    _instance_mutex_handle = handle   # keep alive for the rest of the process
    return True


def main(cli_main: Callable[[list[str]], int] | None = None) -> int:
    _ensure_repo_on_path()
    _install_background_logging()
    log = logging.getLogger("void.voice_startup")
    log.info("STARTUP_REQUESTED")
    if not _acquire_single_instance_lock():
        log.info("DUPLICATE_INSTANCE_DETECTED exiting without starting voice")
        return 0
    if cli_main is None:
        from void.cli import main as cli_main
    log.info("VOICE_AUTOSTART_LAUNCHED")
    try:
        result = cli_main(["voice"])
    except Exception:
        log.exception("VOICE_AUTOSTART_FATAL")
        return 1
    log.info("VOICE_AUTOSTART_EXIT code=%s", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
