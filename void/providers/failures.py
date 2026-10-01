"""What kind of failure a provider just had, and what that means for retrying and failing over.

A 503 from Gemini and a malformed request are both "the call raised", but they call for opposite responses: one is
worth waiting a moment for, the other will fail identically forever and would fail on any other provider too.
Before this module every failure was treated the same - three attempts on the same provider with exponential
backoff, then the task failed - which is how a transient Gemini outage turned "explain ARP" into a 14-16 second
error while a working local model sat idle.

Classification is by exception type first and message second, because the providers wrap several SDKs and none of
them expose a stable error taxonomy. It is deliberately conservative: anything unrecognised is ``other``, which
retries briefly and then fails over, exactly like a transient fault.
"""
from __future__ import annotations

import re

from void.providers.base import ProviderUnavailable

#: (pattern, category), most specific first; the first match wins.
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p, re.I), c) for p, c in (
    (r"\b401\b|unauthorized|invalid[_ ]api[_ ]key|api key not valid|permission denied|\b403\b|forbidden", "auth"),
    (r"quota|billing|insufficient[_ ]balance|exceeded your current", "quota"),
    (r"\b429\b|rate[_ ]limit|resource[_ ]exhausted|too many requests", "rate_limit"),
    (r"timed? ?out|deadline|\b504\b", "timeout"),
    (r"\b5(0[023]|9\d)\b|unavailable|internal error|overloaded|server error|high demand", "server"),
    (r"connection|network|\bdns\b|unreachable|ssl|socket|refused", "network"),
    (r"model.*(not found|not exist|unavailable|unsupported)|no such model|not installed", "unsupported_model"),
    (r"\b400\b|invalid[_ ]request|invalid argument|malformed|unsupported parameter|bad request", "invalid_request"),
))

#: category -> (attempts allowed on THIS provider, may fail over to the next one).
#:
#: "attempts" counts the first call, so 1 means "do not retry". Failing over is allowed almost everywhere: the
#: whole point of a second provider is to answer when the first cannot. The exception is ``invalid_request`` - the
#: request itself is wrong, so another provider would reject it too and the real problem must surface.
POLICY: dict[str, tuple[int, bool]] = {
    "server": (2, True),
    "timeout": (2, True),
    "network": (2, True),
    "malformed_response": (2, True),
    "other": (2, True),
    "rate_limit": (1, True),          # sitting out a rate limit costs the owner more than switching
    "quota": (1, True),               # no amount of retrying creates more quota
    "auth": (1, True),                # a bad credential stays bad
    "unsupported_model": (1, True),
    "unavailable": (1, True),         # the provider said so itself; do not argue with it
    "invalid_request": (1, False),
}


def classify(exc: BaseException) -> str:
    """The failure category of ``exc``. Never raises; always a key of ``POLICY``."""
    if isinstance(exc, ProviderUnavailable):
        # The provider has already decided it cannot serve this call (no credential, model missing, every key
        # cooled). Retrying it is pointless; the next provider is exactly what it exists for.
        return "unavailable"
    name = type(exc).__name__
    text = f"{name}: {exc}"
    for pattern, category in _PATTERNS:
        if pattern.search(text):
            return category
    lowered = name.lower()
    if "timeout" in lowered:
        return "timeout"
    if "connection" in lowered:
        return "network"
    return "other"


def policy(category: str) -> tuple[int, bool]:
    """(attempts allowed on this provider, may fail over). Unknown categories behave like ``other``."""
    return POLICY.get(category, POLICY["other"])


def backoff_s(attempt: int) -> float:
    """Seconds to wait before the next attempt on the SAME provider (``attempt`` is 0-based).

    Bounded and short: the point is to reach a working provider quickly, not to sit out an outage. The
    alternative to waiting is switching, and switching is usually faster.
    """
    return min(1.0 * (attempt + 1), 2.0)
