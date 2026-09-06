"""Phase 9A voice LIVE smoke test (real mic + faster-whisper + Windows SAPI).

Exercises the REAL VoiceSession pipeline end to end WITHOUT cloud services and
WITHOUT Gemini quota: it uses a local EchoAssistant that just repeats the
transcript, so the path is  mic capture -> faster-whisper STT -> Assistant.run
(echo) -> SAPI TTS, plus a TTS-interruption check.

REQUIREMENTS (documented):
  * Windows with an interactive desktop + a working microphone.
  * pip install -r requirements-voice.txt   (faster-whisper, sounddevice, numpy)
  * pywin32 (already in requirements.txt) for SAPI TTS.
  * First run downloads the Whisper model (default "small", ~460 MB) - explicit,
    not during normal V.O.I.D startup.

It is NON-destructive: no files created/deleted, no Gemini calls, no cloud.

Run:  .venv\\Scripts\\python.exe verify\\voice_smoke.py
This script uses a fixed-duration capture (no global hotkey) to stay simple and
non-interactive. The real push-to-talk hotkey + monitor loop are wired by
``void.voice.runtime.VoiceController`` and launched with:  python -m void voice
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.core.kill_switch import KillSwitch          # noqa: E402
from void.core.task import Status                      # noqa: E402
from void.voice.adapters import (                      # noqa: E402
    FasterWhisperSTT, MicAudioCapture, SapiTTS, VoiceDependencyError,
)
from void.voice.session import VoiceSession            # noqa: E402


class _EchoResult:
    def __init__(self, text):
        self.status = Status.COMPLETED
        self.result = text


class _EchoAssistant:
    """Stand-in for Assistant.run that avoids Gemini during the smoke test."""
    def run(self, transcript):
        return _EchoResult(f"You said: {transcript}")


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows.")
        return 0
    try:
        cap = MicAudioCapture()
        stt = FasterWhisperSTT(model_name="small")
        tts = SapiTTS()
    except VoiceDependencyError as exc:
        print(f"SKIP: voice deps missing ({exc}).")
        return 0

    ks = KillSwitch()
    session = VoiceSession(_EchoAssistant(), ks, capture=cap, stt=stt, tts=tts,
                           on_state=lambda s: print("  [state]", s),
                           on_transcript=lambda t: print("  [heard]", t),
                           on_message=lambda m: print("  [msg]", m))

    print("Capturing 4 seconds of audio - speak now (e.g. 'open my notes') ...")
    session.on_ptt_press()
    try:
        time.sleep(4.0)
    finally:
        session.on_ptt_release()   # finalize -> STT -> echo -> SAPI TTS

    # give TTS a moment, then prove interruption works
    time.sleep(0.8)
    print("Interrupting speech with a new PTT press ...")
    session.on_ptt_press()
    session.on_ptt_release()
    time.sleep(0.5)
    session.stop("smoke complete")
    print("VOICE SMOKE: done (verify you heard the echo and the interrupt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
