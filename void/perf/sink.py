"""The performance-telemetry sink: schema-checked JSON lines, rotating, opt-in.

Nothing is written until ``configure()`` is called (runtime entry points do it via
``void.runtime.diagnostics``); ``emit()`` is then a cheap no-op, so instrumented code
paths cost nothing in tests and one-shot commands. See ``schema`` for what may be
recorded (never content).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import secrets as _secrets
import threading
import time
from pathlib import Path

from void.perf import schema
from void.perf.rotate import CopyTruncateRotatingHandler

FILENAME = "perf.jsonl"
_logger = logging.getLogger("void.perf")
_logger.propagate = False                 # never mirrored into void.log
_logger.setLevel(logging.INFO)

_lock = threading.Lock()
_handler: CopyTruncateRotatingHandler | None = None
_stats = {"emitted": 0, "unknown_event": 0, "dropped_fields": 0}
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("void_interaction", default=None)


def new_interaction_id() -> str:
    return _secrets.token_hex(8)


def current_interaction_id() -> str | None:
    return _current.get()


@contextlib.contextmanager
def interaction(interaction_id: str | None = None):
    """Bind an interaction id to the current thread/context for the ``with`` body."""
    iid = interaction_id or new_interaction_id()
    token = _current.set(iid)
    try:
        yield iid
    finally:
        _current.reset(token)


@contextlib.contextmanager
def ensure_interaction(source: str = "cli"):
    """Join the current interaction, or start one (emitting ``activation``).

    Voice mints its own id at activation and binds it in the worker thread, so a voice
    command that reaches ``Assistant.run`` JOINS that id; a bare CLI/one-shot call has
    none, so it gets a fresh id and its own ``activation`` event here."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    with interaction() as iid:
        emit("activation", source=source)
        yield iid


def configure(directory, *, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 5) -> Path | None:
    """Start writing ``<directory>/perf.jsonl``. Idempotent; never raises."""
    global _handler
    with _lock:
        if _handler is not None:
            return Path(_handler.baseFilename)
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            h = CopyTruncateRotatingHandler(directory / FILENAME, maxBytes=int(max_bytes),
                                            backupCount=int(backup_count), encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            _logger.addHandler(h)
            _handler = h
            return directory / FILENAME
        except Exception:                 # diagnostics must never break startup
            return None


def shutdown() -> None:
    """Detach and close the sink (tests use this to stay hermetic)."""
    global _handler
    with _lock:
        if _handler is not None:
            _logger.removeHandler(_handler)
            try:
                _handler.close()
            finally:
                _handler = None


def stats() -> dict:
    return dict(_stats)


def emit(event: str, /, **fields) -> bool:
    """Record one event. Returns True if a line was written. Never raises."""
    if _handler is None:
        return False
    try:
        if "interaction_id" not in fields:
            iid = _current.get()
            if iid is not None:
                fields["interaction_id"] = iid
        checked = schema.validate(event, fields)
        if checked is None:
            _stats["unknown_event"] += 1
            return False
        clean, dropped = checked
        _stats["dropped_fields"] += dropped
        record = {"ts": round(time.time(), 3), "event": event, **clean}
        _logger.info(json.dumps(record, separators=(",", ":")))
        _stats["emitted"] += 1
        return True
    except Exception:
        return False
