"""The runtime health heartbeat: a tiny JSON file the persistent runtime rewrites every
few seconds, so an EXTERNAL reader (``void doctor``) can tell "alive and hearing" from
"alive but deaf" from "not running" without attaching to the process.

Why it exists: in V1 a dead microphone left the process running and the tray looking
normal for hours (D-01) and nothing outside the process could see it.

Contents are counts, flags and timings only - never audio, transcripts or paths.
Written atomically (temp file + ``os.replace``) so a reader never sees a torn file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HEALTH_FILENAME = "health.json"
SCHEMA_VERSION = 1
DEFAULT_INTERVAL_S = 5.0
STALE_AFTER_S = 30.0


def write_health(state_dir, payload: dict) -> bool:
    """Atomically replace ``<state_dir>/health.json``. Never raises; False on failure."""
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        record = {"v": SCHEMA_VERSION, "ts": time.time(), "pid": os.getpid(), **payload}
        target = state_dir / HEALTH_FILENAME
        tmp = state_dir / f"{HEALTH_FILENAME}.tmp{os.getpid()}"
        tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def read_health(state_dir) -> dict | None:
    """The last heartbeat, or None if absent/unreadable. Read-only."""
    try:
        data = json.loads((Path(state_dir) / HEALTH_FILENAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
