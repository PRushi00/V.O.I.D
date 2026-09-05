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
import sys

from void.app import Assistant
from void.core.task import Status
from void.providers.base import ProviderUnavailable
from void.security import secrets


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


def cmd_set_key(name: str) -> int:
    keymap = {"gemini": secrets.GEMINI_API_KEY}
    key_id = keymap.get(name.lower())
    if not key_id:
        print(f"Unknown key '{name}'. Known: {', '.join(keymap)}")
        return 1
    value = getpass.getpass(f"Paste the {name} API key (hidden): ").strip()
    if not value:
        print("No key entered; nothing stored.")
        return 1
    secrets.set_secret(key_id, value)
    print(f"Stored {name} key securely in the OS credential store.")
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
    known = {"run", "resume", "set-key", "set-pin", "tasks", "stop",
             "clear-stop", "ui", "-h", "--help"}
    if argv and argv[0] not in known:
        return cmd_run(" ".join(argv))

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(" ".join(args.goal))
    if args.command == "resume":
        return cmd_resume(args.task_id)
    if args.command == "set-key":
        return cmd_set_key(args.name)
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
