"""V.O.I.D's observability vocabulary, mapped onto OpenTelemetry.

The blueprint's rule here is "do not reinvent tracing" but also "V.O.I.D owns the meaning of its events".
Both are satisfied the same way: V.O.I.D defines the *semantics* - what a task, a route, an action, a
verification and a replan are - and OpenTelemetry carries them.

Two deliberate decisions, both of which keep this cheap:

**The API only, no SDK.** ``opentelemetry.trace`` is already present in this environment as a transitive
dependency; ``opentelemetry.sdk`` is not. The API alone is enough to *emit* spans: with no SDK configured it
hands back a no-op tracer, so instrumenting costs nothing and breaks nothing. Whoever wants to collect
traces installs and configures an SDK and exporter in their own deployment - which is exactly the right
division, because V.O.I.D should not choose anyone's telemetry backend. No dependency was added for this.

**``void/perf`` is not replaced.** V.O.I.D's existing performance stream is a privacy-by-construction
allowlist that has been load-bearing since V2, and it stays the local record. This module is an *additional*
mapping for distributed tracing, not a migration. A span and a perf event can describe the same action
without either owning the other.

**Attributes are allowlisted, like perf events.** A span attribute travels further than a log line - to a
collector, possibly off the machine - so the same discipline applies: identifiers, program-controlled names,
booleans and numbers. There is no attribute for a goal's text, a transcript, tool arguments, page content,
screen content or memory. ``ATTRIBUTES`` is the closed set, and :func:`_clean` drops anything else.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator

#: V.O.I.D's span names. One per meaningful phase, so a trace reads as the blueprint's pipeline.
class Span:
    TASK = "void.task"                   # the whole goal
    PLAN = "void.plan"                   # interpreting and planning
    RESOLVE_ROUTE = "void.route.resolve"  # candidate routes -> selection
    ACTION = "void.action"               # one capability execution
    VERIFY = "void.verify"               # checking the intended state happened
    REPLAN = "void.replan"               # rebuilding after a change or a failure
    PERCEIVE = "void.perceive"           # reading the world
    PROVIDER = "void.provider"           # a model call

    ALL = frozenset({TASK, PLAN, RESOLVE_ROUTE, ACTION, VERIFY, REPLAN, PERCEIVE, PROVIDER})


#: Attribute name -> accepted Python types. The closed set; everything else is dropped.
#:
#: Note what is absent and why: no ``goal``, no ``transcript``, no ``arguments``, no ``content``, no
#: ``detail``. A span is for correlation and timing, not for carrying what the owner said or what a page
#: contained. ``task_id`` and ``route_id`` are engine-minted identifiers, safe to correlate on.
ATTRIBUTES: dict[str, tuple] = {
    "void.task_id": (str,),
    "void.task_status": (str,),
    "void.route_id": (str,),
    "void.route_kind": (str,),
    "void.route_reuses_existing": (bool,),
    "void.route_candidates": (int,),
    "void.step": (int,),
    "void.tool": (str,),
    "void.risk": (str,),
    "void.ok": (bool,),
    "void.verdict": (str,),
    "void.verify_method": (str,),
    "void.failure_kind": (str,),
    "void.replans": (int,),
    "void.provider": (str,),
    "void.model": (str,),
    "void.llm_calls": (int,),
    "void.duration_s": (int, float),
    "void.artifacts": (int,),
}

#: A string attribute longer than this is truncated. Names and ids are short; anything long is a smell.
MAX_VALUE = 64


def _clean(attributes: dict | None) -> dict:
    """Drop anything not in :data:`ATTRIBUTES`, and bound what remains.

    Fails closed: an unknown key is discarded rather than passed through, so adding a span attribute is a
    deliberate edit to the allowlist above and not something a caller can do by accident.
    """
    out: dict = {}
    for key, value in (attributes or {}).items():
        allowed = ATTRIBUTES.get(key)
        if allowed is None:
            continue
        if isinstance(value, bool) and bool not in allowed:
            continue
        if not isinstance(value, allowed):
            continue
        if isinstance(value, str):
            value = " ".join(value.split())[:MAX_VALUE]
            if not value:
                continue
        out[key] = value
    return out


def _tracer():
    """The OpenTelemetry tracer, or None when the API is not importable.

    With the API present but no SDK configured this returns a real tracer object whose spans are no-ops,
    which is the behaviour that makes instrumenting free.
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer("void.orchestration")
    except Exception:                                          # noqa: BLE001 - telemetry is never required
        return None


def available() -> bool:
    """Whether OpenTelemetry can be reached at all. Observation only; nothing depends on it being True."""
    return _tracer() is not None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Record one V.O.I.D phase as an OpenTelemetry span.

    Used as ``with span(Span.ACTION, **{"void.tool": "launch_app"}):``. Never raises and never changes
    control flow: if OpenTelemetry is missing, misconfigured, or throws, the body still runs and the caller
    cannot tell. Telemetry that can break the work it measures is worse than no telemetry.
    """
    # Everything that could fail is resolved BEFORE the yield, so there is exactly one yield on every
    # path. Getting this wrong is easy and the symptom is nasty: a generator that yields twice, or not at
    # all, turns a telemetry problem into a control-flow problem in the code being measured.
    started = None
    try:
        tracer = _tracer()
        cleaned = _clean(attributes)
        if tracer is not None:
            started = tracer.start_as_current_span(name, attributes=cleaned)
    except Exception:                                          # noqa: BLE001 - never required
        started = None
    if started is None:
        yield None
        return
    try:
        with started as active:
            yield active
    except GeneratorExit:
        raise
    except Exception:
        # The BODY raised. Re-raise it: swallowing the caller's exception to protect a span would be far
        # worse than losing the span.
        raise


def record(event, *, task_status: str | None = None) -> dict:
    """A :class:`~void.orchestration.events.TaskEvent` as allowlisted span attributes.

    The bridge between V.O.I.D's own interaction model and OpenTelemetry: the event stream is the source of
    truth about what happened, and this projects the parts that are safe to export. ``detail`` is
    deliberately not projected - it is a human line for the owner, not a trace attribute.
    """
    attributes: dict = {"void.task_id": getattr(event, "task_id", "")}
    for source, target in (("tool", "void.tool"), ("route", "void.route_id"),
                           ("step", "void.step"), ("ok", "void.ok"),
                           ("duration_s", "void.duration_s")):
        value = getattr(event, source, None)
        if value is not None:
            attributes[target] = value
    if task_status:
        attributes["void.task_status"] = task_status
    return _clean(attributes)
