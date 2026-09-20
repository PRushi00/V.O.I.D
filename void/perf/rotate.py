"""Size-bounded log rotation that works with SEVERAL writer processes on Windows.

V.O.I.D's runtime, ``device serve`` and one-shot CLI commands all append to the
same ``void.log``. The stock ``RotatingFileHandler`` rotates by RENAMING the file,
which fails on Windows (WinError 32) whenever another process holds it open - so in
the real deployment the log would silently never rotate (D-10 unfixed).

This handler rotates by COPY-THEN-TRUNCATE instead: shift ``.N`` backups, copy the
live file to ``.1``, truncate the live file in place. Every writer keeps its own
append-mode handle, so nobody has to re-open anything. A few lines written during the
copy may be lost from the backup; that is acceptable for diagnostics. Failure to rotate
never raises (diagnostics must never break the process) - it backs off and retries.
"""
from __future__ import annotations

import logging.handlers
import os
import shutil
import time

_RETRY_AFTER_FAILURE_S = 60.0


class CopyTruncateRotatingHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._next_attempt = 0.0

    def shouldRollover(self, record) -> bool:
        if time.monotonic() < self._next_attempt:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        base = self.baseFilename
        try:
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    src, dst = f"{base}.{i}", f"{base}.{i + 1}"
                    if os.path.exists(src):
                        os.replace(src, dst)
                shutil.copyfile(base, f"{base}.1")
            with open(base, "r+b") as fh:
                fh.truncate(0)
        except OSError:
            self._next_attempt = time.monotonic() + _RETRY_AFTER_FAILURE_S
