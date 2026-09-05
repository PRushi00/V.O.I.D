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
