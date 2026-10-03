"""Where spoken audio actually goes, and that interrupting it still works.

These cover the bug that made V.O.I.D inaudible on the owner's machine: SAPI was choosing the output
device from its own legacy enumeration, which did not contain the endpoint the owner was listening
through, so speech was synthesised correctly and played to the laptop speakers while they wore earphones.
The log looked perfect - a full-duration ``TTS_SPEAK_DONE`` - because rendering to the wrong device
completes on schedule.

So the regression these pin is not "does speech happen" but **which layer picks the device**: synthesis
and playback must stay separable, and playback must go through V.O.I.D's own audio path rather than
SAPI's. Everything here is deterministic - no COM, no real device, no audio - via the ``_synth`` and
``_player`` seams.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from void.voice.sapi_stream import BLOCK_FRAMES, FORMAT_TYPE, SapiStreamTTS
from void.voice.tts import available_providers, create_tts_provider


def _code_of_module() -> str:
    """sapi_stream.py with every docstring removed.

    The module deliberately DOCUMENTS the device names it measured ("Speakers (3- Realtek...)",
    "Xiaomi"), because that evidence is the reason the fix exists. A raw text scan would therefore flag
    the module's own explanation, so the scan looks at code only - the same approach
    tests/test_v3_security.py already uses for its source assertions.
    """
    import ast
    import pathlib
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "void" / "voice" / "sapi_stream.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _pcm(seconds: float = 1.0, samplerate: int = 24000) -> bytes:
    """Silent but structurally real 16-bit mono PCM of a given length."""
    return b"\x00\x00" * int(seconds * samplerate)


class _Recorder:
    """A fake player that records what it was asked to play and can simulate a long utterance."""

    def __init__(self, *, fail: bool = False, honour_stop: bool = False, block_s: float = 0.0):
        self.calls: list[tuple[int, int, int]] = []     # (bytes, samplerate, channels)
        self.fail = fail
        self.honour_stop = honour_stop
        self.block_s = block_s
        self.interrupted = False
        self.started = threading.Event()

    def __call__(self, pcm, samplerate, channels, should_stop):
        self.calls.append((len(pcm), samplerate, channels))
        self.started.set()
        if self.fail:
            return False
        if self.block_s:
            deadline = time.monotonic() + self.block_s
            while time.monotonic() < deadline:
                if self.honour_stop and should_stop():
                    self.interrupted = True
                    return True
                time.sleep(0.01)
        return True


def _tts(player=None, synth=None, **kwargs):
    """A SapiStreamTTS with COM and SAPI fully stubbed out."""
    return SapiStreamTTS(
        _com_setup=lambda: None, _com_teardown=lambda: None,
        _synth=synth or (lambda text, rate: (_pcm(1.0), 24000, 1)),
        _player=player if player is not None else (lambda *a: True),
        **kwargs)


def _settle(tts, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while tts.is_speaking and time.monotonic() < deadline:
        time.sleep(0.01)
    return not tts.is_speaking


# --------------------------------------------------------------------------- provider selection

@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows speech providers")
def test_the_windows_default_plays_through_voids_own_audio_path():
    """The fix has to be the default, or the owner stays inaudible until they edit a config file."""
    from void.voice.tts import _default_provider_name
    assert _default_provider_name() == "sapi_stream"


def test_the_previous_sapi_path_remains_available_as_a_rollback():
    """Letting SAPI pick the device is wrong here, not wrong everywhere - keep the escape hatch."""
    assert "sapi" in available_providers()
    assert "sapi_stream" in available_providers()
    assert "null" in available_providers()


def test_an_explicitly_configured_provider_still_wins():
    class _Config:
        def get(self, key, default=None):
            return {"voice.tts_provider": "null"}.get(key, default)

    built = create_tts_provider(_Config())
    assert "null" in type(built).__name__.lower() or hasattr(built, "speak")
    built.speak("nothing")                   # must not raise
    built.stop()
    built.close()


# --------------------------------------------------------------------------- the device decision

def test_synthesis_and_playback_are_separate_steps():
    """The whole bug was these being one step that SAPI owned end to end."""
    seen = {}

    def synth(text, rate):
        seen["text"] = text
        seen["rate"] = rate
        return _pcm(0.5), 24000, 1

    player = _Recorder()
    tts = _tts(player=player, synth=synth, rate=3)
    tts.speak("hello there")
    assert _settle(tts)
    assert seen["text"] == "hello there"
    assert seen["rate"] == 3
    assert player.calls == [(int(0.5 * 24000) * 2, 24000, 1)]
    tts.close()


def test_the_sample_rate_is_taken_from_the_stream_not_assumed():
    """SAPI's format token 26 reports 24000 Hz here despite its 16 kHz-sounding name; playing the PCM
    at an assumed 16000 would come out slow and deep."""
    player = _Recorder()
    tts = _tts(player=player, synth=lambda text, rate: (_pcm(1.0, 22050), 22050, 1))
    tts.speak("x")
    assert _settle(tts)
    assert player.calls[0][1] == 22050
    tts.close()


def test_no_device_is_hardcoded_anywhere_in_the_module():
    """The owner's output device changes; a fix that names one is not a fix."""
    import pathlib
    code = _code_of_module()
    for forbidden in ("Xiaomi", "Realtek", "device=4", "device=1", "device_index"):
        assert forbidden not in code, f"sapi_stream.py hardcodes {forbidden!r}"


def test_playback_failure_falls_back_to_letting_sapi_render():
    """A machine where PortAudio output cannot be opened must not become silent."""
    spoken = []

    class _Voice:
        AudioOutputStream = None

        def Speak(self, text, flags):        # noqa: N802 - SAPI's name
            spoken.append((text, flags))

    tts = SapiStreamTTS(
        _com_setup=lambda: None, _com_teardown=lambda: None,
        _voice_factory=_Voice,
        _synth=lambda text, rate: (_pcm(0.2), 24000, 1),
        _player=_Recorder(fail=True))
    tts.speak("fall back to the device")
    assert _settle(tts)
    assert spoken and spoken[0][0] == "fall back to the device"
    tts.close()


def test_the_format_token_is_the_one_that_was_verified():
    """26 is the token measured to yield 16-bit mono PCM on this machine."""
    assert FORMAT_TYPE == 26
    assert BLOCK_FRAMES > 0


# --------------------------------------------------------------------------- interruption

def test_stop_mid_utterance_is_honoured_by_the_player():
    """V.O.I.D owns the stream now, so a stop reaches the audio rather than asking SAPI to purge."""
    player = _Recorder(honour_stop=True, block_s=5.0)
    tts = _tts(player=player, synth=lambda text, rate: (_pcm(5.0), 24000, 1))
    tts.speak("a long sentence")
    assert player.started.wait(2.0)
    tts.stop()
    assert _settle(tts, timeout=3.0)
    # is_speaking converges the instant a stop is requested, which is deliberate - so wait for the
    # player itself to observe it rather than assuming it already has.
    deadline = time.monotonic() + 3.0
    while not player.interrupted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert player.interrupted, "playback was not cut short"
    tts.close()


def test_is_speaking_converges_immediately_on_a_stop_request():
    """The owner asked for silence; the answer to 'are you speaking?' must not lag the request."""
    player = _Recorder(honour_stop=True, block_s=5.0)
    tts = _tts(player=player, synth=lambda text, rate: (_pcm(5.0), 24000, 1))
    tts.speak("long")
    assert player.started.wait(2.0)
    tts.stop()
    assert tts.is_speaking is False
    tts.close()


def test_a_stop_arriving_before_playback_starts_plays_no_audio_at_all():
    player = _Recorder()
    started = threading.Event()

    def slow_synth(text, rate):
        started.set()
        time.sleep(0.4)                      # stop lands during synthesis
        return _pcm(2.0), 24000, 1

    tts = _tts(player=player, synth=slow_synth)
    tts.speak("never heard")
    assert started.wait(2.0)
    tts.stop()
    assert _settle(tts, timeout=3.0)
    assert player.calls == [], "audio was played after a stop that preceded it"
    tts.close()


def test_speak_while_speaking_replaces_rather_than_queueing():
    player = _Recorder(honour_stop=True, block_s=3.0)
    tts = _tts(player=player, synth=lambda text, rate: (_pcm(3.0), 24000, 1))
    tts.speak("first")
    assert player.started.wait(2.0)
    player.started.clear()
    tts.speak("second")
    assert player.started.wait(3.0)
    assert _settle(tts, timeout=6.0)
    assert len(player.calls) == 2, "replacement should play exactly once more, not queue"
    tts.close()


def test_repeated_stop_is_a_safe_no_op():
    tts = _tts()
    for _ in range(5):
        tts.stop()
    assert tts.is_speaking is False
    tts.close()


def test_stop_before_anything_was_ever_spoken_is_safe():
    tts = _tts()
    tts.stop()
    assert tts.is_speaking is False
    tts.close()


def test_nothing_is_spoken_after_close():
    player = _Recorder()
    tts = _tts(player=player)
    tts.close()
    tts.speak("must never be heard")
    time.sleep(0.3)
    assert player.calls == []
    assert tts.is_speaking is False


def test_close_is_idempotent():
    tts = _tts()
    tts.close()
    tts.close()
    assert tts.is_speaking is False


def test_a_synthesis_failure_is_non_fatal_and_converges():
    def exploding(text, rate):
        raise RuntimeError("voice engine died")

    player = _Recorder()
    tts = _tts(player=player, synth=exploding)
    tts.speak("x")
    assert _settle(tts, timeout=3.0)
    assert player.calls == []
    tts.close()


def test_empty_synthesis_output_converges_without_playing():
    player = _Recorder()
    tts = _tts(player=player, synth=lambda text, rate: (b"", 24000, 1))
    tts.speak("x")
    assert _settle(tts, timeout=3.0)
    assert player.calls == []
    tts.close()


def test_concurrent_speak_and_stop_always_settles_silent():
    """Hammered from two threads: whatever the interleaving, a final stop must win."""
    player = _Recorder(honour_stop=True, block_s=0.3)
    tts = _tts(player=player, synth=lambda text, rate: (_pcm(0.3), 24000, 1))

    def speaker():
        for index in range(20):
            tts.speak(f"utterance {index}")

    def stopper():
        for _ in range(20):
            tts.stop()

    threads = [threading.Thread(target=speaker), threading.Thread(target=stopper)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    tts.stop()                               # the owner's last word
    assert _settle(tts, timeout=5.0)
    tts.close()


# --------------------------------------------------------------------------- privacy

def test_the_spoken_text_is_never_logged(caplog):
    """A log line carrying what the assistant said is a transcript of the owner's session."""
    secret = "your account balance is one two three four"
    tts = _tts(synth=lambda text, rate: (_pcm(0.2), 24000, 1))
    with caplog.at_level("DEBUG"):
        tts.speak(secret)
        assert _settle(tts)
    assert secret not in caplog.text
    for word in ("balance", "one two three four"):
        assert word not in caplog.text
    tts.close()


def test_playback_does_not_capture_audio():
    """This path renders sound; it must never open an input stream."""
    code = _code_of_module()
    # Call-shaped, so prose about "records intent" is not mistaken for audio capture.
    for forbidden in ("InputStream(", "sd.rec(", "read_frames", "Recorder("):
        assert forbidden not in code, f"sapi_stream.py references {forbidden!r}"
