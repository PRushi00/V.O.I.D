"""Provider-agnostic text-to-speech layer (Phase 9B, step 1).

The voice session/controller depend only on the small ``TTS`` interface
(``is_speaking`` / ``speak`` / ``stop``) defined in :mod:`void.voice.adapters`.
This module makes that interface *pluggable*:

  * ``TTSProvider``       - the interface future providers implement (an alias of
                            the existing ``TTS`` base, so there is one hierarchy).
  * ``NullTTS``           - a safe no-op provider (no speakers). Used on
                            non-Windows hosts, when the voice stack is missing,
                            or when output is disabled - it never raises.
  * ``SapiTTS``           - the local Windows SAPI provider (re-exported from
                            adapters; win32com is imported lazily).
  * ``create_tts_provider`` - a factory that selects a provider by name/config
                            and platform, wrapping it so a TTS failure can never
                            be fatal to the agent/task system.
  * ``register_tts_provider`` - registers a new backend by name, so Piper /
                            ElevenLabs / a WinRT OneCore provider can be added
                            LATER without touching the Agent, session, or
                            controller. (Those backends are NOT added here.)

The Agent never imports Windows SAPI: it goes through this interface only. TTS
has zero authority over tasks, tools, RiskGate, KillSwitch, filesystem policy,
or confirmations - it just turns a string into audio.
"""
from __future__ import annotations

import sys
from typing import Callable

from void.voice.adapters import (
    SapiTTS, TTS, TTSError, VoiceDependencyError,
)

# The provider interface is the existing TTS base - one hierarchy, clear name.
TTSProvider = TTS


class NullTTS(TTS):
    """A provider that produces no audio and never fails.

    Used as the safe default off-Windows, when the optional voice stack is
    absent, or when speech output is disabled. Records what it was asked to say
    so callers/tests can observe intent without a speaker.
    """

    def __init__(self):
        self.spoke: list[str] = []
        self.stops = 0

    @property
    def is_speaking(self) -> bool:
        return False

    def speak(self, text: str) -> None:
        self.spoke.append(text)

    def stop(self) -> None:
        self.stops += 1


class _ResilientTTS(TTS):
    """Wraps a provider so a TTS failure is non-fatal to the core system.

    ``stop`` and ``is_speaking`` never raise (``stop`` is on the kill-switch
    path and must always be safe). ``speak`` normalizes any backend error to
    ``TTSError`` so the session's existing TTS handling (announce + return to
    IDLE) applies uniformly, and no provider-specific or dependency error ever
    escapes into the agent/task layer.
    """

    def __init__(self, delegate: TTS, name: str):
        self.delegate = delegate
        self.name = name

    @property
    def is_speaking(self) -> bool:
        try:
            return bool(self.delegate.is_speaking)
        except Exception:
            return False

    def speak(self, text: str) -> None:
        try:
            self.delegate.speak(text)
        except TTSError:
            raise
        except Exception as exc:   # incl. VoiceDependencyError from a lazy load
            raise TTSError(str(exc)) from exc

    def stop(self) -> None:
        try:
            self.delegate.stop()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.delegate.close()
        except Exception:
            pass


# --- provider registry (the replaceable seam) ---------------------------
#
# name -> zero-arg factory. Add a backend here (or via register_tts_provider)
# and it becomes selectable by config "voice.tts_provider" with NO change to the
# Agent/session/controller. Future examples (not implemented in this step):
#   register_tts_provider("piper", lambda: PiperTTS(...))
#   register_tts_provider("elevenlabs", lambda: ElevenLabsTTS(...))
#   register_tts_provider("onecore", lambda: WinRTOneCoreTTS(...))
def _sapi_stream() -> TTS:
    """Imported lazily so this module keeps importing on a machine without the voice stack."""
    from void.voice.sapi_stream import SapiStreamTTS
    return SapiStreamTTS()


_PROVIDERS: dict[str, Callable[[], TTS]] = {
    # SAPI synthesis played through V.O.I.D's own PortAudio output. The default on Windows, because
    # SAPI's own device enumeration does not necessarily contain the endpoint the owner listens
    # through - measured on the owner's machine, it contained only the laptop speakers while the
    # USB-C earphones carrying their audio were absent from it entirely. See void/voice/sapi_stream.py.
    "sapi_stream": _sapi_stream,
    # SAPI rendering straight to its own chosen device. The previous default, kept as the rollback:
    # set voice.tts_provider to "sapi" to restore it.
    "sapi": SapiTTS,        # local Windows SAPI (SpVoice); surfaces installed voices
    "null": NullTTS,
}


def register_tts_provider(name: str, factory: Callable[[], TTS]) -> None:
    """Register a TTS backend under ``name`` (lower-cased)."""
    _PROVIDERS[str(name).strip().lower()] = factory


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def _default_provider_name() -> str:
    # Local Windows speech where available; a silent, safe null elsewhere.
    #
    # "sapi_stream" rather than "sapi": letting SAPI choose the output device sent speech to whatever
    # was in its legacy enumeration, which on the owner's machine did not include the endpoint they
    # were listening on - so V.O.I.D talked to the laptop speakers while they wore earphones and heard
    # nothing, with a perfectly healthy-looking log. Synthesising to memory and playing through the
    # audio layer the capture broker already uses follows the owner's actual Windows default.
    return "sapi_stream" if sys.platform.startswith("win") else "null"


def create_tts_provider(config=None, *, provider: str | None = None) -> TTS:
    """Build a TTS provider, resilient by construction.

    Resolution order for the provider name: explicit ``provider`` arg, then
    ``config.get("voice.tts_provider")`` (if a Config-like object is given),
    then the platform default ("sapi" on Windows, "null" otherwise). An unknown
    name or a provider that fails to construct falls back to :class:`NullTTS`,
    so this never raises for a missing/optional dependency.
    """
    name = provider
    if name is None and config is not None:
        try:
            name = config.get("voice.tts_provider", None)
        except Exception:
            name = None
    name = (name or _default_provider_name()).strip().lower()

    factory = _PROVIDERS.get(name)
    if factory is None:
        name, factory = "null", NullTTS
    try:
        delegate = factory()
    except Exception:
        # Construction should be cheap/lazy; if a backend still fails here, stay
        # silent rather than break voice startup.
        name, delegate = "null", NullTTS()

    # Apply the configured speaking rate (provider-agnostic; NullTTS ignores it).
    # SAPI Rate is -10..10 (0 = normal); a small positive value is moderately
    # faster without sounding robotic.
    if config is not None:
        try:
            rate = int(config.get("voice.tts_rate", 0))
        except Exception:
            rate = 0
        try:
            delegate.set_rate(rate)
        except Exception:
            pass
    return _ResilientTTS(delegate, name)
