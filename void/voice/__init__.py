"""V.O.I.D voice interface (Phase 9A).

An input/output ADAPTER around the existing Assistant/Agent - never a second
agent. Push-to-talk -> local STT -> Assistant.run() -> local TTS. All heavy /
Windows-only dependencies (faster-whisper, audio capture, win32 SAPI, hotkey)
are imported lazily inside the adapters, so importing this package (and core
V.O.I.D) never requires the optional voice stack.
"""
from void.voice.state import (
    IllegalVoiceTransition, VoiceState, VoiceStateMachine,
)

__all__ = [
    "VoiceState", "VoiceStateMachine", "IllegalVoiceTransition",
    "VoiceSession", "VoiceController",
]


def __getattr__(name):
    # Lazily expose the session/controller so importing the package stays cheap
    # and never pulls the (lazy) adapters unless voice is actually used.
    if name == "VoiceSession":
        from void.voice.session import VoiceSession
        return VoiceSession
    if name == "VoiceController":
        from void.voice.runtime import VoiceController
        return VoiceController
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
