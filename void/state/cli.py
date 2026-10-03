"""``void prefs`` - inspect and change the owner's routing preferences.

A preference is a default, not a permission. Everything here writes to the structured state store and
influences which route the resolver prefers; none of it grants a capability, relaxes a confirmation, or
touches the kill switch. The command surface is deliberately small - list, set, unset, history - because
a preference with a complicated lifecycle is one nobody can predict.

Follows the same shape as :mod:`void.memory.cli`: ``add_parser(sub)`` contributes the subcommands and
``run(args, config)`` executes them, so ``void/cli.py`` gains two lines rather than a new subsystem.
"""
from __future__ import annotations

import time

from void.orchestration.apps import PREFERENCE_KEYS
from void.state import StateError, StateStore


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "prefs", help="Routing preferences (which browser/editor/... to prefer)")
    actions = parser.add_subparsers(dest="prefs_action")
    actions.add_parser("list", help="Show active preferences and where each came from")
    p_set = actions.add_parser("set", help="Set a preference (yours, explicit)")
    p_set.add_argument("key", help=f"one of: {', '.join(sorted(PREFERENCE_KEYS))}")
    p_set.add_argument("value", help="application name, e.g. 'opera gx'")
    p_unset = actions.add_parser("unset", help="Deactivate a preference (history is kept)")
    p_unset.add_argument("key")
    p_history = actions.add_parser("history", help="How a preference changed over time")
    p_history.add_argument("key", nargs="?")


def _store(config) -> StateStore:
    return StateStore(config.state_dir() / "state.sqlite")


def run(args, config) -> int:
    action = getattr(args, "prefs_action", None) or "list"
    store = _store(config)

    if action == "list":
        active = store.preferences()
        shipped = {}
        try:
            shipped = {key: value for key, value in (config.get("preferences", {}) or {}).items()
                       if key in PREFERENCE_KEYS}
        except Exception:                                      # noqa: BLE001
            shipped = {}
        if not active and not shipped:
            print("No preferences set.")
            print(f"Set one with:  python -m void prefs set browser \"opera gx\"")
            return 0
        print("Preferences (an explicit instruction in the moment still overrides these):")
        for pref in active:
            age = time.strftime("%Y-%m-%d", time.localtime(pref.updated_at))
            how = "you set it" if pref.explicit else f"inferred ({pref.confidence:.0%})"
            print(f"  {pref.key:10} {pref.value:16} [{how}, {age}]")
        for key, value in sorted(shipped.items()):
            if not any(pref.key == key for pref in active):
                print(f"  {key:10} {str(value):16} [shipped default]")
        return 0

    if action == "set":
        key = (args.key or "").strip().lower()
        if key not in PREFERENCE_KEYS:
            print(f"'{args.key}' is not a preference V.O.I.D understands. "
                  f"Known: {', '.join(sorted(PREFERENCE_KEYS))}.")
            return 1
        try:
            pref = store.set_preference(key, args.value, source="owner", explicit=True)
        except StateError as bad:
            print(str(bad))
            return 1
        print(f"Preferred {pref.key}: {pref.value}.")
        return 0

    if action == "unset":
        key = (args.key or "").strip().lower()
        if store.deactivate_preference(key):
            print(f"No longer preferring anything in particular for {key}.")
            remaining = None
            try:
                remaining = (config.get("preferences", {}) or {}).get(key)
            except Exception:                                  # noqa: BLE001
                remaining = None
            if remaining:
                print(f"The shipped default applies again: {remaining}.")
            return 0
        print(f"No active preference for {key}.")
        return 0

    if action == "history":
        rows = store.preference_revisions(getattr(args, "key", None))
        if not rows:
            print("No preference history.")
            return 0
        for row in rows[:40]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["at"]))
            value = row["value"] or "-"
            print(f"  {when}  {row['key']:10} {row['action']:10} {value:16} ({row['source']})")
        return 0

    print("Usage: python -m void prefs [list|set|unset|history]")
    return 1
