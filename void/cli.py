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
import threading
import time
from pathlib import Path

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
    return {secrets.GEMINI_API_KEY, secrets.STOP_PIN, credentials.MANIFEST_KEY,
            secrets.MEMORY_KEY}


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


def cmd_tasks(dry_run: bool = False) -> int:
    assistant = Assistant()
    # Tasks whose process died are still `running` in the store; correct that first so
    # the listing is truthful. --dry-run only reports what would change.
    swept = assistant.store.sweep_stale(dry_run=dry_run)
    if swept:
        verb = "would be marked paused" if dry_run else "marked paused (process exited; resume to continue)"
        print(f"{len(swept)} stale task(s) {verb}: {', '.join(swept)}")
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
    # Diagnostics: without this, the mic/wakeword lifecycle logging below is a
    # no-op (no handler is attached anywhere in the logger hierarchy) when this
    # command is run directly from a terminal - the file handler makes a
    # manual run reboot-comparable via ~/.void/void.log; the console handler
    # additionally echoes it live, and safely no-ops under the console-less
    # pythonw autostart (see void.runtime.diagnostics).
    from void.runtime.diagnostics import (
        install_background_logging, install_console_diagnostics,
    )
    install_background_logging()
    install_console_diagnostics()
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

    # Small, optional notification-area listening indicator (NOT the Blackhole/
    # Singularity developer UI - see void.ui.tray_indicator). Best-effort: any
    # failure here (no PySide6, no system tray on this session) must never
    # affect voice - `indicator`/`bridge` simply stay None and cmd_voice()
    # runs exactly as it always has.
    indicator = None
    bridge = None
    try:
        from void.ui.voice_bridge import VoiceStateBridge
        from void.ui import tray_indicator
        bridge = VoiceStateBridge()
    except Exception:
        bridge = None

    def on_state(state):
        _log.info("VOICE_STATE %s", state)
        print(f"  [voice] {state}", flush=True)
        if bridge is not None:
            bridge.stateChanged.emit(state)

    controller = VoiceController.from_assistant(
        assistant,
        on_state=on_state,
        on_transcript=lambda t: print(f"  [heard] {t}", flush=True),
        on_message=lambda m: print(f"  {m}", flush=True),
    )

    exit_requested = threading.Event()
    if bridge is not None:
        indicator = tray_indicator.create(
            assistant, bridge, on_exit=exit_requested.set)

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
        if indicator is not None:
            indicator.run_until(
                lambda: controller.stopped or exit_requested.is_set())
        else:
            while not controller.stopped and not exit_requested.is_set():
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nExiting voice.")
    finally:
        if indicator is not None:
            indicator.stop()
        controller.shutdown("voice CLI exit")
        _log.info("VOICE_RUNTIME_STOPPED")
    if controller.stopped:
        print("Voice stopped (kill switch). Run 'clear-stop' and restart voice "
              "to talk again.")
    return 0


def cmd_autostart(action: str) -> int:
    """Manage persistent login autostart of the voice runtime via Task
    Scheduler (per-user, no admin/service/security change): a login trigger
    AND a workstation-unlock trigger (the Run key only ever fires once per
    fresh sign-in, which is why V.O.I.D could go a whole sleep/wake day
    without starting), plus automatic restart if it ever exits unexpectedly.
    'install' is idempotent and also removes any older Run-key registration
    so exactly one autostart mechanism is ever active; 'remove' undoes it;
    'status' shows the current registration."""
    from void.runtime import autostart, scheduled_task

    if action == "install":
        pythonw = autostart.default_pythonw_path()
        cmd = scheduled_task.install()
        autostart.remove()   # migration: never leave two active mechanisms
        print(f"Autostart installed (Task Scheduler). V.O.I.D will launch at "
              f"login and workstation unlock, and restart automatically if it "
              f"exits unexpectedly:\n  {cmd}")
        if "pythonw.exe" not in pythonw.lower():
            print("  NOTE: pythonw.exe was not found next to this interpreter, "
                  "so a console window may briefly appear at login.")
        print('  Say "Hey V.O.I.D." after the next login/unlock - no terminal needed.')
        return 0
    if action == "remove":
        removed_task = scheduled_task.remove()
        removed_run_key = autostart.remove()   # defensive: clear either mechanism
        removed = removed_task or removed_run_key
        print("Autostart removed." if removed else "Autostart was not installed.")
        return 0
    current = scheduled_task.status()
    print(f"Autostart: INSTALLED\n  {current}" if current
          else "Autostart: not installed. Run 'python -m void autostart install'.")
    return 0


# --- device gateway (mobile companion, V1 foundation) -------------------
#
# Off by default: none of these commands are called from voice/autostart, and
# 'device serve' is the only thing that ever opens a network port. Pairing
# and capability grants are owner-only CLI actions, exactly like `roots`/
# `protect` - never something the agent/LLM or a network request can do to
# itself.

def _device_state_dir():
    from void.config import Config
    return Config.load().state_dir()


def _local_ip_hint() -> str | None:
    """Best-effort local IP hint for the pairing screen. Uses a UDP socket's
    routing lookup (no packets are actually sent for a connected UDP socket)
    to find the address of the interface that would reach the open internet
    - on a laptop tethered to a phone hotspot with no other network, that is
    the hotspot interface's address, which is what needs to be typed into
    the companion device. Purely a convenience hint: the owner should confirm
    it (e.g. via `ipconfig`) if more than one network interface is active."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def cmd_device_pair_start(name: str | None, minutes: float) -> int:
    from void.config import Config
    from void.device import cert as cert_mod
    from void.device.pairing import PairingManager

    state_dir = _device_state_dir()
    cert_path, _ = cert_mod.ensure_cert(state_dir)
    fingerprint = cert_mod.fingerprint(cert_path)
    label = (name or "companion device").strip() or "companion device"

    pairing = PairingManager(state_dir, window_seconds=max(30.0, minutes * 60))
    token = pairing.begin(label)

    from void.device.gateway import running_port
    cfg = Config.load()
    live_port = running_port(state_dir)
    port = live_port if live_port is not None else cfg.get("device.port", 8765)
    ip_hint = _local_ip_hint()

    print(f"Pairing window open for {minutes:.0f} minute(s) (single use).")
    print(f"  Device name:                {label}")
    print(f"  Pairing token:              {token.token}")
    print(f"  Port:                       {port}"
         + ("" if live_port is not None else
            "  (device serve does not appear to be running - this is the "
            "configured default)"))
    print(f"  Certificate fingerprint:    {fingerprint}")
    if ip_hint:
        print(f"  This laptop's address (best guess): {ip_hint}")
        print("  Confirm with 'ipconfig' if this machine has more than one "
              "active network.")
    else:
        print("  Could not guess this laptop's address - check with 'ipconfig'.")
    print("\nOn the companion device, enter the address, port, token, and "
          "fingerprint above. The gateway must be running:\n"
          "  python -m void device serve")
    return 0


def cmd_device_list() -> int:
    import time as time_mod

    from void.device.identity import DeviceRegistry

    reg = DeviceRegistry(_device_state_dir() / "devices.json")
    devices = reg.list()
    if not devices:
        print("No paired devices.")
        return 0
    print("Paired devices:")
    for d in devices:
        last_seen = (time_mod.strftime("%Y-%m-%d %H:%M:%S",
                                       time_mod.localtime(d.last_seen))
                    if d.last_seen else "never")
        print(f"  {d.device_id}  name={d.name!r}  "
              f"capabilities={d.capabilities}  last_seen={last_seen}")
    return 0


def cmd_device_grant(device_id: str, capability: str) -> int:
    from void.device.capabilities import ALL_CAPABILITIES
    from void.device.identity import DeviceRegistry

    if capability not in ALL_CAPABILITIES:
        print(f"Unknown capability {capability!r}. Valid: "
              f"{sorted(ALL_CAPABILITIES)}")
        return 1
    reg = DeviceRegistry(_device_state_dir() / "devices.json")
    try:
        dev = reg.grant(device_id, capability)
    except KeyError:
        print(f"Unknown device: {device_id}")
        return 1
    print(f"Granted {capability!r} to {dev.name!r} ({dev.device_id}).")
    return 0


def cmd_device_revoke(device_id: str, capability: str) -> int:
    from void.device.identity import DeviceRegistry

    reg = DeviceRegistry(_device_state_dir() / "devices.json")
    try:
        dev = reg.revoke_capability(device_id, capability)
    except KeyError:
        print(f"Unknown device: {device_id}")
        return 1
    print(f"Revoked {capability!r} from {dev.name!r} ({dev.device_id}).")
    return 0


def cmd_device_forget(device_id: str) -> int:
    from void.device.identity import DeviceRegistry

    reg = DeviceRegistry(_device_state_dir() / "devices.json")
    removed = reg.forget(device_id)
    print(f"Unpaired {device_id}." if removed
          else f"No such paired device: {device_id}")
    return 0


def cmd_device_serve(host: str | None, port: int | None) -> int:
    """Run the Device Gateway in the foreground. The ONLY command that opens
    a network port; never called by voice/autostart. Authorization for any
    action a paired device requests flows through the SAME KillSwitch and
    RiskGate as every other V.O.I.D entry point (see void.device.capabilities)
    - a headless gateway supplies no confirm_fn, so anything requiring owner
    confirmation is denied, never silently allowed."""
    from void.device.gateway import DeviceGateway
    from void.runtime.diagnostics import (
        install_background_logging, install_console_diagnostics,
    )

    install_background_logging()
    install_console_diagnostics()
    assistant = Assistant(on_event=_event)
    if assistant.kill_switch.engaged:
        print("A stop is currently engaged. Run 'python -m void clear-stop' first.")
        return 1

    gw = DeviceGateway(assistant.config, assistant.tools, assistant.risk_gate,
                      assistant.kill_switch, assistant.config.state_dir(),
                      host=host, port=port)
    gw.start()
    print(f"Device gateway listening on {gw.host}:{gw.port} (HTTPS).")
    print(f"  Certificate fingerprint: {gw.fingerprint}")
    print("Pair a device from another terminal: python -m void device pair-start")
    print("Ctrl+C to stop.")
    try:
        gw.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping device gateway.")
    finally:
        gw.stop()
    return 0


# --- argument parsing --------------------------------------------------

def _readonly_state_dir(override: str | None):
    """The state directory for READ-ONLY tools: never created, never migrated."""
    from void.config import Config
    if override:
        return Path(override)
    return Config.load().peek_state_dir()


def cmd_perf_report(state_dir: str | None, legacy: str, as_json: bool) -> int:
    """Latency/reliability tables from the perf stream (and, for history, the legacy
    void.log markers). Read-only."""
    import json as _json
    from void.perf import report

    root = _readonly_state_dir(state_dir)
    events = report.load_events(root / "perf")
    if legacy == "always" or (legacy == "auto" and not events):
        events = report.parse_legacy_log(root / "void.log") + events
        note = "(using legacy void.log markers)"
    else:
        note = ""
    if not events:
        print(f"No performance data found under {root}.")
        return 1
    data = report.build_report(events)
    print(_json.dumps(data, indent=2, default=str) if as_json else report.format_report(data))
    if note and not as_json:
        print(note)
    return 0


def cmd_doctor(state_dir: str | None, no_probe: bool) -> int:
    """Read-only health report: heartbeat, microphone, wake word, gateway, task store,
    storage. Exit code 0 = ok, 1 = warnings, 2 = failures. It repairs nothing."""
    from void.perf import doctor

    root = _readonly_state_dir(state_dir)
    checks = doctor.run_doctor(root, probe_gateway=not no_probe)
    print(doctor.format_checks(checks))
    return doctor.exit_code(checks)


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
    p_tasks = sub.add_parser("tasks", help="List tasks (first pauses tasks stranded as running)")
    p_tasks.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="Report stale running tasks without changing them")

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

    p_device = sub.add_parser(
        "device",
        help="Manage the mobile companion device gateway (owner-only, opt-in)")
    device_sub = p_device.add_subparsers(dest="device_action")

    p_pair = device_sub.add_parser(
        "pair-start", help="Open a short-lived, single-use pairing window")
    p_pair.add_argument("--name", default=None,
                        help="Label for the device being paired.")
    p_pair.add_argument("--minutes", type=float, default=5.0,
                        help="Pairing window length in minutes (default 5).")

    device_sub.add_parser("list", help="List paired devices")

    p_grant = device_sub.add_parser(
        "grant", help="Grant a capability to a paired device")
    p_grant.add_argument("device_id")
    p_grant.add_argument("capability")

    p_revoke = device_sub.add_parser(
        "revoke", help="Revoke a capability from a paired device")
    p_revoke.add_argument("device_id")
    p_revoke.add_argument("capability")

    p_forget = device_sub.add_parser(
        "forget", help="Fully unpair a device (deletes its shared secret)")
    p_forget.add_argument("device_id")

    p_serve = device_sub.add_parser(
        "serve", help="Run the device gateway in the foreground (Ctrl+C to stop)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    from void.memory import cli as memory_cli
    memory_cli.add_parser(sub)

    p_perf = sub.add_parser("perf", help="Performance telemetry tools (read-only)")
    perf_sub = p_perf.add_subparsers(dest="perf_action")
    p_report = perf_sub.add_parser("report", help="Latency tables (p50/p95; p99 only if n>=300)")
    p_report.add_argument("--state-dir", default=None, help="Read a different state directory")
    p_report.add_argument("--legacy", choices=["auto", "always", "never"], default="auto",
                          help="Also parse pre-V2.0 void.log markers (auto: only if no perf data)")
    p_report.add_argument("--json", action="store_true", dest="as_json")

    p_doctor = sub.add_parser("doctor", help="Read-only health report (never repairs anything)")
    p_doctor.add_argument("--state-dir", default=None, help="Inspect a different state directory")
    p_doctor.add_argument("--no-probe", action="store_true",
                          help="Skip the loopback TLS probe of a running gateway")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Convenience: `python -m void "goal text"` with no subcommand -> run.
    known = {"run", "resume", "clarify", "approve", "deny", "set-key",
             "list-keys", "remove-key", "set-pin", "tasks", "stop",
             "clear-stop", "ui", "voice", "app", "singularity", "autostart",
             "roots", "protect", "device", "perf", "doctor", "memory", "-h", "--help"}
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
        return cmd_tasks(getattr(args, "dry_run", False))
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
    if args.command == "memory":
        from void.config import Config
        from void.memory import cli as memory_cli
        return memory_cli.run(args, Config.load())
    if args.command == "perf":
        if args.perf_action == "report":
            return cmd_perf_report(args.state_dir, args.legacy, args.as_json)
        print("Usage: python -m void perf report [--state-dir DIR] [--legacy auto|always|never] [--json]")
        return 1
    if args.command == "doctor":
        return cmd_doctor(args.state_dir, args.no_probe)
    if args.command == "device":
        if args.device_action == "pair-start":
            return cmd_device_pair_start(args.name, args.minutes)
        if args.device_action == "list":
            return cmd_device_list()
        if args.device_action == "grant":
            return cmd_device_grant(args.device_id, args.capability)
        if args.device_action == "revoke":
            return cmd_device_revoke(args.device_id, args.capability)
        if args.device_action == "forget":
            return cmd_device_forget(args.device_id)
        if args.device_action == "serve":
            return cmd_device_serve(args.host, args.port)
        print("Usage: python -m void device "
              "{pair-start,list,grant,revoke,forget,serve}")
        return 1

    parser.print_help()
    return 0
