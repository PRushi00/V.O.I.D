"""The performance-telemetry SCHEMA: an allowlist of events and typed fields.

Privacy by construction. Telemetry may carry numbers, booleans, values from a fixed
enumeration, random identifiers, and short program-controlled names (tool / provider /
model / exception-class names). It can NEVER carry a transcript, audio, file contents,
tool arguments, a path, a secret or memory text: any field that is not in the event's
allowlist, or whose value does not match its declared type, is DROPPED (and counted).
Free-form strings are not a type here at all.
"""
from __future__ import annotations

import math
import re

_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,48}$")
_ID = re.compile(r"^[0-9a-f]{8,32}$")
_LONG_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{32,}")     # API-key / hex-token shaped


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and -10**12 <= v <= 10**12


def _float(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and -1e9 <= v <= 1e9)


def _bool(v) -> bool:
    return isinstance(v, bool)


def _name(v) -> bool:
    return isinstance(v, str) and bool(_NAME.match(v)) and not _LONG_ALNUM_RUN.search(v)


def _id(v) -> bool:
    return isinstance(v, str) and bool(_ID.match(v))


def _enum(*values):
    allowed = frozenset(values)
    return lambda v: isinstance(v, str) and v in allowed


_STATUS = _enum("pending", "running", "paused", "awaiting_confirmation", "blocked",
                "completed", "failed", "cancelled")

# event -> {field: validator}.  ``interaction_id`` is accepted on every event.
EVENTS: dict[str, dict[str, object]] = {
    "activation": {"source": _enum("wake", "ptt", "cli", "device")},
    "endpoint": {"reason": _enum("silence", "no_speech", "max_duration", "ptt_release"),
                 "capture_s": _float},
    "stt": {"audio_s": _float, "decode_s": _float, "empty": _bool, "backend": _name},
    "route": {"provider": _name, "reason": _enum("select", "failover", "fallback")},
    "llm": {"attempt": _int, "duration_s": _float, "ok": _bool, "tool_calls": _int,
            "error_class": _name, "provider": _name},
    "tool": {"name": _name, "risk": _enum("LOW", "MEDIUM", "HIGH"),
             "duration_s": _float, "ok": _bool},
    "verify": {"ok": _bool, "name": _name},
    "respond": {"kind": _enum("llm_text", "engine_status", "silent")},
    "speak": {"chars": _int, "dur_s": _float},
    "complete": {"status": _STATUS, "total_s": _float, "steps": _int},
    "mic": {"state": _enum("unavailable", "recovery_attempt", "recovery_failed", "recovered"),
            "silence_s": _float, "attempts": _int},
    "gateway": {"period_s": _float, "ok": _int, "rejected": _int, "rate_limited": _int,
                "paired": _int, "dropped": _int, "conn_errors": _int},
}

COMMON = {"interaction_id": _id}
RESERVED = frozenset({"ts", "event"})     # record envelope keys: never usable as fields


def validate(event: str, fields: dict):
    """Return ``(clean_fields, dropped_count)``, or ``None`` for an unknown event."""
    spec = EVENTS.get(event)
    if spec is None:
        return None
    clean: dict = {}
    dropped = 0
    for key, value in fields.items():
        if key in RESERVED:
            dropped += 1
            continue
        check = spec.get(key) or COMMON.get(key)
        if check is not None and check(value):
            clean[key] = value
        else:
            dropped += 1
    return clean, dropped
