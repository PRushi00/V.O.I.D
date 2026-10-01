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
    # ``budget_s`` is the ADAPTIVE trailing-silence budget that ended the capture (seconds). A number, so the
    # fast-vs-safe split over real use is readable from this stream; nothing decides anything from it.
    "endpoint": {"reason": _enum("silence", "no_speech", "max_duration", "ptt_release"),
                 "capture_s": _float, "budget_s": _float},
    # ``chars``/``segments`` are COUNTS, never text: what a slow decode produced is the one fact that separates
    # "the machine was busy" from "Whisper worked harder on this audio", and the tail investigation had neither.
    "stt": {"audio_s": _float, "decode_s": _float, "empty": _bool, "backend": _name,
            "chars": _int, "segments": _int},
    "route": {"provider": _name, "reason": _enum("select", "failover", "fallback", "fast_path", "fast_path_miss"),
              "why": _enum("unknown", "ambiguous", "excluded", "discovery", "failed", "refused"),
              "llm_calls": _int, "kind": _enum("alias", "catalog", "clarify", "not_found", "multi"),
              # How many applications one sentence asked for, and how many of them resolved to nothing. Counts
              # only: a NAME would be dropped by the allowlist, and is not needed to see a partial success.
              "targets": _int, "missing": _int},
    "llm": {"attempt": _int, "duration_s": _float, "ok": _bool, "tool_calls": _int,
            "error_class": _name, "provider": _name},
    "provider_call": {"provider": _name, "model": _name, "slot": _name, "attempt": _int, "duration_s": _float,
                      "ok": _bool,
                      "category": _enum("auth", "forbidden", "quota", "rate_limit", "timeout", "network",
                                        "invalid_request", "unsupported_model", "server", "malformed_response",
                                        "other")},
    "tool": {"name": _name, "risk": _enum("LOW", "MEDIUM", "HIGH"),
             "duration_s": _float, "ok": _bool},
    "verify": {"ok": _bool, "name": _name},
    "respond": {"kind": _enum("llm_text", "engine_status", "silent")},
    "speak": {"chars": _int, "dur_s": _float},
    "complete": {"status": _STATUS, "total_s": _float, "steps": _int},
    "mic": {"state": _enum("unavailable", "recovery_attempt", "recovery_failed", "recovered"),
            "silence_s": _float, "attempts": _int},
    "memory": {"op": _enum("write", "retrieve", "context", "route"), "n": _int, "duration_s": _float},
    "gateway": {"period_s": _float, "ok": _int, "rejected": _int, "rate_limited": _int,
                "paired": _int, "dropped": _int, "conn_errors": _int},
    # V2 observation (domains 3, 6, 7). ``probe`` is a program-controlled name, never a device or process
    # name; ``unavailable`` counts the metrics that could not be read, which is how "the GPU is idle" stays
    # distinguishable from "there is no readable GPU here" in the telemetry as well as in the answer.
    "observe": {"probe": _name, "duration_s": _float, "values": _int, "unavailable": _int},
    # V2 camera (domain 5). The audit trail for a device that points at the owner: every activation, every
    # lapse and every frame. ``op`` and ``state`` are enumerations and ``session_s`` is a duration - there
    # is no field here that could carry an image, a description, or what the owner asked about it.
    "camera": {"op": _enum("activate", "deactivate", "expire", "capture", "denied", "cloud_analysis"),
               "state": _enum("disabled", "off", "active"), "session_s": _float,
               "captures": _int, "cloud": _bool,
               # How many bytes of image left the machine. A COUNT: the allowlist has no field that could
               # carry the image itself, the description, or the owner's question about it.
               "bytes_sent": _int},
    # V2 conversation mode (domain 4). Counts and reasons only, never a transcript: this is how the
    # fast-vs-safe question "do follow-up windows actually get used, or do they mostly lapse?" becomes
    # answerable from real use rather than from an assumption.
    "conversation": {"op": _enum("open", "follow_up", "end"), "turns": _int, "window_s": _float,
                     "why": _enum("silence", "standby_phrase", "max_turns", "stopped", "shutdown",
                                  "no_speech", "aborted")},
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
