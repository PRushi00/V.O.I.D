"""The encrypted SQLite store behind persistent memory (V2.0 T3.2 / T3.7).

``memory.sqlite`` is a separate file from ``tasks.sqlite`` (which holds raw tool output and
transcripts and must never be mined into a long-lived store). Item text is AES-256-GCM
encrypted; only non-revealing metadata is plaintext. Nothing is ever indexed on disk (no
FTS table): retrieval builds an in-RAM index from decrypted ``active`` rows, so there is
nothing on disk to leak or to fail to delete.

Safety properties:
  * created lazily on the first write - a read never creates the file or a key;
  * ``journal_mode=DELETE`` + ``secure_delete=ON``; ``forget`` = row DELETE + VACUUM;
  * a wrong/missing key or a damaged file raises ``MemoryUnavailable`` - it never
    falls back to plaintext, never regenerates a key over existing ciphertext, and never
    deletes or recreates a damaged database;
  * the schema is additive and versioned (``schema_meta``); a newer schema is refused;
  * a per-row ciphertext failure marks just that row unreadable instead of failing the store.

Only the (id, kind, origin, status, sensitivity, cloud flag) binding is authenticated with
the text; timestamps and counters are unauthenticated bookkeeping.
"""
from __future__ import annotations

import contextlib
import os
import secrets as _rnd
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from void.memory import crypto
from void.memory.crypto import DecryptionError, MemoryCipher, MemoryUnavailable

KINDS = ("preference", "fact", "episode")
ORIGINS = ("owner_stated", "voice_stated", "owner_confirmed", "agent_proposed")
STATUSES = ("active", "proposed", "quarantined", "superseded")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (k TEXT PRIMARY KEY, v BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS memory_items (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('preference','fact','episode')),
    text_enc      BLOB NOT NULL,
    origin        TEXT NOT NULL CHECK (origin IN ('owner_stated','voice_stated','owner_confirmed','agent_proposed')),
    status        TEXT NOT NULL CHECK (status IN ('active','proposed','quarantined','superseded')),
    sensitivity   TEXT NOT NULL CHECK (sensitivity IN ('normal','sensitive')),
    cloud_ok      INTEGER NOT NULL CHECK (cloud_ok IN (0,1)),
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    expires_at    REAL,
    supersedes_id TEXT,
    source_task_id TEXT
);
CREATE INDEX IF NOT EXISTS memory_items_status ON memory_items(status);
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, item_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: str
    text: str | None = field(repr=False)   # None => failed authentication (unreadable); never in repr/logs
    origin: str
    status: str
    sensitivity: str
    cloud_ok: int
    created_at: float
    updated_at: float
    last_used_at: float | None
    use_count: int
    expires_at: float | None
    supersedes_id: str | None
    source_task_id: str | None

    @property
    def readable(self) -> bool:
        return self.text is not None


_COLUMNS = ("id, kind, text_enc, origin, status, sensitivity, cloud_ok, created_at, updated_at, "
            "last_used_at, use_count, expires_at, supersedes_id, source_task_id")


_SQLITE_CORRUPT, _SQLITE_NOTADB = 11, 26


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (_SQLITE_CORRUPT, _SQLITE_NOTADB)
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg


def new_id() -> str:
    return "m_" + _rnd.token_hex(5)


class MemoryStore:
    def __init__(self, path, key_provider, now=time.time):
        self.path = Path(path)
        self._keys = key_provider
        self._now = now
        self._cipher: MemoryCipher | None = None
        self._lock = threading.RLock()
        self.busy_timeout_s = 15.0

    # ------------------------------------------------------------ connection / open
    @contextlib.contextmanager
    def _conn(self):
        try:
            con = sqlite3.connect(str(self.path), timeout=self.busy_timeout_s, isolation_level=None)
            try:
                con.execute("PRAGMA secure_delete=ON")
                con.execute("PRAGMA journal_mode=DELETE")
                yield con
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            if _is_corruption(exc):
                raise MemoryUnavailable("db_corrupt", "The memory database is damaged or is not a memory database; "
                                        "it was left untouched.") from exc
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise MemoryUnavailable("busy", "The memory database is busy (locked by another process); "
                                        "try again.") from exc
            raise MemoryUnavailable("db_error", f"The memory database could not be used ({type(exc).__name__}).") from exc
        except sqlite3.Error as exc:
            raise MemoryUnavailable("db_error", f"The memory database could not be used ({type(exc).__name__}).") from exc

    def exists(self) -> bool:
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def _meta(self, con, key):
        row = con.execute("SELECT v FROM schema_meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def _open_existing(self, con) -> MemoryCipher:
        """Validate an existing database and return a cipher proven to match it."""
        try:
            if con.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise MemoryUnavailable("db_corrupt", "The memory database is damaged; it was left untouched.")
            version = self._meta(con, "version")
            check = self._meta(con, "key_check")
        except sqlite3.DatabaseError as exc:
            raise MemoryUnavailable("db_corrupt", "The memory database is damaged or unreadable; it was left untouched.") from exc
        if version is None or check is None:
            raise MemoryUnavailable("db_corrupt", "The memory database is missing its header; it was left untouched.")
        if int(version) > crypto.SCHEMA_VERSION:
            raise MemoryUnavailable("schema_newer", "The memory database was written by a newer V.O.I.D.")
        key = self._keys.get()
        if key is None:
            raise MemoryUnavailable(
                "key_missing",
                "Memory is encrypted and its key is not in the credential store. Restore the key "
                "(it is never regenerated over existing memory). Memory is disabled until then.")
        cipher = MemoryCipher(key)
        try:
            ok = cipher.decrypt(check, crypto.KEY_CHECK_AAD) == crypto.KEY_CHECK_PLAINTEXT
        except DecryptionError:
            ok = False
        if not ok:
            raise MemoryUnavailable("key_wrong", "The memory key in the credential store does not match this database. "
                                    "Memory is disabled; nothing was modified.")
        return cipher

    def _ensure(self, *, create: bool) -> MemoryCipher | None:
        """The verified cipher; None if there is no database and ``create`` is False."""
        with self._lock:
            if self._cipher is not None and self.exists():
                return self._cipher
            self._cipher = None
            if not self.exists():
                if not create:
                    return None
                self._initialise()
                return self._cipher
            with self._conn() as con:
                self._cipher = self._open_existing(con)
            return self._cipher

    def _initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = self._keys.get() or self._keys.create()
        cipher = MemoryCipher(key)
        with self._conn() as con:
            try:
                con.executescript(_SCHEMA)
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT OR REPLACE INTO schema_meta(k, v) VALUES ('version', ?)", (str(crypto.SCHEMA_VERSION),))
                con.execute("INSERT OR REPLACE INTO schema_meta(k, v) VALUES ('generation', '0')")
                con.execute("INSERT OR REPLACE INTO schema_meta(k, v) VALUES ('key_check', ?)",
                            (cipher.encrypt(crypto.KEY_CHECK_PLAINTEXT, crypto.KEY_CHECK_AAD),))
                con.execute("COMMIT")
            except sqlite3.DatabaseError as exc:
                raise MemoryUnavailable("db_corrupt", "The memory database could not be initialised.") from exc
        self._cipher = cipher

    def verify(self) -> str:
        """'absent' | 'ok'; raises MemoryUnavailable when unusable. Read-only."""
        return "ok" if self._ensure(create=False) is not None else "absent"

    # ------------------------------------------------------------ helpers
    def _aad(self, item_id, kind, origin, status, sensitivity, cloud_ok) -> bytes:
        return crypto.item_aad(item_id, kind, origin, status, sensitivity, cloud_ok)

    def _row_to_item(self, row, cipher) -> MemoryItem:
        (iid, kind, blob, origin, status, sens, cloud, created, updated, last, uses, exp, sup, task) = row
        try:
            text = cipher.decrypt(blob, self._aad(iid, kind, origin, status, sens, cloud))
        except DecryptionError:
            text = None
        return MemoryItem(iid, kind, text, origin, status, sens, cloud, created, updated, last, uses, exp, sup, task)

    def _event(self, con, action, item_id, actor) -> None:
        con.execute("INSERT INTO memory_events(ts, item_id, action, actor) VALUES (?,?,?,?)",
                    (self._now(), item_id, action, actor))

    def _bump(self, con) -> None:
        con.execute("UPDATE schema_meta SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT) WHERE k='generation'")

    @contextlib.contextmanager
    def _write(self):
        cipher = self._ensure(create=True)
        with self._conn() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con, cipher
            except BaseException:
                con.execute("ROLLBACK")
                raise
            else:
                self._bump(con)
                con.execute("COMMIT")

    # ------------------------------------------------------------ reads
    def generation(self) -> int:
        if self._ensure(create=False) is None:
            return 0
        with self._conn() as con:
            return int(self._meta(con, "generation") or 0)

    def load(self, item_id: str) -> MemoryItem | None:
        cipher = self._ensure(create=False)
        if cipher is None:
            return None
        with self._conn() as con:
            row = con.execute(f"SELECT {_COLUMNS} FROM memory_items WHERE id=?", (item_id,)).fetchone()
        return self._row_to_item(row, cipher) if row else None

    def list(self, statuses=None) -> list[MemoryItem]:
        cipher = self._ensure(create=False)
        if cipher is None:
            return []
        sql = f"SELECT {_COLUMNS} FROM memory_items"
        params: tuple = ()
        if statuses:
            sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
            params = tuple(statuses)
        sql += " ORDER BY created_at, id"
        with self._conn() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row_to_item(r, cipher) for r in rows]

    def events(self, item_id: str | None = None) -> list[tuple]:
        if self._ensure(create=False) is None:
            return []
        with self._conn() as con:
            if item_id:
                return con.execute("SELECT ts, item_id, action, actor FROM memory_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return con.execute("SELECT ts, item_id, action, actor FROM memory_events ORDER BY id").fetchall()

    # ------------------------------------------------------------ writes
    def insert(self, *, text, kind, origin, status, sensitivity, cloud_ok, actor,
               expires_at=None, supersedes_id=None, source_task_id=None) -> MemoryItem:
        assert kind in KINDS and origin in ORIGINS and status in STATUSES
        iid, now = new_id(), self._now()
        with self._write() as (con, cipher):
            blob = cipher.encrypt(text, self._aad(iid, kind, origin, status, sensitivity, cloud_ok))
            con.execute(f"INSERT INTO memory_items({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, kind, blob, origin, status, sensitivity, int(cloud_ok), now, now, None, 0,
                         expires_at, supersedes_id, source_task_id))
            self._event(con, f"write:{status}", iid, actor)
        return MemoryItem(iid, kind, text, origin, status, sensitivity, int(cloud_ok), now, now, None, 0,
                          expires_at, supersedes_id, source_task_id)

    def update(self, item_id: str, actor: str, action: str, *, text=None, **changes) -> MemoryItem | None:
        """Change an item's text and/or metadata, re-encrypting so the authenticated
        binding always matches the row. ``changes`` may set origin/status/sensitivity/
        cloud_ok/kind/expires_at/supersedes_id."""
        with self._write() as (con, cipher):
            row = con.execute(f"SELECT {_COLUMNS} FROM memory_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return None
            cur = self._row_to_item(row, cipher)
            if cur.text is None and text is None:
                raise MemoryUnavailable("db_corrupt", "That memory entry is unreadable and cannot be modified.")
            new = replace(cur, text=cur.text if text is None else text, updated_at=self._now(), **changes)
            for f, allowed in (("kind", KINDS), ("origin", ORIGINS), ("status", STATUSES)):
                assert getattr(new, f) in allowed
            blob = cipher.encrypt(new.text, self._aad(new.id, new.kind, new.origin, new.status,
                                                      new.sensitivity, new.cloud_ok))
            con.execute("UPDATE memory_items SET kind=?, text_enc=?, origin=?, status=?, sensitivity=?, cloud_ok=?, "
                        "updated_at=?, expires_at=?, supersedes_id=? WHERE id=?",
                        (new.kind, blob, new.origin, new.status, new.sensitivity, int(new.cloud_ok),
                         new.updated_at, new.expires_at, new.supersedes_id, new.id))
            self._event(con, action, item_id, actor)
        return new

    def touch_used(self, ids: list[str]) -> None:
        """Bookkeeping only (use_count / last_used_at); never raises."""
        if not ids or self._ensure(create=False) is None:
            return
        try:
            with self._conn() as con:
                con.execute("BEGIN IMMEDIATE")
                con.executemany("UPDATE memory_items SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                                [(self._now(), i) for i in ids])
                con.execute("COMMIT")
        except (sqlite3.Error, MemoryUnavailable):
            pass

    def _chain(self, con, item_id: str) -> set[str]:
        """The item plus every version linked to it by supersession (older and newer)."""
        seen, todo = set(), [item_id]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for (nid,) in con.execute("SELECT id FROM memory_items WHERE supersedes_id=?", (cur,)):
                todo.append(nid)
            row = con.execute("SELECT supersedes_id FROM memory_items WHERE id=?", (cur,)).fetchone()
            if row and row[0]:
                todo.append(row[0])
        return seen

    def delete(self, item_id: str, actor: str, action: str = "forget") -> int:
        """Hard-delete the item AND all its superseded versions; then VACUUM. Returns rows removed."""
        if self._ensure(create=False) is None:
            return 0
        with self._write() as (con, _cipher):
            ids = self._chain(con, item_id)
            existing = [i for i in ids if con.execute("SELECT 1 FROM memory_items WHERE id=?", (i,)).fetchone()]
            for i in existing:
                con.execute("DELETE FROM memory_items WHERE id=?", (i,))
                self._event(con, action, i, actor)
        self.vacuum()
        return len(existing)

    def delete_many(self, statuses, actor: str, action: str) -> int:
        if self._ensure(create=False) is None:
            return 0
        with self._write() as (con, _cipher):
            marks = ",".join("?" * len(statuses))
            ids = [r[0] for r in con.execute(f"SELECT id FROM memory_items WHERE status IN ({marks})", tuple(statuses))]
            con.execute(f"DELETE FROM memory_items WHERE status IN ({marks})", tuple(statuses))
            for i in ids:
                self._event(con, action, i, actor)
        self.vacuum()
        return len(ids)

    def delete_expired(self, now: float, actor: str = "system") -> int:
        if self._ensure(create=False) is None:
            return 0
        with self._conn() as con:
            due = [r[0] for r in con.execute("SELECT id FROM memory_items WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))]
        n = 0
        for i in due:
            n += self.delete(i, actor, "expire")
        return n

    def vacuum(self) -> None:
        with self._conn() as con:
            con.execute("VACUUM")
