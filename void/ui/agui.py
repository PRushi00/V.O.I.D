"""AG-UI: V.O.I.D's task events, published in a protocol a front end already understands.

V.O.I.D already has an event vocabulary - :class:`~void.orchestration.events.TaskEventKind`, fifteen kinds
covering a task from started to completed - and an :class:`~void.orchestration.events.EventLog` that
anything can subscribe to. This module is a translation layer on top of that subscription and nothing more.
No UI is rewritten, no event is invented, and if AG-UI were dropped tomorrow this file would be deleted and
the rest of V.O.I.D would not notice.

**The direction is the security property.** This adapter is *outbound only*. It has no method that executes
a tool, authorizes an action, changes a task, selects a provider, or answers a confirmation. A front end
receives a stream describing what V.O.I.D is doing; it cannot use that stream to make V.O.I.D do anything.
An AG-UI event is therefore not a request and carries no authority - the same stance V.O.I.D already takes
towards web content, memory and tool output.

That matters because the obvious next step - "let the UI send events back" - is exactly how a UI becomes a
confirmation bypass. If an inbound path is ever added it must arrive as an ordinary request through
``Assistant.run``, pass the control-command classifier, the RiskGate and the consequential check like any
other input. There is deliberately no shortcut here for it to use.

**What travels is allowlisted.** Fields come from :data:`SAFE_FIELDS`: engine-minted identifiers, tool
names, risk levels, booleans and counts. No goal text, no transcript, no tool arguments, no page or screen
content, no memory. This is the same discipline as ``void/perf`` and ``void/orchestration/trace`` and for
the same reason - an event here travels further than a log line, possibly to a browser.

Protocol version: ``ag-ui-protocol`` **1.0.0**, pinned. Pinned rather than tracking latest because an event
schema that changes under a running front end is a broken front end, and because the mapping below is
written against the event types that release actually defines.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from void.orchestration.events import TaskEventKind

_log = logging.getLogger(__name__)

#: The pinned AG-UI protocol package version this mapping was written against.
AGUI_VERSION = "1.0.0"

#: Event fields that may be published, matching :class:`~void.orchestration.events.TaskEvent`'s own closed
#: field set. Everything else is dropped.
#:
#: ``detail`` is excluded deliberately, even though ``TaskEvent`` bounds it and documents it as never
#: carrying tool arguments or content. It is the one free-text field in the event, so it is the one field
#: whose contents this module cannot reason about - and an AG-UI event may end up rendered in a browser.
#: A front end showing "V.O.I.D is opening Notepad" needs the tool name, not a sentence.
SAFE_FIELDS = {
    "task_id": str, "tool": str, "route": str, "step": int, "ok": bool, "duration_s": float,
}

#: Longest string published for any single field.
MAX_VALUE = 120


class AgUiUnavailable(RuntimeError):
    """The AG-UI protocol package is not installed. Never raised at a caller."""


def safe_payload(fields: dict | None) -> dict:
    """Allowlist and bound an event's fields.

    Fails closed: an unrecognised key is dropped, so publishing a new field is a deliberate edit to
    :data:`SAFE_FIELDS` rather than something that happens because an emitter added a keyword argument.
    """
    out: dict = {}
    for key, value in (fields or {}).items():
        expected = SAFE_FIELDS.get(key)
        if expected is None:
            continue
        if value is None:
            continue                     # TaskEvent leaves inapplicable fields as None
        if expected is bool:
            if not isinstance(value, bool):
                continue
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
        elif expected is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            value = round(float(value), 3)
        elif expected is str:
            if not isinstance(value, str):
                continue
            value = " ".join(value.split())[:MAX_VALUE]
            if not value:
                continue
        out[key] = value
    return out


#: V.O.I.D event kind -> the AG-UI shape that carries it, and a step name where one applies.
#:
#: Chosen so a front end's ordinary run lifecycle works without understanding V.O.I.D: a task is a *run*,
#: an action is a *tool call*, and the phases in between are *steps*. The kinds AG-UI has no first-class
#: shape for - paused, resumed, replanned, artifact created, a question for the owner - are published as
#: ``CUSTOM`` under a stable ``void.*`` name rather than being forced into an ill-fitting type or dropped.
MAPPING = {
    TaskEventKind.TASK_STARTED: ("run_started", ""),
    TaskEventKind.INTENT_RESOLVED: ("step_started", "void.understand"),
    TaskEventKind.ROUTE_SELECTED: ("step_started", "void.route"),
    TaskEventKind.ACTION_PROPOSED: ("tool_call_start", ""),
    TaskEventKind.ACTION_EXECUTED: ("tool_call_end", ""),
    TaskEventKind.ACTION_VERIFIED: ("step_finished", "void.verify"),
    TaskEventKind.TASK_PROGRESS: ("step_finished", "void.progress"),
    TaskEventKind.ASK_USER: ("custom", "void.ask_user"),
    TaskEventKind.TASK_PAUSED: ("custom", "void.paused"),
    TaskEventKind.TASK_RESUMED: ("custom", "void.resumed"),
    TaskEventKind.TASK_REPLANNED: ("custom", "void.replanned"),
    TaskEventKind.ARTIFACT_CREATED: ("custom", "void.artifact"),
    TaskEventKind.TASK_FAILED: ("run_error", ""),
    TaskEventKind.TASK_COMPLETED: ("run_finished", ""),
    TaskEventKind.TASK_CANCELLED: ("custom", "void.cancelled"),
}


@dataclass
class Published:
    """One translated event, kept as data so the mapping is testable without a front end."""

    kind: str
    agui_type: str
    payload: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "agui_type": self.agui_type, "payload": dict(self.payload)}


class AgUiAdapter:
    """Publishes V.O.I.D task events as AG-UI events. Outbound only.

    Attach with ``AgUiAdapter(event_log, sink)``; ``sink`` receives each AG-UI event object. A sink that
    raises is dropped and counted, never propagated - a front end that falls over must not fail a task,
    which is the same rule telemetry follows.
    """

    def __init__(self, event_log=None, sink=None, thread_id: str = "void"):
        self._sink = sink
        self._thread_id = str(thread_id or "void")[:64]
        #: Translated events, bounded, for tests and for a front end that connects late.
        self.published: list[Published] = []
        self.dropped = 0
        self.sink_failures = 0
        self._types = _event_types()
        if event_log is not None:
            try:
                event_log.subscribe(self.on_event)
            except Exception:                                   # noqa: BLE001 - never block startup
                _log.info("AGUI_SUBSCRIBE_FAILED")

    @property
    def available(self) -> bool:
        """Whether the AG-UI protocol package is importable. Nothing depends on it being True."""
        return self._types is not None

    def on_event(self, event) -> Published | None:
        """Translate and publish one V.O.I.D task event. Never raises."""
        try:
            return self._publish(event)
        except Exception as exc:                                # noqa: BLE001
            self.dropped += 1
            _log.info("AGUI_TRANSLATE_FAILED kind=%s", type(exc).__name__)
            return None

    def _publish(self, event) -> Published | None:
        kind = getattr(event, "kind", "")
        mapped = MAPPING.get(kind)
        if mapped is None:
            # An unmapped kind is a gap in this file, not something to guess at.
            self.dropped += 1
            _log.info("AGUI_UNMAPPED kind=%s", kind)
            return None
        agui_type, step_name = mapped
        task_id = str(getattr(event, "task_id", "") or "")[:64]
        # Read the event's own closed field set. ``as_dict`` is used when available because it is the
        # shape TaskEvent already offers a UI adapter, and it omits fields that were never set.
        try:
            raw = event.as_dict()
        except Exception:                                       # noqa: BLE001
            raw = {name: getattr(event, name, None)
                   for name in ("task_id", "tool", "route", "step", "ok", "duration_s")}
        payload = safe_payload(raw)
        payload["task_id"] = task_id

        record = Published(kind=kind, agui_type=agui_type, payload=payload)
        self.published.append(record)
        if len(self.published) > 500:
            del self.published[:250]

        built = self._build(agui_type, step_name, task_id, payload)
        if built is not None and callable(self._sink):
            try:
                self._sink(built)
            except Exception as exc:                            # noqa: BLE001 - a UI must not fail a task
                self.sink_failures += 1
                _log.info("AGUI_SINK_FAILED kind=%s", type(exc).__name__)
        return record

    def _build(self, agui_type: str, step_name: str, task_id: str, payload: dict):
        """Construct the AG-UI event object, or None when the protocol package is absent.

        Returning None rather than raising means V.O.I.D works identically with AG-UI uninstalled: the
        translation still happens and is recorded in ``published``, there is simply nothing to hand a sink.
        """
        types = self._types
        if types is None:
            return None
        run_id = task_id or "void"
        try:
            if agui_type == "run_started":
                return types["RunStartedEvent"](thread_id=self._thread_id, run_id=run_id)
            if agui_type == "run_finished":
                return types["RunFinishedEvent"](thread_id=self._thread_id, run_id=run_id,
                                                 result=payload)
            if agui_type == "run_error":
                # The MESSAGE is a failure kind, never a model message or an exception string: those can
                # carry a path, a URL or a fragment of page content.
                return types["RunErrorEvent"](
                    message=payload.get("failure_kind") or "task_failed",
                    code=payload.get("verdict") or None)
            if agui_type == "step_started":
                return types["StepStartedEvent"](step_name=step_name or "void.step")
            if agui_type == "step_finished":
                return types["StepFinishedEvent"](step_name=step_name or "void.step")
            if agui_type == "tool_call_start":
                return types["ToolCallStartEvent"](
                    tool_call_id=_call_id(task_id, payload),
                    tool_call_name=payload.get("tool") or "action")
            if agui_type == "tool_call_end":
                return types["ToolCallEndEvent"](tool_call_id=_call_id(task_id, payload))
            if agui_type == "custom":
                return types["CustomEvent"](name=step_name or "void.event", value=payload)
        except Exception as exc:                                # noqa: BLE001
            self.dropped += 1
            _log.info("AGUI_BUILD_FAILED type=%s kind=%s", agui_type, type(exc).__name__)
            return None
        return None

    def snapshot(self) -> list[dict]:
        """Everything translated so far, for a front end that connects after a task started."""
        return [record.as_dict() for record in self.published]


def _call_id(task_id: str, payload: dict) -> str:
    """A stable identifier for one action within a task.

    Derived from the task id and step so the start and end of the same action correlate, which a front end
    needs in order to close a tool call it opened.
    """
    step = payload.get("step")
    return f"{task_id or 'void'}:{step if isinstance(step, int) else 0}"


def _event_types():
    """The AG-UI event classes, or None when the package is absent."""
    try:
        from ag_ui.core import events
    except Exception:                                           # noqa: BLE001 - optional integration
        return None
    wanted = ("RunStartedEvent", "RunFinishedEvent", "RunErrorEvent", "StepStartedEvent",
              "StepFinishedEvent", "ToolCallStartEvent", "ToolCallEndEvent", "CustomEvent")
    found = {}
    for name in wanted:
        cls = getattr(events, name, None)
        if cls is None:
            return None
        found[name] = cls
    return found
