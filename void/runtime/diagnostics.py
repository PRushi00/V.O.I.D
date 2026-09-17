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
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

_FILE_MARKER = "_void_bg"
_CONSOLE_MARKER = "_void_console"


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
