"""Command-line interface for V.O.I.D V1.

Examples:
    python -m void "find my cybersecurity notes"
    python -m void run "open my resume and tell me its length"
    python -m void set-key gemini
    python -m void tasks
    python -m void resume 1a2b3c4d5e6f
    python -m void stop            # engage emergency stop (any terminal)
    python -m void clear-stop
    python -m void ui              # launch the circular widget
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys

from void.app import Assistant
from void.core.task import Status
from void.providers.base import ProviderUnavailable
from void.security import credentials, secrets


def _confirm(description: str) -> bool:
    """Owner confirmation prompt for high-risk actions."""
    try:
        answer = input(f"\n[V.O.I.D needs authorization] {description}\n"
                       f"Allow this action? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _event(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _print_result(result) -> None:
    print("\n" + "=" * 60)
    print(f"Task {result.task.id}: {result.status.upper()}  "
          f"({result.steps} step(s))")
    if result.result:
        print("-" * 60)
        print(result.result)
    if result.task.error:
        print("-" * 60)
        print(f"Note: {result.task.error}")
    print("=" * 60)


# --- commands ----------------------------------------------------------

def cmd_run(goal: str) -> int:
    assistant = Assistant(confirm_fn=_confirm, on_event=_event)
    # A stop can be engaged from another terminal: python -m void stop
    if assistant.kill_switch.engaged:
        print("A stop is currently engaged. Run 'python -m void clear-stop' "
              "first.")
        return 1
    print(f"V.O.I.D working on: {goal}\n"
          f"(To stop: run 'python -m void stop' in another terminal.)")
    try:
        result = assistant.run(goal)
    except ProviderUnavailable as exc:
        print(f"\nNo AI brain available: {exc}")
        return 1
    _print_result(result)
    return 0 if result.status == Status.COMPLETED else 2


def cmd_resume(task_id: str) -> int:
    assistant = Assistant(confirm_fn=_confirm, on_event=_event)
    assistant.clear_stop()  # resuming implies the owner has cleared the stop
    try:
        result = assistant.resume(task_id)
    except ProviderUnavailable as exc:
        print(f"\nNo AI brain available: {exc}")
        return 1
    _print_result(result)
    return 0 if result.status == Status.COMPLETED else 2


# --- Gemini credential management --------------------------------------
#
# The primary credential lives under secrets.GEMINI_API_KEY ("gemini_api_key").
# Additional named credentials are stored under their own alias and tracked in
# a names-only manifest (credentials.MANIFEST_KEY). No key VALUE is ever printed,
# logged, or written to the manifest.

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _reserved_names() -> set[str]:
    """Names that must never be used as an additional-credential alias."""
    return {secrets.GEMINI_API_KEY, secrets.STOP_PIN, credentials.MANIFEST_KEY}


def _validate_alias(alias: str) -> str | None:
    """Return an error string for an invalid alias, or None if it is valid."""
    if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
        return "use letters, digits, '_' or '-' only (no spaces, not empty)"
    if alias in _reserved_names():
        return f"'{alias}' is a reserved name"
    return None


def _load_manifest_names() -> list | None:
    """Raw manifest list, or None if absent/invalid/unreadable (names only)."""
    try:
        raw = secrets.get_secret(credentials.MANIFEST_KEY)
    except secrets.SecretStoreError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _ordered_unique(names) -> list[str]:
    """Primary first, then the rest; deduped; only non-empty string names."""
    ordered = [secrets.GEMINI_API_KEY] + [n for n in names
                                          if n != secrets.GEMINI_API_KEY]
    seen: set[str] = set()
    out: list[str] = []
    for n in ordered:
        if isinstance(n, str) and n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _add_to_manifest(alias: str) -> None:
    """Add an alias to the names-only manifest (idempotent, primary first)."""
    base = _load_manifest_names()
    ordered = _ordered_unique(base if base is not None else [secrets.GEMINI_API_KEY])
    if alias not in ordered:
        ordered.append(alias)
    secrets.set_secret(credentials.MANIFEST_KEY, json.dumps(ordered))


def cmd_set_key(name: str, alias: str | None = None) -> int:
    keymap = {"gemini": secrets.GEMINI_API_KEY}
    if name.lower() not in keymap:
        print(f"Unknown key '{name}'. Known: {', '.join(keymap)}")
        return 1

    if alias is None:
        # Primary credential - unchanged behavior.
        key_id = keymap[name.lower()]
        label = name
    else:
        err = _validate_alias(alias)
        if err:
            print(f"Invalid credential name '{alias}': {err}. To set the "
                  f"primary key, use 'set-key gemini' without --name.")
            return 1
        key_id = alias
        label = alias

    value = getpass.getpass(f"Paste the {label} API key (hidden): ").strip()
    if not value:
        print("No key entered; nothing stored.")
        return 1
    try:
        secrets.set_secret(key_id, value)
        if alias is not None:
            _add_to_manifest(alias)
    except secrets.SecretStoreError:
        # Never surface raw backend text (it could echo the attempted value).
        print("Could not store the credential in the OS credential store.")
        return 1

    if alias is None:
        print(f"Stored {name} key securely in the OS credential store.")
    else:
        print(f"Stored additional Gemini credential '{alias}' and updated the "
              f"credential list.")
    return 0


def cmd_list_keys() -> int:
    # Names only - the pool reads just the manifest, never a secret value.
    pool = credentials.CredentialPool()
    names = pool.names()
    print("Gemini credentials (names only; key values are never displayed):")
    for n in names:
        tag = " (primary)" if n == secrets.GEMINI_API_KEY else ""
        print(f"  - {n}{tag}")
    return 0


def cmd_remove_key(alias: str) -> int:
    if alias == secrets.GEMINI_API_KEY:
        print(f"Refusing to remove the primary credential "
              f"'{secrets.GEMINI_API_KEY}' via remove-key.")
        return 1
    base = _load_manifest_names()
    additional = [n for n in _ordered_unique(base or [secrets.GEMINI_API_KEY])
                  if n != secrets.GEMINI_API_KEY]
    if alias not in additional:
        print(f"No additional Gemini credential named '{alias}'.")
        return 1
    try:
        secrets.delete_secret(alias)
        remaining = [n for n in additional if n != alias]
        if remaining:
            secrets.set_secret(credentials.MANIFEST_KEY,
                               json.dumps([secrets.GEMINI_API_KEY] + remaining))
        else:
            # Back to the pristine single-key state (no manifest).
            secrets.delete_secret(credentials.MANIFEST_KEY)
    except secrets.SecretStoreError:
        print("Could not update the OS credential store.")
        return 1
    print(f"Removed Gemini credential '{alias}'.")
    return 0


def cmd_set_pin() -> int:
    pin = getpass.getpass("Set emergency-stop PIN (hidden): ").strip()
    if not pin:
        print("No PIN entered; nothing stored.")
        return 1
    secrets.set_secret(secrets.STOP_PIN, pin)
    print("Stored stop PIN. Set kill_switch.require_pin: true in config to "
          "enforce it.")
    return 0


def cmd_tasks() -> int:
    assistant = Assistant()
    tasks = assistant.store.list()
    if not tasks:
        print("No tasks yet.")
        return 0
    print(f"{'ID':<14}{'STATUS':<12}{'STEPS':<7}GOAL")
    for t in tasks:
        print(f"{t.id:<14}{t.status:<12}{t.steps:<7}{t.goal[:50]}")
    return 0


def cmd_stop(pin: str | None) -> int:
    assistant = Assistant()
    ok = assistant.stop(reason="CLI stop command", pin=pin)
    if ok:
        print("EMERGENCY STOP engaged. Running tasks will halt at the next "
              "checkpoint. Run 'clear-stop' to resume normal operation.")
        return 0
    print("Stop NOT engaged: PIN authentication failed.")
    return 1


def cmd_clear_stop() -> int:
    assistant = Assistant()
    assistant.clear_stop()
    print("Stop cleared. V.O.I.D can run tasks again.")
    return 0


def cmd_ui() -> int:
    try:
        from void.ui.widget import launch
    except Exception as exc:
        print(f"Could not load the UI: {exc}\n"
              f"Make sure PySide6 is installed: pip install PySide6")
        return 1
    return launch()


# --- argument parsing --------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="void", description="V.O.I.D V1")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run a natural-language goal")
    p_run.add_argument("goal", nargs="+")

    p_resume = sub.add_parser("resume", help="Resume a paused/interrupted task")
    p_resume.add_argument("task_id")

    p_key = sub.add_parser("set-key", help="Store an API key securely")
    p_key.add_argument("name", choices=["gemini"])
    p_key.add_argument("--name", dest="alias", default=None,
                       help="Alias for an ADDITIONAL Gemini credential "
                            "(omit to set the primary key).")

    sub.add_parser("list-keys", help="List Gemini credential names (no values)")

    p_rm = sub.add_parser("remove-key",
                          help="Remove an additional Gemini credential")
    p_rm.add_argument("alias", help="Alias of the additional credential.")

    sub.add_parser("set-pin", help="Set the emergency-stop PIN")
    sub.add_parser("tasks", help="List tasks")

    p_stop = sub.add_parser("stop", help="Engage the emergency stop")
    p_stop.add_argument("--pin", default=None)

    sub.add_parser("clear-stop", help="Clear an engaged stop")
    sub.add_parser("ui", help="Launch the circular widget")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Convenience: `python -m void "goal text"` with no subcommand -> run.
    known = {"run", "resume", "set-key", "list-keys", "remove-key", "set-pin",
             "tasks", "stop", "clear-stop", "ui", "-h", "--help"}
    if argv and argv[0] not in known:
        return cmd_run(" ".join(argv))

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(" ".join(args.goal))
    if args.command == "resume":
        return cmd_resume(args.task_id)
    if args.command == "set-key":
        return cmd_set_key(args.name, alias=args.alias)
    if args.command == "list-keys":
        return cmd_list_keys()
    if args.command == "remove-key":
        return cmd_remove_key(args.alias)
    if args.command == "set-pin":
        return cmd_set_pin()
    if args.command == "tasks":
        return cmd_tasks()
    if args.command == "stop":
        return cmd_stop(args.pin)
    if args.command == "clear-stop":
        return cmd_clear_stop()
    if args.command == "ui":
        return cmd_ui()

    parser.print_help()
    return 0
