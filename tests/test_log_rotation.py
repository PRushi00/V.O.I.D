"""D-10 (T0.6): the diagnostic log is size-bounded, including when SEVERAL processes
append to it (the runtime, ``device serve`` and CLI commands all share void.log)."""
import logging
import os
import sys
import types
from pathlib import Path

from void.perf.rotate import CopyTruncateRotatingHandler
from void.runtime import diagnostics


def _record(msg):
    return logging.LogRecord("void.test", logging.INFO, __file__, 1, msg, None, None)


def _handler(path, max_bytes=4000, backups=3):
    h = CopyTruncateRotatingHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def test_single_writer_rotates_and_stays_bounded(tmp_path):
    path = tmp_path / "void.log"
    h = _handler(path)
    for i in range(600):
        h.handle(_record(f"line {i:04d} " + "x" * 40))
    h.close()
    files = sorted(tmp_path.glob("void.log*"))
    assert 2 <= len(files) <= 4                                   # live + <=3 backups
    assert sum(f.stat().st_size for f in files) < 4 * 4000 + 1500


def test_two_writers_holding_the_file_open_still_rotate_without_error(tmp_path):
    """The Windows problem: rename-based rotation fails while another process holds the
    file. Two handlers on one file model two processes."""
    path = tmp_path / "void.log"
    a, b = _handler(path), _handler(path)
    errors = []
    for h in (a, b):
        h.handleError = lambda record, _e=errors: _e.append(record)   # any logging error is a failure
    for i in range(800):
        (a if i % 2 else b).handle(_record(f"w{i % 2} line {i:04d} " + "y" * 40))
    a.close()
    b.close()
    assert errors == []
    assert (tmp_path / "void.log.1").exists(), "log never rotated with two writers"
    assert sum(f.stat().st_size for f in tmp_path.glob("void.log*")) < 4 * 4000 + 3000


def test_backups_shift_in_order(tmp_path):
    path = tmp_path / "void.log"
    h = _handler(path, max_bytes=600, backups=3)
    for i in range(120):
        h.handle(_record(f"n{i:03d} " + "z" * 30))
    h.close()
    newest_in_backup1 = (tmp_path / "void.log.1").read_text(encoding="utf-8")
    oldest_kept = (tmp_path / "void.log.3").read_text(encoding="utf-8") if (tmp_path / "void.log.3").exists() else ""
    if oldest_kept:
        assert int(oldest_kept.split()[0][1:]) < int(newest_in_backup1.split()[0][1:])


def test_rotation_failure_never_raises_and_backs_off(tmp_path, monkeypatch):
    path = tmp_path / "void.log"
    h = _handler(path, max_bytes=500)
    calls = []
    real_copy = __import__("shutil").copyfile

    def failing_copy(*a, **k):
        calls.append(1)
        raise PermissionError("[WinError 32] in use")

    monkeypatch.setattr("void.perf.rotate.shutil.copyfile", failing_copy)
    for i in range(200):
        h.handle(_record(f"line {i} " + "q" * 40))                 # must not raise
    h.close()
    assert len(calls) == 1, "rotation was retried on every record instead of backing off"
    assert path.exists() and path.stat().st_size > 500             # kept writing


def test_install_background_logging_uses_the_bounded_handler_with_config_limits(tmp_path, monkeypatch):
    class _Cfg:
        def state_dir(self):
            return tmp_path

        def get(self, key, default=None):
            return {"logging.max_bytes": 200_000, "logging.backup_count": 2, "perf.enabled": True}.get(key, default)

    module = types.SimpleNamespace(Config=types.SimpleNamespace(load=lambda: _Cfg()))
    monkeypatch.setitem(sys.modules, "void.config", module)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = diagnostics.install_background_logging()
        assert path == tmp_path / "void.log"
        h = [x for x in root.handlers if getattr(x, "_void_bg", False)][0]
        assert isinstance(h, CopyTruncateRotatingHandler)
        assert (h.maxBytes, h.backupCount) == (200_000, 2)
        assert (tmp_path / "perf").is_dir()                        # perf stream configured next to the log
    finally:
        for x in list(root.handlers):
            if x not in before:
                root.removeHandler(x)
                x.close()


def test_defaults_apply_when_config_has_no_get():
    class _Bare:
        pass

    assert diagnostics._log_limits(_Bare()) == (5 * 1024 * 1024, 5)
    assert diagnostics._log_limits(None) == (5 * 1024 * 1024, 5)


def test_absurdly_small_configured_limits_are_clamped():
    class _Cfg:
        def get(self, key, default=None):
            return {"logging.max_bytes": 10, "logging.backup_count": 0}.get(key, default)

    max_bytes, backups = diagnostics._log_limits(_Cfg())
    assert max_bytes >= 64 * 1024 and backups >= 1
