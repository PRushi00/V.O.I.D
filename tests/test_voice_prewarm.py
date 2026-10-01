"""Warming the application catalog at start-up.

Measured on the owner's machine (docs/VOICE_REGRESSION_2026-09-24.md): the first "open <app>" after a restart paid
473 ms for the initial discovery, which dominated every other cost in the command path. It is pure start-up cost -
the catalog is identical whenever it is built - so it is built before anyone is waiting on it, exactly as the
speech model already was.
"""
import pytest


def test_the_application_catalog_is_warmed_at_start_like_the_speech_model(tmp_path, monkeypatch):
    """The first "open <app>" after a restart paid 473 ms for the initial discovery. That is pure start-up cost:
    the catalog is identical whenever it is built, so it is built before anyone is waiting on it."""
    import threading

    from void.voice.runtime import VoiceController

    built = threading.Event()

    class _Cat:
        def entries(self):
            built.set()
            return []

    class _Fast:
        catalog = _Cat()

    class _Assistant:
        _fast = _Fast()

    class _Session:
        _assistant = _Assistant()

    c = VoiceController.__new__(VoiceController)
    c._session = _Session()
    c._prewarm_apps()
    assert built.wait(timeout=5), "the catalog was not warmed at start"


def test_a_runtime_without_a_catalog_still_starts(tmp_path):
    from void.voice.runtime import VoiceController

    class _Session:
        pass

    c = VoiceController.__new__(VoiceController)
    c._session = _Session()
    c._prewarm_apps()                    # must not raise


def test_start_warms_the_application_catalog_and_not_only_the_speech_model():
    """Calling the warm-up is the point of it: exercising the method alone would still pass if start() stopped
    invoking it, which is exactly how the first command silently went back to paying 473 ms."""
    import inspect

    from void.voice.runtime import VoiceController

    src = inspect.getsource(VoiceController.start)
    assert "self._prewarm_stt()" in src and "self._prewarm_apps()" in src


def test_a_failing_prewarm_is_not_an_unhandled_thread_exception():
    """A background warm-up that dies loudly would print a traceback into the owner's log on every start."""
    import threading

    from void.voice.runtime import VoiceController

    escaped = []
    done = threading.Event()

    class _Cat:
        def entries(self):
            try:
                raise RuntimeError("discovery exploded")
            finally:
                done.set()

    class _Session:
        _assistant = type("A", (), {"_fast": type("F", (), {"catalog": _Cat()})()})()

    previous = threading.excepthook
    threading.excepthook = lambda args: escaped.append(args)
    try:
        c = VoiceController.__new__(VoiceController)
        c._session = _Session()
        c._prewarm_apps()
        assert done.wait(timeout=5)
        for t in threading.enumerate():
            if t.name == "void-apps-warmup":
                t.join(timeout=5)
    finally:
        threading.excepthook = previous
    assert escaped == [], f"the warm-up thread died with {escaped!r}"


def test_a_catalog_that_fails_to_warm_never_stops_the_runtime():
    import threading

    from void.voice.runtime import VoiceController

    tried = threading.Event()

    class _Cat:
        def entries(self):
            tried.set()
            raise RuntimeError("discovery exploded")

    class _Session:
        _assistant = type("A", (), {"_fast": type("F", (), {"catalog": _Cat()})()})()

    c = VoiceController.__new__(VoiceController)
    c._session = _Session()
    c._prewarm_apps()
    assert tried.wait(timeout=5)
