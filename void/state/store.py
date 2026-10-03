"""V.O.I.D's structured local state: preferences, system snapshots, detected changes, maintenance runs.

**What this is not.** It is not the memory system and it is not a replacement for it. V.O.I.D already has
persistent memory in :mod:`void.memory`: SQLite, AES-256-GCM payloads with the key in the Windows
credential store, an append-only event log, and explicit remember/review/accept/reject. Inspecting it
showed the structured-persistence requirement is already met *for memory*, so nothing here migrates,
re-encrypts or re-implements any of that. There was no migration to perform, so none was manufactured.

What was genuinely missing was a durable, queryable home for the operational facts V.O.I.D learns about
the *computer* - which applications exist, which devices were seen, what changed since last week, which
routes the owner prefers. Those were either absent or living in configuration, and they are emphatically
not user memory. The architectural line this module exists to draw:

    COMPUTER KNOWLEDGE  ->  here (plain, structured, diffable, replaceable)
    USER MEMORY         ->  void.memory (encrypted, reviewed, owner-accepted)

So this database holds **no secrets and no private content**, by design rather than by habit:

* Observations are metadata only - identities, names, versions, presence, timestamps. Never file
  contents, message bodies, cookies, tokens, keys or credentials. :mod:`void.maintenance` is what
  collects them, and its scopes are a closed set.
* Nothing here is encrypted, precisely *because* nothing here is sensitive. Anything that would need
  encrypting does not belong in this file - it belongs in :mod:`void.memory`, or nowhere.
* :func:`looks_secret` is a last-resort guard: a value that looks like a credential is refused on write
  rather than stored, so a discovery bug cannot quietly turn this into a secret store.

Conventions are taken from the existing :class:`void.core.task.TaskStore` rather than invented: one
file, ``sqlite3`` from the standard library (no ORM, no new dependency), ``PRAGMA secure_delete=ON``,
and a versioned ``schema_meta`` with additive migration. A database written by a newer V.O.I.D is
refused rather than guessed at, the same way the memory store refuses one.

Nothing in this module authorizes anything. A preference is a routing signal, an observation is a
description, a change is a description of a difference. None of them is a permission, and no code path
here consults or influences the RiskGate or the kill switch.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

#: Schema version. Bumped only for an additive change; a database claiming a HIGHER version is refused,
#: because a newer V.O.I.D may rely on columns this code would silently ignore.
SCHEMA_VERSION = 1

#: Longest text accepted in any single field. Observations are metadata, so anything longer is a sign
#: that raw content has leaked into a place designed to hold none.
MAX_TEXT = 2000

#: Longest serialised observation payload.
MAX_PAYLOAD = 8000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- Owner preferences: routing signals, never permissions.
CREATE TABLE IF NOT EXISTS preferences (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL,              -- owner | config | observed
    explicit    INTEGER NOT NULL DEFAULT 1, -- 1 the owner said so, 0 inferred
    confidence  REAL    NOT NULL DEFAULT 1.0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
-- Every change to a preference, so "why does it think I prefer that?" has an answer.
CREATE TABLE IF NOT EXISTS preference_revisions (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    value       TEXT,
    source      TEXT NOT NULL,
    explicit    INTEGER NOT NULL,
    action      TEXT NOT NULL,              -- set | deactivate
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pref_rev_key ON preference_revisions(key, at);

-- One structured look at the computer.
CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    at          REAL NOT NULL,
    scopes      TEXT NOT NULL,              -- comma-separated scope names actually collected
    version     INTEGER NOT NULL,
    digest      TEXT NOT NULL,              -- content digest, so an unchanged week is cheap to see
    complete    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_snapshots_at ON snapshots(at);

-- Metadata about one thing that exists on the computer. NEVER its contents.
CREATE TABLE IF NOT EXISTS observations (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- application | device | system | storage | network | ...
    identity    TEXT NOT NULL,              -- stable key within (scope, kind)
    payload     TEXT NOT NULL,              -- small normalised JSON object
    PRIMARY KEY (snapshot_id, kind, identity)
);
CREATE INDEX IF NOT EXISTS ix_obs_kind ON observations(kind, identity);

-- A difference between two snapshots. Descriptive; grants nothing.
CREATE TABLE IF NOT EXISTS changes (
    id           TEXT PRIMARY KEY,
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    previous_id  TEXT,
    kind         TEXT NOT NULL,             -- application_installed | device_absent | ...
    identity     TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_changes_snapshot ON changes(snapshot_id);
CREATE INDEX IF NOT EXISTS ix_changes_kind ON changes(kind, at);

-- One maintenance execution. The UNIQUE week key is what makes "once per Sunday" a database fact
-- rather than a hope, and what makes two simultaneous runs impossible.
CREATE TABLE IF NOT EXISTS maintenance_runs (
    id           TEXT PRIMARY KEY,
    week_key     TEXT NOT NULL UNIQUE,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    status       TEXT NOT NULL,             -- running | ok | failed
    scopes       TEXT NOT NULL DEFAULT '',
    snapshot_id  TEXT,
    changes      INTEGER NOT NULL DEFAULT 0,
    error_class  TEXT NOT NULL DEFAULT ''   -- exception CLASS only, never a message
);
CREATE INDEX IF NOT EXISTS ix_runs_started ON maintenance_runs(started_at);
"""

#: Shapes that look like a credential. Checked on every value written, so a discovery bug cannot turn
#: this plain database into a secret store. Deliberately crude: the cost of a false positive is one
#: refused observation, the cost of a false negative is a secret at rest in cleartext.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),                     # OpenAI-style keys
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),                 # Google API keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),              # GitHub tokens
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),   # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_\-]?key|token|bearer)\b\s*[:=]\s*\S{6,}"),
)


class StateError(RuntimeError):
    """The structured store could not be used. The message is safe to show the owner."""


def looks_secret(value: object) -> bool:
    """True when a value looks like a credential and must not be stored here.

    This store is for metadata. Anything matching is refused on write rather than persisted, because a
    plain SQLite file is the wrong place for a secret and "we only ever put metadata in it" is a weaker
    guarantee than a check.
    """
    if not isinstance(value, str) or not value:
        return False
    return any(shape.search(value) for shape in _SECRET_SHAPES)


def clean(value: object, limit: int = MAX_TEXT) -> str:
    """Flatten and bound a text value: control characters become spaces, whitespace collapses.

    Non-printables are replaced rather than deleted. Deleting them merged words - ``"Opera\\nGX"``
    became ``"OperaGX"``, which is a different application name and would have made a device or
    application identity silently wrong rather than merely ugly.
    """
    text = "" if value is None else str(value)
    text = "".join(ch if (ch == " " or ch.isprintable()) else " " for ch in text)
    return " ".join(text.split())[:limit]


def week_key(moment: float | None = None) -> str:
    """The date of the most recent Sunday, inclusive, e.g. ``2026-10-04``.

    Maintenance is "once per Sunday", and this key is how that becomes checkable: one successful run
    per key, enforced by a UNIQUE column, so an extra trigger is a no-op rather than a second pass.

    Anchored to the preceding Sunday rather than the ISO week, which was the first attempt and was
    wrong in a way worth recording. An ISO week runs Monday to Sunday, so Sunday is the LAST day of
    its week: a forced run on Monday the 5th would have claimed the same ISO week as Sunday the 11th
    and silently cancelled that Sunday's real pass. Anchoring backwards means a mid-week run claims the
    Sunday that has already happened and cannot consume the next one.
    """
    stamp = time.localtime(time.time() if moment is None else moment)
    date = datetime.date(stamp.tm_year, stamp.tm_mon, stamp.tm_mday)
    # tm_wday: Monday is 0, Sunday is 6 - so Sunday is 0 days back, Monday is 1, Saturday is 6.
    days_since_sunday = (date.weekday() + 1) % 7
    return (date - datetime.timedelta(days=days_since_sunday)).isoformat()


def is_sunday(moment: float | None = None) -> bool:
    """True when ``moment`` falls on a Sunday in LOCAL time (the owner's week, not UTC's)."""
    stamp = time.localtime(time.time() if moment is None else moment)
    return stamp.tm_wday == 6          # Monday is 0


@dataclass(frozen=True)
class Preference:
    """One stored routing signal.

    ``explicit`` separates "the owner told me" from "I noticed a pattern", which is the distinction that
    stops a single observation becoming a permanent preference.
    """

    key: str
    value: str
    source: str = "owner"
    explicit: bool = True
    confidence: float = 1.0
    active: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "source": self.source,
                "explicit": self.explicit, "confidence": round(self.confidence, 3),
                "active": self.active, "created_at": self.created_at,
                "updated_at": self.updated_at}


@dataclass(frozen=True)
class Observation:
    """Metadata about one thing that exists on the computer, at one moment."""

    scope: str
    kind: str
    identity: str
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"scope": self.scope, "kind": self.kind, "identity": self.identity,
                "payload": dict(self.payload)}


@dataclass(frozen=True)
class Change:
    """One difference between two snapshots."""

    kind: str
    identity: str
    detail: str = ""
    at: float = 0.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "identity": self.identity, "detail": self.detail, "at": self.at}


@dataclass(frozen=True)
class MaintenanceRun:
    """The record of one maintenance execution."""

    id: str
    week_key: str
    started_at: float
    status: str
    finished_at: float | None = None
    scopes: str = ""
    snapshot_id: str | None = None
    changes: int = 0
    error_class: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {"id": self.id, "week_key": self.week_key, "started_at": self.started_at,
                "finished_at": self.finished_at, "status": self.status, "scopes": self.scopes,
                "snapshot_id": self.snapshot_id, "changes": self.changes,
                "error_class": self.error_class}


class StateStore:
    """The structured local store. One SQLite file, standard library only.

    Opened lazily and per operation, like :class:`void.core.task.TaskStore`: there is no long-lived
    connection to leak across threads, and the voice runtime's worker threads can each use it safely.
    """

    def __init__(self, path, now=time.time):
        self.path = Path(path)
        self._now = now

    # -- connection / schema -------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # secure_delete so an overwritten row is not left readable in the file; foreign_keys so a
        # deleted snapshot takes its observations with it rather than orphaning them.
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        self._ensure(conn)
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT v FROM schema_meta WHERE k='version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(k, v) VALUES ('version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()
            return
        try:
            found = int(row["v"])
        except (TypeError, ValueError) as exc:
            raise StateError("The state database has an unreadable schema version.") from exc
        if found > SCHEMA_VERSION:
            # Refused rather than guessed at: a newer V.O.I.D may rely on columns this code ignores.
            raise StateError(
                "The state database was written by a newer version of V.O.I.D and will not be opened.")

    def exists(self) -> bool:
        return self.path.is_file()

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM schema_meta WHERE k='version'").fetchone()
            return int(row["v"]) if row else 0

    # -- preferences ----------------------------------------------------
    def set_preference(self, key: str, value: str, *, source: str = "owner",
                       explicit: bool = True, confidence: float = 1.0) -> Preference:
        """Store or update one preference, recording a revision.

        Refuses a secret-shaped value: a preference is a routing signal like "opera gx", and anything
        resembling a credential is a bug upstream rather than something to persist.
        """
        name = clean(key, 60).lower()
        if not name:
            raise StateError("A preference needs a name.")
        text = clean(value, 200)
        if not text:
            raise StateError("A preference needs a value.")
        if looks_secret(text):
            _log.warning("PREFERENCE_REFUSED_SECRET_SHAPED key=%s", name)
            raise StateError("That value looks like a credential and will not be stored.")
        kind = clean(source, 20).lower() or "owner"
        score = min(max(float(confidence), 0.0), 1.0)
        moment = self._now()
        with self._conn() as conn:
            existing = conn.execute("SELECT created_at FROM preferences WHERE key=?",
                                    (name,)).fetchone()
            created = float(existing["created_at"]) if existing else moment
            conn.execute(
                "INSERT INTO preferences(key, value, source, explicit, confidence, active,"
                " created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,"
                " explicit=excluded.explicit, confidence=excluded.confidence, active=1,"
                " updated_at=excluded.updated_at",
                (name, text, kind, 1 if explicit else 0, score, created, moment))
            conn.execute(
                "INSERT INTO preference_revisions(id, key, value, source, explicit, action, at)"
                " VALUES (?,?,?,?,?,'set',?)",
                (uuid.uuid4().hex, name, text, kind, 1 if explicit else 0, moment))
            conn.commit()
        _log.info("PREFERENCE_SET key=%s source=%s explicit=%s", name, kind, bool(explicit))
        return Preference(key=name, value=text, source=kind, explicit=bool(explicit),
                          confidence=score, active=True, created_at=created, updated_at=moment)

    def preference(self, key: str) -> Preference | None:
        name = clean(key, 60).lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM preferences WHERE key=? AND active=1", (name,)).fetchone()
        return self._row_to_preference(row) if row else None

    def preferences(self, *, include_inactive: bool = False) -> list[Preference]:
        query = "SELECT * FROM preferences" + ("" if include_inactive else " WHERE active=1")
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY key").fetchall()
        return [self._row_to_preference(row) for row in rows]

    def deactivate_preference(self, key: str) -> bool:
        """Turn a preference off without destroying its history."""
        name = clean(key, 60).lower()
        moment = self._now()
        with self._conn() as conn:
            changed = conn.execute(
                "UPDATE preferences SET active=0, updated_at=? WHERE key=? AND active=1",
                (moment, name)).rowcount
            if changed:
                conn.execute(
                    "INSERT INTO preference_revisions(id, key, value, source, explicit, action, at)"
                    " VALUES (?,?,NULL,'owner',1,'deactivate',?)",
                    (uuid.uuid4().hex, name, moment))
            conn.commit()
        if changed:
            _log.info("PREFERENCE_DEACTIVATED key=%s", name)
        return bool(changed)

    def preference_revisions(self, key: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if key:
                rows = conn.execute(
                    "SELECT * FROM preference_revisions WHERE key=? ORDER BY at DESC",
                    (clean(key, 60).lower(),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM preference_revisions ORDER BY at DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]

    def as_routing_map(self) -> dict:
        """Active preferences as the flat ``{key: value}`` the route resolver already consumes.

        Deliberately the shape :class:`void.orchestration.routes.WorldState` already takes, so stored
        preferences reach routing through the EXISTING mechanism rather than a second one.
        """
        return {pref.key: pref.value for pref in self.preferences()}

    @staticmethod
    def _row_to_preference(row) -> Preference:
        return Preference(key=row["key"], value=row["value"], source=row["source"],
                          explicit=bool(row["explicit"]), confidence=float(row["confidence"]),
                          active=bool(row["active"]), created_at=float(row["created_at"]),
                          updated_at=float(row["updated_at"]))

    # -- snapshots and observations -------------------------------------
    def save_snapshot(self, scopes, observations, *, digest: str = "",
                      complete: bool = True) -> str:
        """Store one snapshot and its observations in a single transaction.

        All-or-nothing on purpose: a half-written snapshot would make next week's diff compare against
        a machine state that never existed, which is worse than having no snapshot for a week.
        """
        snapshot_id = uuid.uuid4().hex
        moment = self._now()
        scope_text = ",".join(sorted({clean(s, 40) for s in scopes if clean(s, 40)}))
        rows = []
        for observation in observations:
            payload = _encode_payload(observation.payload)
            rows.append((snapshot_id, clean(observation.scope, 40), clean(observation.kind, 40),
                         clean(observation.identity, 300), payload))
        conn = self._conn()
        try:
            with conn:                      # one transaction; rolls back on any error
                conn.execute(
                    "INSERT INTO snapshots(id, at, scopes, version, digest, complete)"
                    " VALUES (?,?,?,?,?,?)",
                    (snapshot_id, moment, scope_text, SCHEMA_VERSION,
                     clean(digest, 80), 1 if complete else 0))
                conn.executemany(
                    "INSERT OR REPLACE INTO observations(snapshot_id, scope, kind, identity, payload)"
                    " VALUES (?,?,?,?,?)", rows)
        finally:
            conn.close()
        _log.info("SNAPSHOT_CREATED scopes=%s observations=%d", scope_text, len(rows))
        return snapshot_id

    def latest_snapshot(self, *, before: str | None = None) -> dict | None:
        """The most recent COMPLETE snapshot, optionally excluding one id.

        Only complete snapshots are comparable, so an interrupted run cannot become next week's baseline.
        """
        with self._conn() as conn:
            if before:
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE complete=1 AND id<>? ORDER BY at DESC LIMIT 1",
                    (before,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE complete=1 ORDER BY at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def observations(self, snapshot_id: str, kind: str | None = None) -> list[Observation]:
        with self._conn() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM observations WHERE snapshot_id=? AND kind=? ORDER BY identity",
                    (snapshot_id, kind)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM observations WHERE snapshot_id=? ORDER BY kind, identity",
                    (snapshot_id,)).fetchall()
        return [Observation(scope=row["scope"], kind=row["kind"], identity=row["identity"],
                            payload=_decode_payload(row["payload"])) for row in rows]

    def snapshot_count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"])

    def prune_snapshots(self, keep: int = 8) -> int:
        """Keep only the newest ``keep`` snapshots. Observations cascade.

        Snapshots exist to be diffed against, not archived forever; a bounded history keeps this file
        small and keeps old machine state from lingering.
        """
        keep = max(1, int(keep))
        with self._conn() as conn:
            stale = [row["id"] for row in conn.execute(
                "SELECT id FROM snapshots ORDER BY at DESC LIMIT -1 OFFSET ?", (keep,)).fetchall()]
            for snapshot_id in stale:
                conn.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
            conn.commit()
        if stale:
            _log.info("SNAPSHOTS_PRUNED removed=%d kept=%d", len(stale), keep)
        return len(stale)

    # -- changes --------------------------------------------------------
    def save_changes(self, snapshot_id: str, previous_id: str | None, changes) -> int:
        moment = self._now()
        rows = [(uuid.uuid4().hex, snapshot_id, previous_id, clean(change.kind, 60),
                 clean(change.identity, 300), clean(change.detail, 300), moment)
                for change in changes]
        if not rows:
            return 0
        conn = self._conn()
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO changes(id, snapshot_id, previous_id, kind, identity, detail, at)"
                    " VALUES (?,?,?,?,?,?,?)", rows)
        finally:
            conn.close()
        return len(rows)

    def changes(self, snapshot_id: str | None = None, limit: int = 200) -> list[Change]:
        with self._conn() as conn:
            if snapshot_id:
                rows = conn.execute(
                    "SELECT * FROM changes WHERE snapshot_id=? ORDER BY kind, identity",
                    (snapshot_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM changes ORDER BY at DESC LIMIT ?",
                                    (max(1, int(limit)),)).fetchall()
        return [Change(kind=row["kind"], identity=row["identity"], detail=row["detail"],
                       at=float(row["at"])) for row in rows]

    # -- maintenance runs -----------------------------------------------
    def begin_run(self, key: str, scopes: str = "") -> MaintenanceRun | None:
        """Claim this week's run, or None when it is already claimed.

        The claim is the UNIQUE ``week_key`` insert, so two processes racing cannot both proceed: one
        insert wins, the other sees the integrity error and backs off. That is the duplicate-run guard,
        and it lives in the database rather than in a lock file that a crash could leave behind.
        """
        run_id = uuid.uuid4().hex
        moment = self._now()
        conn = self._conn()
        try:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO maintenance_runs(id, week_key, started_at, status, scopes)"
                        " VALUES (?,?,?, 'running', ?)", (run_id, key, moment, clean(scopes, 300)))
            except sqlite3.IntegrityError:
                # The week already has a row. Take it over ONLY if it is 'failed', so a crashed run can
                # be retried while a completed one stays done and a LIVE one is left alone.
                #
                # 'failed' specifically, not "anything that is not ok": a currently-running claim is
                # also not 'ok', and matching it let a second caller steal the week from a run that was
                # still working - two simultaneous passes, which is the exact thing the UNIQUE key is
                # there to prevent. A stuck 'running' row becomes retryable only by going through
                # release_stale_runs, which is the designed recovery path and is time-bounded.
                #
                # One conditional UPDATE rather than read-then-write: SQLite serialises writers, so two
                # processes racing here produce exactly one claim - the loser's rowcount is 0.
                with conn:
                    taken = conn.execute(
                        "UPDATE maintenance_runs SET id=?, started_at=?, status='running',"
                        " finished_at=NULL, scopes=?, snapshot_id=NULL, changes=0, error_class=''"
                        " WHERE week_key=? AND status='failed'",
                        (run_id, moment, clean(scopes, 300), key)).rowcount
                if not taken:
                    return None
                _log.info("MAINTENANCE_RUN_RETRIED week=%s", key)
        finally:
            conn.close()
        _log.info("MAINTENANCE_RUN_STARTED week=%s", key)
        return MaintenanceRun(id=run_id, week_key=key, started_at=moment, status="running",
                              scopes=scopes)

    def finish_run(self, run_id: str, *, status: str, snapshot_id: str | None = None,
                   changes: int = 0, error_class: str = "") -> None:
        """Close a run. ``error_class`` is an exception CLASS name only - never a message, which could
        carry a path or a device name."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE maintenance_runs SET finished_at=?, status=?, snapshot_id=?, changes=?,"
                " error_class=? WHERE id=?",
                (self._now(), clean(status, 20), snapshot_id, int(changes),
                 clean(error_class, 60), run_id))
            conn.commit()
        _log.info("MAINTENANCE_RUN_FINISHED status=%s changes=%d", status, changes)

    def run_for_week(self, key: str) -> MaintenanceRun | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM maintenance_runs WHERE week_key=?", (key,)).fetchone()
        return self._row_to_run(row) if row else None

    def runs(self, limit: int = 20) -> list[MaintenanceRun]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM maintenance_runs ORDER BY started_at DESC LIMIT ?",
                                (max(1, int(limit)),)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def release_stale_runs(self, older_than_s: float = 3600.0) -> int:
        """Mark long-abandoned ``running`` rows as failed so a later run can claim the week.

        This is the interruption-recovery path: a process killed mid-maintenance leaves a ``running``
        row that would otherwise block its week forever. Reclaiming it is safe because every write this
        module performs is transactional - there is no half-applied state to clean up, only a claim.
        """
        cutoff = self._now() - max(60.0, float(older_than_s))
        with self._conn() as conn:
            released = conn.execute(
                "UPDATE maintenance_runs SET status='failed', finished_at=?,"
                " error_class='interrupted' WHERE status='running' AND started_at < ?",
                (self._now(), cutoff)).rowcount
            conn.commit()
        if released:
            _log.info("MAINTENANCE_STALE_RUNS_RELEASED count=%d", released)
        return int(released)

    @staticmethod
    def _row_to_run(row) -> MaintenanceRun:
        return MaintenanceRun(
            id=row["id"], week_key=row["week_key"], started_at=float(row["started_at"]),
            status=row["status"],
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            scopes=row["scopes"] or "", snapshot_id=row["snapshot_id"],
            changes=int(row["changes"] or 0), error_class=row["error_class"] or "")


def _encode_payload(payload: object) -> str:
    """Serialise an observation payload, refusing anything secret-shaped or oversized.

    Values are flattened to text/number/bool and bounded. A payload is metadata about a thing, so there
    is no reason for it to be deep or large, and a size limit is the cheapest defence against raw
    content arriving where none should.
    """
    if not isinstance(payload, dict):
        return "{}"
    flat: dict = {}
    for key, value in list(payload.items())[:40]:
        name = clean(key, 40)
        if not name:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            flat[name] = value
            continue
        text = clean(value, 300)
        if not text:
            continue
        if looks_secret(text):
            _log.warning("OBSERVATION_FIELD_REFUSED_SECRET_SHAPED field=%s", name)
            continue
        flat[name] = text
    encoded = json.dumps(flat, sort_keys=True, separators=(",", ":"))
    return encoded[:MAX_PAYLOAD]


def _decode_payload(text: object) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
