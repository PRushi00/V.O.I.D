"""Shared privacy-safe lifecycle diagnostics for every V.O.I.D voice entry
point (autostart, the developer ``void voice`` command, and the opt-in
``app`` / ``singularity`` UI launchers).

A console-less ``pythonw`` run (the normal autostart path) has nowhere to
print - a startup crash or a silent stall would otherwise simply vanish.
``install_background_logging()`` routes V.O.I.D's own WARNING+ (and its own
INFO-level lifecycle markers) to ``<state_dir>/void.log``. Third-party
libraries stay at WARNING so the file does not fill with noise. It never
records audio, transcripts, secrets, or credentials - only lifecycle stage,
counts, durations, and exception info. Idempotent - safe to call from
multiple entry points, and safe to call more than once.

Previously this ~20-line function was duplicated (with a small behavioral
gap) between the Singularity UI launcher and the autostart launcher, and NOT
called at all by the plain ``void voice`` command - so a developer testing
voice manually from a terminal got no ~/.void/void.log output whatsoever for
any of the mic/wake lifecycle markers, even though the log file's format
promised it. This module is the one implementation all three now share.

Test isolation: an automated test that calls a real entry point (e.g.
``cli.cmd_voice()``) WITHOUT redirecting ``void.config.Config`` - as a prior
regression did - must never attach a handler to the user's actual
``~/.void/void.log``. Once attached, a logging handler stays on the root
logger for the rest of that process, so every later test's ordinary
synthetic log calls (including deliberately-raised test exceptions) would
otherwise land in the SAME file as real runtime evidence, making it useless
for real-hardware validation. ``install_background_logging`` therefore
refuses to open the real production path while running under pytest,
falling back to a session-scoped temp file instead. A test that wants to
exercise the REAL file-writing behavior does so exactly as
``tests/test_diagnostics.py`` already did before this change: redirect
``void.config`` to a fake pointed at ``tmp_path`` - that path is never equal
to the real production path, so the guard never applies to it.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

_FILE_MARKER = "_void_bg"
_CONSOLE_MARKER = "_void_console"


def _real_production_log_path() -> Path:
    """The actual, non-redirected production path: ``~/.void/void.log``."""
    return Path(os.path.expanduser("~")) / ".void" / "void.log"


def _running_under_pytest() -> bool:
    """Reliable, dependency-free "are we a test process" signal - true for
    the WHOLE pytest process (collection included), not just inside a test
    function, so it also catches module-level/fixture-time calls."""
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def install_background_logging() -> Path | None:
    """Attach the shared FileHandler to the root logger, once. Returns the
    log path on success, or ``None`` if a handler was already installed or
    the attempt failed (never raises - diagnostics must never break startup)."""
    root = logging.getLogger()
    if any(getattr(h, _FILE_MARKER, False) for h in root.handlers):
        return None
    try:
        from void.config import Config
        log_path = Config.load().state_dir() / "void.log"
    except Exception:
        log_path = Path(tempfile.gettempdir()) / "void.log"
    if _running_under_pytest() and log_path == _real_production_log_path():
        # Config was NOT redirected by the caller (see module docstring) -
        # never contaminate the real production log from a test process.
        log_path = Path(tempfile.gettempdir()) / "void-test-session.log"
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        setattr(handler, _FILE_MARKER, True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.WARNING)          # quiet by default (third-party)
        logging.getLogger("void").setLevel(logging.INFO)   # our own lifecycle
        return log_path
    except Exception:
        return None


def install_console_diagnostics() -> None:
    """Additionally echo void's own lifecycle log records to the console, for
    a developer running a launcher interactively. A no-op under a console-less
    ``pythonw`` autostart, where ``sys.stderr`` is None and a StreamHandler
    would raise on its first emit - so this is always safe to call
    unconditionally from any entry point, launched either way."""
    if sys.stderr is None:
        return
    root = logging.getLogger()
    if any(getattr(h, _CONSOLE_MARKER, False) for h in root.handlers):
        return
    try:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _CONSOLE_MARKER, True)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
    except Exception:
        pass
