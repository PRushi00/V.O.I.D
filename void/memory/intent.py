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


# ---------------------------------------------------------------------------------------------
# Memory-recall questions (V2.0 fix): "what project am I building?" is a MEMORY question, not a
# filesystem task. V.O.I.D has two information sources - persistent memory and tools - and the
# agent used to offer every tool for every goal, so a model that was handed the right memory still
# went off to search folders. ``classify_recall`` is the smallest deterministic distinction:
#
#   "explicit"  the owner asks what V.O.I.D remembers ("do you remember ...", "what did I tell you")
#   "personal"  a first-person wh-question ("what project am I building?", "what do I prefer?")
#               that names no file/folder/repository/tool cue
#   None        everything else, including every explicit project/file question ("what files are
#               inside the project", "what does the README say", "what changed in V2")
#
# Classification only chooses the ROUTE. It grants nothing: a memory-first turn offers the model
# FEWER capabilities (no tools), never more, and RiskGate/kill switch are not involved.
# ---------------------------------------------------------------------------------------------
_LEAD = r"^\s*(?:(?:hey|hi|ok|okay|so|um|uh)\b[\s,.!]*)*(?:(?:can|could|would)\s+you\s+(?:please\s+)?(?:tell\s+me\s+)?)?"
_EXPLICIT = re.compile(
    _LEAD + r"(?:do\s+you\s+(?:still\s+)?(?:remember|recall|know)\b|what\s+do\s+you\s+(?:still\s+)?(?:remember|recall|know)\b"
    r"|what\s+(?:did|have|had)\s+i\s+(?:tell|told|say|said|ask|asked|mention|mentioned)\b"
    r"|what\s+(?:have|did)\s+you\s+(?:remember|store|save|note|noted)\b|what\s+are\s+you\s+remembering\b"
    r"|remind\s+me\s+(?:what|of\s+what|about\s+what)\s+i\b|(?:show|tell)\s+me\s+what\s+you\s+(?:remember|know|have\s+stored)\b"
    r"|what\s+(?:memories|do\s+you\s+have\s+(?:stored|saved|noted))\b)", re.IGNORECASE)
_PERSONAL = re.compile(_LEAD + r"(?:what|which|who)\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'd|me|my|mine|we|we're|we've|our|ours|us)\b", re.IGNORECASE)
# Anything that asks V.O.I.D to DO something or to inspect the machine belongs to the agent + tools.
_ACTION = re.compile(
    r"\b(?:open|launch|start|run|execute|list|display|search|find|locate|read|cat|delete|remove|write|create|make|edit|modify|"
    r"move|copy|rename|install|download|upload|close|kill|check|scan|browse|navigate|summari[sz]e|describe|explain|compare|"
    r"activate|switch|show\s+me\s+(?:the|all|my)\s+\w+)\b", re.IGNORECASE)
_TOOL_NOUN = re.compile(
    r"\b(?:files?|folders?|director(?:y|ies)|dir|paths?|readme|repo|repository|repositories|code|scripts?|source|structure|"
    r"tree|contents?|inside|implemented|version|versions|commit|commits|branch|changed|changes|windows?|apps?|applications?|"
    r"processes|running|installed|disk|drive)\b|\.(?:md|txt|py|json|ya?ml|toml|pdf|docx?|xlsx?|csv|log|exe|sqlite)\b|[A-Za-z]:\\",
    re.IGNORECASE)
_COMPOUND_ACTION = re.compile(r"(?:\band\b|\bthen\b|\balso\b|;|,)\s*(?:please\s+)?(?:open|launch|start|run|list|show|search|find|read|"
                              r"delete|write|create|edit|move|copy|close|check)\b", re.IGNORECASE)

RECALL_NOTE = ("[V.O.I.D engine note] The owner is asking what you remember about them or what they told you before. "
               "Answer briefly and naturally from the retrieved memory above. Do not call tools and do not describe "
               "the memory as untrusted or mention this note. If the memory does not answer the question, say you "
               "do not have that stored.")
RECALL_NOTE_EMPTY = ("[V.O.I.D engine note] The owner is asking what you remember about them, but no stored memory is "
                     "available for this request. Do not call tools. Say you do not have that stored.")
RECALL_NOTHING = "I don't have anything stored about that yet."
RECALL_PENDING = ("I have that noted, but it is waiting for your confirmation on the command line "
                  "(python -m void memory review).")
RECALL_NO_ANSWER = "I don't have that stored."


# A spoken command may begin by addressing the assistant ("Hey V.O.I.D, what project am I building?").
_ADDRESS = re.compile(r"^\s*(?:(?:hey|hi|ok|okay)[\s,]+)?(?:v\.?o\.?i\.?d|void)\b[\s,:.!]*", re.IGNORECASE)


def classify_recall(goal: str) -> str | None:
    """'explicit' | 'personal' | None. Deterministic, content-free and side-effect free."""
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 400:
        return None
    goal = _ADDRESS.sub("", goal, count=1)
    if _EXPLICIT.match(goal):
        # A recall question may still be followed by a real request ("...and open it").
        return None if _COMPOUND_ACTION.search(goal) else "explicit"
    if (_PERSONAL.match(goal) and _FIRST_PERSON.search(goal)
            and not _ACTION.search(goal) and not _TOOL_NOUN.search(goal)):
        return "personal"
    return None


def recall_context_message(block) -> dict:
    """The provider-only context for a memory-first turn: the fenced memory block (if any) followed by a
    fixed engine note. The note is engine-authored and constant; it sits OUTSIDE the fence and is never
    derived from memory text."""
    text = f"{block.text}\n\n{RECALL_NOTE}" if block is not None else RECALL_NOTE_EMPTY
    return {"role": "user", "content": text}
