"""The MCP-facing error taxonomy.

Two jobs, and they pull against each other:

* an MCP client needs a STABLE, machine-readable reason a call failed, so it can tell "that application is not
  installed" from "the owner refused" from "V.O.I.D is stopped";
* V.O.I.D must not leak anything useful to an attacker through that channel - no stack traces, no API keys, no
  internal authentication material, and no absolute paths the caller did not already supply.

So every failure leaves this layer as one of a fixed set of codes plus a short message that this module decides.
The underlying security decision is PRESERVED, never softened: a RiskGate denial is ``denied``, a protected-root
refusal is ``protected``, and neither can become ``ok``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    """The complete set of failure reasons an MCP caller can observe."""

    INVALID_INPUT = "invalid_input"        # the argument did not pass schema/normalisation
    NOT_FOUND = "not_found"                # no such application, path, or window
    AMBIGUOUS = "ambiguous"                # several candidates; V.O.I.D never guesses between them
    DENIED = "denied"                      # RiskGate refused, or the owner refused
    PROTECTED = "protected"                # a protected root / protected location refused it
    STOPPED = "stopped"                    # the kill switch is engaged
    UNAVAILABLE = "unavailable"            # a backend V.O.I.D depends on is not usable right now
    EXECUTION_FAILED = "execution_failed"  # the capability ran and reported failure
    TIMEOUT = "timeout"                    # the capability did not finish in time
    INTERNAL = "internal"                  # an unexpected fault; the detail is deliberately not forwarded


#: Patterns that must never reach an MCP client, whatever a lower layer put in a message. Applied to every
#: outgoing message as a belt-and-braces pass - the codes above are what callers are meant to branch on.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{12,}"), "[redacted]"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), "[redacted]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I), "[redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pin)\b\s*[:=]\s*\S+"), r"\1=[redacted]"),
    # A long unbroken alphanumeric run is key-shaped; nothing V.O.I.D legitimately says looks like this.
    (re.compile(r"\b[A-Za-z0-9]{40,}\b"), "[redacted]"),
    # Traceback frames carry file paths and internal structure.
    (re.compile(r'(?s)Traceback \(most recent call last\).*'), "[internal error]"),
    (re.compile(r'(?m)^\s*File "[^"]+", line \d+.*$'), "[internal error]"),
)

#: Hard ceiling on any message that crosses the boundary.
MAX_MESSAGE = 400


def sanitise(message: object) -> str:
    """A message safe to hand an MCP client: redacted, single-line, bounded. Never raises."""
    text = "" if message is None else str(message)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class McpError:
    """A failure, as the client sees it."""

    code: ErrorCode
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message}


def error(code: ErrorCode, message: object) -> McpError:
    return McpError(code=code, message=sanitise(message))


class VoidMcpError(Exception):
    """Raised inside the adapter to abandon a call with a specific code. Never propagated to the client as-is."""

    def __init__(self, code: ErrorCode, message: object):
        self.mcp = error(code, message)
        super().__init__(self.mcp.message)
