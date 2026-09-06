"""V.O.I.D voice interface (Phase 9A).

An input/output ADAPTER around the existing Assistant/Agent - never a second
agent. Push-to-talk -> local STT -> Assistant.run() -> local TTS. All heavy /
Windows-only dependencies (faster-whisper, audio capture, win32 SAPI, hotkey)
are imported lazily inside the adapters, so importing this package (and core
V.O.I.D) never requires the optional voice stack.
"""
from void.voice.state import (
    VoiceCommand, VoiceEvent, VoiceState, reduce_voice,
)

__all__ = [
    "VoiceState", "VoiceEvent", "VoiceCommand", "reduce_voice",
    "VoiceSession", "VoiceController",
    "TTSProvider", "NullTTS", "create_tts_provider", "register_tts_provider",
    "WakeWordDetector", "NullWakeDetector", "create_wake_detector",
    "register_wake_provider", "WAKE_DETECTED",
]

_WAKE_NAMES = frozenset({
    "WakeWordDetector", "NullWakeDetector", "OpenWakeWordDetector",
    "create_wake_detector", "register_wake_provider", "WAKE_DETECTED",
    "WakeWordError", "WakeWordConfigError", "WakeWordBackendError",
})


def __getattr__(name):
    # Lazily expose the session/controller/TTS/wake layer so importing the
    # package stays cheap and never pulls the (lazy) adapters unless voice is
    # used.
    if name == "VoiceSession":
        from void.voice.session import VoiceSession
        return VoiceSession
    if name == "VoiceController":
        from void.voice.runtime import VoiceController
        return VoiceController
    if name in ("TTSProvider", "NullTTS", "create_tts_provider",
                "register_tts_provider"):
        import void.voice.tts as _tts
        return getattr(_tts, name)
    if name in _WAKE_NAMES:
        import void.voice.wake as _wake
        return getattr(_wake, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
