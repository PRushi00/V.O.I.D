"""``void doctor``: a strictly READ-ONLY health report.

It never repairs, restarts, deletes, migrates or creates anything: no directory is
created, SQLite is opened with ``mode=ro``, and the only network activity is one TLS
handshake to a gateway on loopback (and only if a gateway address file exists).

What it is for: the D-01 failure - a process that is alive but deaf - was invisible from
outside for hours. The runtime now writes a heartbeat (``void.perf.health``); this reads
it and says plainly what is wrong.
"""
from __future__ import annotations

import socket
import sqlite3
import ssl
import time
from dataclasses import dataclass
from pathlib import Path

from void.perf import health

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
MIC_SILENCE_WARN_S = 8.0          # same threshold the runtime's own supervisor uses
STALE_TASK_AGE_S = 900.0          # same threshold TaskStore.sweep_stale uses
LOG_WARN_BYTES = 15 * 1024 * 1024  # 3x the default 5 MB rotation size


@dataclass
class Check:
    name: str
    status: str
    detail: str


def exit_code(checks: list[Check]) -> int:
    if any(c.status == FAIL for c in checks):
        return 2
    if any(c.status == WARN for c in checks):
        return 1
    return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _check_heartbeat(state_dir: Path, now: float, stale_after: float) -> list[Check]:
    rec = health.read_health(state_dir)
    if rec is None:
        return [Check("runtime heartbeat", WARN,
                      "no heartbeat file: the voice runtime is not running, or predates V2.0")]
    age = now - float(rec.get("ts", 0))
    checks: list[Check] = []
    if age > stale_after:
        checks.append(Check("runtime heartbeat", FAIL,
                            f"STALE: last heartbeat {age:.0f}s ago (pid {rec.get('pid')}) - "
                            "the runtime is hung, dead or was stopped"))
    else:
        checks.append(Check("runtime heartbeat", OK,
                            f"fresh ({age:.0f}s ago, pid {rec.get('pid')}, state {rec.get('state')})"))
    mic = rec.get("mic") or {}
    if mic.get("supervised"):
        if mic.get("broker_closed"):
            checks.append(Check("microphone", INFO, "closed by owner shutdown"))
        elif not mic.get("broker_running"):
            checks.append(Check("microphone", FAIL,
                                f"NOT RUNNING (stream failed to restart; recovery attempts "
                                f"{mic.get('recovery_attempts')}; retrying every ~{mic.get('backoff_s')}s)"))
        elif mic.get("healthy") is False:
            checks.append(Check("microphone", FAIL,
                                f"UNHEALTHY: no audio frames for {mic.get('seconds_since_frame')}s "
                                f"(recovery attempts {mic.get('recovery_attempts')}) - wake word is deaf"))
        elif (mic.get("seconds_since_frame") or 0) > MIC_SILENCE_WARN_S:
            checks.append(Check("microphone", WARN,
                                f"no audio frames for {mic['seconds_since_frame']:.1f}s"))
        else:
            checks.append(Check("microphone", OK, "delivering audio frames"))
    else:
        checks.append(Check("microphone", INFO, "not supervised (no audio broker in this runtime)"))
    wake = rec.get("wake") or {}
    if wake.get("configured"):
        if wake.get("broken"):
            checks.append(Check("wake word", FAIL, "wake detector disabled after a start failure (PTT only)"))
        else:
            checks.append(Check("wake word", OK, "armed" if wake.get("armed") else "configured (not armed right now)"))
    return checks


def _check_gateway(state_dir: Path, probe: bool) -> list[Check]:
    from void.device.gateway import running_port     # local import: keeps doctor light

    port = running_port(state_dir)
    if port is None:
        return [Check("device gateway", INFO, "not running (opt-in; started only by `device serve`)")]
    if not probe:
        return [Check("device gateway", INFO, f"address file present (port {port}); probe skipped")]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE        # loopback liveness probe only; carries no data, no credentials
    t0 = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as raw, \
                ctx.wrap_socket(raw, server_hostname="localhost"):
            pass
    except Exception as exc:               # noqa: BLE001 - the type is the diagnosis
        return [Check("device gateway", FAIL,
                      f"port {port} in address file but TLS handshake failed ({type(exc).__name__}) - "
                      "gateway stalled, stopped, or the file is stale")]
    return [Check("device gateway", OK,
                  f"answers TLS on 127.0.0.1:{port} in {(time.perf_counter() - t0) * 1000:.0f} ms")]


def _check_storage(state_dir: Path) -> list[Check]:
    checks = []
    log = state_dir / "void.log"
    if log.exists():
        size = _size(log)
        checks.append(Check("diagnostic log", WARN if size > LOG_WARN_BYTES else OK,
                            f"void.log {size / 1024 / 1024:.1f} MB"
                            + (" - larger than the rotation limit allows: is an old (pre-V2.0) process still writing it?"
                               if size > LOG_WARN_BYTES else "")))
    perf_dir = state_dir / "perf"
    if perf_dir.is_dir():
        total = sum(_size(p) for p in perf_dir.glob("perf.jsonl*"))
        checks.append(Check("perf stream", OK, f"{total / 1024:.0f} KB across {len(list(perf_dir.glob('perf.jsonl*')))} file(s)"))
    else:
        checks.append(Check("perf stream", INFO, "no perf data yet"))
    return checks


def _check_tasks(state_dir: Path, now: float) -> list[Check]:
    db = state_dir / "tasks.sqlite"
    if not db.exists():
        return [Check("task store", INFO, "no task database yet")]
    try:
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            total, = con.execute("SELECT COUNT(*) FROM tasks").fetchone()
            running, = con.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()
            stale, = con.execute("SELECT COUNT(*) FROM tasks WHERE status='running' AND updated_at < ?",
                                 (now - STALE_TASK_AGE_S,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return [Check("task store", WARN, f"could not read tasks.sqlite read-only ({type(exc).__name__})")]
    if stale:
        return [Check("task store", WARN,
                      f"{stale} task(s) stuck 'running' with no update for >{STALE_TASK_AGE_S / 60:.0f} min "
                      f"(of {total} total): `void tasks` will mark them paused")]
    return [Check("task store", OK, f"{total} task(s), {running} running")]


def run_doctor(state_dir, *, now: float | None = None, probe_gateway: bool = True,
               stale_after: float = health.STALE_AFTER_S) -> list[Check]:
    state_dir = Path(state_dir)
    now = time.time() if now is None else now
    if not state_dir.is_dir():
        return [Check("state directory", WARN,
                      f"{state_dir} does not exist: V.O.I.D has not run for this user (nothing was created)")]
    checks = [Check("state directory", OK, str(state_dir))]
    checks += _check_heartbeat(state_dir, now, stale_after)
    checks += _check_gateway(state_dir, probe_gateway)
    checks += _check_tasks(state_dir, now)
    checks += _check_storage(state_dir)
    return checks


def format_checks(checks: list[Check]) -> str:
    mark = {OK: "[ ok ]", INFO: "[info]", WARN: "[WARN]", FAIL: "[FAIL]"}
    return "\n".join(f"{mark[c.status]} {c.name:20s} {c.detail}" for c in checks)
