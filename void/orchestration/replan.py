"""Changing a task that is already running, without throwing away what is done.

The behaviour this exists for:

    V.O.I.D is three steps into writing a report.
    Owner: "Instead of that, add a section on costs."

Restarting is the wrong answer - the research is done, the outline is done, and redoing them wastes minutes
and may produce something different. Ignoring the change is also wrong. The right answer is to keep what is
still valid, invalidate what the change affects, and continue from there.

So a modification is applied in four deterministic moves:

1. **Find the boundary.** Steps already committed stay; steps not yet executed are the ones a change can
   touch. The boundary is the task's own ledger position, not a model's opinion about it.
2. **Keep completed work.** A succeeded step is never rewritten. Its record stays in the ledger exactly as
   it was, because it is the evidence that the work happened.
3. **Invalidate what is affected.** Pending steps after the boundary are marked superseded rather than
   deleted - a task's history should show that a plan changed, not silently appear to have always been the
   new plan.
4. **Record and hand back.** The modification is appended to the task's own ``v3`` state and the status
   moves to REPLANNING, which is the signal to whatever plans next that it should plan from here.

Two things this deliberately does **not** do. It does not undo completed work: reversing a side effect is a
different, consequential operation that needs its own authorization, and silently "rolling back" a sent
message or a deleted file would be far worse than leaving it. And it does not decide *what* the new plan is -
that is semantic work for the planner; this module establishes the state the planner starts from.

A modification is an instruction about work, never an authorization. The owner asking for a different
section does not pre-approve the steps that produce it: each new step is authorized when it runs, by its own
risk level and RiskGate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from void.core.task import Status, Task

#: How much of the owner's requested change is retained. The full phrasing belongs to the planner's prompt;
#: what the task state keeps is a record that a change was asked for and roughly what it was.
MAX_ASK = 300

#: Ledger statuses that mean a step is finished and must not be touched by a modification.
_COMMITTED = frozenset({"succeeded", "failed", "cancelled"})

#: What a superseded step's status becomes. A distinct value, so "we chose not to do this because the plan
#: changed" is never confused with "this failed" or "the owner cancelled it".
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Modification:
    """One owner-requested change to a running task."""

    asked: str
    at: float = 0.0
    #: The ledger index from which the plan is being rebuilt. Steps before this are kept.
    from_step: int = 0
    #: How many pending steps this invalidated.
    invalidated: int = 0
    #: How many committed steps were preserved.
    preserved: int = 0

    def as_dict(self) -> dict:
        return {"asked": self.asked, "at": round(self.at or time.time(), 3),
                "from_step": self.from_step, "invalidated": self.invalidated,
                "preserved": self.preserved}


def _short(text: object, limit: int = MAX_ASK) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split())[:limit]


def boundary(task: Task) -> int:
    """The first ledger index a modification may touch.

    Everything before it is committed. Computed from the ledger rather than from ``current_step`` alone,
    because a step can be in flight: the honest boundary is after the last step that actually finished.
    """
    committed = 0
    for index, entry in enumerate(task.plan or []):
        if str(entry.get("status", "")).lower() in _COMMITTED:
            committed = index + 1
    return committed


def can_modify(task: Task) -> tuple[bool, str]:
    """Whether this task can still be changed, and why not when it cannot.

    A terminal task is not modifiable: "instead of that" after something completed is a new request, not an
    amendment, and treating it as an amendment would silently resurrect finished work.
    """
    if task.status in Status.TERMINAL:
        return False, f"that task is already {task.status}"
    return True, ""


def apply_modification(task: Task, asked: str) -> Modification | None:
    """Apply an owner's change in place. Returns what was done, or None when it could not be applied.

    Mutates the task and leaves it in REPLANNING for the planner to pick up. The caller is responsible for
    persisting it (``store.save(task)``) - this function does no I/O, which keeps it a pure, testable state
    transition.
    """
    text = _short(asked)
    if not text:
        return None
    allowed, _why = can_modify(task)
    if not allowed:
        return None

    from_step = boundary(task)
    ledger = task.plan or []
    invalidated = 0
    for entry in ledger[from_step:]:
        if str(entry.get("status", "")).lower() in _COMMITTED:
            continue
        # Marked, not deleted: the history should show that the plan changed here.
        entry["status"] = SUPERSEDED
        entry["outcome_summary"] = "superseded by a change the owner asked for"
        entry.pop("unresolved_failure", None)
        invalidated += 1

    modification = Modification(asked=text, at=time.time(), from_step=from_step,
                                invalidated=invalidated, preserved=from_step)
    state = task.v3 if isinstance(task.v3, dict) else {}
    state.setdefault("modifications", []).append(modification.as_dict())
    state["replans"] = int(state.get("replans") or 0) + 1
    task.v3 = state
    task.current_step = from_step
    task.status = Status.REPLANNING
    task.touch()
    return modification


# --- replanning after a failure ---------------------------------------------------------------------

class FailureKind:
    """Why a step failed, which is what decides whether another route could help.

    The distinction that matters: a failure of the *route* (this way of doing it did not work) can often be
    recovered by choosing a different route. A failure of *authorization* or a deliberate stop cannot, and
    retrying it would be both useless and, in the authorization case, badgering the owner.
    """

    ROUTE_FAILED = "route_failed"            # the mechanism failed - try another route
    TARGET_MISSING = "target_missing"        # the thing does not exist - a different route will not help
    DENIED = "denied"                        # the owner or policy said no - never retry around this
    STOPPED = "stopped"                      # the kill switch - stop, full stop
    UNKNOWN = "unknown"

    ALL = frozenset({ROUTE_FAILED, TARGET_MISSING, DENIED, STOPPED, UNKNOWN})
    #: Kinds where trying a materially different route is reasonable.
    RECOVERABLE = frozenset({ROUTE_FAILED, UNKNOWN})


@dataclass(frozen=True)
class RecoveryDecision:
    """What to do about a failure. Deterministic, from the failure kind and what has been tried."""

    should_replan: bool
    why: str
    #: Route ids already attempted for this step, so a replan does not propose one of them again.
    exclude_routes: frozenset[str] = frozenset()


def classify_failure(kind_hint: str, detail: str = "") -> str:
    """Map an execution outcome onto a :class:`FailureKind`.

    Takes the ``kind`` the agent funnel already produces (``unauthorized`` / ``unknown`` /
    ``tool_failure``), so this is a translation of existing V2 outcomes rather than a second classifier with
    its own opinions.
    """
    hint = (kind_hint or "").strip().lower()
    if hint in ("unauthorized", "denied"):
        return FailureKind.DENIED
    if hint in ("stopped", "stop_requested"):
        return FailureKind.STOPPED
    if hint in ("unknown", "not_found", "missing"):
        return FailureKind.TARGET_MISSING
    if hint in ("tool_failure", "failed", "error"):
        return FailureKind.ROUTE_FAILED
    return FailureKind.UNKNOWN


def decide_recovery(task: Task, failure_kind: str, *, attempted_routes=(),
                    max_replans: int = 3) -> RecoveryDecision:
    """Should V.O.I.D try a different route, and if so what must it avoid?

    The bound is the point. "Do not blindly retry the same action forever" needs a number, and
    ``max_replans`` is it: after that many rebuilds the task fails honestly rather than looping. The
    exclusion set is the other half - a replan that proposed the route that just failed would be a retry
    wearing a different hat.
    """
    attempted = frozenset(route for route in attempted_routes if route)
    if failure_kind not in FailureKind.RECOVERABLE:
        return RecoveryDecision(False, f"a {failure_kind} failure is not something another route fixes",
                                attempted)
    state = task.v3 if isinstance(task.v3, dict) else {}
    replans = int(state.get("replans") or 0)
    if replans >= max(0, int(max_replans)):
        return RecoveryDecision(False, f"already replanned {replans} time(s); stopping instead of looping",
                                attempted)
    return RecoveryDecision(True, "the route failed, so a materially different one is worth trying",
                            attempted)


def record_failure(task: Task, *, step: int, kind: str, detail: str = "") -> None:
    """Append a failure to the task's own state. Counts and short reasons, never tool arguments."""
    state = task.v3 if isinstance(task.v3, dict) else {}
    state.setdefault("failures", []).append(
        {"step": int(step), "at": round(time.time(), 3),
         "kind": kind if kind in FailureKind.ALL else FailureKind.UNKNOWN,
         "detail": _short(detail, 200)})
    task.v3 = state
    task.touch()


def checkpoint(task: Task, label: str = "") -> dict:
    """Record a resume point. Cheap, and what makes a long task recoverable after a restart."""
    state = task.v3 if isinstance(task.v3, dict) else {}
    entry = {"step": int(task.current_step), "at": round(time.time(), 3), "label": _short(label, 80)}
    state.setdefault("checkpoints", []).append(entry)
    task.v3 = state
    task.touch()
    return entry


def note_artifact(task: Task, path: str, kind: str = "", *, verified: bool = False) -> dict:
    """Record a file this task produced.

    ``path`` is expected to be one the confined file layer already accepted - this records what happened,
    it does not grant access to anything.
    """
    state = task.v3 if isinstance(task.v3, dict) else {}
    entry = {"path": _short(path, 300), "kind": _short(kind, 20), "verified": bool(verified)}
    state.setdefault("artifacts", []).append(entry)
    task.v3 = state
    task.touch()
    return entry
