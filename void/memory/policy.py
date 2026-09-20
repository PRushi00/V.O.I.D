"""The memory write policy (V2.0 T3.3): a deterministic gate no model output can override.

Two questions are answered for every candidate memory, in code (not config, not the LLM):

1. May it be stored at all?   Empty, over-long and secret-shaped text is REJECTED.
2. In what state does it land? By ORIGIN and PROVENANCE, never by what the text claims:

    owner typed it (CLI / UI)                        -> active   (owner_stated)
    owner spoke it (voice transcript)                -> proposed (voice_stated)   [opt-in auto-accept
                                                        for non-sensitive, non-authority text; default off]
    the model proposed it (propose_memory)           -> proposed (agent_proposed)
    ... during a run that has read tool output       -> quarantined (agent_proposed)
    ... or whose text reads like an authority claim  -> quarantined
    owner reviews a proposed/quarantined item        -> active   (owner_confirmed)

There is NO path from model output to ``active``: the only functions that can produce
``active`` are reached by the CLI/UI (typed by the owner) or the owner's explicit review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from void.memory.index import tokenize
from void.memory.scope import OWNER_CHANNELS
from void.security import secretscan

MAX_TEXT_CHARS = 500

_SENSITIVE = {
    "health": r"\b(medical|medicine|medication|prescription|diagnos\w*|therapy|therapist|doctor|illness|disease|"
              r"allerg\w*|surgery|hospital|mental health|depress\w*|anxiety|pregnan\w*)\b",
    "finance": r"\b(bank|salary|income|loan|debt|mortgage|tax(?:es)?|credit score|invest\w*|net worth|iban|"
               r"account balance|bitcoin|crypto\w*)\b",
    "biometric": r"\b(fingerprint|face id|faceprint|retina|dna|voiceprint|biometric\w*)\b",
    "identity_document": r"\b(passport|driver'?s licen[cs]e|social security|aadhaar|national id|visa number)\b",
    "personal_contact": r"(?:[\w.+-]+@[\w-]+\.[\w.-]+)|(?:\+?\d[\d ()-]{8,}\d)|\b(home address|phone number|email address)\b",
    "protected_category": r"\b(religio\w*|politic\w*|sexual orientation|ethnic\w*|immigration|criminal record)\b",
}
_SENSITIVE_RX = {k: re.compile(v, re.IGNORECASE) for k, v in _SENSITIVE.items()}

# Text that talks about permissions/instructions. Harmless as owner-typed DATA, but from any
# non-owner origin it is the shape of a poisoning attempt, so it lands quarantined.
_AUTHORITY = re.compile(
    r"\b(authori[sz]\w*|permission\w*|permit\w*|allow\w*|grant\w*|unrestricted|may always|without (?:asking|confirm\w*|"
    r"approval|permission)|ignore (?:all |any |the )?(?:previous|prior|above)|system prompt|new instructions?|"
    r"override|bypass|disable (?:the )?(?:safety|security|risk|confirmation)|do not ask|never ask|"
    r"you (?:must|should) (?:always|never)|approve\w* (?:all|every|any))\b", re.IGNORECASE)

_PREFERENCE = re.compile(r"\b(prefer\w*|favou?rite|i (?:like|love|hate|dislike|enjoy)|call me|always|never)\b", re.IGNORECASE)


def normalise(text: str) -> str:
    """Single line, no control characters, collapsed whitespace."""
    text = "".join(ch if ch.isprintable() or ch in "\t\n" else " " for ch in (text or ""))
    return re.sub(r"\s+", " ", text).strip()


def classify_kind(text: str) -> str:
    return "preference" if _PREFERENCE.search(text) else "fact"


def classify_sensitivity(text: str) -> str:
    return "sensitive" if any(rx.search(text) for rx in _SENSITIVE_RX.values()) else "normal"


def reads_like_authority(text: str) -> bool:
    return bool(_AUTHORITY.search(text))


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str | None = None            # content-free code when rejected
    status: str | None = None
    origin: str | None = None
    kind: str | None = None
    sensitivity: str = "normal"
    cloud_ok: int = 1
    text: str = ""


def _reject(reason: str) -> Decision:
    return Decision(False, reason=reason)


def gate(raw_text: str, *, kind: str | None = None) -> tuple[str, str | None, str | None]:
    """(clean_text, kind, rejection_reason). The universal content gate."""
    text = normalise(raw_text)
    if not text:
        return text, None, "empty"
    if len(text) > MAX_TEXT_CHARS:
        return text, None, "too_long"
    cats = secretscan.detect(text)
    if cats:
        return text, None, "secret:" + "+".join(cats)
    return text, kind or classify_kind(text), None


def decide_owner(raw_text: str, *, channel: str, kind: str | None = None,
                 voice_auto_accept: bool = False, cloud_normal: bool = True) -> Decision:
    """Owner-originated write. ``channel`` is set by V.O.I.D's own code, never by a model."""
    text, kind, reason = gate(raw_text, kind=kind)
    if reason:
        return _reject(reason)
    sens = classify_sensitivity(text)
    cloud_ok = 0 if sens == "sensitive" or not cloud_normal else 1
    if channel in OWNER_CHANNELS:
        return Decision(True, status="active", origin="owner_stated", kind=kind, sensitivity=sens,
                        cloud_ok=cloud_ok, text=text)
    # A speech transcript is not proof of the owner's intent: it lands for review.
    # Opt-in only (default off). Never for sensitive text or text that reads like a permission claim.
    auto = voice_auto_accept and sens == "normal" and not reads_like_authority(text)
    return Decision(True, status="active" if auto else "proposed", origin="voice_stated", kind=kind,
                    sensitivity=sens, cloud_ok=cloud_ok, text=text)


def decide_proposal(raw_text: str, *, kind: str | None, tainted: bool, cloud_normal: bool = True) -> Decision:
    """A model-originated proposal. Can only ever yield ``proposed`` or ``quarantined``."""
    if kind is not None and kind not in ("preference", "fact", "episode"):
        kind = None
    text, kind, reason = gate(raw_text, kind=kind)
    if reason:
        return _reject(reason)
    sens = classify_sensitivity(text)
    cloud_ok = 0 if sens == "sensitive" or not cloud_normal else 1
    quarantine = tainted or reads_like_authority(text)
    return Decision(True, status="quarantined" if quarantine else "proposed", origin="agent_proposed",
                    kind=kind, sensitivity=sens, cloud_ok=cloud_ok, text=text)


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content tokens (0..1); 1.0 for the same content."""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


DUPLICATE_SIMILARITY = 0.85
