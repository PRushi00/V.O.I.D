"""``void maintenance`` - run the weekly pass, or ask what it found.

The scheduling story this command completes: the runner is due-driven and claims its week in the
database, so whatever fires it cannot cause a second pass. That means any scheduler works - Windows
Task Scheduler, the owner typing the command, or a future runtime tick - and none of them needs to
know what day it is. ``run`` is the safe default (does nothing unless due); ``--force`` is for trying
it now, and still cannot run twice in a week; ``--dry-run`` collects and diffs without writing.

Deliberately NOT a scheduler of its own. V.O.I.D already registers Windows tasks in
:mod:`void.runtime.scheduled_task`; this adds no second mechanism, and because the runner self-gates,
pointing anything at ``void maintenance run`` is enough.
"""
from __future__ import annotations

import time

from void.maintenance import DEFAULT_SCOPES, Maintenance, Scope
from void.state import StateStore


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "maintenance", help="Weekly look at this computer (applications, devices, system state)")
    actions = parser.add_subparsers(dest="maintenance_action")
    p_run = actions.add_parser("run", help="Run the weekly pass if it is due")
    p_run.add_argument("--force", action="store_true",
                       help="Run even if it is not Sunday (still at most once per week)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Collect and compare without writing anything")
    p_run.add_argument("--scopes", default="",
                       help=f"comma-separated subset of: {', '.join(sorted(Scope.ALL))}")
    actions.add_parser("status", help="When it last ran, what changed, and whether it is due")
    p_changes = actions.add_parser("changes", help="Changes detected by recent runs")
    p_changes.add_argument("--limit", type=int, default=40)


def _build(config):
    store = StateStore(config.state_dir() / "state.sqlite")
    catalog = None
    try:
        from void.actions.computer import AppCatalog, make_backend
        catalog = AppCatalog(make_backend())
    except Exception:                                          # noqa: BLE001 - APPLICATIONS scope then yields nothing
        catalog = None
    memory = None
    try:
        if config.get("memory.enabled", True):
            from void.memory.service import MemoryService
            memory = MemoryService.from_config(config)
    except Exception:                                          # noqa: BLE001 - proposals are optional
        memory = None
    return store, Maintenance(store, catalog=catalog, config=config, memory=memory)


def run(args, config) -> int:
    action = getattr(args, "maintenance_action", None) or "status"
    store, maintenance = _build(config)

    if action == "run":
        scopes = None
        raw = (getattr(args, "scopes", "") or "").strip()
        if raw:
            wanted = [part.strip().upper() for part in raw.split(",") if part.strip()]
            unknown = [name for name in wanted if name not in Scope.ALL]
            if unknown:
                print(f"Unknown scope(s): {', '.join(unknown)}.")
                print(f"Known: {', '.join(sorted(Scope.ALL))}.")
                return 1
            scopes = tuple(wanted)
        result = maintenance.run(scopes=scopes, force=bool(getattr(args, "force", False)),
                                 dry_run=bool(getattr(args, "dry_run", False)))
        print(result.describe())
        if result.errors:
            # Scope NAMES and exception CLASSES only - never what could not be read.
            print(f"  could not read: {', '.join(result.errors)}")
        for change in result.changes[:20]:
            print(f"  {change.kind:24} {change.identity[:48]}  {change.detail}")
        if len(result.changes) > 20:
            print(f"  ... and {len(result.changes) - 20} more")
        for text in result.proposals:
            print(f"  proposed for memory (awaiting your review): {text}")
        if result.proposals:
            print("  Review with:  python -m void memory review")
        return 0 if result.ran or not result.errors else 1

    if action == "status":
        due, reason = maintenance.due()
        print(f"Due now: {'yes' if due else 'no'} ({reason}).")
        runs = store.runs(limit=5)
        if not runs:
            print("It has never run. Try:  python -m void maintenance run --force")
            return 0
        print(f"Snapshots kept: {store.snapshot_count()}.")
        print("Recent runs:")
        for entry in runs:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.started_at))
            note = f" ({entry.error_class})" if entry.error_class else ""
            print(f"  {when}  {entry.week_key}  {entry.status:8} "
                  f"{entry.changes} change(s){note}")
        return 0

    if action == "changes":
        changes = store.changes(limit=max(1, int(getattr(args, "limit", 40))))
        if not changes:
            print("No changes recorded yet.")
            return 0
        for change in changes:
            when = time.strftime("%Y-%m-%d", time.localtime(change.at))
            print(f"  {when}  {change.kind:24} {change.identity[:44]}  {change.detail}")
        return 0

    print("Usage: python -m void maintenance [run|status|changes]")
    return 1
