"""Deterministic natural-language memory commands ("remember that ...").

These are recognised by a fixed grammar BEFORE any model sees the goal, and handled by the
memory subsystem directly. That has three consequences that matter:

  * no model output is involved in creating the memory - the text stored is the owner's own
    words, so an LLM cannot turn "remember" into an authorization or rewrite the content;
  * the goal is never written to ``tasks.sqlite`` (which is plaintext), so remembered text is
    not copied into an unencrypted store;
  * from a VOICE transcript the reply is a fixed phrase (never an echo of what was heard).

Grammar (case-insensitive, whole goal):
    remember [that|this][:] <text>                          (not "remember to ...": a reminder)
    [no|that's wrong|actually|correction][,] remember ...   -> correct the best-matching memory
    forget [that|about][:] <text>                           -> owner channels only
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from void.memory.crypto import MemoryUnavailable
from void.memory.scope import OWNER_CHANNELS

_CORRECT = re.compile(
    r"^\s*(?:(?:no|nope|that'?s\s+(?:wrong|incorrect|not\s+right)|that\s+is\s+(?:wrong|incorrect)|actually|correction)"
    r"\s*[,.:;!\-]*\s*)+(?:please\s+)?remember(?:\s+that|\s+this)?\s*[:,]?\s+(?P<t>\S.*)$", re.IGNORECASE | re.DOTALL)
_REMEMBER = re.compile(r"^\s*(?:please\s+)?remember(?:\s+that|\s+this)?\s*[:,]?\s+(?P<t>\S.*)$", re.IGNORECASE | re.DOTALL)
_FORGET = re.compile(r"^\s*(?:please\s+)?forget(?:\s+that|\s+about)?\s*[:,]?\s+(?P<t>\S.*)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class MemoryIntent:
    action: str          # remember | correct | forget
    text: str


def parse(goal: str) -> MemoryIntent | None:
    if not isinstance(goal, str) or len(goal) > 2000:
        return None
    m = _CORRECT.match(goal)
    if m:
        return MemoryIntent("correct", m.group("t").strip())
    m = _REMEMBER.match(goal)
    if m and not re.match(r"(?i)to\s", m.group("t")):          # "remember to call mom" is a reminder
        return MemoryIntent("remember", m.group("t").strip())
    m = _FORGET.match(goal)
    if m:
        return MemoryIntent("forget", m.group("t").strip())
    return None


_REJECT_TEXT = {
    "empty": "there was nothing to remember",
    "too_long": "it is too long (keep it under 500 characters)",
}


def _reject_message(reason: str) -> str:
    if reason.startswith("secret:"):
        return ("it looks like a secret (" + reason.split(":", 1)[1].replace("+", ", ") +
                "). I never store secrets")
    return _REJECT_TEXT.get(reason, "it was rejected by the memory policy")


# Fixed phrases for the voice channel: never an echo of the transcript.
VOICE_PROPOSED = "I've noted that. Please confirm it on the command line before I rely on it."
VOICE_ACTIVE = "Okay, I'll remember that."
VOICE_REJECTED = "I can't store that."
VOICE_FORGET = "To forget something, please use the command line."
VOICE_UNAVAILABLE = "Memory is unavailable right now."


def handle(service, goal: str, channel: str) -> str | None:
    """Run a memory command if ``goal`` is one; return the reply text, else None."""
    intent = parse(goal)
    if intent is None:
        return None
    voice = channel not in OWNER_CHANNELS
    try:
        if intent.action == "forget":
            if voice:
                return VOICE_FORGET
            out = service.forget_matching(intent.text)
            if out.deleted:
                return f"Forgot {out.deleted} memory item(s)."
            if out.ambiguous_ids:
                return ("Several memories match; nothing was deleted. Use `python -m void memory list`, then "
                        "`python -m void memory forget <id>`.")
            return "I found no matching memory."

        res = (service.correct_by_query(intent.text, channel=channel) if intent.action == "correct"
               else service.remember(intent.text, channel=channel))
        if res.status == "rejected":
            return VOICE_REJECTED if voice else f"I can't store that: {_reject_message(res.reason or '')}."
        if voice:
            return VOICE_ACTIVE if res.status == "active" else VOICE_PROPOSED
        if res.status == "duplicate":
            return "I already have that in memory."
        if res.status == "superseded":
            return f"Corrected my memory (replaced {res.replaced_id}): {res.item.text}"
        note = ("" if res.status == "active"
                else " It is pending your review (`python -m void memory review`).")
        hint = (f" Similar existing memories: {', '.join(res.similar_ids)} (use `memory correct <id> \"...\"` to replace one)."
                if res.similar_ids else "")
        return f"Remembered ({res.item.id}): {res.item.text}.{note}{hint}"
    except MemoryUnavailable as exc:
        return VOICE_UNAVAILABLE if voice else f"Memory is unavailable: {exc}"
