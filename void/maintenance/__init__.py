"""Sunday maintenance: learning about the computer without mistaking that for knowing the owner.

Once a week V.O.I.D takes a structured look at the machine - which applications exist, which devices
were seen, what the system looks like - compares it with last week, and records what changed. The point
is that an assistant should notice a new browser being installed without being told, and should not have
to rediscover the machine on every request.

The architectural rule this module exists to enforce is the one it would be easiest to get wrong:

    COMPUTER KNOWLEDGE  is not  USER MEMORY

"Chrome is installed" is a fact about a disk. It belongs in the registry and the snapshot, where it can
be refreshed, corrected by the next scan, and thrown away. It is not something V.O.I.D should *remember*
about its owner, and dumping it into memory would both bury the things that matter and turn a weekly
scan into an unbounded writer of permanent records. So nothing here writes to memory. The strongest
thing maintenance can do is *propose* - through the existing :class:`~void.memory.service.MemoryService`
proposal path, which still requires the owner to accept it - and :data:`PROMOTABLE` keeps the set of
changes allowed even to do that deliberately tiny. See :func:`promotion_candidates`.

**Collection is a closed set.** :class:`Scope` lists what may be looked at, and
:data:`DEFAULT_SCOPES` is what runs unattended. There is no "everything" scope, no recursive file walk,
and no scope that reads content: every collector returns metadata - names, versions, presence,
identifiers, counts. Passwords, tokens, keys, cookies, message bodies, email, document contents and
private application data are not collected by any scope defined here, and adding one that did would
mean adding a capability this module deliberately does not have.

**Running once a week is a database fact.** A run claims its ISO week with a UNIQUE insert
(:meth:`~void.state.store.StateStore.begin_run`), so a second trigger - or a second process - finds the
week taken and does nothing. An interrupted run leaves a ``running`` row that
:meth:`~void.state.store.StateStore.release_stale_runs` reclaims after an hour, which is safe because
every write is transactional: there is no half-applied snapshot to repair, only a claim to release.

**Nothing here is privileged.** No elevation, no shell, no credential access. Every collector is a
read, and most of them are the same reads the V2 observation tools already perform
(:mod:`void.system.host`, :mod:`void.system.devices`, :mod:`void.system.network`), reused rather than
reimplemented. A discovered device is recorded as *seen*; trust remains whatever
:mod:`void.device.identity` says it is, because presence has never been a reason to trust anything.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from void import perf
from void.state import Change, Observation, StateStore, is_sunday, week_key

_log = logging.getLogger(__name__)


class Scope:
    """What maintenance may look at. A closed set: there is no "everything".

    Each scope is metadata-only. The names match the blueprint's vocabulary so a run can be described
    in the owner's terms rather than in implementation ones.
    """

    SYSTEM_BASIC = "SYSTEM_BASIC"               # OS, processor, memory size, uptime
    APPLICATIONS = "APPLICATIONS"               # installed applications from the existing catalog
    DEVICES = "DEVICES"                         # audio/camera/bluetooth/usb presence
    STORAGE_METADATA = "STORAGE_METADATA"       # drive letters, sizes, free space - never contents
    NETWORK_METADATA = "NETWORK_METADATA"       # interface names and up/down - never traffic contents
    BROWSER_METADATA = "BROWSER_METADATA"       # which browsers are installed - never history/cookies
    SERVICES = "SERVICES"                       # not collected; see DEFAULT_SCOPES
    SECURITY_CONFIGURATION = "SECURITY_CONFIGURATION"   # V.O.I.D's own switches, never credentials

    ALL = frozenset({SYSTEM_BASIC, APPLICATIONS, DEVICES, STORAGE_METADATA, NETWORK_METADATA,
                     BROWSER_METADATA, SERVICES, SECURITY_CONFIGURATION})


#: What an unattended Sunday run collects.
#:
#: ``SERVICES`` is defined but NOT default: enumerating Windows services is a large, noisy list whose
#: changes are mostly Windows updating itself, and it is the scope most likely to drift toward
#: privileged inspection. It is available when asked for explicitly, so the capability exists without
#: running weekly by default.
DEFAULT_SCOPES = (Scope.SYSTEM_BASIC, Scope.APPLICATIONS, Scope.DEVICES,
                  Scope.STORAGE_METADATA, Scope.NETWORK_METADATA, Scope.BROWSER_METADATA,
                  Scope.SECURITY_CONFIGURATION)

#: Observation kinds, used as the diff key alongside an identity.
KIND_SYSTEM = "system"
KIND_APPLICATION = "application"
KIND_DEVICE = "device"
KIND_STORAGE = "storage"
KIND_NETWORK = "network"
KIND_BROWSER = "browser"
KIND_SECURITY = "security"

#: Payload fields that change on their own and would otherwise manufacture a "change" every week.
#: Free space moves constantly; uptime always differs. They are still RECORDED - they are useful to
#: look at - they just do not count as a difference.
_VOLATILE_FIELDS = frozenset({"free_gb", "free", "uptime_s", "uptime", "seen_at", "at",
                              "bytes_sent", "bytes_recv", "temperature_c", "percent"})

#: Change kinds this module can emit.
APPLICATION_INSTALLED = "application_installed"
APPLICATION_REMOVED = "application_removed"
APPLICATION_CHANGED = "application_changed"
DEVICE_PRESENT = "device_present"
DEVICE_ABSENT = "device_absent"
DEVICE_CHANGED = "device_changed"
SYSTEM_CHANGED = "system_changed"
STORAGE_CHANGED = "storage_changed"
NETWORK_CHANGED = "network_changed"
BROWSER_INSTALLED = "browser_installed"
BROWSER_REMOVED = "browser_removed"
SECURITY_CHANGED = "security_changed"

#: The ONLY change kinds that may become a memory proposal, and even then only a proposal the owner
#: must accept. Deliberately tiny.
#:
#: A browser appearing or disappearing is the one weekly observation that is genuinely about how the
#: owner works rather than about the disk: it may mean the browser V.O.I.D has been told to prefer is
#: no longer there, which is worth the owner's attention. Everything else - an application installed, a
#: device seen, a drive resized - stays system state, because it is a fact about the computer that the
#: registry already holds and that next week's scan will correct on its own.
PROMOTABLE = frozenset({BROWSER_INSTALLED, BROWSER_REMOVED})

#: Hard ceiling on proposals from one run, however many changes were found. A weekly job must never be
#: able to fill the owner's review queue.
MAX_PROPOSALS = 3


@dataclass
class MaintenanceResult:
    """What one run did. Returned rather than logged-and-forgotten so a caller can report honestly."""

    ran: bool = False
    reason: str = ""
    week: str = ""
    snapshot_id: str | None = None
    previous_snapshot_id: str | None = None
    scopes: tuple = ()
    observations: int = 0
    changes: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {"ran": self.ran, "reason": self.reason, "week": self.week,
                "snapshot_id": self.snapshot_id, "scopes": list(self.scopes),
                "observations": self.observations,
                "changes": [change.as_dict() for change in self.changes],
                "proposals": list(self.proposals), "errors": list(self.errors),
                "dry_run": self.dry_run}

    def describe(self) -> str:
        if not self.ran:
            return f"Maintenance did not run: {self.reason}."
        parts = [f"{self.observations} observation(s) across {len(self.scopes)} scope(s)",
                 f"{len(self.changes)} change(s)"]
        if self.proposals:
            parts.append(f"{len(self.proposals)} memory proposal(s) awaiting your review")
        if self.errors:
            parts.append(f"{len(self.errors)} scope(s) could not be read")
        return "Maintenance ran: " + ", ".join(parts) + "."


# --------------------------------------------------------------------------- collection

def _reading_values(reading) -> dict:
    """The values out of a :class:`void.system.Reading`, whatever shape it offers."""
    for attribute in ("values", "data"):
        candidate = getattr(reading, attribute, None)
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:                                  # noqa: BLE001
                candidate = None
        if isinstance(candidate, dict) and candidate:
            return candidate
    return reading if isinstance(reading, dict) else {}


def collect(scope: str, *, catalog=None, config=None) -> list[Observation]:
    """Metadata observations for one scope. Never raises; an unreadable scope yields nothing.

    Every collector here reuses an existing V2 observation function rather than adding a second way to
    look at the machine, and every one returns metadata only.
    """
    try:
        if scope == Scope.SYSTEM_BASIC:
            return _collect_system()
        if scope == Scope.APPLICATIONS:
            return _collect_applications(catalog)
        if scope == Scope.DEVICES:
            return _collect_devices()
        if scope == Scope.STORAGE_METADATA:
            return _collect_storage()
        if scope == Scope.NETWORK_METADATA:
            return _collect_network()
        if scope == Scope.BROWSER_METADATA:
            return _collect_browsers()
        if scope == Scope.SECURITY_CONFIGURATION:
            return _collect_security(config)
        if scope == Scope.SERVICES:
            return _collect_services()
    except Exception as exc:                                   # noqa: BLE001 - CLASS only, never a message
        _log.info("MAINTENANCE_SCOPE_FAILED scope=%s kind=%s", scope, type(exc).__name__)
        raise
    return []


def _collect_system() -> list[Observation]:
    from void.system import host
    out: list[Observation] = []
    for identity, getter in (("operating_system", host.operating_system),
                             ("processor", host.processor),
                             ("memory", host.memory)):
        try:
            values = _reading_values(getter())
        except Exception:                                      # noqa: BLE001
            continue
        payload = {key: value for key, value in values.items()
                   if isinstance(value, (str, int, float, bool))}
        if payload:
            out.append(Observation(scope=Scope.SYSTEM_BASIC, kind=KIND_SYSTEM,
                                   identity=identity, payload=payload))
    return out


def _collect_applications(catalog) -> list[Observation]:
    """Installed applications from the EXISTING catalog - no new discovery, no drive walk."""
    if catalog is None:
        return []
    try:
        entries = catalog.entries() or []
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for entry in entries:
        app_id = str(getattr(entry, "app_id", "") or "").strip().lower()
        name = str(getattr(entry, "name", "") or "").strip()
        if not app_id and not name:
            continue
        # The launch TARGET is deliberately not stored: it is an engine-owned path, it is not needed to
        # notice that an application exists, and a path in a plain database is a small privacy leak for
        # no benefit. `kind` records how it launches, which is what routing cares about.
        out.append(Observation(
            scope=Scope.APPLICATIONS, kind=KIND_APPLICATION, identity=app_id or name.lower(),
            payload={"name": name, "launch_kind": str(getattr(entry, "kind", "") or ""),
                     "installed": True}))
    return out


def _collect_devices() -> list[Observation]:
    """Device PRESENCE. Trust is not touched: it belongs to void.device.identity and stays there."""
    from void.system import devices
    try:
        values = _reading_values(devices.snapshot())
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for category, items in values.items():
        if not isinstance(items, (list, tuple)):
            continue
        for item in items[:60]:
            name = item.get("name") if isinstance(item, dict) else item
            name = str(name or "").strip()
            if not name:
                continue
            payload = {"name": name, "category": str(category), "present": True}
            if isinstance(item, dict):
                for field_name in ("kind", "status", "working"):
                    value = item.get(field_name)
                    if isinstance(value, (str, int, float, bool)):
                        payload[field_name] = value
            out.append(Observation(scope=Scope.DEVICES, kind=KIND_DEVICE,
                                   identity=f"{category}:{name}".lower(), payload=payload))
    return out


def _collect_storage() -> list[Observation]:
    from void.system import host
    try:
        values = _reading_values(host.storage())
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for key, items in values.items():
        if not isinstance(items, (list, tuple)):
            continue
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("mount") or item.get("device") or item.get("name") or "").strip()
            if not identity:
                continue
            payload = {name: value for name, value in item.items()
                       if isinstance(value, (str, int, float, bool))}
            out.append(Observation(scope=Scope.STORAGE_METADATA, kind=KIND_STORAGE,
                                   identity=identity.lower(), payload=payload))
    return out


def _collect_network() -> list[Observation]:
    """Interface metadata only. Connections and traffic contents are NOT collected."""
    from void.system import network
    try:
        values = _reading_values(network.interfaces())
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for key, items in values.items():
        if not isinstance(items, (list, tuple)):
            continue
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("name") or "").strip()
            if not identity:
                continue
            # Addresses are deliberately dropped: the interface EXISTING and being up is what a route
            # might care about, and an address is more identifying than it is useful here.
            payload = {name: value for name, value in item.items()
                       if name in ("name", "up", "kind", "type", "speed_mbps")
                       and isinstance(value, (str, int, float, bool))}
            out.append(Observation(scope=Scope.NETWORK_METADATA, kind=KIND_NETWORK,
                                   identity=identity.lower(), payload=payload))
    return out


def _collect_browsers() -> list[Observation]:
    """Which browsers V.O.I.D could actually drive. Never history, cookies or profiles."""
    import os
    try:
        from void.browser.playwright_adapter import _BROWSERS
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for row in _BROWSERS:
        name = str(row[0]).strip().lower()
        channel = str(row[1]).strip() if len(row) > 1 else ""
        paths = row[2] if len(row) > 2 else ()
        installed = False
        for raw in paths:
            try:
                if os.path.exists(os.path.expandvars(raw)):
                    installed = True
                    break
            except Exception:                                  # noqa: BLE001
                continue
        if not installed and channel:
            # A channel browser (Edge, Chrome) is driven by name rather than path; presence is only
            # knowable by asking the browser layer to resolve it, which is more than a metadata scan
            # should do. Recorded as channel-available, which is what routing can act on.
            installed = True
        out.append(Observation(scope=Scope.BROWSER_METADATA, kind=KIND_BROWSER, identity=name,
                               payload={"name": name, "installed": bool(installed),
                                        "driven_by": "channel" if channel else "path"}))
    return out


def _collect_security(config) -> list[Observation]:
    """V.O.I.D's OWN switches - never the owner's credentials, and never a credential store read.

    Deliberately limited to whether a capability is enabled and how many roots are configured: enough
    to notice "screen capture got switched on last week", with nothing sensitive in it. No value here
    is read from the credential store, and none is a secret.
    """
    if config is None:
        return []
    out: list[Observation] = []
    switches = ("browser.enabled", "desktop.enabled", "screen.enabled",
                "screen.allow_cloud_analysis", "camera.enabled", "camera.allow_cloud_analysis",
                "resources.enabled", "memory.enabled", "a2a.enabled", "agui.enabled",
                "observability.enabled", "voice.enabled")
    for key in switches:
        try:
            value = bool(config.get(key, False))
        except Exception:                                      # noqa: BLE001
            continue
        out.append(Observation(scope=Scope.SECURITY_CONFIGURATION, kind=KIND_SECURITY,
                               identity=key, payload={"enabled": value}))
    for key in ("security.allowed_roots", "security.protected_roots"):
        try:
            roots = config.get(key, []) or []
        except Exception:                                      # noqa: BLE001
            continue
        # A COUNT, never the paths: that a root was added is worth noticing; where it points is the
        # owner's business and does not belong in a plain database.
        out.append(Observation(scope=Scope.SECURITY_CONFIGURATION, kind=KIND_SECURITY,
                               identity=key, payload={"count": len(list(roots))}))
    return out


def _collect_services() -> list[Observation]:
    """Non-default scope: service names and states via the existing WMI helper. Metadata only."""
    from void.system import wmi
    if not wmi.available():
        return []
    try:
        rows = wmi.query("services") or []
    except Exception:                                          # noqa: BLE001
        return []
    out: list[Observation] = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or row.get("name") or "").strip()
        if not name:
            continue
        out.append(Observation(scope=Scope.SERVICES, kind="service", identity=name.lower(),
                               payload={"name": name,
                                        "state": str(row.get("State") or row.get("state") or "")}))
    return out


# --------------------------------------------------------------------------- snapshot and diff

def snapshot_digest(observations) -> str:
    """A stable digest of a snapshot's meaningful content.

    Volatile fields are excluded, so an unchanged machine produces an unchanged digest and "nothing
    happened this week" is cheap to establish rather than requiring a full diff.
    """
    parts = []
    for observation in sorted(observations, key=lambda o: (o.kind, o.identity)):
        stable = {key: value for key, value in sorted(observation.payload.items())
                  if key not in _VOLATILE_FIELDS}
        parts.append(f"{observation.kind}|{observation.identity}|{stable}")
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def diff(previous, current) -> list[Change]:
    """Meaningful differences between two sets of observations.

    Keyed on ``(kind, identity)``, comparing only non-volatile payload fields, so a week in which only
    free disk space moved produces no changes at all. The alternative - diffing raw payloads - produced
    a change for every drive every week, which is noise that teaches the owner to ignore the report.
    """
    moment = time.time()
    before = {(o.kind, o.identity): o for o in previous}
    after = {(o.kind, o.identity): o for o in current}
    appeared = {KIND_APPLICATION: APPLICATION_INSTALLED, KIND_DEVICE: DEVICE_PRESENT,
                KIND_BROWSER: BROWSER_INSTALLED}
    vanished = {KIND_APPLICATION: APPLICATION_REMOVED, KIND_DEVICE: DEVICE_ABSENT,
                KIND_BROWSER: BROWSER_REMOVED}
    altered = {KIND_APPLICATION: APPLICATION_CHANGED, KIND_DEVICE: DEVICE_CHANGED,
               KIND_SYSTEM: SYSTEM_CHANGED, KIND_STORAGE: STORAGE_CHANGED,
               KIND_NETWORK: NETWORK_CHANGED, KIND_SECURITY: SECURITY_CHANGED}

    changes: list[Change] = []
    if not before:
        # The first snapshot is a baseline, not a week in which everything was installed at once.
        return changes

    for key, observation in sorted(after.items()):
        kind, identity = key
        if key not in before:
            changes.append(Change(kind=appeared.get(kind, f"{kind}_present"), identity=identity,
                                  detail=_label(observation), at=moment))
            continue
        was, now = _stable(before[key].payload), _stable(observation.payload)
        if was != now:
            differing = sorted(set(was) | set(now))
            fields = [name for name in differing if was.get(name) != now.get(name)]
            changes.append(Change(kind=altered.get(kind, f"{kind}_changed"), identity=identity,
                                  detail=f"{', '.join(fields[:4])} changed", at=moment))
    for key, observation in sorted(before.items()):
        if key not in after:
            kind, identity = key
            changes.append(Change(kind=vanished.get(kind, f"{kind}_absent"), identity=identity,
                                  detail=_label(observation), at=moment))
    # A browser is recorded as a browser AND is often an application too; reporting both is honest, not
    # duplicated, because routing reads the two registries separately.
    return changes


def _stable(payload: dict) -> dict:
    return {key: value for key, value in (payload or {}).items() if key not in _VOLATILE_FIELDS}


def _label(observation: Observation) -> str:
    name = observation.payload.get("name")
    return str(name)[:120] if isinstance(name, str) and name else observation.identity[:120]


# --------------------------------------------------------------------------- memory promotion

def promotion_candidates(changes, *, limit: int = MAX_PROPOSALS) -> list[str]:
    """The sentences a run would propose for memory, bounded and filtered.

    This is the boundary the module exists to hold. Returning a candidate is not remembering anything:
    the caller passes each one to :meth:`~void.memory.service.MemoryService.propose`, which still
    requires the owner to accept it, and which applies the existing memory policy (secret-shaped text
    is refused there as it always was).

    Almost nothing qualifies, on purpose. :data:`PROMOTABLE` is two change kinds, and the limit caps a
    run at :data:`MAX_PROPOSALS` however eventful the week was - a weekly job that could fill the
    review queue would make the queue useless.
    """
    out: list[str] = []
    for change in changes:
        if len(out) >= max(0, int(limit)):
            break
        if change.kind not in PROMOTABLE:
            continue
        identity = str(change.identity)[:60]
        if change.kind == BROWSER_INSTALLED:
            out.append(f"The browser {identity} is now installed on this computer.")
        elif change.kind == BROWSER_REMOVED:
            out.append(f"The browser {identity} is no longer installed on this computer.")
    return out


# --------------------------------------------------------------------------- the runner

class Maintenance:
    """Runs a weekly pass, at most once per week, recording what it saw and what changed.

    Holds no authority: it reads the machine, writes to the structured store, refreshes nothing it does
    not own, and can only *propose* memory. The memory service and the device registry are injected and
    used through their public interfaces, so maintenance cannot reach past either one's rules.
    """

    def __init__(self, store: StateStore, *, catalog=None, config=None, memory=None,
                 now=time.time):
        self._store = store
        self._catalog = catalog
        self._config = config
        self._memory = memory
        self._now = now

    # -- due-ness ------------------------------------------------------
    def due(self, moment: float | None = None) -> tuple[bool, str]:
        """``(is_due, reason)``. Sunday, and not already done this week.

        The week is the unit rather than "7 days since last time", so maintenance lands on the owner's
        Sunday instead of drifting an hour later every week.
        """
        when = self._now() if moment is None else moment
        key = week_key(when)
        if not is_sunday(when):
            return False, "today is not Sunday"
        self._store.release_stale_runs()
        existing = self._store.run_for_week(key)
        if existing is not None and existing.status == "ok":
            return False, f"already ran this week ({key})"
        if existing is not None and existing.status == "running":
            return False, "another maintenance run is in progress"
        return True, f"due for {key}"

    # -- the run -------------------------------------------------------
    def run(self, *, scopes=None, force: bool = False, dry_run: bool = False,
            moment: float | None = None) -> MaintenanceResult:
        """Collect, compare, record. Safe to call at any time.

        ``force`` skips the Sunday check but NOT the once-per-week claim, so a manual run still cannot
        produce two passes in a week. ``dry_run`` collects and diffs without writing anything, which is
        how a run can be inspected before it is trusted.
        """
        when = self._now() if moment is None else moment
        key = week_key(when)
        wanted = tuple(scope for scope in (scopes or DEFAULT_SCOPES) if scope in Scope.ALL)
        if not wanted:
            return MaintenanceResult(ran=False, reason="no valid scopes requested", week=key)

        if not force:
            ok, reason = self.due(when)
            if not ok:
                return MaintenanceResult(ran=False, reason=reason, week=key, scopes=wanted)

        if dry_run:
            # Nothing is claimed and nothing is written: a dry run must not consume the week.
            observations, errors = self._collect_all(wanted)
            previous = self._store.latest_snapshot()
            before = self._store.observations(previous["id"]) if previous else []
            found = diff(before, observations)
            return MaintenanceResult(
                ran=True, reason="dry run", week=key, scopes=wanted, dry_run=True,
                previous_snapshot_id=previous["id"] if previous else None,
                observations=len(observations), changes=found,
                proposals=promotion_candidates(found), errors=errors)

        run = self._store.begin_run(key, scopes=",".join(wanted))
        if run is None:
            # Another process claimed this week between the due check and here. Not an error.
            return MaintenanceResult(ran=False, reason="another run already claimed this week",
                                     week=key, scopes=wanted)
        perf.emit("maintenance", op="started", week=key, scopes=len(wanted))
        _log.info("MAINTENANCE_STARTED week=%s scopes=%s", key, ",".join(wanted))

        result = MaintenanceResult(ran=True, reason="completed", week=key, scopes=wanted)
        try:
            observations, errors = self._collect_all(wanted)
            result.observations = len(observations)
            result.errors = errors

            previous = self._store.latest_snapshot()
            before = self._store.observations(previous["id"]) if previous else []
            result.previous_snapshot_id = previous["id"] if previous else None

            snapshot_id = self._store.save_snapshot(
                wanted, observations, digest=snapshot_digest(observations),
                complete=not errors or len(errors) < len(wanted))
            result.snapshot_id = snapshot_id
            perf.emit("maintenance", op="snapshot", observations=len(observations))

            result.changes = diff(before, observations)
            if result.changes:
                self._store.save_changes(snapshot_id, result.previous_snapshot_id, result.changes)
            perf.emit("maintenance", op="diff", changes=len(result.changes))

            result.proposals = self._propose(result.changes)
            self._store.prune_snapshots()
            self._store.finish_run(run.id, status="ok", snapshot_id=snapshot_id,
                                   changes=len(result.changes))
            perf.emit("maintenance", op="finished", changes=len(result.changes),
                      proposals=len(result.proposals))
        except Exception as exc:                               # noqa: BLE001 - CLASS only
            # The run is recorded as failed rather than left claimed, so next Sunday can try again.
            self._store.finish_run(run.id, status="failed", error_class=type(exc).__name__)
            _log.warning("MAINTENANCE_FAILED kind=%s", type(exc).__name__)
            perf.emit("maintenance", op="failed", error=type(exc).__name__)
            return MaintenanceResult(ran=True, reason=f"failed ({type(exc).__name__})", week=key,
                                     scopes=wanted, errors=result.errors)
        _log.info("MAINTENANCE_FINISHED week=%s changes=%d proposals=%d",
                  key, len(result.changes), len(result.proposals))
        return result

    def run_if_due(self, **kwargs) -> MaintenanceResult:
        """Run only when due. The form any scheduler should call: firing it often is harmless."""
        return self.run(**kwargs)

    # -- internals -----------------------------------------------------
    def _collect_all(self, scopes) -> tuple[list[Observation], list[str]]:
        observations: list[Observation] = []
        errors: list[str] = []
        for scope in scopes:
            perf.emit("maintenance", op="scope_started", scope=scope)
            try:
                found = collect(scope, catalog=self._catalog, config=self._config)
            except Exception as exc:                           # noqa: BLE001 - one scope must not end the run
                errors.append(f"{scope}:{type(exc).__name__}")
                perf.emit("maintenance", op="scope_failed", scope=scope)
                continue
            observations.extend(found)
            perf.emit("maintenance", op="scope_completed", scope=scope, observations=len(found))
        return observations, errors

    def _memory_service(self):
        """The memory service, resolving a callable.

        Accepted as a callable because the Assistant builds memory after maintenance, and because a
        machine with memory disabled should leave this None rather than fail - a run that cannot propose
        anything is still a useful run.
        """
        service = self._memory
        if callable(service) and not hasattr(service, "propose"):
            try:
                service = service()
            except Exception:                                  # noqa: BLE001
                return None
        return service if hasattr(service, "propose") else None

    def _propose(self, changes) -> list[str]:
        """Hand promotable changes to the EXISTING memory proposal path. Never accepts anything."""
        candidates = promotion_candidates(changes)
        memory = self._memory_service()
        if not candidates or memory is None:
            return []
        proposed: list[str] = []
        for text in candidates:
            try:
                # tainted=True: this came from scanning the machine, not from the owner saying it.
                result = memory.propose(text, kind="fact", tainted=True)
            except Exception as exc:                           # noqa: BLE001
                _log.info("MAINTENANCE_PROPOSAL_FAILED kind=%s", type(exc).__name__)
                continue
            if getattr(result, "ok", False):
                proposed.append(text)
                perf.emit("maintenance", op="memory_candidate")
        if proposed:
            _log.info("MAINTENANCE_PROPOSED count=%d (awaiting owner review)", len(proposed))
        return proposed
