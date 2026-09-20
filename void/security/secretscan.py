"""Best-effort detector for secret-shaped text (V2.0, used by the memory write gate).

Returns CATEGORY NAMES ONLY - never the matched text - so a detection can be reported or
logged without repeating the secret. It is a heuristic: false negatives exist (a secret
phrased in an unusual way will pass), which is why the owner-review queue remains the
backstop. It is deliberately conservative about false positives on ordinary prose.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_KEY_SHAPES = re.compile(
    r"AIza[0-9A-Za-z_\-]{30,}"            # Google API key
    r"|\bsk-[A-Za-z0-9_\-]{20,}"          # OpenAI/Anthropic style
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"      # GitHub tokens
    r"|\bAKIA[0-9A-Z]{16}\b"              # AWS access key id
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"    # Slack
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"   # JWT
)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_ASSIGNMENT = re.compile(
    r"\b(?:pass(?:word|wd|phrase)?|pwd|secret|api[ _-]?key|access[ _-]?key|token|pin|cvv|otp)\b"
    r"\s*(?:is|are|was|=|:|->)\s*\S+", re.IGNORECASE)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_AADHAAR = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")
_CARDISH = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in counts.values())


def detect(text: str) -> list[str]:
    """Sorted list of category names found in ``text`` (possibly empty)."""
    if not isinstance(text, str):
        return []
    found: set[str] = set()
    if _KEY_SHAPES.search(text):
        found.add("api_key")
    if _PEM.search(text):
        found.add("private_key")
    if _ASSIGNMENT.search(text):
        found.add("credential_assignment")
    if _SSN.search(text) or _AADHAAR.search(text):
        found.add("government_id")
    for m in _CARDISH.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.add("card_number")
    for m in _LONG_TOKEN.finditer(text):
        tok = m.group()
        if (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)
                and _entropy(tok) >= 3.5):
            found.add("high_entropy_token")
    return sorted(found)
