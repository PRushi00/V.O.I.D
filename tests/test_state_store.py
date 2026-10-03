"""The structured local state store: preferences, snapshots, changes, maintenance claims.

What these pin is the boundary the store exists to draw. V.O.I.D already had secure persistent memory -
SQLite, AES-256-GCM, owner-reviewed - so this file is NOT another memory system and must never become
one. The tests therefore care as much about what it refuses to hold (anything secret-shaped) and what it
leaves alone (the memory database) as about what it stores.

The other thing under test is the once-per-week claim, because "run maintenance on Sunday" is only
trustworthy if a second trigger, a second process, or a crash mid-run cannot produce a second pass.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from void.state import (MAX_PAYLOAD, SCHEMA_VERSION, Change, Observation, StateError, StateStore,
                        clean, is_sunday, looks_secret, week_key)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.sqlite")


def _obs(identity, kind="application", **payload):
    return Observation(scope="APPLICATIONS", kind=kind, identity=identity, payload=payload)


# --------------------------------------------------------------------------- database and schema

def test_the_database_is_created_on_first_use(tmp_path):
    store = StateStore(tmp_path / "nested" / "state.sqlite")
    assert not store.exists()
    store.set_preference("browser", "opera gx")
    assert store.exists()
    assert store.schema_version() == SCHEMA_VERSION


def test_the_schema_is_versioned_and_a_newer_one_is_refused(tmp_path):
    """A database written by a newer V.O.I.D may rely on columns this code would silently ignore, so
    it is refused rather than guessed at - the same stance void.memory.store already takes."""
    path = tmp_path / "state.sqlite"
    store = StateStore(path)
    store.set_preference("browser", "opera gx")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_meta SET v=? WHERE k='version'", (str(SCHEMA_VERSION + 1),))
        conn.commit()
    with pytest.raises(StateError):
        StateStore(path).preferences()


def test_an_unreadable_schema_version_is_refused(tmp_path):
    path = tmp_path / "state.sqlite"
    StateStore(path).set_preference("browser", "opera gx")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_meta SET v='not a number' WHERE k='version'")
        conn.commit()
    with pytest.raises(StateError):
        StateStore(path).preferences()


def test_reopening_the_same_file_keeps_everything(tmp_path):
    """Persistence across restart: a new store object on the same path is a new process as far as the
    data is concerned."""
    path = tmp_path / "state.sqlite"
    StateStore(path).set_preference("browser", "opera gx", source="owner")
    snapshot = StateStore(path).save_snapshot(["APPLICATIONS"], [_obs("a1", name="Notepad")])
    reopened = StateStore(path)
    assert reopened.preference("browser").value == "opera gx"
    assert len(reopened.observations(snapshot)) == 1
    assert reopened.snapshot_count() == 1


def test_this_store_does_not_touch_the_memory_database(tmp_path):
    """The hybrid boundary: structured state is its own file, and memory keeps its own."""
    store = StateStore(tmp_path / "state.sqlite")
    store.set_preference("browser", "opera gx")
    assert store.path.name == "state.sqlite"
    assert not (tmp_path / "memory.sqlite").exists()


# --------------------------------------------------------------------------- secrets

@pytest.mark.parametrize("secret", [
    "sk-abcdefghij1234567890",
    "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
    "password: hunter2hunter2",
    "api_key = abcdef123456",
    "-----BEGIN RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
])
def test_secret_shaped_values_are_recognised(secret):
    assert looks_secret(secret)


@pytest.mark.parametrize("ordinary", ["opera gx", "Microsoft Edge", "C:", "Notepad", "", "wifi-2"])
def test_ordinary_values_are_not_mistaken_for_secrets(ordinary):
    assert not looks_secret(ordinary)


def test_a_secret_shaped_preference_is_refused_rather_than_stored(store):
    """A plain SQLite file is the wrong home for a credential, and "we only put metadata in it" is a
    weaker guarantee than a check."""
    with pytest.raises(StateError):
        store.set_preference("browser", "api_key: abcdef123456")
    assert store.preferences() == []


def test_a_secret_shaped_observation_field_is_dropped_not_stored(store):
    snapshot = store.save_snapshot(
        ["APPLICATIONS"],
        [_obs("a1", name="Thing", note="token: ghp_abcdefghijklmnopqrstuvwxyz0123", version="1.0")])
    payload = store.observations(snapshot)[0].payload
    assert payload["name"] == "Thing" and payload["version"] == "1.0"
    assert "note" not in payload, "a secret-shaped observation field reached the database"


def test_an_oversized_payload_is_bounded(store):
    snapshot = store.save_snapshot(["APPLICATIONS"], [_obs("a1", blob="x" * 50_000)])
    stored = store.observations(snapshot)[0]
    assert len(str(stored.payload)) <= MAX_PAYLOAD + 200


def test_payload_values_are_flattened_to_simple_types(store):
    snapshot = store.save_snapshot(
        ["APPLICATIONS"], [_obs("a1", name="Thing", nested={"a": 1}, listy=[1, 2], ok=True, n=3)])
    payload = store.observations(snapshot)[0].payload
    assert payload["ok"] is True and payload["n"] == 3 and payload["name"] == "Thing"
    for value in payload.values():
        assert isinstance(value, (str, int, float, bool))


# --------------------------------------------------------------------------- preferences

def test_a_preference_round_trips_with_its_provenance(store):
    pref = store.set_preference("browser", "Opera GX", source="owner", explicit=True)
    assert pref.key == "browser" and pref.value == "Opera GX"
    loaded = store.preference("browser")
    assert loaded.source == "owner" and loaded.explicit is True and loaded.confidence == 1.0
    assert loaded.created_at > 0 and loaded.updated_at > 0


def test_setting_a_preference_again_updates_rather_than_duplicates(store):
    store.set_preference("browser", "opera gx")
    first = store.preference("browser").created_at
    store.set_preference("browser", "edge")
    assert len(store.preferences()) == 1
    again = store.preference("browser")
    assert again.value == "edge"
    assert again.created_at == first, "created_at should survive an update"


def test_an_inferred_preference_is_marked_as_such(store):
    """Installed, used and preferred stay separate concepts: something V.O.I.D merely noticed must not
    look like something the owner said."""
    pref = store.set_preference("browser", "chrome", source="observed", explicit=False,
                                confidence=0.4)
    assert pref.explicit is False and pref.source == "observed"
    assert 0.0 < pref.confidence < 1.0


def test_confidence_is_clamped(store):
    assert store.set_preference("browser", "a", confidence=9.0).confidence == 1.0
    assert store.set_preference("editor", "b", confidence=-3.0).confidence == 0.0


def test_deactivating_keeps_the_history(store):
    store.set_preference("browser", "opera gx")
    assert store.deactivate_preference("browser") is True
    assert store.preference("browser") is None
    assert store.preferences() == []
    assert store.preferences(include_inactive=True), "the row should still be there"
    actions = [row["action"] for row in store.preference_revisions("browser")]
    assert "deactivate" in actions and "set" in actions


def test_deactivating_something_unset_is_not_an_error(store):
    assert store.deactivate_preference("browser") is False


def test_revisions_record_every_change(store):
    store.set_preference("browser", "opera gx")
    store.set_preference("browser", "edge")
    store.deactivate_preference("browser")
    assert len(store.preference_revisions("browser")) == 3


@pytest.mark.parametrize("key, value", [("", "edge"), ("   ", "edge"), ("browser", ""),
                                        ("browser", "   ")])
def test_an_empty_preference_is_refused(store, key, value):
    with pytest.raises(StateError):
        store.set_preference(key, value)


def test_the_routing_map_is_the_shape_routing_already_consumes(store):
    """Fed to WorldState.preferences, which the resolver already reads - no second mechanism."""
    store.set_preference("browser", "opera gx")
    store.set_preference("editor", "vs code")
    store.deactivate_preference("editor")
    assert store.as_routing_map() == {"browser": "opera gx"}


# --------------------------------------------------------------------------- snapshots

def test_a_snapshot_and_its_observations_are_written_together(store):
    snapshot = store.save_snapshot(["APPLICATIONS"], [_obs("a1", name="A"), _obs("a2", name="B")])
    assert store.snapshot_count() == 1
    assert {o.identity for o in store.observations(snapshot)} == {"a1", "a2"}


def test_a_failed_snapshot_write_leaves_nothing_behind(store, monkeypatch):
    """All-or-nothing: a half-written snapshot would make next week's diff compare against a machine
    state that never existed, which is worse than having no snapshot for a week.

    Failure is forced by making a second snapshot reuse the first one's primary key, so the INSERT
    fails inside the transaction after its observation rows were already prepared.
    """
    first = store.save_snapshot(["APPLICATIONS"], [_obs("a1", name="A")])

    import void.state.store as module

    class _FixedUuid:
        hex = first

    monkeypatch.setattr(module.uuid, "uuid4", lambda: _FixedUuid())
    with pytest.raises(sqlite3.IntegrityError):
        store.save_snapshot(["APPLICATIONS"], [_obs("b1", name="B")])
    monkeypatch.undo()
    assert store.snapshot_count() == 1, "a rolled-back snapshot must not persist"
    assert {o.identity for o in store.observations(first)} == {"a1"}, \
        "the failed snapshot's observations must not have landed"


def test_only_complete_snapshots_become_the_baseline(store):
    """An interrupted run must not become next week's comparison point."""
    store.save_snapshot(["APPLICATIONS"], [_obs("a1", name="A")], complete=True)
    partial = store.save_snapshot(["APPLICATIONS"], [_obs("a2", name="B")], complete=False)
    latest = store.latest_snapshot()
    assert latest is not None and latest["id"] != partial


def test_snapshots_are_pruned_to_a_bounded_history(store):
    for index in range(12):
        store.save_snapshot(["APPLICATIONS"], [_obs(f"a{index}", name="A")])
    assert store.snapshot_count() == 12
    removed = store.prune_snapshots(keep=4)
    assert removed == 8 and store.snapshot_count() == 4


def test_pruning_takes_the_observations_with_it(store):
    old = store.save_snapshot(["APPLICATIONS"], [_obs("a1", name="A")])
    for index in range(3):
        store.save_snapshot(["APPLICATIONS"], [_obs(f"b{index}", name="B")])
    store.prune_snapshots(keep=1)
    assert store.observations(old) == [], "observations should cascade with their snapshot"


# --------------------------------------------------------------------------- changes

def test_changes_are_recorded_against_their_snapshot(store):
    snapshot = store.save_snapshot(["APPLICATIONS"], [_obs("a1", name="A")])
    written = store.save_changes(snapshot, None, [Change(kind="application_installed",
                                                          identity="a1", detail="A")])
    assert written == 1
    recorded = store.changes(snapshot)
    assert len(recorded) == 1 and recorded[0].kind == "application_installed"


def test_saving_no_changes_is_a_no_op(store):
    snapshot = store.save_snapshot(["APPLICATIONS"], [])
    assert store.save_changes(snapshot, None, []) == 0


# --------------------------------------------------------------------------- the weekly claim

def test_a_week_can_only_be_claimed_once(store):
    key = week_key()
    assert store.begin_run(key) is not None
    assert store.begin_run(key) is None, "a second claim on the same week must be refused"


def test_two_threads_racing_produce_exactly_one_claim(store):
    """The duplicate-run guard is a UNIQUE insert rather than a lock file, so a crash cannot leave a
    stale lock and a race cannot produce two passes."""
    key = week_key()
    won: list = []
    barrier = threading.Barrier(6)

    def claim():
        barrier.wait()
        if store.begin_run(key) is not None:
            won.append(1)

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(won) == 1, f"{len(won)} runs claimed the same week"


def test_a_finished_run_records_its_outcome(store):
    run = store.begin_run(week_key(), scopes="APPLICATIONS")
    store.finish_run(run.id, status="ok", snapshot_id="abc", changes=3)
    stored = store.run_for_week(week_key())
    assert stored.succeeded and stored.changes == 3 and stored.finished_at is not None


def test_a_failed_run_records_only_the_exception_class(store):
    """Never a message: an exception string can carry a path or a device name."""
    run = store.begin_run(week_key())
    store.finish_run(run.id, status="failed", error_class="OSError")
    stored = store.run_for_week(week_key())
    assert stored.status == "failed" and stored.error_class == "OSError"


def test_an_interrupted_run_is_reclaimable(store):
    """Interruption recovery: a process killed mid-run leaves a claim, not corrupt data, so releasing
    the claim is all that is needed."""
    key = week_key()
    assert store.begin_run(key) is not None
    assert store.begin_run(key) is None, "a LIVE claim must block a second run"

    # Time has to actually pass: release_stale_runs clamps to a minimum age so a run that started
    # seconds ago is never mistaken for an abandoned one. Driven through the store's own clock rather
    # than by sleeping.
    assert store.release_stale_runs(older_than_s=60.0) == 0, "a fresh claim is not stale"
    store._now = lambda: time.time() + 7200           # two hours later
    assert store.release_stale_runs(older_than_s=3600.0) == 1
    assert store.run_for_week(key).status == "failed"
    # The week must now be retryable - otherwise one crash makes that Sunday permanently unrunnable.
    assert store.begin_run(key) is not None, "a released week could not be retried"
    assert store.run_for_week(key).status == "running"


def test_a_week_that_succeeded_is_never_rerun(store):
    """The other half of takeover: it must not undo a completed pass."""
    key = week_key()
    run = store.begin_run(key)
    store.finish_run(run.id, status="ok", changes=2)
    assert store.begin_run(key) is None
    assert store.run_for_week(key).succeeded and store.run_for_week(key).changes == 2


def test_a_fresh_claim_is_not_released_as_stale(store):
    store.begin_run(week_key())
    assert store.release_stale_runs(older_than_s=3600.0) == 0


def test_runs_are_listed_newest_first(store):
    for offset in range(3):
        run = store.begin_run(f"2026-W{40 + offset:02d}")
        store.finish_run(run.id, status="ok")
    keys = [run.week_key for run in store.runs()]
    assert keys == sorted(keys, reverse=True)


# --------------------------------------------------------------------------- helpers

def test_week_key_is_anchored_to_the_preceding_sunday():
    """Anchored BACKWARDS so a mid-week run cannot consume the coming Sunday's pass.

    The first attempt used the ISO week, which runs Monday to Sunday and so puts Sunday LAST: a forced
    run on Monday the 5th shared a key with Sunday the 11th and would have silently cancelled it.
    """
    import datetime
    sunday = datetime.datetime(2026, 10, 4, 12, 0).timestamp()
    monday = datetime.datetime(2026, 10, 5, 12, 0).timestamp()
    saturday = datetime.datetime(2026, 10, 10, 12, 0).timestamp()
    next_sunday = datetime.datetime(2026, 10, 11, 12, 0).timestamp()

    assert week_key(sunday) == "2026-10-04"
    assert week_key(monday) == "2026-10-04", "a mid-week run belongs to the Sunday just gone"
    assert week_key(saturday) == "2026-10-04"
    assert week_key(next_sunday) == "2026-10-11", "the next Sunday is its own pass"
    assert week_key(monday) != week_key(next_sunday), \
        "a forced mid-week run must not consume the coming Sunday"


def test_is_sunday_uses_local_time():
    import datetime
    assert is_sunday(datetime.datetime(2026, 10, 4, 12, 0).timestamp())
    assert not is_sunday(datetime.datetime(2026, 10, 5, 12, 0).timestamp())


def test_clean_separates_rather_than_deletes_control_characters():
    """Deleting them merged words: "Opera<newline>GX" became "OperaGX", a different application name,
    so an identity would have been silently wrong rather than merely untidy."""
    assert clean("  a\n\tb  ") == "a b"
    assert clean("Opera\nGX") == "Opera GX"
    assert len(clean("x" * 5000)) <= 2000
    assert clean(None) == ""
