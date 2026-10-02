"""The V.O.I.D-native interaction model: semantic events about a task's life.

These events are V.O.I.D's own vocabulary for "what is happening", and that ownership is the point. A UI
protocol - AG-UI today, something else in three years - should be an *adapter* that translates these into
its own wire format. If the internal model were defined by an external protocol, changing protocols would
mean changing V.O.I.D's task engine, which is exactly the coupling the V3 architecture avoids.

So this module has no transport, no serialisation framework, and no dependency on anything outside V.O.I.D.
It is a small typed record and an append-only log.

**Events are observations, never instructions.** An event records that something happened; emitting one
cannot cause anything. In particular ``ASK_USER`` does not grant anything and ``ACTION_VERIFIED`` does not
authorize a next step - the authorization for any action is decided, as always, by the tool's risk level
and ``RiskGate`` at the moment it runs.

**Events carry no secrets and no payloads.** ``detail`` is a short human-readable line and the typed fields
are identifiers, names and numbers. There is deliberately no field for tool arguments, file contents, page
text, screen content or a model's reasoning: an event stream is the thing most likely to be logged, shown
in a UI, or shipped to a collector, so it is the last place raw content should live. The same reasoning as
``void/perf/schema.py``, applied to a different stream.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

#: How much of a free-text detail line is kept. Long enough for "Opera was already open", short enough that
#: nothing substantial can be smuggled through it.
MAX_DETAIL = 200

#: How many events one log retains. A long task should not grow without bound in memory; the durable record
#: is the task's own ``plan`` ledger in SQLite, not this.
MAX_EVENTS = 500


class TaskEventKind:
    """The kinds of thing that can happen to a task.

    A closed set, matching the blueprint's vocabulary. Closed on purpose: a UI adapter can switch over this
    exhaustively, and an unrecognised kind is a bug rather than something to render as "unknown".
    """

    TASK_STARTED = "task_started"
    #: The engine has interpreted the goal. Carries the intent, never the model's reasoning.
    INTENT_RESOLVED = "intent_resolved"
    #: A route was chosen from candidates. Carries which, and why it won.
    ROUTE_SELECTED = "route_selected"
    #: An action is about to be attempted. Emitted BEFORE authorization, so a refusal is visible as a
    #: proposal that did not become an execution.
    ACTION_PROPOSED = "action_proposed"
    ACTION_EXECUTED = "action_executed"
    ACTION_VERIFIED = "action_verified"
    TASK_PROGRESS = "task_progress"
    #: V.O.I.D needs something only the owner can supply: a decision, a clarification, a confirmation.
    ASK_USER = "ask_user"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    #: The owner changed the task, or a failure forced a new plan.
    TASK_REPLANNED = "task_replanned"
    ARTIFACT_CREATED = "artifact_created"
    TASK_FAILED = "task_failed"
    TASK_COMPLETED = "task_completed"
    TASK_CANCELLED = "task_cancelled"

    ALL = frozenset({
        TASK_STARTED, INTENT_RESOLVED, ROUTE_SELECTED, ACTION_PROPOSED, ACTION_EXECUTED,
        ACTION_VERIFIED, TASK_PROGRESS, ASK_USER, TASK_PAUSED, TASK_RESUMED, TASK_REPLANNED,
        ARTIFACT_CREATED, TASK_FAILED, TASK_COMPLETED, TASK_CANCELLED,
    })

    #: Kinds after which a task is not running any more. A UI can stop showing progress on these.
    TERMINAL = frozenset({TASK_FAILED, TASK_COMPLETED, TASK_CANCELLED})


def _short(text: object, limit: int = MAX_DETAIL) -> str:
    """A single-line, length-bounded string. Never ``None``, never multi-line."""
    if text is None:
        return ""
    flattened = " ".join(str(text).split())
    return flattened[:limit]


@dataclass(frozen=True)
class TaskEvent:
    """One thing that happened to one task.

    Frozen: an event is a record of the past, and nothing downstream should be able to edit history. A
    consumer that needs to annotate makes its own object.
    """

    kind: str
    task_id: str
    #: Monotonic-ish wall clock, for ordering within a log and for latency arithmetic.
    at: float = field(default_factory=time.time)
    #: Short human line. Bounded and flattened; never tool arguments or content.
    detail: str = ""
    #: The capability involved, when one is. A tool name, which is program-controlled text.
    tool: str | None = None
    #: The route involved, when one is. A route id, which the engine minted.
    route: str | None = None
    #: 0-based index into the task's step ledger, when the event is about a step.
    step: int | None = None
    #: True/False for a verification outcome; None when the event is not a verification.
    ok: bool | None = None
    #: Seconds, for events that measure something.
    duration_s: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in TaskEventKind.ALL:
            raise ValueError(f"unknown task event kind: {self.kind!r}")
        # Frozen dataclass: normalise through object.__setattr__, which is the documented way.
        object.__setattr__(self, "detail", _short(self.detail))

    @property
    def terminal(self) -> bool:
        return self.kind in TaskEventKind.TERMINAL

    def as_dict(self) -> dict:
        """A plain dict for a UI adapter or a log line. Only set fields appear."""
        out: dict = {"kind": self.kind, "task_id": self.task_id, "at": round(self.at, 3)}
        if self.detail:
            out["detail"] = self.detail
        for name in ("tool", "route", "step", "ok", "duration_s"):
            value = getattr(self, name)
            if value is not None:
                out[name] = round(value, 3) if name == "duration_s" else value
        return out


class EventLog:
    """An append-only, bounded, thread-safe record of what happened, with optional live subscribers.

    Thread-safe because the voice runtime emits from a worker thread while a UI reads from another, which
    is the existing shape of V.O.I.D rather than a new design. Bounded because a long-running task must not
    grow memory without limit; the durable record is the task's own ledger in SQLite.

    A subscriber that raises is isolated and dropped from consideration for that event rather than being
    allowed to break the task. A UI crashing must not stop work.
    """

    def __init__(self, max_events: int = MAX_EVENTS,
                 on_event: Callable[[TaskEvent], None] | None = None):
        self._max = max(1, int(max_events))
        self._events: list[TaskEvent] = []
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[TaskEvent], None]] = []
        if on_event is not None:
            self._subscribers.append(on_event)

    def subscribe(self, consumer: Callable[[TaskEvent], None]) -> None:
        with self._lock:
            self._subscribers.append(consumer)

    def emit(self, kind: str, task_id: str, **fields) -> TaskEvent:
        """Record an event and hand it to subscribers. Returns the event that was recorded."""
        event = TaskEvent(kind=kind, task_id=task_id, **fields)
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max:
                # Drop oldest. The durable ledger keeps the full history; this is a live window.
                del self._events[:len(self._events) - self._max]
            consumers = list(self._subscribers)
        for consumer in consumers:
            try:
                consumer(event)
            except Exception:                                  # noqa: BLE001 - a UI must not break a task
                continue
        return event

    def events(self, kind: str | None = None) -> list[TaskEvent]:
        """A snapshot, oldest first, optionally of one kind. A copy - callers cannot mutate history."""
        with self._lock:
            if kind is None:
                return list(self._events)
            return [event for event in self._events if event.kind == kind]

    def last(self, kind: str | None = None) -> TaskEvent | None:
        with self._lock:
            for event in reversed(self._events):
                if kind is None or event.kind == kind:
                    return event
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def as_dicts(self) -> list[dict]:
        return [event.as_dict() for event in self.events()]


def summarise(events: Iterable[TaskEvent]) -> str:
    """One line describing a run, for a concise spoken answer.

    The blueprint's interaction philosophy: routine feedback should be brief, with detail on request. This
    is the brief form, built from what actually happened rather than from what was planned.
    """
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    executed = counts.get(TaskEventKind.ACTION_EXECUTED, 0)
    verified = counts.get(TaskEventKind.ACTION_VERIFIED, 0)
    replans = counts.get(TaskEventKind.TASK_REPLANNED, 0)
    parts = []
    if executed:
        parts.append(f"{executed} action(s)")
    if verified:
        parts.append(f"{verified} verified")
    if replans:
        parts.append(f"replanned {replans} time(s)")
    return ", ".join(parts) if parts else "nothing ran"
