"""T0.9: tasks stranded as ``running`` by a dead process are paused, safely.

All databases here are synthetic; the owner's real ~/.void/tasks.sqlite is never opened."""
import sqlite3
import threading
import time

import pytest

from void.core.task import INTERRUPTED_ERROR, STALE_RUNNING_AFTER_S, Status, Task, TaskStore

NOW = 1_800_000_000.0
OLD = NOW - STALE_RUNNING_AFTER_S - 60
RECENT = NOW - 60


def _seed(path, rows):
    """rows: [(id, status, updated_at)] into a V1-shaped table (no plan/pending columns)."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                   messages TEXT NOT NULL, steps INTEGER NOT NULL, result TEXT, error TEXT,
                   created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    for tid, status, upd in rows:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", (tid, f"goal {tid}", status, "[]", 3, None, None, upd, upd))
    con.commit()
    con.close()


def _snapshot(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id,status,error,updated_at,goal,messages,steps FROM tasks ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "tasks.sqlite"
    _seed(db, [("old-run", Status.RUNNING, OLD), ("new-run", Status.RUNNING, RECENT),
               ("done", Status.COMPLETED, OLD), ("failed", Status.FAILED, OLD),
               ("cancelled", Status.CANCELLED, OLD), ("paused", Status.PAUSED, OLD),
               ("awaiting", Status.AWAITING_CONFIRMATION, OLD), ("blocked", Status.BLOCKED, OLD),
               ("pending", Status.PENDING, OLD), ("old-run-2", Status.RUNNING, OLD - 5000)])
    return TaskStore(db)          # opening it applies V1's additive migration, as in production


def test_only_stale_running_rows_are_paused(store):
    before = {r[0]: r for r in _snapshot(store.db_path)}
    ids = store.sweep_stale(now=NOW)
    assert sorted(ids) == ["old-run", "old-run-2"]
    after = {r[0]: r for r in _snapshot(store.db_path)}
    for tid in ("old-run", "old-run-2"):
        assert after[tid][1] == Status.PAUSED and after[tid][2] == INTERRUPTED_ERROR
        assert after[tid][3] == before[tid][3], "updated_at must be preserved"
        assert after[tid][4:] == before[tid][4:], "goal/messages/steps must be untouched"
    for tid in set(before) - {"old-run", "old-run-2"}:
        assert after[tid] == before[tid], f"{tid} was modified"


def test_a_swept_task_is_resumable_by_the_owner_not_completed(store):
    store.sweep_stale(now=NOW)
    t = store.load("old-run")
    assert t.status == Status.PAUSED and t.status in Status.RESUMABLE and t.status not in Status.TERMINAL
    assert t.error == INTERRUPTED_ERROR


def test_dry_run_reports_but_changes_nothing(store):
    before = _snapshot(store.db_path)
    assert sorted(store.sweep_stale(dry_run=True, now=NOW)) == ["old-run", "old-run-2"]
    assert _snapshot(store.db_path) == before


def test_sweep_is_idempotent(store):
    assert len(store.sweep_stale(now=NOW)) == 2
    after_first = _snapshot(store.db_path)
    assert store.sweep_stale(now=NOW) == []
    assert _snapshot(store.db_path) == after_first


def test_threshold_is_respected_at_the_boundary(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed(db, [("edge", Status.RUNNING, NOW - STALE_RUNNING_AFTER_S), ("just-over", Status.RUNNING, NOW - STALE_RUNNING_AFTER_S - 1)])
    s = TaskStore(db)
    assert s.sweep_stale(now=NOW) == ["just-over"]
    assert s.sweep_stale(older_than_s=30, now=NOW) == ["edge"]


def test_empty_database_and_no_rows(tmp_path):
    s = TaskStore(tmp_path / "empty.sqlite")
    assert s.sweep_stale() == []


def test_a_live_process_checkpoint_between_two_connections_is_never_clobbered(tmp_path):
    """A second connection (a live process) checkpoints a task the sweeper considers stale:
    whichever order they run in, a task that was refreshed stays RUNNING."""
    db = tmp_path / "t.sqlite"
    s = TaskStore(db)
    live = Task(goal="live")
    live.status = Status.RUNNING
    s.save(live)
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET updated_at = ?", (OLD,))
    con.commit()
    con.close()

    # the live process checkpoints (save() touches updated_at to the real "now") ...
    live.steps = 1
    s.save(live)
    # ... and the sweeper runs afterwards, at real time: the row is fresh, so it is left alone
    assert s.sweep_stale() == []
    assert s.load(live.id).status == Status.RUNNING


def test_concurrent_sweeps_and_checkpoints_are_safe(tmp_path):
    db = tmp_path / "t.sqlite"
    s = TaskStore(db)
    tasks = []
    for i in range(20):
        t = Task(goal=f"g{i}")
        t.status = Status.RUNNING
        s.save(t)
        tasks.append(t)
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET updated_at = ?", (time.time() - 3600,))
    con.commit()
    con.close()
    fresh = set()
    errors = []

    def checkpointer():
        try:
            for t in tasks[:10]:
                t.steps += 1
                s.save(t)                       # refreshes updated_at -> not stale any more
                fresh.add(t.id)
        except Exception as exc:                 # pragma: no cover
            errors.append(exc)

    def sweeper(out):
        try:
            out.extend(s.sweep_stale())
        except Exception as exc:                 # pragma: no cover
            errors.append(exc)

    a, b = [], []
    threads = [threading.Thread(target=checkpointer), threading.Thread(target=sweeper, args=(a,)),
               threading.Thread(target=sweeper, args=(b,))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(20)
    assert not errors, errors
    assert not (set(a) & set(b)), "two sweepers paused the same row"
    for t in tasks[10:]:                         # never checkpointed: must end paused
        assert s.load(t.id).status == Status.PAUSED
    for t in tasks[:10]:                         # checkpointed: paused only if the sweep won the race, else running
        assert s.load(t.id).status in (Status.RUNNING, Status.PAUSED)
        if s.load(t.id).status == Status.PAUSED:
            assert t.id in set(a) | set(b)


def test_a_corrupt_status_row_is_neither_swept_nor_hidden(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed(db, [("weird", "zombie", OLD), ("old", Status.RUNNING, OLD)])
    s = TaskStore(db)
    assert s.sweep_stale(now=NOW) == ["old"]
    assert sqlite3.connect(db).execute("SELECT status FROM tasks WHERE id='weird'").fetchone()[0] == "zombie"


# ------------------------------------------------------------------ integration
def test_cli_tasks_sweeps_then_lists_and_dry_run_does_not(tmp_path, monkeypatch, capsys):
    from void import cli
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    state = home / ".void"
    state.mkdir(parents=True)
    _seed(state / "tasks.sqlite", [("stale-one", Status.RUNNING, time.time() - 7200)])

    assert cli.main(["tasks", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would be marked paused" in out and "stale-one" in out
    assert TaskStore(state / "tasks.sqlite").load("stale-one").status == Status.RUNNING

    assert cli.main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "marked paused" in out and "stale-one" in out
    assert TaskStore(state / "tasks.sqlite").load("stale-one").status == Status.PAUSED
    assert cli.main(["tasks"]) == 0
    assert "stale task" not in capsys.readouterr().out          # idempotent


def test_runtime_start_sweeps_and_a_failing_sweep_does_not_block_startup(tmp_path):
    from void.config import Config
    from void.voice.runtime import VoiceController

    class _Store:
        calls = 0

        def sweep_stale(self):
            type(self).calls += 1
            raise sqlite3.OperationalError("database is locked")

    class _A:
        config = Config({"app": {"state_dir": str(tmp_path / "s")}, "voice": {"enabled": False}})
        store = _Store()
        kill_switch = None

    try:
        VoiceController.from_assistant(_A())      # may fail later for unrelated (fake) reasons
    except Exception as exc:
        assert not isinstance(exc, sqlite3.OperationalError), "the sweep failure escaped"
    assert _Store.calls == 1
