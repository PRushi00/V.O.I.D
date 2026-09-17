"""Tests for void.runtime.diagnostics: the ONE shared implementation behind
every launcher's lifecycle logging (autostart, the `void voice` command, and
the opt-in UI launchers). Covers the actual gap found while investigating a
post-reboot "is the microphone getting audio" report: `void voice`, run
directly, previously installed no handler at all, so its own INFO-level
lifecycle markers (AUDIO_BROKER_STARTED, WAKE_ARMED, ...) were silently
dropped rather than landing in ~/.void/void.log."""
from __future__ import annotations

import logging
import sys

import pytest

from void.runtime import diagnostics


def _fake_config_module(state_dir):
    """A minimal stand-in for the void.config MODULE (not just the class):
    diagnostics does `from void.config import Config`, which needs an
    attribute named Config on whatever sys.modules["void.config"] resolves to."""
    class _Config:
        @staticmethod
        def load():
            class _Loaded:
                @staticmethod
                def state_dir():
                    return state_dir
            return _Loaded()

    class _Module:
        Config = _Config

    return _Module()


@pytest.fixture(autouse=True)
def _clean_root_logger():
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
    root.setLevel(before_level)


def test_install_background_logging_writes_to_state_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "void.config", _fake_config_module(tmp_path))
    path = diagnostics.install_background_logging()
    assert path == tmp_path / "void.log"
    logging.getLogger("void.somewhere").info("hello")
    assert path.exists()
    assert "hello" in path.read_text(encoding="utf-8")


def test_install_background_logging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "void.config", _fake_config_module(tmp_path))
    first = diagnostics.install_background_logging()
    second = diagnostics.install_background_logging()
    assert first is not None and second is None    # second call is a no-op
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if getattr(h, "_void_bg", False)]
    assert len(file_handlers) == 1                  # never double-attached


def test_install_background_logging_falls_back_when_config_unavailable(monkeypatch):
    def _broken_import(name, *a, **k):
        if name == "void.config":
            raise ImportError("boom")
        return real_import(name, *a, **k)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", _broken_import)
    path = diagnostics.install_background_logging()
    assert path is not None and path.name == "void.log"   # tempfile fallback, never raises


def test_install_console_diagnostics_noop_when_no_stderr(monkeypatch):
    # The console-less pythonw autostart path: sys.stderr is None. Must not
    # raise, and must not attach a handler that would crash on first emit.
    monkeypatch.setattr(diagnostics.sys, "stderr", None)
    diagnostics.install_console_diagnostics()
    root = logging.getLogger()
    assert not any(getattr(h, "_void_console", False) for h in root.handlers)


def test_install_console_diagnostics_attaches_once():
    diagnostics.install_console_diagnostics()
    diagnostics.install_console_diagnostics()
    root = logging.getLogger()
    console_handlers = [h for h in root.handlers if getattr(h, "_void_console", False)]
    assert len(console_handlers) == 1


# --- regression: tests must never contaminate the real production log ----
#
# Root cause of the actual contamination found in a real ~/.void/void.log:
# tests/test_voice_only_runtime.py's test_headless_voice_command_does_not_
# require_ui_modules calls the real cli.cmd_voice() (only Assistant and
# VoiceController are faked, not Config). cmd_voice() calls
# install_background_logging() unconditionally, which - before this fix -
# resolved the REAL ~/.void/void.log and attached a FileHandler to the root
# logger for the rest of that pytest PROCESS. Every later test's ordinary
# logging (including deliberately-raised test exceptions like
# RuntimeError("boom") / RuntimeError("backend gone"), and repeated
# synthetic WAKE_DETECTED/COMMAND_CAPTURE_STARTED/... bursts from the voice
# integration tests) then landed in the SAME real file, indistinguishable
# from genuine hardware evidence.

def test_never_writes_to_real_production_log_path_when_config_is_unmocked(monkeypatch):
    import void.config as real_config_module
    monkeypatch.setitem(sys.modules, "void.config", real_config_module)  # explicitly real
    assert diagnostics._running_under_pytest()      # sanity: this IS a pytest process
    path = diagnostics.install_background_logging()
    assert path is not None
    real_log = diagnostics._real_production_log_path()
    assert path != real_log

    marker = "REGRESSION_TEST_SYNTHETIC_EVENT_MUST_NOT_REACH_PRODUCTION_LOG"
    logging.getLogger("void.somewhere").warning(marker)
    if real_log.exists():
        assert marker not in real_log.read_text(encoding="utf-8")


def test_guard_does_not_apply_when_not_running_under_pytest(tmp_path, monkeypatch):
    # The guard must be invisible to real production behavior (the actual
    # autostart / a real `void voice` process is never "under pytest").
    monkeypatch.setattr(diagnostics, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(diagnostics, "_real_production_log_path",
                        lambda: tmp_path / "void.log")
    monkeypatch.setitem(sys.modules, "void.config", _fake_config_module(tmp_path))
    path = diagnostics.install_background_logging()
    assert path == tmp_path / "void.log"    # used as-is, never redirected


def test_guard_does_not_apply_when_config_is_explicitly_redirected(tmp_path, monkeypatch):
    # A test that WANTS to exercise the real file-writing behavior (as the
    # tests above already do) redirects Config first - that path never
    # equals the real production path, so the guard is simply inert for it.
    monkeypatch.setitem(sys.modules, "void.config", _fake_config_module(tmp_path))
    path = diagnostics.install_background_logging()
    assert path == tmp_path / "void.log"
    assert path != diagnostics._real_production_log_path()
