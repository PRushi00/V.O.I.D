"""Sunday maintenance: scopes, snapshots, diffs, and the line between knowing and remembering.

The property most of these defend is the one the whole module exists for:

    COMPUTER KNOWLEDGE  is not  USER MEMORY

"Chrome is installed" is a fact about a disk that next week's scan will correct on its own. It belongs
in the snapshot and the registry, and writing it into memory would both bury the things that matter and
turn a weekly job into an unbounded writer of permanent records. So several tests here exist purely to
assert that maintenance cannot write memory, cannot accept a proposal, and cannot flood the review
queue even on an eventful week.

The rest cover the parts that only matter when something goes wrong: a run that is interrupted, a
trigger that fires twice, a scope that cannot be read, and a week in which nothing changed but the
clock.
"""
from __future__ import annotations

import time

import pytest

from void.maintenance import (APPLICATION_CHANGED, APPLICATION_INSTALLED, APPLICATION_REMOVED,
                              BROWSER_INSTALLED, BROWSER_REMOVED, DEFAULT_SCOPES, DEVICE_ABSENT,
                              DEVICE_PRESENT, KIND_APPLICATION, KIND_BROWSER, KIND_DEVICE,
                              KIND_SECURITY, KIND_STORAGE, MAX_PROPOSALS, PROMOTABLE, Maintenance,
                              Scope, diff, promotion_candidates, snapshot_digest)
from void.state import Change, Observation, StateStore, week_key


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.sqlite")


def obs(kind, identity, scope="APPLICATIONS", **payload):
    return Observation(scope=scope, kind=kind, identity=identity, payload=payload)


class _FakeMemory:
    """Records proposals; fails loudly if anything tries to accept or write directly."""

    def __init__(self, ok=True):
        self.proposed: list = []
        self.ok = ok

    def propose(self, text, **kwargs):
        self.proposed.append((text, kwargs))
        return type("R", (), {"ok": self.ok})()

    def remember(self, *args, **kwargs):
        raise AssertionError("maintenance must never write memory directly")

    def accept(self, *args, **kwargs):
        raise AssertionError("maintenance must never accept a proposal")


class _FakeCatalog:
    def __init__(self, entries):
        self._entries = entries

    def entries(self):
        return self._entries


class _Entry:
    def __init__(self, app_id, name, kind="lnk", target=r"C:\secret\path\app.exe"):
        self.app_id, self.name, self.kind, self.target = app_id, name, kind, target


# --------------------------------------------------------------------------- scopes

def test_the_scope_set_is_closed():
    """There is no "everything" scope, and an unrecognised one is not collected."""
    assert "EVERYTHING" not in Scope.ALL
    assert "FILES" not in Scope.ALL
    assert Scope.ALL == {Scope.SYSTEM_BASIC, Scope.APPLICATIONS, Scope.DEVICES,
                         Scope.STORAGE_METADATA, Scope.NETWORK_METADATA, Scope.BROWSER_METADATA,
                         Scope.SERVICES, Scope.SECURITY_CONFIGURATION}


def test_services_is_available_but_not_run_unattended():
    """Defined so the capability exists; excluded from the default run because its weekly churn is
    mostly Windows updating itself."""
    assert Scope.SERVICES in Scope.ALL
    assert Scope.SERVICES not in DEFAULT_SCOPES


def test_an_invalid_scope_request_runs_nothing(store):
    result = Maintenance(store).run(scopes=("NOT_A_SCOPE",), force=True)
    assert result.ran is False and "no valid scopes" in result.reason
    assert store.snapshot_count() == 0


def test_only_requested_scopes_are_collected(store):
    maintenance = Maintenance(store, catalog=_FakeCatalog([_Entry("a1", "Notepad")]))
    result = maintenance.run(scopes=(Scope.APPLICATIONS,), force=True)
    assert result.scopes == (Scope.APPLICATIONS,)
    kinds = {o.kind for o in store.observations(result.snapshot_id)}
    assert kinds == {KIND_APPLICATION}


# --------------------------------------------------------------------------- privacy

def test_an_application_observation_records_no_path(store):
    """The launch target is an engine-owned path and is not needed to notice an application exists;
    a path in a plain database is a privacy leak for no benefit."""
    catalog = _FakeCatalog([_Entry("a1", "Notepad", target=r"C:\Users\someone\secret\np.exe")])
    result = Maintenance(store, catalog=catalog).run(scopes=(Scope.APPLICATIONS,), force=True)
    payload = store.observations(result.snapshot_id)[0].payload
    assert payload["name"] == "Notepad"
    for value in payload.values():
        assert "secret" not in str(value).lower()
    assert not any("target" in key or "path" in key for key in payload)


def test_the_security_scope_records_switches_and_root_counts_never_paths(store):
    class _Config:
        def get(self, key, default=None):
            if key == "security.allowed_roots":
                return [r"C:\Users\someone\Private", r"D:\Work"]
            if key.endswith(".enabled") or key.endswith("_analysis"):
                return True
            return default

    result = Maintenance(store, config=_Config()).run(
        scopes=(Scope.SECURITY_CONFIGURATION,), force=True)
    payloads = {o.identity: o.payload for o in store.observations(result.snapshot_id)}
    assert payloads["security.allowed_roots"] == {"count": 2}, "paths must not be recorded"
    assert payloads["browser.enabled"] == {"enabled": True}
    blob = str(payloads)
    assert "Private" not in blob and "C:" not in blob


def test_no_scope_collects_content_or_credentials():
    """The collectors are a closed set and none of them reads a file, a message or a secret."""
    import ast
    import inspect
    import void.maintenance as module
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in ("read_text", "read_bytes", "open(", "keyring", "get_password",
                      "cookies", "credential", "subprocess", "os.system"):
        assert forbidden not in code, f"a collector reaches for {forbidden!r}"


# --------------------------------------------------------------------------- snapshots

def test_a_run_writes_one_snapshot_with_its_observations(store):
    catalog = _FakeCatalog([_Entry("a1", "Notepad"), _Entry("a2", "Opera GX")])
    result = Maintenance(store, catalog=catalog).run(scopes=(Scope.APPLICATIONS,), force=True)
    assert result.ran and result.observations == 2
    assert store.snapshot_count() == 1
    assert len(store.observations(result.snapshot_id)) == 2


def test_a_dry_run_writes_nothing(store):
    catalog = _FakeCatalog([_Entry("a1", "Notepad")])
    result = Maintenance(store, catalog=catalog).run(
        scopes=(Scope.APPLICATIONS,), force=True, dry_run=True)
    assert result.ran and result.dry_run and result.observations == 1
    assert store.snapshot_count() == 0
    assert store.runs() == [], "a dry run must not consume the week"


def test_the_digest_ignores_fields_that_move_on_their_own():
    """Free space and uptime change every week; treating them as content would mean every snapshot
    looked different and "nothing happened" could never be established cheaply."""
    before = [obs(KIND_STORAGE, "c:", total_gb=475, free_gb=120)]
    after = [obs(KIND_STORAGE, "c:", total_gb=475, free_gb=61)]
    assert snapshot_digest(before) == snapshot_digest(after)
    bigger = [obs(KIND_STORAGE, "c:", total_gb=900, free_gb=120)]
    assert snapshot_digest(before) != snapshot_digest(bigger)


# --------------------------------------------------------------------------- diff

def test_the_first_snapshot_is_a_baseline_not_a_week_of_installations():
    """Otherwise the first run would report every application on the machine as newly installed."""
    assert diff([], [obs(KIND_APPLICATION, "a1", name="A")]) == []


def test_an_installed_application_is_detected():
    changes = diff([obs(KIND_APPLICATION, "a1", name="A")],
                   [obs(KIND_APPLICATION, "a1", name="A"), obs(KIND_APPLICATION, "a2", name="B")])
    assert [(c.kind, c.identity) for c in changes] == [(APPLICATION_INSTALLED, "a2")]


def test_a_removed_application_is_detected():
    changes = diff([obs(KIND_APPLICATION, "a1", name="A"), obs(KIND_APPLICATION, "a2", name="B")],
                   [obs(KIND_APPLICATION, "a1", name="A")])
    assert [(c.kind, c.identity) for c in changes] == [(APPLICATION_REMOVED, "a2")]


def test_a_version_change_is_detected_and_names_the_field():
    changes = diff([obs(KIND_APPLICATION, "a1", name="A", version="1.0")],
                   [obs(KIND_APPLICATION, "a1", name="A", version="2.0")])
    assert len(changes) == 1
    assert changes[0].kind == APPLICATION_CHANGED and "version" in changes[0].detail


def test_a_volatile_field_moving_is_not_a_change():
    """A week in which only free disk space moved must produce no changes at all - noise teaches the
    owner to ignore the report."""
    assert diff([obs(KIND_STORAGE, "c:", total_gb=475, free_gb=120)],
                [obs(KIND_STORAGE, "c:", total_gb=475, free_gb=61)]) == []


def test_an_unchanged_machine_produces_no_changes():
    same = [obs(KIND_APPLICATION, "a1", name="A"), obs(KIND_DEVICE, "audio:x", name="X")]
    assert diff(same, list(same)) == []


def test_devices_appearing_and_disappearing_are_detected():
    before = [obs(KIND_DEVICE, "audio:mic", scope="DEVICES", name="Mic")]
    after = [obs(KIND_DEVICE, "audio:cam", scope="DEVICES", name="Cam")]
    kinds = {(c.kind, c.identity) for c in diff(before, after)}
    assert (DEVICE_PRESENT, "audio:cam") in kinds
    assert (DEVICE_ABSENT, "audio:mic") in kinds


def test_a_browser_appearing_and_disappearing_is_detected():
    before = [obs(KIND_BROWSER, "opera gx", scope="BROWSER_METADATA", installed=True)]
    after = [obs(KIND_BROWSER, "edge", scope="BROWSER_METADATA", installed=True)]
    kinds = {(c.kind, c.identity) for c in diff(before, after)}
    assert (BROWSER_INSTALLED, "edge") in kinds and (BROWSER_REMOVED, "opera gx") in kinds


# --------------------------------------------------------------------------- the memory boundary

def test_an_application_observation_never_becomes_a_memory_candidate():
    """The headline rule. "Chrome is installed" is registry state, not something to remember."""
    changes = diff([obs(KIND_APPLICATION, "a1", name="A")],
                   [obs(KIND_APPLICATION, "a1", name="A"), obs(KIND_APPLICATION, "chrome",
                                                               name="Chrome")])
    assert promotion_candidates(changes) == []


def test_a_device_being_seen_never_becomes_a_memory_candidate():
    changes = diff([obs(KIND_DEVICE, "bt:a", scope="DEVICES", name="A")],
                   [obs(KIND_DEVICE, "bt:a", scope="DEVICES", name="A"),
                    obs(KIND_DEVICE, "bt:xyz", scope="DEVICES", name="XYZ")])
    assert promotion_candidates(changes) == []


def test_the_promotable_set_is_deliberately_tiny():
    assert PROMOTABLE == {BROWSER_INSTALLED, BROWSER_REMOVED}


def test_a_promotable_change_yields_a_candidate_sentence():
    candidates = promotion_candidates([Change(kind=BROWSER_REMOVED, identity="opera gx")])
    assert len(candidates) == 1 and "opera gx" in candidates[0]


def test_one_run_can_never_flood_the_review_queue():
    many = [Change(kind=BROWSER_INSTALLED, identity=f"b{index}") for index in range(100)]
    assert len(promotion_candidates(many)) == MAX_PROPOSALS


def test_maintenance_proposes_and_never_accepts(store):
    """It goes through the EXISTING proposal path, which still needs the owner. _FakeMemory raises if
    anything calls accept() or remember()."""
    memory = _FakeMemory()
    maintenance = Maintenance(store, memory=memory)
    proposed = maintenance._propose([Change(kind=BROWSER_REMOVED, identity="opera gx")])
    assert proposed and len(memory.proposed) == 1


def test_a_proposal_from_a_machine_scan_is_marked_untrusted(store):
    """It came from scanning a disk, not from the owner saying it, so it carries taint into the
    existing memory policy rather than arriving as though they had."""
    memory = _FakeMemory()
    Maintenance(store, memory=memory)._propose([Change(kind=BROWSER_REMOVED, identity="opera gx")])
    assert memory.proposed[0][1].get("tainted") is True


def test_a_rejected_proposal_is_not_counted_as_promoted(store):
    """The existing memory policy may refuse it - secret-shaped, too long, duplicate. That is its
    decision to make, and maintenance reports honestly."""
    memory = _FakeMemory(ok=False)
    proposed = Maintenance(store, memory=memory)._propose(
        [Change(kind=BROWSER_REMOVED, identity="opera gx")])
    assert proposed == []


def test_maintenance_works_with_memory_disabled(store):
    """A machine with memory off should still get snapshots and diffs."""
    assert Maintenance(store, memory=None)._propose(
        [Change(kind=BROWSER_REMOVED, identity="x")]) == []


def test_a_memory_service_that_raises_does_not_fail_the_run(store):
    class _Exploding:
        def propose(self, text, **kwargs):
            raise RuntimeError("memory is locked")

    assert Maintenance(store, memory=_Exploding())._propose(
        [Change(kind=BROWSER_REMOVED, identity="x")]) == []


# --------------------------------------------------------------------------- scheduling

def test_it_is_not_due_on_a_non_sunday(store):
    monday = time.mktime(time.strptime("2026-10-05 12:00", "%Y-%m-%d %H:%M"))
    due, reason = Maintenance(store, now=lambda: monday).due()
    assert due is False and "Sunday" in reason


def test_it_is_due_on_a_sunday_that_has_not_run(store):
    sunday = time.mktime(time.strptime("2026-10-04 12:00", "%Y-%m-%d %H:%M"))
    due, reason = Maintenance(store, now=lambda: sunday).due()
    assert due is True and "due for" in reason


def test_it_is_not_due_again_after_succeeding_this_week(store):
    sunday = time.mktime(time.strptime("2026-10-04 12:00", "%Y-%m-%d %H:%M"))
    maintenance = Maintenance(store, catalog=_FakeCatalog([]), now=lambda: sunday)
    assert maintenance.run(scopes=(Scope.APPLICATIONS,)).ran is True
    due, reason = maintenance.due()
    assert due is False and "already ran" in reason


def test_a_second_trigger_in_the_same_week_does_nothing(store):
    """Idempotency: the 15-minute watchdog style of trigger must be harmless."""
    sunday = time.mktime(time.strptime("2026-10-04 12:00", "%Y-%m-%d %H:%M"))
    maintenance = Maintenance(store, catalog=_FakeCatalog([_Entry("a1", "A")]), now=lambda: sunday)
    first = maintenance.run(scopes=(Scope.APPLICATIONS,))
    second = maintenance.run(scopes=(Scope.APPLICATIONS,))
    assert first.ran and not second.ran
    assert store.snapshot_count() == 1


def test_forcing_still_cannot_run_twice_in_a_week(store):
    maintenance = Maintenance(store, catalog=_FakeCatalog([]))
    assert maintenance.run(scopes=(Scope.APPLICATIONS,), force=True).ran is True
    assert maintenance.run(scopes=(Scope.APPLICATIONS,), force=True).ran is False
    assert store.snapshot_count() == 1


def test_run_if_due_is_safe_to_call_repeatedly(store):
    monday = time.mktime(time.strptime("2026-10-05 12:00", "%Y-%m-%d %H:%M"))
    maintenance = Maintenance(store, now=lambda: monday)
    for _ in range(5):
        assert maintenance.run_if_due().ran is False
    assert store.snapshot_count() == 0


# --------------------------------------------------------------------------- failure and recovery

def test_a_scope_that_cannot_be_read_does_not_end_the_run(store, monkeypatch):
    import void.maintenance as module
    real = module.collect

    def partly_broken(scope, **kwargs):
        if scope == Scope.DEVICES:
            raise OSError("device enumeration failed")
        return real(scope, **kwargs)

    monkeypatch.setattr(module, "collect", partly_broken)
    maintenance = Maintenance(store, catalog=_FakeCatalog([_Entry("a1", "A")]))
    result = maintenance.run(scopes=(Scope.APPLICATIONS, Scope.DEVICES), force=True)
    assert result.ran is True
    assert any("DEVICES" in error for error in result.errors)
    assert result.observations == 1, "the readable scope still produced observations"
    assert store.run_for_week(week_key()).succeeded


def test_a_run_that_fails_outright_is_recorded_as_failed(store, monkeypatch):
    import void.maintenance as module
    monkeypatch.setattr(module, "snapshot_digest",
                        lambda observations: (_ for _ in ()).throw(RuntimeError("boom")))
    result = Maintenance(store, catalog=_FakeCatalog([])).run(
        scopes=(Scope.APPLICATIONS,), force=True)
    assert "failed" in result.reason
    recorded = store.run_for_week(week_key())
    assert recorded.status == "failed" and recorded.error_class == "RuntimeError"


def test_a_failed_week_can_be_retried(store, monkeypatch):
    """Otherwise one bad Sunday would make that Sunday permanently unrunnable."""
    import void.maintenance as module
    monkeypatch.setattr(module, "snapshot_digest",
                        lambda observations: (_ for _ in ()).throw(RuntimeError("boom")))
    maintenance = Maintenance(store, catalog=_FakeCatalog([_Entry("a1", "A")]))
    assert "failed" in maintenance.run(scopes=(Scope.APPLICATIONS,), force=True).reason
    monkeypatch.undo()
    retried = maintenance.run(scopes=(Scope.APPLICATIONS,), force=True)
    assert retried.ran and retried.reason == "completed"
    assert store.run_for_week(week_key()).succeeded


def test_a_failed_run_leaves_no_partial_snapshot(store, monkeypatch):
    import void.maintenance as module
    monkeypatch.setattr(module, "snapshot_digest",
                        lambda observations: (_ for _ in ()).throw(RuntimeError("boom")))
    Maintenance(store, catalog=_FakeCatalog([_Entry("a1", "A")])).run(
        scopes=(Scope.APPLICATIONS,), force=True)
    assert store.snapshot_count() == 0


def test_the_result_describes_itself_honestly(store):
    catalog = _FakeCatalog([_Entry("a1", "A")])
    result = Maintenance(store, catalog=catalog).run(scopes=(Scope.APPLICATIONS,), force=True)
    assert "1 observation" in result.describe()
    blocked = Maintenance(store).run(scopes=("NOPE",), force=True)
    assert "did not run" in blocked.describe()


# --------------------------------------------------------------------------- registries

def test_an_application_observation_carries_what_routing_needs(store):
    """Installed state and how it launches; enough for the registry, nothing more."""
    catalog = _FakeCatalog([_Entry("a1", "Opera GX", kind="exe")])
    result = Maintenance(store, catalog=catalog).run(scopes=(Scope.APPLICATIONS,), force=True)
    payload = store.observations(result.snapshot_id)[0].payload
    assert payload["installed"] is True and payload["launch_kind"] == "exe"
    assert payload["name"] == "Opera GX"


def test_a_missing_catalog_yields_no_applications_rather_than_failing(store):
    result = Maintenance(store, catalog=None).run(scopes=(Scope.APPLICATIONS,), force=True)
    assert result.ran and result.observations == 0


def test_maintenance_never_grants_device_trust():
    """Presence is an observation. Trust belongs to void.device.identity and is not touched here."""
    import ast
    import inspect
    import void.maintenance as module
    code = ast.unparse(ast.parse(inspect.getsource(module)))
    for forbidden in ("grant", "pair(", "trust(", "set_trust", "authorize"):
        assert forbidden not in code, f"maintenance references {forbidden!r}"


def test_a_device_observation_records_presence_not_trust(store, monkeypatch):
    import void.maintenance as module

    def fake_devices(scope, **kwargs):
        if scope != Scope.DEVICES:
            return []
        return [obs(KIND_DEVICE, "bt:mouse", scope="DEVICES", name="Mouse", present=True)]

    monkeypatch.setattr(module, "collect", fake_devices)
    result = Maintenance(store).run(scopes=(Scope.DEVICES,), force=True)
    payload = store.observations(result.snapshot_id)[0].payload
    assert payload["present"] is True
    assert "trusted" not in payload and "authorized" not in payload
