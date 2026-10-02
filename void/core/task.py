"""Task model and durable checkpoint store.

A task belongs to V.O.I.D, not to a device or a single run. Its full state -
goal, message history, step count, status - is persisted to SQLite after every
step, so a crash or a stop can be resumed from the last checkpoint instead of
starting over.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# A task still `running` this long after its last checkpoint belongs to a process that
# died (every step re-saves it), so it can no longer be running.
STALE_RUNNING_AFTER_S = 900.0
INTERRUPTED_ERROR = "interrupted: process exited"


class Status:
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"                       # stopped/interrupted but resumable
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # a HIGH-risk step needs owner OK
    BLOCKED = "blocked"                     # halted awaiting an external condition
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"                 # owner-cancelled; not resumable
    # --- V3 orchestration phases (void/orchestration) ---------------------
    # Added for the V3 task engine, which needs to distinguish "deciding what to do" and "checking it
    # worked" from "doing it". PENDING remains the created-but-not-started state the blueprint calls
    # CREATED; AWAITING_CONFIRMATION remains WAITING_FOR_USER. Nothing was renamed: V2 statuses are
    # persisted in the owner's existing database and in tests, so these are additions only.
    PLANNING = "planning"                   # interpreting the goal / choosing a route
    REPLANNING = "replanning"               # a failure or an owner change forced a new plan
    VERIFYING = "verifying"                 # checking the intended state actually happened

    # Resumable = can still make progress (with owner input where needed).
    RESUMABLE = {RUNNING, PAUSED, AWAITING_CONFIRMATION, PLANNING, REPLANNING, VERIFYING}
    # Terminal = no further execution.
    TERMINAL = {COMPLETED, FAILED, CANCELLED}
    # Every recognized status. A persisted/in-memory value outside this set is
    # corrupted or from an incompatible future version, never a value this
    # code intentionally wrote - it must fail closed, never be treated as
    # implicitly resumable.
    ALL = {PENDING, RUNNING, PAUSED, AWAITING_CONFIRMATION, BLOCKED,
           COMPLETED, FAILED, CANCELLED, PLANNING, REPLANNING, VERIFYING}
    # Phases where V.O.I.D is deciding rather than executing. Useful to a UI, and to the control-command
    # layer: "stop" during PLANNING pauses, exactly as it does during RUNNING.
    DECIDING = {PLANNING, REPLANNING, VERIFYING}


class CorruptedTaskState(ValueError):
    """A task's status is not a recognized Status value.

    Raised instead of silently coercing/defaulting so a corrupted row or an
    incompatible future status can never be treated as resumable.
    """


@dataclass
class Task:
    goal: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = Status.PENDING
    messages: list[dict] = field(default_factory=list)
    steps: int = 0
    result: str | None = None
    error: str | None = None
    # A proposed-but-not-yet-executed operation held for owner input. Two shapes,
    # distinguished by an optional "kind":
    #  - confirmation (kind absent): {"assistant_text": str|None,
    #      "tool_calls": [{"name","arguments","id","signature",
    #                      "risk","requires_confirmation"}]}
    #      -> resolved by Agent.resume_pending(approve/deny).
    #  - directory disambiguation: {"kind": "directory_disambiguation",
    #      "candidates": [{"index": 1-based int, "name": str, "path": str}, ...],
    #      "prompt": str, "created_at": float}
    #      -> status is BLOCKED; resolved by Agent.resume_clarification(number).
    # Contains only tool names/arguments/risk metadata and already-confined
    # directory paths - never secrets/keys.
    pending: dict | None = None
    # Engine-owned execution ledger: one entry per COMMITTED logical step,
    # built by the Agent from ACTUAL tool execution (never written by the LLM).
    # Entry shape: {"index": int,
    #               "calls": [{"tool": str, "arguments_summary": str}],
    #               "status": "executing|succeeded|failed|cancelled|
    #                          awaiting_confirmation",
    #               "outcome_summary": str,
    #               "unresolved_failure": bool (optional; True when a tool
    #                 execution failed and still needs handling)}.
    # Observability/recovery context only - NOT an executable script, and never
    # a place for secrets or raw tool output.
    plan: list[dict] = field(default_factory=list)
    # Engine-owned pointer to the current/most-recent logical step (index into
    # plan). Never set by the LLM.
    current_step: int = 0
    # --- V3 orchestration state (void/orchestration) ----------------------
    # One dict, persisted in one additional column, holding what the V3 task engine needs beyond the V2
    # ledger. A dict rather than a dozen columns because these fields are read and written together, by
    # one subsystem, and a schema change per field would be churn for no gain.
    #
    # Shape (every key optional; absent means "not known yet"):
    #   {"intent":        str      - what V.O.I.D understood the goal to be (NOT the model's reasoning)
    #    "route":         dict     - the selected route's inspectable form (Route.as_dict())
    #    "applications":  [str]    - application names this task has touched
    #    "artifacts":     [{"path": str, "kind": str, "verified": bool}]  - files produced
    #    "checkpoints":   [{"step": int, "at": float, "label": str}]      - resume points
    #    "verifications": [{"step": int, "ok": bool, "how": str, "detail": str}]
    #    "modifications": [{"at": float, "asked": str, "from_step": int}] - owner changes mid-task
    #    "failures":      [{"step": int, "at": float, "kind": str, "detail": str}]
    #    "replans":       int      - how many times the plan has been rebuilt
    #   }
    #
    # SECURITY: engine-owned, exactly like ``plan``. It is written by V.O.I.D from what actually happened,
    # never by a model, and it carries no secrets, no tool arguments and no file contents - only paths the
    # file layer already confined, program-controlled names, counts and short summaries. Task state is
    # NOT memory and cannot authorize anything: nothing in here is ever read by RiskGate.
    v3: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


class TaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Overwritten/deleted row content is zeroed instead of lingering in free pages: a value that is
        # cleared or redacted (e.g. a resolved pending step) must not stay readable in the file.
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id          TEXT PRIMARY KEY,
                    goal        TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    messages    TEXT NOT NULL,
                    steps       INTEGER NOT NULL,
                    result      TEXT,
                    error       TEXT,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    pending     TEXT,
                    plan        TEXT,
                    current_step INTEGER,
                    v3          TEXT
                )
                """
            )
            # Migrate pre-existing databases additively (deterministic,
            # non-destructive). Old rows keep their data; new columns default to
            # NULL and load as an empty ledger / step 0.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "pending" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN pending TEXT")
            if "plan" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN plan TEXT")
            if "current_step" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN current_step INTEGER")
            if "v3" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN v3 TEXT")

    def save(self, task: Task) -> None:
        """Insert or update a task - this is the checkpoint operation."""
        task.touch()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, goal, status, messages, steps, result, error,
                     created_at, updated_at, pending, plan, current_step, v3)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,
                    status=excluded.status,
                    messages=excluded.messages,
                    steps=excluded.steps,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    pending=excluded.pending,
                    plan=excluded.plan,
                    current_step=excluded.current_step,
                    v3=excluded.v3
                """,
                (
                    task.id, task.goal, task.status,
                    json.dumps(task.messages), task.steps,
                    task.result, task.error,
                    task.created_at, task.updated_at,
                    json.dumps(task.pending) if task.pending is not None else None,
                    json.dumps(task.plan) if task.plan else None,
                    task.current_step,
                    json.dumps(task.v3) if task.v3 else None,
                ),
            )

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        keys = row.keys()
        status = row["status"]
        if status not in Status.ALL:
            # Never coerce/default a corrupted or unrecognized status - fail
            # closed instead of silently returning a Task that downstream
            # code (e.g. Agent.resume) could treat as resumable. The row
            # itself is left untouched.
            raise CorruptedTaskState(
                f"Task {row['id']!r} has an unrecognized status {status!r}; "
                f"refusing to load it."
            )
        pending_raw = row["pending"] if "pending" in keys else None
        pending = json.loads(pending_raw) if pending_raw else None
        # Malformed stored ledger data must fail safely to an empty ledger,
        # never crash load or corrupt engine state.
        plan: list[dict] = []
        if "plan" in keys and row["plan"]:
            try:
                loaded = json.loads(row["plan"])
                if isinstance(loaded, list):
                    plan = loaded
            except (ValueError, TypeError):
                plan = []
        current_step = 0
        if "current_step" in keys and row["current_step"] is not None:
            try:
                current_step = int(row["current_step"])
            except (ValueError, TypeError):
                current_step = 0
        # Malformed V3 state fails safely to empty, exactly as a malformed ledger does: a task whose
        # orchestration state cannot be read is still a loadable, resumable task.
        v3: dict = {}
        if "v3" in keys and row["v3"]:
            try:
                loaded_v3 = json.loads(row["v3"])
                if isinstance(loaded_v3, dict):
                    v3 = loaded_v3
            except (ValueError, TypeError):
                v3 = {}
        return Task(
            id=row["id"], goal=row["goal"], status=status,
            messages=json.loads(row["messages"]), steps=row["steps"],
            result=row["result"], error=row["error"],
            pending=pending, plan=plan, current_step=current_step, v3=v3,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def load(self, task_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list(self, status: str | None = None, limit: int = 50) -> list[Task]:
        query = "SELECT * FROM tasks"
        params: tuple = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = params + (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    def sweep_stale(self, *, older_than_s: float = STALE_RUNNING_AFTER_S,
                    dry_run: bool = False, now: float | None = None) -> list[str]:
        """Mark tasks stuck ``running`` after their process died as ``paused``.

        Fail-safe by construction: the result is PAUSED (resumable only by the owner via
        ``resume``), never COMPLETED, and nothing is executed or auto-resumed. Only
        ``running`` rows whose ``updated_at`` is older than ``older_than_s`` are touched;
        anything a live process checkpointed recently, and every other status, is left
        exactly as it was. ``updated_at`` is preserved so history stays honest. Selection
        and update happen in one write transaction, so a concurrent checkpoint cannot be
        clobbered. Returns the affected ids (would-be ids when ``dry_run``)."""
        cutoff = (time.time() if now is None else now) - older_than_s
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM tasks WHERE status=? AND updated_at < ? ORDER BY updated_at",
                (Status.RUNNING, cutoff))]
            if ids and not dry_run:
                conn.executemany(
                    "UPDATE tasks SET status=?, error=? WHERE id=? AND status=? AND updated_at < ?",
                    [(Status.PAUSED, INTERRUPTED_ERROR, i, Status.RUNNING, cutoff) for i in ids])
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        if ids:
            _log.info("TASK_SWEEP %s count=%d", "would_pause" if dry_run else "paused", len(ids))
        return ids

    def resumable(self) -> list[Task]:
        """Tasks that were interrupted and can be resumed."""
        out: list[Task] = []
        for st in Status.RESUMABLE:
            out.extend(self.list(status=st))
        out.sort(key=lambda t: t.updated_at, reverse=True)
        return out
