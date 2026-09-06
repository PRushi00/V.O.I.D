"""Deterministic voice-session state machine (Phase 9B step 3).

A PURE lifecycle reducer:

    reduce_voice(state, event) -> (new_state, commands)

It owns ONLY the voice-session lifecycle. It never inspects task / RiskGate /
KillSwitch internals, never chooses intent, and never raises: an unlisted
(state, event) pair deterministically stays put with no side effects. The
authoritative owner of the *current* state + generation is the VoiceSession;
this module only decides the transition and the side-effect COMMANDS the
session must carry out.

States:
    IDLE          - ready; mic closed; accepts PTT_DOWN
    LISTENING     - mic capturing; PTT held; accepts PTT_UP
    CAPTURED      - audio finalized; transient handoff to STT
    TRANSCRIBING  - STT running for the current generation
    DISPATCHED    - transcript handed to Assistant.run(); BUSY / single-flight
    SPEAKING      - finalized response being spoken (async, interruptible)
    ERROR         - transient; cleans up then recovers to IDLE
    STOPPED       - KillSwitch-latched; only an explicit re-arm leaves it
    CLOSED        - terminal shutdown; nothing may resurrect a voice session
"""
from __future__ import annotations


class VoiceState:
    IDLE = "idle"
    LISTENING = "listening"
    CAPTURED = "captured"
    TRANSCRIBING = "transcribing"
    DISPATCHED = "dispatched"
    SPEAKING = "speaking"
    ERROR = "error"
    STOPPED = "stopped"
    CLOSED = "closed"


# States from which normal lifecycle work may still be interrupted by a global
# event (KillSwitch / shutdown / internal error). STOPPED and CLOSED are not
# here: they are latched/terminal and handled first.
_ACTIVE = frozenset({
    VoiceState.IDLE, VoiceState.LISTENING, VoiceState.CAPTURED,
    VoiceState.TRANSCRIBING, VoiceState.DISPATCHED, VoiceState.SPEAKING,
    VoiceState.ERROR,
})


class VoiceEvent:
    # external / adapter edges
    PTT_DOWN = "ptt_down"
    PTT_UP = "ptt_up"
    # internal pipeline results (carry the originating generation)
    BEGIN_STT = "begin_stt"                 # CAPTURED -> TRANSCRIBING
    STT_OK = "stt_ok"                       # non-empty transcript
    STT_EMPTY = "stt_empty"                 # empty / invalid transcript
    STT_FAILED = "stt_failed"
    DISPATCH_OK_SPEAK = "dispatch_ok_speak"     # finalized response has speech
    DISPATCH_OK_SILENT = "dispatch_ok_silent"   # nothing to speak / TTS off
    DISPATCH_FAILED = "dispatch_failed"
    SPEAK_FAILED = "speak_failed"           # TTS failed (response preserved)
    TTS_DONE = "tts_done"                   # speech finished / retired
    CAPTURE_FAILED = "capture_failed"
    INTERNAL_ERROR = "internal_error"
    RECOVER = "recover"                     # ERROR -> IDLE after cleanup
    # authoritative / lifecycle
    KILLSWITCH = "killswitch"
    SHUTDOWN = "shutdown"
    REARM = "rearm"                         # explicit non-voice restart


class VoiceCommand:
    NEW_GENERATION = "new_generation"       # invalidate in-flight async work
    MIC_OPEN = "mic_open"
    CAPTURE_FINALIZE = "capture_finalize"   # stop capture, keep the audio
    MIC_CLOSE = "mic_close"
    RUN_STT = "run_stt"                     # blocking; emits STT_* with the gen
    RUN_DISPATCH = "run_dispatch"           # blocking; emits DISPATCH_* with gen
    SPEAK = "speak"                         # async; emits SPEAK_FAILED on error
    TTS_STOP = "tts_stop"
    TTS_CLOSE = "tts_close"
    WARN_EMPTY = "warn_empty"


C = VoiceCommand
S = VoiceState
E = VoiceEvent

_ERROR_CLEANUP = (C.MIC_CLOSE, C.TTS_STOP)


def reduce_voice(state: str, event: str) -> tuple[str, tuple[str, ...]]:
    """Pure transition function. Returns (new_state, commands). Never raises;
    unknown/invalid combinations deterministically stay put with no commands."""
    # --- terminal / latched states first ------------------------------
    if state == S.CLOSED:
        return (S.CLOSED, ())                       # nothing resurrects CLOSED
    if state == S.STOPPED:
        if event == E.SHUTDOWN:
            return (S.CLOSED,
                    (C.NEW_GENERATION, C.MIC_CLOSE, C.TTS_STOP, C.TTS_CLOSE))
        if event == E.REARM:
            return (S.IDLE, (C.NEW_GENERATION,))    # explicit non-voice restart
        return (S.STOPPED, ())                       # latched: ignore everything

    # --- authoritative events from ANY active state -------------------
    if event == E.KILLSWITCH:
        return (S.STOPPED, (C.NEW_GENERATION, C.MIC_CLOSE, C.TTS_STOP))
    if event == E.SHUTDOWN:
        return (S.CLOSED,
                (C.NEW_GENERATION, C.MIC_CLOSE, C.TTS_STOP, C.TTS_CLOSE))

    # --- ERROR is transient; only RECOVER (or the authoritative events
    #     above) may leave it -----------------------------------------
    if state == S.ERROR:
        if event == E.RECOVER:
            return (S.IDLE, ())
        return (S.ERROR, ())

    # any unexpected internal failure in an active state -> ERROR + cleanup
    if event in (E.INTERNAL_ERROR, E.CAPTURE_FAILED):
        return (S.ERROR, _ERROR_CLEANUP)

    # --- per-state transitions ----------------------------------------
    if state == S.IDLE:
        if event == E.PTT_DOWN:
            return (S.LISTENING, (C.NEW_GENERATION, C.MIC_OPEN))
        return (S.IDLE, ())

    if state == S.LISTENING:
        if event == E.PTT_UP:
            return (S.CAPTURED, (C.CAPTURE_FINALIZE,))
        return (S.LISTENING, ())                     # repeated PTT_DOWN -> ignore

    if state == S.CAPTURED:
        if event == E.BEGIN_STT:
            return (S.TRANSCRIBING, (C.RUN_STT,))
        return (S.CAPTURED, ())

    if state == S.TRANSCRIBING:
        if event == E.STT_OK:
            return (S.DISPATCHED, (C.RUN_DISPATCH,))
        if event == E.STT_EMPTY:
            return (S.IDLE, (C.MIC_CLOSE, C.WARN_EMPTY))
        if event == E.STT_FAILED:
            return (S.ERROR, _ERROR_CLEANUP)
        if event == E.PTT_DOWN:                      # cancel STT, new capture
            return (S.LISTENING, (C.NEW_GENERATION, C.MIC_OPEN))
        return (S.TRANSCRIBING, ())

    if state == S.DISPATCHED:
        if event == E.DISPATCH_OK_SPEAK:
            return (S.SPEAKING, (C.SPEAK,))
        if event == E.DISPATCH_OK_SILENT:
            return (S.IDLE, ())
        if event == E.DISPATCH_FAILED:
            return (S.ERROR, _ERROR_CLEANUP)
        # PTT_DOWN and everything else -> ignore (single-flight)
        return (S.DISPATCHED, ())

    if state == S.SPEAKING:
        if event == E.TTS_DONE:
            return (S.IDLE, ())
        if event == E.PTT_DOWN:                      # barge-in (speech only)
            return (S.LISTENING, (C.TTS_STOP, C.NEW_GENERATION, C.MIC_OPEN))
        if event == E.SPEAK_FAILED:                  # response preserved by owner
            return (S.ERROR, _ERROR_CLEANUP)
        return (S.SPEAKING, ())

    return (state, ())                               # unreachable-safe default
