"""V.O.I.D's orchestration layer: how a goal becomes verified work.

This package is the V3 addition to V.O.I.D, and it is deliberately *above* the V2 capability layer rather
than beside it. V2 answers "what can V.O.I.D do and may it do this?" - tools, RiskGate, the kill switch,
the single ``Agent._run_call`` funnel. V3 answers "given this goal and the computer's current state, which
of those capabilities should run, in what order, and did it actually work?"

    goal -> intent -> observe -> route -> authorize -> act -> verify -> adapt -> complete

What lives here, and what deliberately does not:

``events``    the V.O.I.D-native interaction model. Semantic events about a task's life, owned by V.O.I.D
              rather than by whatever UI protocol is fashionable. An AG-UI or A2UI adapter would consume
              these; neither defines them.
``routes``    the route model and resolver. A *route* is an inspectable, scored way of reaching a goal;
              the resolver picks one. Existing valid state beats launching something new.
``apps``      the application registry: installed vs running vs preferred vs last-observed, over the
              existing ``AppCatalog`` rather than a second discovery mechanism.
``verify``    did the intended state actually happen? Structured checks first, never a screenshot by
              default.
``commands``  deterministic command semantics. "stop" while speaking means stop speaking, not cancel the
              task - decided here, before any model sees the words.
``replan``    applying an owner's mid-task change: keep completed work, invalidate what the change
              affects, continue.
``trace``     V.O.I.D's own observability vocabulary, mapped onto OpenTelemetry spans.

Three rules hold across the package:

**Nothing here authorizes anything.** Orchestration decides what to *attempt*. Every attempt still goes
through ``Agent._run_call`` - kill switch, then the tool's own risk level, then ``RiskGate``. A route is a
proposal; a plan is a proposal; a verification result is an observation. None of them is a permission, and
none of them can raise a risk level or skip a confirmation.

**Nothing here is a second capability.** The registry reads the existing catalog, routes execute existing
tools, verification uses existing observation tools. If orchestration needed a capability V2 lacks, the
right move is a new Tool in the existing registry, not a private execution path here.

**Everything here is deterministic where it can be.** Route scoring, state transitions, command
classification and verification are pure functions over observed state, tested as such. The model is
consulted for semantic interpretation and planning - not to decide whether a tab exists, whether an
application is installed, or whether the owner said "stop".
"""
from __future__ import annotations

from void.orchestration.events import (TaskEvent, TaskEventKind, EventLog)

__all__ = ["TaskEvent", "TaskEventKind", "EventLog"]
