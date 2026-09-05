"""Tests for the durable task store and checkpointing."""
from void.core.task import Status, Task, TaskStore


def test_save_and_load_roundtrip(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    task = Task(goal="do the thing")
    task.messages = [{"role": "user", "content": "do the thing"}]
    task.steps = 3
    store.save(task)

    loaded = store.load(task.id)
    assert loaded is not None
    assert loaded.goal == "do the thing"
    assert loaded.steps == 3
    assert loaded.messages == task.messages


def test_update_is_upsert(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    task = Task(goal="g")
    store.save(task)
    task.steps = 5
    task.status = Status.COMPLETED
    store.save(task)

    loaded = store.load(task.id)
    assert loaded.steps == 5
    assert loaded.status == Status.COMPLETED
    # Only one row for this id.
    assert len([t for t in store.list() if t.id == task.id]) == 1


def test_resumable_filters_status(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    a = Task(goal="running", status=Status.RUNNING)
    b = Task(goal="paused", status=Status.PAUSED)
    c = Task(goal="done", status=Status.COMPLETED)
    for t in (a, b, c):
        store.save(t)

    resumable_ids = {t.id for t in store.resumable()}
    assert a.id in resumable_ids
    assert b.id in resumable_ids
    assert c.id not in resumable_ids


def test_list_by_status(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    store.save(Task(goal="x", status=Status.COMPLETED))
    store.save(Task(goal="y", status=Status.FAILED))
    assert len(store.list(status=Status.COMPLETED)) == 1


# --- Phase 5B: expanded states, pending, legacy compatibility ----------

import json
import sqlite3


def test_new_states_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    for st in (Status.AWAITING_CONFIRMATION, Status.BLOCKED, Status.CANCELLED):
        t = Task(goal=f"g-{st}", status=st)
        store.save(t)
        assert store.load(t.id).status == st


def test_status_sets():
    assert Status.AWAITING_CONFIRMATION in Status.RESUMABLE
    assert Status.CANCELLED in Status.TERMINAL
    assert Status.CANCELLED not in Status.RESUMABLE


def test_resumable_includes_awaiting_confirmation(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    a = Task(goal="awaiting", status=Status.AWAITING_CONFIRMATION)
    c = Task(goal="cancelled", status=Status.CANCELLED)
    store.save(a)
    store.save(c)
    ids = {t.id for t in store.resumable()}
    assert a.id in ids and c.id not in ids
    blocked = Task(goal="blocked", status=Status.BLOCKED)
    store.save(blocked)
    assert blocked.id not in {t.id for t in store.resumable()}
    assert Status.BLOCKED not in Status.RESUMABLE
    assert Status.BLOCKED not in Status.TERMINAL


def test_pending_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    t = Task(goal="g", status=Status.AWAITING_CONFIRMATION)
    t.pending = {"assistant_text": "deleting",
                 "tool_calls": [{"name": "delete_file",
                                 "arguments": {"path": "x.txt"},
                                 "id": None, "signature": None,
                                 "risk": "HIGH", "requires_confirmation": True}]}
    store.save(t)
    loaded = store.load(t.id)
    assert loaded.pending == t.pending
    assert loaded.status == Status.AWAITING_CONFIRMATION


def test_pending_defaults_none(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    t = Task(goal="g")
    store.save(t)
    assert store.load(t.id).pending is None


def test_legacy_db_without_pending_column_loads(tmp_path):
    # Simulate a pre-5B database: create the OLD schema (no 'pending' column),
    # insert a legacy row, then open it with the current TaskStore.
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                messages TEXT NOT NULL, steps INTEGER NOT NULL, result TEXT,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacy1", "old goal", "paused",
             json.dumps([{"role": "user", "content": "hi"}]), 2,
             None, None, 1.0, 2.0))
    store = TaskStore(str(db))          # migrates: adds 'pending' column
    loaded = store.load("legacy1")
    assert loaded is not None
    assert loaded.status == "paused"    # old state still loads
    assert loaded.steps == 2
    assert loaded.pending is None
    # And the migrated store can still write/read the new field.
    loaded.pending = {"assistant_text": None, "tool_calls": []}
    store.save(loaded)
    assert store.load("legacy1").pending == {"assistant_text": None,
                                             "tool_calls": []}


# --- Phase 7: execution ledger (plan / current_step) -------------------

def test_plan_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    t = Task(goal="g")
    t.plan = [{"index": 0,
               "calls": [{"tool": "search_files", "arguments_summary": "query=x"}],
               "status": "succeeded", "outcome_summary": "ok"}]
    t.current_step = 0
    store.save(t)
    loaded = store.load(t.id)
    assert loaded.plan == t.plan
    assert loaded.current_step == 0


def test_plan_defaults_empty(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    t = Task(goal="g")
    store.save(t)
    loaded = store.load(t.id)
    assert loaded.plan == [] and loaded.current_step == 0


def test_legacy_db_without_plan_columns_loads(tmp_path):
    db = tmp_path / "legacy2.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                messages TEXT NOT NULL, steps INTEGER NOT NULL, result TEXT,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                pending TEXT)""")   # has pending but NOT plan/current_step
        conn.execute(
            "INSERT INTO tasks (id,goal,status,messages,steps,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("leg2", "g", "completed",
             json.dumps([{"role": "user", "content": "hi"}]), 3, 1.0, 2.0))
    store = TaskStore(str(db))   # migrates: adds plan + current_step columns
    loaded = store.load("leg2")
    assert loaded is not None
    assert loaded.plan == [] and loaded.current_step == 0
    loaded.plan = [{"index": 0, "calls": [], "status": "succeeded",
                    "outcome_summary": ""}]
    store.save(loaded)
    assert store.load("leg2").plan[0]["status"] == "succeeded"


def test_malformed_plan_fails_safe(tmp_path):
    db = tmp_path / "m.sqlite"
    store = TaskStore(str(db))
    t = Task(goal="g")
    store.save(t)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE tasks SET plan=? WHERE id=?", ("not json{{", t.id))
    loaded = store.load(t.id)
    assert loaded.plan == []          # malformed ledger -> empty, no crash
