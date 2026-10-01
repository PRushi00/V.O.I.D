"""Read-only observation of the machine V.O.I.D runs on.

This package answers questions about the host - how busy it is, what devices are attached, what the
network looks like. It is the *source* layer: it produces facts and nothing else. It contains no tools,
no authorization, no model, and it never changes anything. The capability tools that expose a curated
subset of these facts to the agent live in ``void/actions/system.py``, ``devices.py`` and ``network.py``,
and they reach the owner through the same RiskGate funnel as every other tool.

Four rules hold everywhere in this package, and each one is tested:

**Never fabricate.** A metric that cannot be read is ``None``, and the reason it could not be read is
recorded in :attr:`Reading.unavailable`. "0%" and "unknown" are different answers and this layer never
confuses them. This machine, for instance, genuinely cannot report CPU temperature (psutil ships no
Windows sensor support) - so V.O.I.D says that, rather than inventing a number.

**No shell.** Nothing here builds a command string. The one probe that needs an external program
(``gpu.py``, for ``nvidia-smi``) passes a frozen argument list with ``shell=False``; device enumeration
uses WMI through pywin32, which spawns no process at all. A caller-supplied string never reaches either.

**No secrets, no content.** Process listings carry a name, a pid and resource usage - never a command
line, which routinely holds API keys and private paths. Network listings carry addresses and ports -
never payloads. Nothing here reads a file the owner did not ask about.

**Bounded.** Every probe has a timeout and every list has a cap, so one wedged driver or one machine
with 4000 processes cannot stall the voice loop or flood a model's context.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Probes that call out to another program get this long, in seconds, and then they are abandoned. Short
#: on purpose: these answers feed a spoken reply, and a slow truthful answer is worse than "I couldn't
#: read that" when the owner is standing there waiting.
PROBE_TIMEOUT_S = 3.0


@dataclass
class Reading:
    """What one probe managed to observe, plus an honest account of what it could not.

    ``values`` holds only facts actually read. ``unavailable`` maps the name of a metric that was
    *expected* but could not be obtained to a short, non-sensitive reason. A caller can therefore tell
    "the GPU is idle" from "there is no readable GPU here", which is the whole point of this type.
    """
    values: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0

    def set(self, name: str, value: Any) -> "Reading":
        """Record a fact. ``None`` is not a fact - use :meth:`miss` instead."""
        if value is not None:
            self.values[name] = value
        return self

    def miss(self, name: str, reason: str) -> "Reading":
        """Record that ``name`` could not be read, and why."""
        self.unavailable[name] = _short(reason)
        return self

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def merge(self, other: "Reading", prefix: str = "") -> "Reading":
        """Fold another probe's reading into this one, optionally namespacing its keys."""
        for key, value in other.values.items():
            self.values[f"{prefix}{key}"] = value
        for key, reason in other.unavailable.items():
            self.unavailable[f"{prefix}{key}"] = reason
        return self

    @property
    def ok(self) -> bool:
        """True when the probe read at least one fact."""
        return bool(self.values)


def _short(reason: object, limit: int = 120) -> str:
    """A reason safe to show the owner and to put in a model's context.

    Exception text can carry a path, a device serial or a command line, so this keeps it short and
    collapses whitespace. It is a human hint, never something to parse.
    """
    text = " ".join(str(reason).split())
    return text[:limit] if len(text) > limit else (text or "unavailable")


def describe_error(exc: BaseException) -> str:
    """A one-line, non-sensitive description of why a probe failed."""
    message = _short(exc, limit=80)
    name = type(exc).__name__
    return f"{name}: {message}" if message and message != "unavailable" else name


class _Timer:
    """Measures a probe so latency is reported from measurement, never from a guess."""

    def __init__(self, reading: Reading):
        self._reading = reading

    def __enter__(self) -> Reading:
        self._t0 = time.perf_counter()
        return self._reading

    def __exit__(self, *_exc) -> bool:
        self._reading.duration_s = time.perf_counter() - self._t0
        return False


def timed(reading: Reading | None = None) -> _Timer:
    """``with timed() as r:`` - a Reading whose ``duration_s`` is filled in on exit."""
    return _Timer(reading if reading is not None else Reading())
