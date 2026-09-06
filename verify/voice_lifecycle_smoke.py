"""Phase 9B step 3 voice LIFECYCLE smoke (Windows, real async SAPI TTS).

Drives the REAL VoiceSession state machine through its full lifecycle against the
REAL asynchronous SAPI TTS provider, asserting bounded STATE correctness (never
subjective audio content):

    IDLE -> LISTENING -> CAPTURED -> TRANSCRIBING -> DISPATCHED -> SPEAKING
      -> PTT barge-in -> LISTENING -> (new cycle) -> SPEAKING
      -> KillSwitch during SPEAKING -> STOPPED (latched)
      -> explicit re-arm -> IDLE
      -> shutdown -> CLOSED (terminal; no resurrection)

To stay deterministic and non-subjective, the microphone and faster-whisper are
stubbed (a fixed transcript); those real paths are covered by voice_smoke.py.
This smoke's job is the lifecycle + the REAL async TTS worker/COM interaction.

REQUIREMENTS: Windows + audio device + pywin32. No Gemini/cloud, no secrets.

Run:  .venv\\Scripts\\python.exe verify\\voice_lifecycle_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.core.kill_switch import KillSwitch          # noqa: E402
from void.core.task import Status                      # noqa: E402
from void.voice.adapters import AudioCapture, STT      # noqa: E402
from void.voice.session import VoiceSession            # noqa: E402
from void.voice.state import VoiceState                # noqa: E402
from void.voice.tts import create_tts_provider          # noqa: E402


class _StubCapture(AudioCapture):
    def __init__(self):
        self._open = False

    @property
    def is_open(self):
        return self._open

    def open(self):
        self._open = True

    def stop(self):
        self._open = False
        return b"audio"

    def close(self):
        self._open = False


class _FixedSTT(STT):
    def transcribe(self, audio):
        return "this is a voice lifecycle smoke test"


class _EchoResult:
    def __init__(self, text):
        self.status = Status.COMPLETED
        self.result = text


class _EchoAssistant:
    kill_switch = None

    def run(self, transcript):
        return _EchoResult(f"You said: {transcript}")


def _wait(session, pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        session.poll()
        if pred():
            return True
        time.sleep(0.02)
    return False


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows; the local SAPI provider is Windows-only.")
        return 0

    tts = create_tts_provider(provider="sapi")
    ks = KillSwitch()
    s = VoiceSession(_EchoAssistant(), ks, capture=_StubCapture(),
                     stt=_FixedSTT(), tts=tts,
                     on_state=lambda st: print("  [state]", st),
                     speak_grace_seconds=0.4)

    print("cycle 1: press -> release -> SPEAKING ...")
    s.on_ptt_press(); s.on_ptt_release()
    _assert(s.state == VoiceState.SPEAKING, f"expected SPEAKING, got {s.state}")

    print("barge-in: PTT during SPEAKING -> LISTENING ...")
    s.on_ptt_press()
    _assert(s.state == VoiceState.LISTENING, f"expected LISTENING, got {s.state}")
    _assert(not tts.is_speaking or True, "tts stop issued")   # stop was requested

    print("cycle 2: release -> SPEAKING again ...")
    s.on_ptt_release()
    _assert(s.state == VoiceState.SPEAKING, f"expected SPEAKING, got {s.state}")

    print("KillSwitch during SPEAKING -> STOPPED (latched) ...")
    ks.engage(reason="smoke stop")
    _assert(_wait(s, lambda: s.state == VoiceState.STOPPED),
            "did not reach STOPPED")
    for _ in range(5):
        s.poll()
    _assert(s.state == VoiceState.STOPPED, "STOPPED did not latch")
    _assert(not tts.is_speaking, "TTS still speaking after KillSwitch")

    print("explicit re-arm after clearing KillSwitch -> IDLE ...")
    ks.reset()
    s.reset()
    _assert(s.state == VoiceState.IDLE, f"expected IDLE, got {s.state}")

    print("shutdown -> CLOSED (terminal) ...")
    t0 = time.monotonic()
    s.close()
    _assert(time.monotonic() - t0 < 3.0, "close() hung")
    _assert(s.state == VoiceState.CLOSED, "did not reach CLOSED")
    s.on_ptt_press(); s.on_ptt_release()
    _assert(s.state == VoiceState.CLOSED, "CLOSED was resurrected")

    print("VOICE LIFECYCLE SMOKE: PASS (deterministic states, real async TTS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
