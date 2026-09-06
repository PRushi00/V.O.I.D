"""Phase 9B TTS smoke test: speak one line through the local Windows provider.

Exercises the provider-agnostic TTS layer end to end against a REAL speaker:
create_tts_provider -> SapiTTS (Windows SAPI SpVoice) -> audible speech, plus an
interruption check. It is deliberately separate from the deterministic unit
tests (which use fakes and need no audio hardware).

REQUIREMENTS:
  * Windows with an interactive desktop + working audio output.
  * pywin32 (already in requirements.txt) for SAPI.

It is NON-destructive: no files, no Gemini/cloud, no secrets are spoken - only a
fixed, non-sensitive check phrase.

Run:  .venv\\Scripts\\python.exe verify\\tts_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.voice.adapters import TTSError            # noqa: E402
from void.voice.tts import create_tts_provider       # noqa: E402

# Fixed, non-sensitive phrases only. Never speak secrets/credentials.
_CHECK = "V O I D voice check. Local text to speech is working."
_INTERRUPT = ("This sentence is intentionally long so that it can be cut off "
              "in the middle to prove that speech is interruptible.")


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows; the local SAPI provider is Windows-only.")
        return 0

    tts = create_tts_provider(provider="sapi")
    print(f"Provider: {getattr(tts, 'name', '?')}")

    try:
        print("Speaking check phrase ...")
        tts.speak(_CHECK)
        # SAPI speak is async; wait for it to finish for the smoke.
        for _ in range(50):
            if not tts.is_speaking:
                break
            time.sleep(0.1)

        print("Speaking a long phrase, then interrupting after ~1s ...")
        tts.speak(_INTERRUPT)
        time.sleep(1.0)
        tts.stop()               # purge: should cut off immediately
        time.sleep(0.3)
        assert not tts.is_speaking, "speech did not stop after stop()"
    except TTSError as exc:
        print(f"TTS FAILED (non-fatal): {exc}")
        return 1

    print("TTS SMOKE: done (verify you heard the check phrase and the cut-off).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
