"""One-shot REAL two-command voice smoke (interactive, Windows).

TEST HARNESS ONLY - creates no production behavior. It builds the REAL voice
stack through the existing wiring (VoiceController.from_assistant): real
microphone, real faster-whisper STT, the REAL Assistant, real SAPI TTS, the
real PTTActivation global hotkey, the real serial voice worker, and the real
monitor loop. Nothing is stubbed or faked.

It guides you through two spoken commands, the second delivered as a physical
PTT barge-in WHILE V.O.I.D is speaking the first answer, and observes the state/
transcript sequence produced by the real pipeline.

Run:  .venv\\Scripts\\python.exe verify\\voice_two_command_smoke.py

Requires: Windows, a working microphone + speakers, the optional voice stack
(requirements-voice.txt) installed, and a configured Gemini key (the real
Assistant is used). PTT key is voice.ptt_hotkey (default: hold Ctrl+Space).
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.app import Assistant                          # noqa: E402
from void.voice.adapters import VoiceDependencyError    # noqa: E402
from void.voice.runtime import VoiceController          # noqa: E402
from void.voice.state import VoiceState                 # noqa: E402

_lock = threading.Lock()
_states: list[str] = []          # ordered state names as observed
_transcripts: list[str] = []     # ordered [heard] transcripts
_messages: list[str] = []


def _on_state(st: str) -> None:
    with _lock:
        _states.append(st)
    print(f"  [state] {st}", flush=True)


def _on_transcript(t: str) -> None:
    with _lock:
        _transcripts.append(t)
    print(f"  [heard] {t!r}", flush=True)


def _on_message(m: str) -> None:
    with _lock:
        _messages.append(m)
    print(f"  {m}", flush=True)


def _snapshot(getter):
    with _lock:
        return getter()


def _wait_for(pred, timeout: float, poll: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _snapshot(pred):
            return True
        time.sleep(poll)
    return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _barge_in_happened() -> bool:
    # A SPEAKING -> LISTENING adjacency means PTT interrupted live speech.
    with _lock:
        seq = list(_states)
    return any(seq[i] == VoiceState.SPEAKING and seq[i + 1] == VoiceState.LISTENING
               for i in range(len(seq) - 1))


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows.")
        return 0

    assistant = Assistant(on_event=lambda m: print(f"  [agent] {m}", flush=True))
    hotkey = assistant.config.get("voice.ptt_hotkey", "ctrl+space")
    controller = VoiceController.from_assistant(
        assistant, on_state=_on_state, on_transcript=_on_transcript,
        on_message=_on_message)

    try:
        controller.start()
    except VoiceDependencyError as exc:
        print(f"SKIP: voice deps missing ({exc}).")
        return 0

    results = {
        "first_transcript": False, "first_spoke": False, "barge_in": False,
        "second_transcript": False, "second_distinct": False,
        "second_spoke": False,
    }
    # Marker = number of transcripts / states observed at the moment the BARGE-IN
    # prompt is shown. Transcripts captured at index >= t_marker belong to the
    # command-2 era, so a spurious echo captured during command 1 can never be
    # mistaken for the second command (the original index-[1] bug).
    t_marker: int | None = None
    s_marker = 0
    closed = False

    print("=" * 64)
    print("REAL TWO-COMMAND VOICE TEST")
    print(f"PTT key: hold '{hotkey}'. Do not type SPACE in this console while")
    print("the test runs (the global hotkey may pick it up).")
    print("=" * 64)

    try:
        # -------- COMMAND 1 --------
        print("\nCOMMAND 1")
        print(f"  Press and HOLD {hotkey}, say: \"What is the capital of Japan?\"")
        print("  Then RELEASE the key.\n")

        if _wait_for(lambda: len(_transcripts) >= 1, timeout=120):
            results["first_transcript"] = True
            # First response should reach SPEAKING (real Assistant + SAPI).
            results["first_spoke"] = _wait_for(
                lambda: VoiceState.SPEAKING in _states, timeout=60)

            # -------- BARGE-IN + COMMAND 2 --------
            with _lock:
                t_marker = len(_transcripts)     # command-2 boundary
                s_marker = len(_states)
            print("\nBARGE-IN TEST")
            print("  WHILE V.O.I.D is still speaking the first answer:")
            print(f"  press and HOLD {hotkey}, say: \"What is two plus two?\"")
            print("  Then RELEASE the key.\n")

            # A NEW transcript beyond the marker == the command-2 utterance.
            if _wait_for(lambda: len(_transcripts) > t_marker, timeout=150):
                results["second_transcript"] = True
                results["barge_in"] = _barge_in_happened()
                # Second response should reach SPEAKING (a SPEAKING recorded at
                # or after the barge marker), not merely reuse command 1's.
                results["second_spoke"] = _wait_for(
                    lambda: any(st == VoiceState.SPEAKING
                                for st in _states[s_marker:]), timeout=60)
                # Let the second reply finish speaking (retire to IDLE), bounded.
                _wait_for(lambda: controller.session.state == VoiceState.IDLE,
                          timeout=60)
            else:
                print("\nTIMEOUT: no second (command-2) transcript was received.")
        else:
            print("\nTIMEOUT: no first transcript was received.")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        try:
            controller.shutdown("two-command smoke complete")
        except Exception as exc:  # pragma: no cover
            print(f"  (cleanup) shutdown error: {exc}", flush=True)
        closed = controller.session.state == VoiceState.CLOSED

    # -------- REPORT (reflects the ACTUAL observed event stream) --------
    with _lock:
        seq = list(_states)
        transcripts = list(_transcripts)

    marker = t_marker if t_marker is not None else len(transcripts)
    first = transcripts[0] if transcripts else None
    phase2 = transcripts[marker:]                 # command-2-era transcripts
    second = phase2[-1] if phase2 else None        # last post-barge utterance
    results["second_distinct"] = bool(
        first is not None and second is not None
        and _norm(first) != _norm(second) and _norm(second))

    print("\n" + "=" * 64)
    print("OBSERVED STATE SEQUENCE:")
    print("  " + " -> ".join(seq) if seq else "  (none)")
    print("ALL TRANSCRIPTS (in order):")
    if transcripts:
        for i, tr in enumerate(transcripts):
            phase = "cmd1/pre-barge" if i < marker else "cmd2/post-barge"
            print(f"  [{i}] ({phase}) {tr!r}")
    else:
        print("  (none)")
    print("-" * 64)
    print(f"FIRST  transcript: {first!r}")
    print(f"SECOND transcript: {second!r}")
    if len(phase2) > 1:
        print(f"  (note: {len(phase2)} post-barge transcripts observed; using "
              f"the last as the command-2 result)")
    print("-" * 64)
    print(f"1. first transcript received : {results['first_transcript']}")
    print(f"2. first reached SPEAKING     : {results['first_spoke']}")
    print(f"3. barge-in (SPEAKING->LISTEN): {results['barge_in']}")
    print(f"4. second transcript received : {results['second_transcript']}")
    print(f"5. second distinct from first : {results['second_distinct']}")
    print(f"6. second reached SPEAKING     : {results['second_spoke']}")
    print(f"7. session closed cleanly      : {closed}")
    print("=" * 64)

    # Barge-in is NOT required to pass: if the first reply finished before the
    # user pressed PTT, barge-in is legitimately False. What must hold is that a
    # distinct second command really flowed through the pipeline and was spoken.
    second_ok = (results["second_transcript"] and results["second_distinct"]
                 and results["second_spoke"])
    passed = (results["first_transcript"] and results["first_spoke"]
              and second_ok and closed)
    verdict = "PASS" if passed else "REVIEW (see checks above)"
    if passed and not results["barge_in"]:
        verdict += " (note: second command succeeded WITHOUT a live barge-in; " \
                   "the first reply finished before PTT_DOWN)"
    print("TWO-COMMAND SMOKE:", verdict)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
