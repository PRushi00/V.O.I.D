"""Phase 9B TTS smoke test: async speech + interruption on the real Windows SAPI
provider, driven through the provider-agnostic interface.

This is separate from the deterministic unit tests (which use a fake SpVoice and
need no audio). It exercises the REAL COM-owning worker thread and asserts the
Step 2 contract in ways that do NOT depend on subjective audio timing:

  1. speak() returns promptly (async) while speech is running.
  2. stop() from the main/control thread interrupts and returns promptly.
  3. is_speaking() eventually becomes False after stop and after completion.
  4. speak-while-speaking (replace) and repeated speak/stop cycles do not hang.
  5. close()/shutdown while speaking does not hang and cleans up COM.

REQUIREMENTS: Windows + audio device + pywin32 (already in requirements.txt).
It is NON-destructive and speaks only fixed, non-sensitive phrases (no secrets).

Run:  .venv\\Scripts\\python.exe verify\\tts_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.voice.adapters import TTSError            # noqa: E402
from void.voice.tts import create_tts_provider       # noqa: E402

_LONG = ("This is a deliberately long sentence used only to verify that speech "
         "runs asynchronously and can be interrupted cleanly from another "
         "control path without waiting for the whole sentence to finish.")
_SHORT = "V O I D voice check."


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows; the local SAPI provider is Windows-only.")
        return 0

    tts = create_tts_provider(provider="sapi")
    print(f"Provider: {getattr(tts, 'name', '?')}")
    try:
        # (1) async + (2)(3) interruptible from the control path.
        print("speak(long); expecting async return while speaking ...")
        t0 = time.monotonic()
        tts.speak(_LONG)
        elapsed = time.monotonic() - t0
        _assert(elapsed < 2.0, f"speak() blocked too long ({elapsed:.2f}s)")
        _assert(tts.is_speaking, "is_speaking should be True right after speak()")

        time.sleep(0.6)                       # let a little audio play
        print("stop(); expecting prompt interruption ...")
        t0 = time.monotonic()
        tts.stop()
        _assert(time.monotonic() - t0 < 1.0, "stop() did not return promptly")

        deadline = time.monotonic() + 3.0
        while tts.is_speaking and time.monotonic() < deadline:
            time.sleep(0.02)
        _assert(not tts.is_speaking, "is_speaking did not converge to False")

        # (4) replace + repeated cycles do not hang.
        print("speak/replace/stop cycles ...")
        for _ in range(3):
            tts.speak(_LONG)
            tts.speak(_SHORT)                 # replace
            tts.stop()
        deadline = time.monotonic() + 3.0
        while tts.is_speaking and time.monotonic() < deadline:
            time.sleep(0.02)
        _assert(not tts.is_speaking, "cycles left it speaking")

        # (5) shutdown while speaking must not hang.
        print("close() while speaking ...")
        tts.speak(_LONG)
        t0 = time.monotonic()
        tts.close()
        _assert(time.monotonic() - t0 < 3.0, "close() hung")
        _assert(not tts.is_speaking, "still speaking after close()")
    except TTSError as exc:
        print(f"TTS FAILED (non-fatal): {exc}")
        return 1

    print("TTS SMOKE: PASS (async, interruptible, no hangs, clean shutdown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
