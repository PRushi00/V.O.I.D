"""Task model and durable checkpoint store.

A task belongs to V.O.I.D, not to a device or a single run. Its full state -
goal, message history, step count, status - is persisted to SQLite after every
step, so a crash or a stop can be resumed from the last checkpoint instead of
starting over.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class Status:
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"       # stopped/interrupted but resumable
    COMPLETED = "completed"
    FAILED = "failed"

    RESUMABLE = {RUNNING, PAUSED}
    TERMINAL = {COMPLETED, FAILED}


@dataclass
class Task:
    goal: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = Status.PENDING
    messages: list[dict] = field(default_factory=list)
    steps: int = 0
    result: str | None = None
    error: str | None = None
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
                    updated_at  REAL NOT NULL
                )
                """
            )

    def save(self, task: Task) -> None:
        """Insert or update a task - this is the checkpoint operation."""
        task.touch()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, goal, status, messages, steps, result, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,
                    status=excluded.status,
                    messages=excluded.messages,
                    steps=excluded.steps,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    task.id, task.goal, task.status,
                    json.dumps(task.messages), task.steps,
                    task.result, task.error,
                    task.created_at, task.updated_at,
                ),
            )

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], goal=row["goal"], status=row["status"],
            messages=json.loads(row["messages"]), steps=row["steps"],
            result=row["result"], error=row["error"],
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

    def resumable(self) -> list[Task]:
        """Tasks that were interrupted and can be resumed."""
        out: list[Task] = []
        for st in Status.RESUMABLE:
            out.extend(self.list(status=st))
        out.sort(key=lambda t: t.updated_at, reverse=True)
        return out
