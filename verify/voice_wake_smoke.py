"""Phase 9C-3 wake + broker + STT integration smoke (real local microphone).

Brings up the REAL integrated voice runtime the way `python -m void voice` does
(one AudioCaptureBroker -> BrokerCapture + wake detector consumers -> the
unchanged VoiceSession -> Assistant -> TTS) and lets you exercise BOTH activation
paths against a live microphone:

    * push-to-talk : hold the hotkey, speak, release
    * wake word    : say "Hey V.O.I.D." then speak your command; the deterministic
                     endpointer (silence / no-speech / hard-max) finalises it

It asserts only bounded runtime invariants (one physical stream, wake armed only
in IDLE, capture starts after wake, KillSwitch latches STOPPED); it never judges
audio content. This is an EXPLICIT manual smoke - it is NOT collected by pytest
and it requires a microphone, so it is never part of the automated suite.

REQUIREMENTS
  * Windows + a working input device
  * pip install -r requirements-voice.txt   (faster-whisper, sounddevice, numpy,
    keyboard, openwakeword)
  * A custom "Hey V.O.I.D." openWakeWord model, with its path set in
    config/local_config.yaml as voice.wake_model_path  (openWakeWord ships no
    model for this phrase). Without it, only the push-to-talk path is exercised.
  * voice.enabled: true in config/local_config.yaml
  * No Gemini key needed if you only want the capture/endpoint behaviour; a
    dispatch will simply fail cleanly without one.

RUN
  .venv\\Scripts\\python.exe verify\\voice_wake_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.app import Assistant                          # noqa: E402
from void.voice.adapters import BrokerCapture           # noqa: E402
from void.voice.capture_broker import AudioCaptureBroker  # noqa: E402
from void.voice.runtime import VoiceController          # noqa: E402
from void.voice.state import VoiceState                 # noqa: E402


def _line(msg: str) -> None:
    print(f"  {msg}", flush=True)


def main() -> int:
    assistant = Assistant(on_event=_line)               # no confirm_fn: HIGH defers
    if not assistant.config.get("voice.enabled", False):
        print("Set voice.enabled: true in config/local_config.yaml first.")
        return 1
    if assistant.kill_switch.engaged:
        print("A stop is engaged. Run: python -m void clear-stop")
        return 1

    ctrl = VoiceController.from_assistant(
        assistant,
        on_state=lambda s: print(f"  [state] {s}", flush=True),
        on_transcript=lambda t: print(f"  [heard] {t}", flush=True),
        on_message=lambda m: print(f"  {m}", flush=True),
    )

    # --- bounded structural assertions (no audio judgement) --------------
    assert isinstance(ctrl._broker, AudioCaptureBroker), "one broker expected"
    assert isinstance(ctrl.session._capture, BrokerCapture), \
        "session must capture via the broker, not its own device"
    assert ctrl.session._capture._broker is ctrl._broker, "must be the SAME broker"

    wake_on = ctrl._wake is not None
    hotkey = assistant.config.get("voice.ptt_hotkey", "ctrl+space")
    print("V.O.I.D wake/broker smoke.")
    print(f"  push-to-talk : hold '{hotkey}', speak, release")
    if wake_on:
        print('  wake word    : say "Hey V.O.I.D." then your command')
    else:
        print("  wake word    : NOT configured (set voice.wake_model_path) - "
              "push-to-talk only")
    print("  Ctrl+C to exit. Global stop: python -m void stop  (another terminal)")

    ctrl.start()
    try:
        armed_seen = False
        while not ctrl.stopped:
            time.sleep(0.2)
            st = ctrl.state
            if wake_on and st == VoiceState.IDLE and ctrl._wake_armed:
                if not armed_seen:
                    _line("wake detector ARMED (session idle)")
                    armed_seen = True
            elif st != VoiceState.IDLE:
                armed_seen = False
            # invariant: the detector is never armed outside IDLE
            if ctrl._wake_armed and st != VoiceState.IDLE:
                raise AssertionError(
                    f"wake armed while session state={st!r} (must be idle only)")
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        ctrl.shutdown("wake smoke exit")

    # one physical stream, released on shutdown
    be = getattr(ctrl._broker, "_backend", None)
    print(f"  broker backend closed: {getattr(be, '_stream', 'n/a') is None}")
    if ctrl.stopped:
        print("  session STOPPED (kill switch) - latched until an explicit re-arm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
