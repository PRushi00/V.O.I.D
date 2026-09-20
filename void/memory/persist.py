"""Keep decrypted memory out of plaintext task history.

``tasks.sqlite`` is a plaintext database (D-06). Memory is encrypted at rest precisely so that its
text is not sitting in a readable file - but a model that was handed a memory will restate it, and
the agent loop persists every model message and the final result. Without a guard, an encrypted memory
becomes plaintext the first time it is used in an answer.

The guard lives at the single write boundary. When the current run's provider context carried
memory, every save goes through ``MemorySafeStore``, which writes a REDACTED COPY of the task:

  * assistant prose (message text, the final ``result``, a pending step's ``assistant_text``) is
    replaced wholesale by a placeholder. This is structural, not a string match: a model paraphrases,
    so removing only exact memory strings would not be a guarantee;
  * the string VALUES of tool-call arguments in the persisted history, and the plan ledger's argument
    summaries, are redacted outright (a model can put a memory fragment in an argument);
  * tool output, ``error`` and other stored text have the exact memory strings scrubbed (best effort);
  * the goal, system prompt and other owner text are untouched, and status/steps/tool names/ids are
    kept so the task list, approvals and the stale-task sweep keep working.

Stated limit: a step that is AWAITING OWNER APPROVAL keeps its ``pending`` tool-call arguments verbatim,
because they are the instruction ``approve`` will execute. That only matters if the model placed memory
text in the arguments of a HIGH-risk call, and ``pending`` is cleared as soon as the owner decides.

The LIVE task object and the ``AgentResult`` returned to the caller are never altered, so the owner
still gets the full answer; only the persisted copy is redacted. Runs whose context carried no memory
are saved exactly as before.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from dataclasses import dataclass
from typing import Callable

ANSWER_PLACEHOLDER = "[not stored: this answer used encrypted memory]"
SCRUB_PLACEHOLDER = "[memory]"
ARG_PLACEHOLDER = "[redacted]"
PENDING_MARKER = "memory_protected"      # key added to a persisted ``pending`` dict of a memory-influenced task


@dataclass(frozen=True)
class Injection:
    """What a memory context callable returns: the provider-only messages, plus what must never persist."""
    messages: tuple = ()
    protected: tuple = ()          # exact strings (memory text) to scrub from stored tool output / errors
    carries_memory: bool = True    # False for an engine note with no memory in it (nothing to protect)


def was_redacted(task) -> bool:
    """True if ``task`` was previously persisted through the guard (its placeholders are the marker).
    A task that was ever memory-influenced stays protected when it is resumed, even if this time no
    memory is retrieved for it (memory forgotten, key unavailable, a cloud provider filtering it...)."""
    if task.result == ANSWER_PLACEHOLDER:
        return True
    for m in task.messages or ():
        if m.get("role") != "assistant":
            continue
        if m.get("content") == ANSWER_PLACEHOLDER:
            return True
        if any(ARG_PLACEHOLDER in (c.get("arguments") or {}).values() for c in m.get("tool_calls") or ()):
            return True
    return isinstance(task.pending, dict) and bool(task.pending.get(PENDING_MARKER))


def _scrubber(protected) -> Callable[[str], str]:
    parts = sorted({p for p in protected if isinstance(p, str) and len(p.strip()) >= 3}, key=len, reverse=True)
    if not parts:
        return lambda s: s
    rx = re.compile("|".join(re.escape(p) for p in parts), re.IGNORECASE)
    return lambda s: rx.sub(SCRUB_PLACEHOLDER, s)


def _walk(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    return value


def _redact_message(m: dict, scrub) -> dict:
    m = _walk(m, scrub)
    if m.get("role") == "assistant":
        if isinstance(m.get("content"), str) and m["content"].strip():
            m["content"] = ANSWER_PLACEHOLDER                    # model prose may paraphrase memory
        for call in m.get("tool_calls") or ():
            if isinstance(call.get("arguments"), dict):          # keep the shape, drop every string value
                call["arguments"] = _walk(call["arguments"], lambda _s: ARG_PLACEHOLDER)
    return m


def _argument_strings(task) -> list[str]:
    """Every string value the model supplied in a tool call (persisted history and any awaiting step)."""
    found: list[str] = []

    def collect(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)

    calls = [c for m in task.messages or () if m.get("role") == "assistant" for c in m.get("tool_calls") or ()]
    if isinstance(task.pending, dict):
        calls += task.pending.get("tool_calls") or []
    for call in calls:
        collect(call.get("arguments"))
    return found


def redact_task(task, injection: Injection):
    """A redacted deep copy of ``task`` for persistence. The original is not modified."""
    # A tool can only echo what the model passed it (a path, a name, some content), and the model may have
    # put a memory fragment there. So besides the memory strings themselves, scrub every string value the
    # model supplied as a tool-call argument out of tool output, errors and the ledger.
    scrub = _scrubber((*injection.protected, *_argument_strings(task)))
    safe = dataclasses.replace(task, messages=copy.deepcopy(task.messages), pending=copy.deepcopy(task.pending),
                               plan=copy.deepcopy(task.plan))
    safe.messages = [m if m.get("role") in ("system", "user") else _redact_message(m, scrub) for m in safe.messages]
    if safe.result:
        safe.result = ANSWER_PLACEHOLDER
    if safe.error:
        safe.error = scrub(safe.error)
    if isinstance(safe.pending, dict):
        calls = safe.pending.get("tool_calls")                   # the instruction approve() executes: verbatim
        pending = _walk({k: v for k, v in safe.pending.items() if k != "tool_calls"}, scrub)
        if pending.get("assistant_text"):
            pending["assistant_text"] = ANSWER_PLACEHOLDER
        if calls is not None:
            pending["tool_calls"] = calls
        pending[PENDING_MARKER] = True       # an awaiting step may hold no placeholder at all: mark it explicitly
        safe.pending = pending
    ledger = []
    for entry in safe.plan or []:
        entry = _walk(entry, scrub)
        for c in entry.get("calls") or ():
            if "arguments_summary" in c:
                c["arguments_summary"] = ARG_PLACEHOLDER
        ledger.append(entry)
    safe.plan = ledger
    return safe


class MemorySafeStore:
    """A ``TaskStore`` stand-in for the agent: identical API, redacting saves while memory is in play."""

    def __init__(self, inner, active_injection: Callable[[], "Injection | None"]):
        self._inner = inner
        self._active = active_injection

    def save(self, task) -> None:
        injection = self._active()
        if injection is None or not injection.carries_memory:
            self._inner.save(task)
            return
        safe = redact_task(task, injection)
        self._inner.save(safe)                                   # save() stamps updated_at on the copy...
        task.updated_at = safe.updated_at                        # ...so keep the live task's clock in step

    def __getattr__(self, name):
        return getattr(self._inner, name)
