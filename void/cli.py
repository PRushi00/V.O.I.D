"""Command-line interface for V.O.I.D V1.

Examples:
    python -m void "find my cybersecurity notes"
    python -m void run "open my resume and tell me its length"
    python -m void set-key gemini
    python -m void tasks
    python -m void resume 1a2b3c4d5e6f
    python -m void approve 1a2b3c4d5e6f   # authorize a pending HIGH-risk step
    python -m void deny 1a2b3c4d5e6f      # refuse a pending HIGH-risk step
    python -m void stop            # engage emergency stop (any terminal)
    python -m void clear-stop
    python -m void ui              # launch the circular widget
    python -m void voice          # launch the push-to-talk voice interface
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import re
import sys
import time

from void.app import Assistant
from void.core.task import Status
from void.providers.base import ProviderUnavailable
from void.security import credentials, secrets

_log = logging.getLogger("void.cli")


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
    pending = result.task.pending or {}
    if (result.status == Status.BLOCKED
            and pending.get("kind") == "directory_disambiguation"):
        print("-" * 60)
        print(pending.get("prompt", ""))
        print(f"\nTo choose: python -m void clarify {result.task.id} <number>")
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


def cmd_clarify(task_id: str, selection: str) -> int:
    """Resolve a BLOCKED directory-disambiguation by number; continue the task."""
    assistant = Assistant(confirm_fn=_confirm, on_event=_event)
    try:
        result = assistant.clarify(task_id, selection)
    except ValueError as exc:
        print(str(exc))
        return 1
    except ProviderUnavailable as exc:
        print(f"\nNo AI brain available: {exc}")
        return 1
    _print_result(result)
    return 0 if result.status == Status.COMPLETED else 2


def cmd_approve(task_id: str) -> int:
    """Owner approves a task's pending HIGH-risk step; it executes exactly once."""
    assistant = Assistant(confirm_fn=_confirm, on_event=_event)
    try:
        result = assistant.approve(task_id)
    except ValueError as exc:
        print(str(exc))
        return 1
    except ProviderUnavailable as exc:
        print(f"\nNo AI brain available: {exc}")
        return 1
    _print_result(result)
    return 0 if result.status == Status.COMPLETED else 2


def cmd_deny(task_id: str) -> int:
    """Owner denies a task's pending HIGH-risk step; it will not execute."""
    assistant = Assistant(confirm_fn=_confirm, on_event=_event)
    try:
        result = assistant.deny(task_id)
    except ValueError as exc:
        print(str(exc))
        return 1
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


def cmd_roots(action: str, path: str | None) -> int:
    """Owner-only management of trusted filesystem roots.

    This is an authorization boundary: it is a CLI command, never an agent
    tool. Changes take effect on the next V.O.I.D run (config is loaded at
    startup).
    """
    from void import roots as roots_mod

    if action == "list":
        print("Allowed filesystem roots:")
        for r in roots_mod.list_roots():
            print(f"  - {r}")
        return 0
    if not path:
        print(f"'roots {action}' requires an exact directory path.")
        return 1
    try:
        if action == "add":
            added = roots_mod.add_root(path)
            print(f"Authorized new allowed root: {added}")
        else:  # remove
            removed = roots_mod.remove_root(path)
            print(f"Removed allowed root: {removed}")
    except roots_mod.RootError as exc:
        print(f"Could not {action} root: {exc}")
        return 1
    return 0


def cmd_protect(action: str, path: str | None) -> int:
    """Owner-only management of protected (excluded) filesystem roots.

    Protected roots override allowed roots (deny-always). Like `roots`, this is
    a CLI command, never an agent tool. Changes take effect on the next run.
    """
    from void import roots as roots_mod

    if action == "list":
        print("Protected (excluded) filesystem roots:")
        for r in roots_mod.list_protected():
            print(f"  - {r}")
        return 0
    if not path:
        print(f"'protect {action}' requires an exact directory path.")
        return 1
    try:
        if action == "add":
            added = roots_mod.add_protected(path)
            print(f"Protected (excluded) directory: {added}")
        else:  # remove
            removed = roots_mod.remove_protected(path)
            print(f"Removed protected directory: {removed}")
    except roots_mod.RootError as exc:
        print(f"Could not {action} protected root: {exc}")
        return 1
    return 0


def cmd_ui() -> int:
    try:
        from void.ui.widget import launch
    except Exception as exc:
        print(f"Could not load the UI: {exc}\n"
              f"Make sure PySide6 is installed: pip install PySide6")
        return 1
    return launch()


def cmd_app() -> int:
    """Launch the persistent desktop application: one Assistant, one
    VoiceController (the sole microphone owner), and the orb widget, all
    composed by void.runtime.app.VoidRuntime. KillSwitch engagement halts
    execution through the existing mechanisms but does not exit this
    process; closing the window does."""
    try:
        from void.runtime.app import main as run_app
    except Exception as exc:
        print(f"Could not load the persistent app: {exc}\n"
              f"Make sure PySide6 is installed: pip install PySide6")
        return 1
    return run_app()


def cmd_singularity() -> int:
    """Launch the persistent desktop app with the WebGL Blackhole overlay
    (void-singularity-renderer in a transparent, click-through QWebEngineView)
    instead of the default QPainter orb. Same runtime, same single Assistant /
    VoiceController / mic owner - only the desktop presence surface differs.
    Opt-in so the WebGL path can be validated on the real display before it
    ever becomes the default; the QPainter orb (`python -m void app`) remains
    the rollback."""
    try:
        from void.ui.singularity_overlay import launch
    except Exception as exc:
        print(f"Could not load the WebGL overlay: {exc}\n"
              f"Make sure PySide6 with QtWebEngine is installed: pip install PySide6")
        return 1
    return launch()


def cmd_voice() -> int:
    """Launch the headless, voice-first runtime.

    Voice is a thin I/O adapter over the SAME Assistant.run() path the text CLI
    uses: transcripts are ordinary untrusted input, and RiskGate / KillSwitch /
    the durable confirmation mechanism stay authoritative. HIGH-risk actions are
    NOT approved by voice - they defer to AWAITING_CONFIRMATION (no confirm_fn),
    handled out-of-band; a spoken 'yes' is just another goal, never an approval.
    """
    _log.info("VOICE_RUNTIME_STARTING")
    assistant = Assistant(on_event=_event)  # no confirm_fn -> HIGH-risk deferred
    if not assistant.config.get("voice.enabled", False):
        print("Voice is disabled. Set 'voice.enabled: true' in "
              "config/local_config.yaml, then install the optional voice stack:\n"
              "  pip install -r requirements-voice.txt")
        return 1
    if assistant.kill_switch.engaged:
        print("A stop is currently engaged. Run 'python -m void clear-stop' "
              "first.")
        return 1

    from void.voice.adapters import VoiceDependencyError
    from void.voice.runtime import VoiceController

    def on_state(state):
        _log.info("VOICE_STATE %s", state)
        print(f"  [voice] {state}", flush=True)

    controller = VoiceController.from_assistant(
        assistant,
        on_state=on_state,
        on_transcript=lambda t: print(f"  [heard] {t}", flush=True),
        on_message=lambda m: print(f"  {m}", flush=True),
    )
    hotkey = assistant.config.get("voice.ptt_hotkey", "ctrl+space")
    try:
        controller.start()
    except VoiceDependencyError as exc:
        _log.exception("VOICE_RUNTIME_START_FAILED dependency")
        print(f"Voice dependencies missing: {exc}\n"
              f"  pip install -r requirements-voice.txt")
        return 1
    except Exception:
        _log.exception("VOICE_RUNTIME_START_FAILED")
        print("Voice runtime could not start. Check ~/.void/void.log for details.")
        return 1

    wake_on = getattr(controller, "_wake", None) is not None
    _log.info("VOICE_RUNTIME_READY wake_configured=%s", wake_on)
    wake_line = ('Say "Hey V.O.I.D." to start, or hold'
                 if wake_on else "Hold")
    print(f"V.O.I.D voice ready. {wake_line} '{hotkey}' to talk, release to send.\n"
          f"The Whisper model downloads on first use. Ctrl+C to exit.\n"
          f"(To stop everything: run 'python -m void stop' in another terminal.)")
    try:
        while not controller.stopped:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nExiting voice.")
    finally:
        controller.shutdown("voice CLI exit")
        _log.info("VOICE_RUNTIME_STOPPED")
    if controller.stopped:
        print("Voice stopped (kill switch). Run 'clear-stop' and restart voice "
              "to talk again.")
    return 0


def cmd_autostart(action: str) -> int:
    """Manage silent login autostart of the voice runtime (HKCU Run key,
    no admin/service/security change). 'install' makes V.O.I.D start at login
    with no terminal; 'remove' undoes it; 'status' shows the current entry."""
    from void.runtime import autostart

    if action == "install":
        pythonw = autostart.default_pythonw_path()
        cmd = autostart.install()
        print(f"Autostart installed. V.O.I.D will launch at login:\n  {cmd}")
        if "pythonw.exe" not in pythonw.lower():
            print("  NOTE: pythonw.exe was not found next to this interpreter, "
                  "so a console window may briefly appear at login.")
        print('  Say "Hey V.O.I.D." after the next login - no terminal needed.')
        return 0
    if action == "remove":
        removed = autostart.remove()
        print("Autostart removed." if removed else "Autostart was not installed.")
        return 0
    current = autostart.status()
    print(f"Autostart: INSTALLED\n  {current}" if current
          else "Autostart: not installed. Run 'python -m void autostart install'.")
    return 0


# --- argument parsing --------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="void", description="V.O.I.D V1")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run a natural-language goal")
    p_run.add_argument("goal", nargs="+")

    p_resume = sub.add_parser("resume", help="Resume a paused/interrupted task")
    p_resume.add_argument("task_id")

    p_clarify = sub.add_parser(
        "clarify",
        help="Answer a blocked directory-choice prompt by number, then continue")
    p_clarify.add_argument("task_id")
    p_clarify.add_argument("selection",
                           help="The candidate number to use (e.g. 2).")

    p_approve = sub.add_parser(
        "approve", help="Approve a task awaiting HIGH-risk confirmation")
    p_approve.add_argument("task_id")

    p_deny = sub.add_parser(
        "deny", help="Deny a task awaiting HIGH-risk confirmation")
    p_deny.add_argument("task_id")

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
    sub.add_parser("voice", help="Launch the headless voice-first runtime")
    sub.add_parser(
        "app", help="Launch the persistent desktop application (orb + voice)")
    sub.add_parser(
        "singularity",
        help="Launch the persistent app with the WebGL Blackhole overlay (opt-in)")
    p_autostart = sub.add_parser(
        "autostart", help="Manage silent login autostart of the voice runtime")
    p_autostart.add_argument("action", choices=["install", "remove", "status"])

    p_roots = sub.add_parser(
        "roots", help="Manage trusted filesystem roots (owner-only)")
    p_roots.add_argument("action", choices=["list", "add", "remove"])
    p_roots.add_argument("path", nargs="?", default=None,
                         help="Exact directory path (for add/remove).")

    p_protect = sub.add_parser(
        "protect",
        help="Manage protected (excluded) filesystem roots (owner-only)")
    p_protect.add_argument("action", choices=["list", "add", "remove"])
    p_protect.add_argument("path", nargs="?", default=None,
                           help="Exact directory path (for add/remove).")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Convenience: `python -m void "goal text"` with no subcommand -> run.
    known = {"run", "resume", "clarify", "approve", "deny", "set-key",
             "list-keys", "remove-key", "set-pin", "tasks", "stop",
             "clear-stop", "ui", "voice", "app", "singularity", "autostart",
             "roots", "protect", "-h", "--help"}
    if argv and argv[0] not in known:
        return cmd_run(" ".join(argv))

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(" ".join(args.goal))
    if args.command == "resume":
        return cmd_resume(args.task_id)
    if args.command == "clarify":
        return cmd_clarify(args.task_id, args.selection)
    if args.command == "approve":
        return cmd_approve(args.task_id)
    if args.command == "deny":
        return cmd_deny(args.task_id)
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
    if args.command == "voice":
        return cmd_voice()
    if args.command == "app":
        return cmd_app()
    if args.command == "singularity":
        return cmd_singularity()
    if args.command == "autostart":
        return cmd_autostart(args.action)
    if args.command == "roots":
        return cmd_roots(args.action, args.path)
    if args.command == "protect":
        return cmd_protect(args.action, args.path)

    parser.print_help()
    return 0
