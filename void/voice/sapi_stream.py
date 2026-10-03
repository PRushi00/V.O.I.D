"""Windows SAPI synthesis, played through V.O.I.D's own audio output path.

Why this module exists, measured on the owner's machine (2026-10-02).
:class:`void.voice.adapters.SapiTTS` lets SAPI render straight to a device, and SAPI chooses that device
from its OWN legacy enumeration (``HKLM\\SOFTWARE\\Microsoft\\Speech\\AudioOutput\\TokenEnums\\MMAudioOut``).
On this machine that enumeration contains exactly ONE token::

    FRESH SpVoice is bound to: Speakers (3- Realtek(R) Audio)
    SAPI enumerates 1 audio output token:
      [0] Speakers (3- Realtek(R) Audio)

while the endpoint the owner actually listens through - a USB-C earphone dongle that PortAudio reports as
the system default output - is **not in SAPI's list at all**. So speech was synthesised correctly and
rendered to the laptop's built-in speakers, while the owner, wearing earphones, heard nothing.

The logs looked perfect throughout, which is why this stayed invisible::

    TTS_SPEAK_STARTED device='Headphones (Xiaomi Type-C Earphones 1)' chars=502
    TTS_SPEAK_DONE                              <- 31.1 s later

Rendering to the wrong device still completes on schedule, and that device string was read back from the
token SAPI had bound, not from the endpoint carrying the owner's audio. A direct ``System.Speech`` test
sounded fine for the same reason: it was audible from the speakers.

The fix is to stop letting SAPI pick the device. SAPI synthesises into a memory stream - offline,
measured 0.01 s for 4.7 s of speech, no device touched - and the resulting PCM is played through
``sounddevice``, the same PortAudio layer the capture broker already depends on, which enumerates modern
endpoints and follows the Windows default. No new dependency, no hardcoded device: the owner's choice in
Windows *is* PortAudio's default output, so honouring one honours the other.

Three things improve as a side effect:

* **Interruption gets stronger, not weaker.** V.O.I.D owns the output stream, so a stop ``abort()``s it
  and discards buffered frames immediately instead of asking SAPI to purge.
* **The logged device becomes true.** It now reports the PortAudio device actually opened, which is the
  fact whose absence let a wrong-device bug look like a working one.
* **Synthesis and playback are separable**, so a failure in either is distinguishable in the log.

Lifecycle discipline is deliberately identical to :class:`~void.voice.adapters.SapiTTS`: one dedicated
COM-owning worker thread, a single latest-command slot guarded by one lock, a monotonic sequence, and the
same semantics - speak-while-speaking REPLACES with no queue, a stop arriving before playback starts
starts no audio at all, and a repeated stop is a safe no-op. Callers never touch COM or PortAudio.

If playback cannot be opened at all (no output device, PortAudio absent), this falls back to letting SAPI
render to its own device - the previous behaviour - so a machine where this path is unavailable is no
worse off than before rather than silent.
"""
from __future__ import annotations

import logging
import threading

from void.voice.adapters import TTS, TTSError, VoiceDependencyError

_log = logging.getLogger("void.voice.adapters")

#: SAPI audio format token for 16-bit mono PCM. The ACTUAL sample rate is read back from the stream
#: rather than assumed: this token reports 24000 Hz here, not the 16000 its common name suggests.
FORMAT_TYPE = 26

#: Frames written per block. At 24 kHz this is ~43 ms, which bounds how long a stop takes to be heard
#: while keeping the write loop cheap.
BLOCK_FRAMES = 1024


class SapiStreamTTS(TTS):
    """SAPI as the synthesiser, PortAudio as the output. See the module docstring for why.

    The ``_voice_factory`` / ``_com_setup`` / ``_com_teardown`` / ``_synth`` / ``_player`` parameters are
    internal seams for deterministic tests (inject a fake SpVoice, skip real COM, substitute synthesis or
    playback); production constructs ``SapiStreamTTS()`` with no arguments.
    """

    _SVSF_DEFAULT = 0              # synchronous synthesis into the stream

    def __init__(self, *, rate: int = 0, _voice_factory=None, _com_setup=None,
                 _com_teardown=None, _synth=None, _player=None):
        self._rate = max(-10, min(10, int(rate)))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._latest = None            # (kind, text, seq): the newest request wins
        self._seq = 0
        self._speaking = False
        self._shutdown = False
        self._closed = False
        self._started = False
        self._worker: threading.Thread | None = None
        self._start_error: Exception | None = None
        #: The live output stream, so a stop can abort it from the caller's thread.
        self._out = None
        self._out_lock = threading.Lock()
        # test seams (None in production)
        self._voice_factory = _voice_factory
        self._com_setup = _com_setup
        self._com_teardown = _com_teardown
        self._synth = _synth           # (text, rate) -> (pcm_bytes, samplerate, channels)
        self._player = _player         # (pcm, samplerate, channels, should_stop) -> bool

    # --- public, provider-agnostic API --------------------------------
    def set_rate(self, rate: int) -> None:
        self._rate = max(-10, min(10, int(rate)))

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def speak(self, text: str) -> None:
        with self._lock:
            if self._closed:
                return                 # terminal: never speak after close
        self._ensure_worker()
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            self._latest = ("speak", text, self._seq)
            self._speaking = True      # optimistic: intent to speak
            self._idle.clear()
            self._wake.set()
        self._abort_playback()         # replace anything currently playing

    def stop(self) -> None:
        # Idempotent and non-blocking: records intent and cuts audio; never joins.
        with self._lock:
            if self._closed or not self._started:
                self._speaking = False
                return
            self._seq += 1
            self._latest = ("stop", None, self._seq)
            self._speaking = False     # converge immediately on request
            self._wake.set()
        self._abort_playback()

    def close(self) -> None:
        with self._lock:
            worker = self._worker
            self._closed = True
            self._shutdown = True
            self._speaking = False
            self._wake.set()
        self._abort_playback()
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        with self._lock:
            self._worker = None
            self._idle.set()

    # --- playback control (safe from any thread) ----------------------
    def _abort_playback(self) -> None:
        """Discard whatever is already buffered in the output device.

        ``abort`` rather than ``stop``: stop drains the buffer, so the owner keeps hearing the tail of the
        sentence they just interrupted; abort throws it away. Never raises - a stream that has already
        closed is exactly the state this wanted.
        """
        with self._out_lock:
            out = self._out
        if out is None:
            return
        try:
            out.abort()
        except Exception:              # noqa: BLE001 - already closed / backend gone
            pass

    def _should_stop(self) -> bool:
        with self._lock:
            return self._latest is not None or self._shutdown

    # --- worker bootstrap ---------------------------------------------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._started:
                if self._start_error is not None:
                    raise self._start_error
                return
            if self._voice_factory is None and self._synth is None:
                # Fail fast with a clean error if the dependency is absent.
                try:
                    import pythoncom  # noqa: F401
                    import win32com.client  # noqa: F401
                except ImportError as exc:
                    self._started = True
                    self._start_error = VoiceDependencyError(
                        "Windows TTS needs pywin32 (pip install -r requirements.txt)")
                    raise self._start_error from exc
            self._started = True
            self._ready.clear()
            self._worker = threading.Thread(
                target=self._run, name="void-tts-sapi-stream", daemon=True)
            self._worker.start()
        # Wait (bounded) for init so a COM failure surfaces synchronously.
        self._ready.wait(timeout=5.0)
        with self._lock:
            if self._start_error is not None:
                raise self._start_error

    def _run(self) -> None:
        setup = self._com_setup
        teardown = self._com_teardown
        if (setup is None or teardown is None) and self._synth is None:
            import pythoncom
            setup = setup or pythoncom.CoInitialize
            teardown = teardown or pythoncom.CoUninitialize
        try:
            if setup is not None:
                setup()
        except Exception as exc:       # noqa: BLE001
            with self._lock:
                self._start_error = TTSError(f"could not initialize COM: {exc}")
            self._ready.set()
            return
        try:
            self._ready.set()
            self._loop()
        finally:
            try:
                if teardown is not None:
                    teardown()
            except Exception:          # noqa: BLE001
                pass

    # --- the worker loop ----------------------------------------------
    def _loop(self) -> None:
        voice = None
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                if self._shutdown:
                    break
                cmd, self._latest = self._latest, None
            if cmd is None:
                continue
            kind, text, _seq = cmd
            if kind == "stop":
                with self._lock:
                    self._speaking = False
                    if self._latest is None:
                        self._idle.set()
                continue
            # kind == "speak": if a newer command already arrived, start nothing.
            with self._lock:
                superseded = self._latest is not None or self._shutdown
            if superseded:
                continue
            # A voice is needed for real synthesis, and also whenever a factory was injected - that is
            # what makes the device-fallback path reachable in a test that stubs synthesis.
            if voice is None and (self._synth is None or self._voice_factory is not None):
                try:
                    voice = self._make_voice()
                except Exception as exc:   # noqa: BLE001
                    _log.warning("TTS_VOICE_INIT_FAILED %s", type(exc).__name__)
                    self._settle()
                    continue
            self._utter(voice, text)

    def _make_voice(self):
        if self._voice_factory is not None:
            return self._voice_factory()
        import win32com.client
        return win32com.client.Dispatch("SAPI.SpVoice")

    def _settle(self) -> None:
        """Converge to not-speaking unless a newer command is already waiting."""
        with self._lock:
            if self._latest is None:
                self._speaking = False
                self._idle.set()

    def _utter(self, voice, text: str) -> None:
        """Synthesise ``text`` offline, then play it. Runs only on the worker thread."""
        try:
            pcm, samplerate, channels = self._synthesize(voice, text)
        except Exception as exc:       # noqa: BLE001 - the CLASS only, never the text
            _log.warning("TTS_SYNTH_FAILED %s", type(exc).__name__)
            self._settle()
            return
        if not pcm:
            self._settle()
            return
        if self._should_stop():        # stopped between synthesis and playback: no audio at all
            self._settle()
            return
        if not self._play(pcm, samplerate, channels):
            # Playback unavailable: fall back to letting SAPI render to its own device, which is the
            # pre-fix behaviour. Worse than this path, but better than silence.
            _log.info("TTS_PLAYBACK_UNAVAILABLE falling_back=sapi_device")
            self._speak_on_device(voice, text)
        self._settle()

    def _synthesize(self, voice, text: str):
        """``(pcm_bytes, samplerate, channels)`` for ``text``. No device is touched."""
        if self._synth is not None:
            return self._synth(text, self._rate)
        import win32com.client
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        audio_format = win32com.client.Dispatch("SAPI.SpAudioFormat")
        audio_format.Type = FORMAT_TYPE
        stream.Format = audio_format
        try:
            previous = voice.AudioOutputStream
        except Exception:              # noqa: BLE001 - not every build exposes a readable default
            previous = None
        voice.AudioOutputStream = stream
        try:
            try:
                voice.Rate = int(self._rate)
            except Exception:          # noqa: BLE001
                pass
            voice.Speak(text, self._SVSF_DEFAULT)      # synchronous, into the stream
            wave = stream.Format.GetWaveFormatEx()
            samplerate = int(wave.SamplesPerSec)
            channels = max(1, int(wave.Channels))
            payload = bytes(bytearray(stream.GetData()))
        finally:
            # Release the stream so the SpVoice does not hold a finished buffer alive.
            try:
                voice.AudioOutputStream = previous
            except Exception:          # noqa: BLE001
                pass
        return payload, samplerate, channels

    def _play(self, pcm: bytes, samplerate: int, channels: int) -> bool:
        """Play ``pcm`` through PortAudio's default output. True when playback was attempted.

        Written in bounded blocks so a stop is honoured within roughly one block rather than at the end of
        the sentence, and aborted by :meth:`_abort_playback` from any thread.
        """
        if self._player is not None:
            return bool(self._player(pcm, samplerate, channels, self._should_stop))
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            return False
        frames = np.frombuffer(pcm, dtype="<i2")
        if channels > 1:
            frames = frames.reshape(-1, channels)
        try:
            out = sd.OutputStream(samplerate=samplerate, channels=channels, dtype="int16")
            out.start()
        except Exception as exc:       # noqa: BLE001 - no device / backend refused
            _log.warning("TTS_OUTPUT_OPEN_FAILED %s", type(exc).__name__)
            return False
        with self._out_lock:
            self._out = out
        try:
            device_name = "?"
            try:
                device_name = sd.query_devices(out.device)["name"]
            except Exception:          # noqa: BLE001
                pass
            # The device PortAudio ACTUALLY opened, plus the audio shape. Never the text itself.
            _log.info("TTS_SPEAK_STARTED device=%r samplerate=%d samples=%d",
                      device_name, samplerate, len(frames))
            total = len(frames)
            position = 0
            interrupted = False
            while position < total:
                if self._should_stop():
                    interrupted = True
                    break
                block = frames[position: position + BLOCK_FRAMES]
                try:
                    out.write(block)
                except Exception:      # noqa: BLE001 - aborted mid-write is the normal stop path
                    interrupted = True
                    break
                position += len(block)
            if interrupted:
                _log.info("TTS_SPEAK_INTERRUPTED played_s=%.2f",
                          position / float(samplerate or 1))
            else:
                _log.info("TTS_SPEAK_DONE")
            return True
        finally:
            with self._out_lock:
                self._out = None
            for release in (getattr(out, "abort", None), getattr(out, "close", None)):
                try:
                    if release is not None:
                        release()
                except Exception:      # noqa: BLE001
                    pass

    def _speak_on_device(self, voice, text: str) -> None:
        """Last resort: let SAPI render to its own device (the pre-fix path)."""
        if voice is None:
            return
        try:
            voice.Speak(text, 0)
        except Exception as exc:       # noqa: BLE001
            _log.warning("TTS_DEVICE_FALLBACK_FAILED %s", type(exc).__name__)
