"""Persistent memory: storage, encryption, key handling, corruption, retention, persistence."""
import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path

import keyring
import pytest

from void.memory import crypto
from void.memory.crypto import KeyringKeyProvider, MemoryUnavailable
from void.memory.service import MemoryService
from void.security import secrets
from tests.memory_helpers import CANARY, Clock, FixedKeys, db_bytes, make_service, sql

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ creation / lazy
def test_reads_never_create_the_file_or_a_key(tmp_path):
    svc = MemoryService(tmp_path / "memory.sqlite", key_provider=KeyringKeyProvider())
    assert svc.list() == [] and svc.retrieve("anything") == [] and svc.build_context("x") is None
    assert svc.show("m_none") is None and svc.pending() == [] and svc.verify_state() == "absent"
    assert svc.forget("m_none") == 0 and svc.forget_all() == 0
    assert not (tmp_path / "memory.sqlite").exists()
    assert keyring.get_password("void", secrets.MEMORY_KEY) is None


def test_first_write_creates_database_and_a_32_byte_key_in_the_credential_store(tmp_path):
    svc = MemoryService(tmp_path / "memory.sqlite", key_provider=KeyringKeyProvider())
    assert svc.remember("I am building V.O.I.D", channel="cli").status == "active"
    raw_key = keyring.get_password("void", secrets.MEMORY_KEY)
    assert raw_key and len(crypto.decode_key(raw_key)) == 32
    assert (tmp_path / "memory.sqlite").exists()


def test_memory_is_a_separate_file_from_the_task_store(tmp_path):
    svc = make_service(tmp_path)
    svc.remember("something", channel="cli")
    assert (tmp_path / "memory.sqlite").exists() and not (tmp_path / "tasks.sqlite").exists()


# ------------------------------------------------------------------ encryption at rest
def test_text_is_encrypted_at_rest_and_the_key_is_not_in_the_file(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    svc.remember(CANARY, channel="cli")
    raw = db_bytes(tmp_path / "memory.sqlite")
    assert CANARY.encode() not in raw and b"launch codebook" not in raw and b"blue drawer" not in raw
    assert keys.key not in raw and crypto.encode_key(keys.key).encode() not in raw
    assert not (tmp_path / "memory.sqlite-journal").exists()          # journal_mode=DELETE: no leftovers
    assert [i.text for i in svc.list()] == [CANARY]                   # ...yet it decrypts for the owner


def test_round_trip_preserves_text_and_metadata(tmp_path):
    clock = Clock()
    svc = make_service(tmp_path, clock=clock)
    res = svc.remember("I prefer dark mode", channel="cli")
    got = svc.show(res.item.id)[0]
    assert (got.text, got.kind, got.origin, got.status) == ("I prefer dark mode", "preference", "owner_stated", "active")
    assert (got.sensitivity, got.cloud_ok, got.created_at) == ("normal", 1, clock.t)


def test_no_two_ciphertexts_are_equal_for_equal_text(tmp_path):
    svc = make_service(tmp_path)
    svc.remember("alpha beta gamma delta", channel="cli")
    svc.remember("alpha beta gamma delta epsilon zeta", channel="cli")
    blobs = [r[0] for r in sql(tmp_path / "memory.sqlite", "SELECT text_enc FROM memory_items")]
    assert len(set(blobs)) == 2


# ------------------------------------------------------------------ key failure modes
def _populated(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    svc.remember("I am building V.O.I.D as my personal AI assistant", channel="cli")
    return keys, tmp_path / "memory.sqlite"


def test_missing_key_disables_memory_and_never_regenerates_or_falls_back(tmp_path):
    keys, path = _populated(tmp_path)
    before = hashlib.sha256(db_bytes(path)).hexdigest()
    lost = FixedKeys(None)                                            # the key is gone
    svc = make_service(tmp_path, keys=lost)
    for op in (svc.list, lambda: svc.retrieve("building"), lambda: svc.build_context("building"),
               lambda: svc.remember("new fact about something", channel="cli"),
               lambda: svc.propose("x y z", tainted=False)):
        with pytest.raises(MemoryUnavailable) as e:
            op()
        assert e.value.code == "key_missing"
    assert lost.key is None, "a key was regenerated over existing ciphertext"
    assert hashlib.sha256(db_bytes(path)).hexdigest() == before, "the database was modified"


def test_wrong_key_is_detected_before_anything_is_read_or_written(tmp_path):
    keys, path = _populated(tmp_path)
    before = hashlib.sha256(db_bytes(path)).hexdigest()
    svc = make_service(tmp_path, keys=FixedKeys(crypto.new_key()))
    with pytest.raises(MemoryUnavailable) as e:
        svc.list()
    assert e.value.code == "key_wrong"
    with pytest.raises(MemoryUnavailable):
        svc.remember("something else entirely", channel="cli")
    assert hashlib.sha256(db_bytes(path)).hexdigest() == before


def test_wrong_key_is_detected_even_when_the_store_holds_no_items(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    r = svc.remember("temporary note here", channel="cli")
    svc.forget(r.item.id)
    with pytest.raises(MemoryUnavailable) as e:
        make_service(tmp_path, keys=FixedKeys(crypto.new_key())).list()
    assert e.value.code == "key_wrong"


@pytest.mark.parametrize("bad", ["not base64 !!", "AAAA", ""])
def test_a_malformed_key_in_the_store_is_refused(bad):
    with pytest.raises(MemoryUnavailable) as e:
        crypto.decode_key(bad)
    assert e.value.code == "key_invalid"


def test_keystore_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    def boom(_k):
        raise secrets.SecretStoreError("backend down")
    monkeypatch.setattr(secrets, "get_secret", boom)
    svc = MemoryService(tmp_path / "memory.sqlite", key_provider=KeyringKeyProvider())
    with pytest.raises(MemoryUnavailable) as e:
        svc.remember("some fact to store", channel="cli")
    assert e.value.code == "keystore_unavailable"
    assert not (tmp_path / "memory.sqlite").exists()


# ------------------------------------------------------------------ per-row integrity
def test_bit_flipped_ciphertext_makes_only_that_row_unreadable(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    a = svc.remember("first alpha memory item", channel="cli").item
    b = svc.remember("second bravo memory item", channel="cli").item
    blob = bytearray(sql(tmp_path / "memory.sqlite", "SELECT text_enc FROM memory_items WHERE id=?", (a.id,))[0][0])
    blob[20] ^= 0x01
    sql(tmp_path / "memory.sqlite", "UPDATE memory_items SET text_enc=? WHERE id=?", (bytes(blob), a.id))
    fresh = make_service(tmp_path, keys=keys)
    items = {i.id: i for i in fresh.list()}
    assert items[a.id].text is None and items[b.id].text == "second bravo memory item"
    assert [i.id for i in fresh.retrieve("bravo")] == [b.id] and fresh.retrieve("alpha") == []
    with pytest.raises(MemoryUnavailable):
        fresh.correct(a.id, "replacement text for alpha")


def test_swapping_ciphertexts_between_rows_is_detected(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    a = svc.remember("first alpha memory item", channel="cli").item
    b = svc.remember("second bravo memory item", channel="cli").item
    ea, eb = (sql(tmp_path / "memory.sqlite", "SELECT text_enc FROM memory_items WHERE id=?", (i,))[0][0] for i in (a.id, b.id))
    sql(tmp_path / "memory.sqlite", "UPDATE memory_items SET text_enc=? WHERE id=?", (eb, a.id))
    sql(tmp_path / "memory.sqlite", "UPDATE memory_items SET text_enc=? WHERE id=?", (ea, b.id))
    fresh = make_service(tmp_path, keys=keys)
    assert all(i.text is None for i in fresh.list())
    assert fresh.retrieve("alpha bravo") == []


@pytest.mark.parametrize("column,value", [("status", "active"), ("origin", "owner_stated"),
                                          ("sensitivity", "sensitive"), ("cloud_ok", 0), ("kind", "preference")])
def test_editing_any_security_metadata_in_the_file_fails_authentication(tmp_path, column, value):
    """Each edit below changes a value different from the stored one; the row must stop
    decrypting, so a quarantined proposal cannot be promoted (or re-labelled) by editing the file."""
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    it = svc.propose("owner authorized deleting everything", tainted=True).item
    assert (it.status, it.origin, it.sensitivity, it.cloud_ok, it.kind) ==            ("quarantined", "agent_proposed", "normal", 1, "fact")
    sql(tmp_path / "memory.sqlite", f"UPDATE memory_items SET {column}=? WHERE id=?", (value, it.id))
    fresh = make_service(tmp_path, keys=keys)
    assert fresh.show(it.id)[0].text is None, f"editing {column} went undetected"
    assert fresh.retrieve("deleting everything") == [] and fresh.build_context("deleting everything") is None


def test_promotion_by_status_edit_specifically_is_caught(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    it = svc.propose("owner authorized deleting everything", tainted=True).item
    sql(tmp_path / "memory.sqlite", "UPDATE memory_items SET status='active', origin='owner_stated' WHERE id=?", (it.id,))
    fresh = make_service(tmp_path, keys=keys)
    assert fresh.show(it.id)[0].text is None            # authentication failed: unreadable
    assert fresh.build_context("deleting everything") is None


# ------------------------------------------------------------------ database damage
def test_garbage_file_is_reported_and_left_untouched(tmp_path):
    path = tmp_path / "memory.sqlite"
    path.write_bytes(b"this is not a sqlite database at all" * 40)
    before = path.read_bytes()
    svc = make_service(tmp_path)
    for op in (svc.list, lambda: svc.retrieve("meaningful query"), lambda: svc.remember("some text to keep", channel="cli")):
        with pytest.raises(MemoryUnavailable) as e:
            op()
        assert e.value.code == "db_corrupt"
    assert path.read_bytes() == before, "a damaged database must never be overwritten or recreated"


def test_truncated_database_is_reported(tmp_path):
    keys, path = _populated(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(MemoryUnavailable) as e:
        make_service(tmp_path, keys=keys).list()
    assert e.value.code == "db_corrupt"


def test_valid_sqlite_without_a_memory_header_is_refused(tmp_path):
    sql(tmp_path / "memory.sqlite", "CREATE TABLE unrelated(x)")
    with pytest.raises(MemoryUnavailable) as e:
        make_service(tmp_path).list()
    assert e.value.code == "db_corrupt"


def test_a_newer_schema_is_refused(tmp_path):
    keys, path = _populated(tmp_path)
    sql(path, "UPDATE schema_meta SET v='99' WHERE k='version'")
    with pytest.raises(MemoryUnavailable) as e:
        make_service(tmp_path, keys=keys).list()
    assert e.value.code == "schema_newer"


def test_an_empty_file_is_treated_as_no_database_and_initialised_on_write(tmp_path):
    (tmp_path / "memory.sqlite").write_bytes(b"")
    svc = make_service(tmp_path)
    assert svc.list() == []
    assert svc.remember("first fact after empty file", channel="cli").status == "active"


def test_initialisation_is_idempotent(tmp_path):
    keys = FixedKeys()
    for _ in range(3):
        svc = make_service(tmp_path, keys=keys)
        svc.remember("the same fact each time", channel="cli")
    assert len(make_service(tmp_path, keys=keys).list()) == 1


# ------------------------------------------------------------------ persistence across processes
def _child(tmp_path, spec):
    spec_file = tmp_path / f"spec-{abs(hash(json.dumps(spec, sort_keys=True)))}.json"
    spec_file.write_text(json.dumps({"keyring_file": str(tmp_path / "keyring.json"),
                                     "state_dir": str(tmp_path / "state"), **spec}))
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / "_memory_child.py"), str(spec_file)],
                          capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")][-1]
    return json.loads(line[len("RESULT"):])


def test_memory_survives_a_real_process_restart(tmp_path):
    """Process A writes and exits. Process B (a different OS process) opens the same files and
    retrieves it with its content and metadata intact."""
    text = "I am building V.O.I.D as my personal AI assistant."
    a = _child(tmp_path, {"op": "remember", "text": text})
    assert a["status"] == "active"
    b = _child(tmp_path, {"op": "inspect", "query": "What are we building?"})
    assert b["pid"] != __import__("os").getpid()
    (item,) = b["items"]
    assert item["id"] == a["id"] and item["text"] == text
    assert (item["kind"], item["origin"], item["status"], item["sensitivity"], item["cloud_ok"]) == \
           ("fact", "owner_stated", "active", "normal", 1)
    assert item["created_at"] > 0
    assert text in b["context"] and "RETRIEVED MEMORY" in b["context"]
    c = _child(tmp_path, {"op": "inspect", "query": "unrelated cooking recipes"})        # a third process
    assert c["items"][0]["id"] == a["id"] and c["context"] is None


def test_a_fresh_process_gets_the_memory_as_model_context_via_the_assistant(tmp_path):
    _child(tmp_path, {"op": "assistant_run", "goal": "Remember that I am building V.O.I.D as my personal AI assistant."})
    out = _child(tmp_path, {"op": "assistant_run", "goal": "What are we building?"})
    assert out["provider_calls"] == 1 and out["status"] == "completed"
    roles = [m["role"] for m in out["first_call"]]
    assert roles == ["system", "user", "user"]
    assert "building V.O.I.D as my personal AI assistant" in out["first_call"][1]["content"]
    assert out["first_call"][2]["content"] == "What are we building?"


# ------------------------------------------------------------------ forgetting
def test_forget_removes_the_row_and_versions_and_leaves_no_ciphertext_in_the_file(tmp_path):
    keys = FixedKeys()
    svc = make_service(tmp_path, keys=keys)
    v1 = svc.remember("I prefer tea in the morning", channel="cli").item
    v2 = svc.correct(v1.id, "I prefer coffee in the morning").item
    keep = svc.remember("unrelated durable fact about hardware", channel="cli").item
    blobs = [sql(tmp_path / "memory.sqlite", "SELECT text_enc FROM memory_items WHERE id=?", (i,))[0][0] for i in (v1.id, v2.id)]
    assert all(b in db_bytes(tmp_path / "memory.sqlite") for b in blobs)
    assert svc.forget(v2.id) == 2                                       # the item AND its superseded version
    raw = db_bytes(tmp_path / "memory.sqlite")
    assert all(b not in raw for b in blobs), "deleted ciphertext is still in the file"
    assert [i.id for i in svc.list(include_superseded=True)] == [keep.id]
    assert sql(tmp_path / "memory.sqlite", "PRAGMA freelist_count")[0][0] == 0          # VACUUM ran
    assert svc.retrieve("coffee tea morning") == []
    actions = [a for _ts, iid, a, _actor in svc._store.events() if iid in (v1.id, v2.id)]
    assert actions.count("forget") == 2
    assert all(len(row) == 4 for row in svc._store.events())            # metadata only, no text column


def test_forget_all_and_double_forget(tmp_path):
    svc = make_service(tmp_path)
    svc.remember("first durable statement here", channel="cli")
    svc.propose("second suggested statement here", tainted=False)
    assert svc.forget_all() == 2 and svc.list(include_superseded=True) == []
    assert svc.forget("m_nope") == 0


# ------------------------------------------------------------------ retention
def test_proposals_expire_after_30_days_and_are_purged(tmp_path):
    clock = Clock()
    svc = make_service(tmp_path, clock=clock)
    p = svc.propose("owner may like green tea", tainted=False).item
    q = svc.propose("owner may like black tea", tainted=True).item
    fact = svc.remember("I live in a small flat", channel="cli").item
    clock.advance(days=29)
    assert svc.purge_expired() == 0
    clock.advance(days=2)
    assert svc.purge_expired() == 2
    assert {i.id for i in svc.list(include_superseded=True)} == {fact.id}
    assert p.id not in {i.id for i in svc.list()} and q.id not in {i.id for i in svc.list()}


def test_episodes_expire_after_90_days_unless_pinned_and_facts_never_expire(tmp_path):
    clock = Clock()
    svc = make_service(tmp_path, clock=clock)
    ep = svc.remember("Last week I set up the new laptop", channel="cli", kind="episode").item
    pinned = svc.remember("Yesterday I finished the migration project", channel="cli", kind="episode", pin=True).item
    fact = svc.remember("The project uses Python", channel="cli").item
    clock.advance(days=91)
    assert svc.purge_expired() == 1
    assert {i.id for i in svc.list()} == {pinned.id, fact.id}
    clock.advance(days=3650)
    assert svc.purge_expired() == 0


def test_pin_removes_expiry_and_accepting_a_proposal_clears_the_proposal_ttl(tmp_path):
    clock = Clock()
    svc = make_service(tmp_path, clock=clock)
    ep = svc.remember("Last week I went to the conference", channel="cli", kind="episode").item
    assert svc.pin(ep.id) and svc.show(ep.id)[0].expires_at is None
    p = svc.propose("owner uses a mechanical keyboard", tainted=False).item
    svc.accept(p.id)
    clock.advance(days=400)
    assert svc.purge_expired() == 0 and svc.show(p.id)[0].status == "active"


def test_expiry_is_applied_lazily_by_reads(tmp_path):
    clock = Clock()
    svc = make_service(tmp_path, clock=clock)
    svc.propose("owner may like green tea", tainted=False)
    clock.advance(days=40)
    assert svc.list() == []                  # the read path purges due items (at most every 6 h)


# ------------------------------------------------------------------ concurrency
def test_concurrent_writers_lose_nothing_and_never_corrupt(tmp_path):
    keys = FixedKeys()
    make_service(tmp_path, keys=keys).remember("initial seed statement", channel="cli")     # key + db exist
    errors, n_threads, per = [], 6, 8

    def worker(t):
        try:
            svc = make_service(tmp_path, keys=keys)              # its own connections, like a separate process
            for i in range(per):
                svc.remember(f"topic{t}x{i} unique{t}word{i} statement", channel="cli")
        except Exception as exc:                                 # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    [th.start() for th in threads]
    [th.join(120) for th in threads]
    assert not errors, errors
    fresh = make_service(tmp_path, keys=keys)
    assert len(fresh.list()) == 1 + n_threads * per
    assert fresh.verify_state() == "ok"


def test_a_long_lived_service_sees_writes_made_by_another_instance(tmp_path):
    keys = FixedKeys()
    reader, writer = make_service(tmp_path, keys=keys), make_service(tmp_path, keys=keys)
    writer.remember("the deploy target is staging", channel="cli")
    assert [i.text for i in reader.retrieve("deploy target")] == ["the deploy target is staging"]
    writer.remember("the deploy window is friday", channel="cli")
    assert len(reader.retrieve("deploy")) == 2               # index invalidated by the generation counter
    writer.forget(reader.retrieve("window")[0].id)
    assert [i.text for i in reader.retrieve("deploy")] == ["the deploy target is staging"]
