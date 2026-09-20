"""T0.8: health heartbeat, ``void doctor`` and ``void perf report``.

The diagnostics exist so that a D-01-style failure (process alive, microphone dead) is
visible from OUTSIDE the process. They must also be strictly read-only: a diagnostic
that changes the machine it is diagnosing cannot be trusted or run casually."""
import json
import os
import random
import sqlite3
import time
from pathlib import Path

import pytest

from tests.test_voice_mic_recovery import _rig, _silent_rig, _tick
from void import cli, perf
from void.core.task import Status
from void.perf import doctor, health, report
from void.voice.runtime import _HEALTH_INTERVAL_S


def _tree(root: Path) -> dict:
    """path -> (size, mtime_ns) for everything under root: proof of 'nothing changed'."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, p.is_dir())
    return out


# ------------------------------------------------------------------ heartbeat file
def test_write_health_is_atomic_and_readable(tmp_path):
    assert health.write_health(tmp_path, {"state": "idle"}) is True
    rec = health.read_health(tmp_path)
    assert rec["state"] == "idle" and rec["pid"] == os.getpid() and rec["v"] == health.SCHEMA_VERSION
    assert [p.name for p in tmp_path.iterdir()] == ["health.json"], "no temp file may be left behind"


def test_write_health_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert health.write_health(blocker / "sub", {"a": 1}) is False      # parent is a file
    assert health.write_health(tmp_path, {"bad": object()}) is False    # not serialisable


def test_read_health_tolerates_absent_and_corrupt(tmp_path):
    assert health.read_health(tmp_path) is None
    (tmp_path / "health.json").write_text("{not json", encoding="utf-8")
    assert health.read_health(tmp_path) is None
    (tmp_path / "health.json").write_text("[1,2]", encoding="utf-8")
    assert health.read_health(tmp_path) is None


# ------------------------------------------------------------------ runtime heartbeat
def test_controller_writes_a_heartbeat_at_the_cadence_and_never_faster():
    ctrl, broker, backend, session, states, clock = _rig()
    beats = []
    ctrl._health_sink = beats.append
    broker.start()
    ctrl.poll_once()
    assert len(beats) == 1
    for _ in range(int(_HEALTH_INTERVAL_S) - 1):
        clock.advance(1.0)
        backend.emit()
        ctrl.poll_once()
    assert len(beats) == 1, "heartbeat written faster than its cadence"
    clock.advance(1.0)
    backend.emit()
    ctrl.poll_once()
    assert len(beats) == 2
    snap = beats[-1]
    assert snap["mic"]["supervised"] is True and snap["mic"]["broker_running"] is True
    assert snap["state"] == "idle"
    assert set(snap["wake"]) == {"configured", "armed", "broken", "capture_active"}


def test_a_failing_health_sink_cannot_break_the_monitor_tick():
    ctrl, broker, backend, session, states, clock = _rig()

    def boom(_payload):
        raise OSError("disk full")

    ctrl._health_sink = boom
    broker.start()
    ctrl.poll_once()                               # must not raise
    clock.advance(_HEALTH_INTERVAL_S + 1)
    ctrl.poll_once()


def test_heartbeat_reports_the_d01_state_as_not_running_not_closed():
    ctrl, broker, backend, _s, clock = _silent_rig(fail_on={2})
    beats = []
    ctrl._health_sink = beats.append
    ctrl.poll_once()                               # restart raises -> broker not running, not closed
    snap = beats[-1]["mic"]
    assert snap["broker_running"] is False and snap["broker_closed"] is False
    assert snap["healthy"] is False and snap["recovery_attempts"] == 1


def test_heartbeat_contains_only_counts_flags_and_timings():
    ctrl, broker, *_ = _rig()
    broker.start()
    blob = json.dumps(ctrl.health_snapshot())
    for word in ("transcript", "audio", "\\", "/", "token", "key"):
        assert word not in blob.replace("broker_", "").replace("seconds_since_frame", ""), word


def test_mic_transitions_are_emitted_to_perf(tmp_path):
    path = perf.configure(tmp_path / "perf")
    try:
        ctrl, broker, backend, _s, clock = _silent_rig(fail_on={2})
        ctrl.poll_once()
        _tick(ctrl, backend, clock, 300, stop_when_healthy=True)
    finally:
        perf.shutdown()
    states = [json.loads(x)["state"] for x in Path(path).read_text(encoding="utf-8").splitlines() if x]
    assert states[0] == "unavailable" and "recovery_attempt" in states and states[-1] == "recovered"


# ------------------------------------------------------------------ doctor
def _fresh_state(tmp_path, **mic):
    d = tmp_path / "state"
    d.mkdir()
    payload = {"state": "idle",
               "mic": {"supervised": True, "healthy": True, "broker_running": True, "broker_closed": False,
                       "seconds_since_frame": 0.2, "recovery_attempts": 0, "restart_pending": False,
                       "backoff_s": 2.0},
               "wake": {"configured": True, "armed": True, "broken": False, "capture_active": False}}
    payload["mic"].update(mic)
    health.write_health(d, payload)
    return d


def _by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_healthy_runtime_is_all_ok(tmp_path):
    d = _fresh_state(tmp_path)
    checks = _by_name(doctor.run_doctor(d, probe_gateway=False))
    assert checks["runtime heartbeat"].status == doctor.OK
    assert checks["microphone"].status == doctor.OK
    assert doctor.exit_code(list(checks.values())) == 0


def test_doctor_flags_the_d01_state(tmp_path):
    d = _fresh_state(tmp_path, healthy=False, broker_running=False, recovery_attempts=7, backoff_s=60.0,
                     seconds_since_frame=None)
    checks = _by_name(doctor.run_doctor(d, probe_gateway=False))
    assert checks["microphone"].status == doctor.FAIL and "NOT RUNNING" in checks["microphone"].detail
    assert doctor.exit_code(list(checks.values())) == 2


def test_doctor_flags_a_deaf_but_running_stream(tmp_path):
    d = _fresh_state(tmp_path, healthy=False, seconds_since_frame=42.0)
    checks = _by_name(doctor.run_doctor(d, probe_gateway=False))
    assert checks["microphone"].status == doctor.FAIL and "deaf" in checks["microphone"].detail


def test_doctor_flags_a_stale_heartbeat_as_hung_or_dead(tmp_path):
    d = _fresh_state(tmp_path)
    checks = _by_name(doctor.run_doctor(d, now=time.time() + 600, probe_gateway=False))
    assert checks["runtime heartbeat"].status == doctor.FAIL and "STALE" in checks["runtime heartbeat"].detail


def test_doctor_owner_shutdown_is_not_a_failure(tmp_path):
    d = _fresh_state(tmp_path, broker_closed=True, broker_running=False)
    assert _by_name(doctor.run_doctor(d, probe_gateway=False))["microphone"].status == doctor.INFO


def test_doctor_missing_state_dir_is_a_warning_and_creates_nothing(tmp_path):
    missing = tmp_path / "never-ran"
    before = _tree(tmp_path)
    checks = doctor.run_doctor(missing, probe_gateway=False)
    assert checks[0].status == doctor.WARN and doctor.exit_code(checks) == 1
    assert _tree(tmp_path) == before and not missing.exists()


def test_doctor_no_heartbeat_is_a_warning(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    checks = _by_name(doctor.run_doctor(d, probe_gateway=False))
    assert checks["runtime heartbeat"].status == doctor.WARN


def test_doctor_reports_stale_running_tasks_read_only(tmp_path):
    from void.core.task import Task, TaskStore

    d = _fresh_state(tmp_path)
    store = TaskStore(d / "tasks.sqlite")
    t = Task(goal="synthetic")
    t.status = Status.RUNNING
    store.save(t)
    con = sqlite3.connect(d / "tasks.sqlite")
    con.execute("UPDATE tasks SET updated_at = ?", (time.time() - 3600,))
    con.commit()
    con.close()
    before = _tree(d)
    checks = _by_name(doctor.run_doctor(d, probe_gateway=False))
    assert checks["task store"].status == doctor.WARN and "1 task(s) stuck" in checks["task store"].detail
    assert _tree(d) == before, "doctor modified the task database"
    con = sqlite3.connect(d / "tasks.sqlite")
    assert con.execute("SELECT status FROM tasks").fetchone()[0] == Status.RUNNING
    con.close()


def test_doctor_is_read_only_on_a_populated_state_dir(tmp_path):
    d = _fresh_state(tmp_path)
    (d / "void.log").write_text("x" * 100, encoding="utf-8")
    (d / "perf").mkdir()
    (d / "perf" / "perf.jsonl").write_text('{"event":"mic","ts":1}\n', encoding="utf-8")
    (d / "devices.json").write_text("{}", encoding="utf-8")
    before = _tree(tmp_path)
    doctor.run_doctor(d, probe_gateway=False)
    assert _tree(tmp_path) == before


def test_doctor_gateway_probe_detects_a_dead_and_a_live_port(tmp_path):
    import socket

    d = _fresh_state(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                              # nothing listens here now
    from void.device import gateway as gw
    (d / gw.ADDRESS_FILENAME).write_text(json.dumps({"port": port}), encoding="utf-8")
    checks = _by_name(doctor.run_doctor(d, probe_gateway=True))
    assert checks["device gateway"].status == doctor.FAIL


def test_doctor_gateway_probe_succeeds_against_a_real_gateway(tmp_path):
    from tests.test_gateway_availability import _gateway

    gw_obj = _gateway(tmp_path)
    try:
        d = gw_obj.state_dir
        checks = _by_name(doctor.run_doctor(d, probe_gateway=True))
        assert checks["device gateway"].status == doctor.OK, checks["device gateway"].detail
    finally:
        gw_obj.stop()


# ------------------------------------------------------------------ report statistics
def test_percentile_matches_known_values():
    vals = list(range(1, 101))
    assert report.percentile(vals, 50) == pytest.approx(50.5)
    assert report.percentile(vals, 95) == pytest.approx(95.05)
    assert report.percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError):
        report.percentile([], 50)


def test_p99_only_reported_with_enough_samples_and_low_n_flagged():
    few = report.summarize([1.0] * 5)
    assert "p99" not in few and few["low_n"] is True
    mid = report.summarize(list(range(100)))
    assert "p99" not in mid and "low_n" not in mid
    many = report.summarize(list(range(report.P99_MIN_SAMPLES)))
    assert "p99" in many
    assert report.summarize([]) is None


def test_percentiles_are_ordered_for_random_data_seeded():
    rng = random.Random(20260921)
    for _ in range(50):
        vals = [rng.uniform(0, 30) for _ in range(rng.randint(1, 400))]
        s = report.summarize(vals)
        assert s["min"] <= s["p50"] <= s["p95"] <= s["max"]
        if "p99" in s:
            assert s["p95"] <= s["p99"] <= s["max"]


def _write_events(perf_dir: Path, events, name="perf.jsonl"):
    perf_dir.mkdir(parents=True, exist_ok=True)
    (perf_dir / name).write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_report_aggregates_stages_status_and_failures(tmp_path):
    evts = []
    for i in range(30):
        evts.append({"event": "llm", "ts": i, "ok": True, "duration_s": 1.0 + i / 10, "tool_calls": i % 2})
    evts.append({"event": "llm", "ts": 99, "ok": False, "error_class": "ConnectionError"})
    evts += [{"event": "complete", "ts": 100, "status": "completed", "total_s": 4.0},
             {"event": "complete", "ts": 101, "status": "failed", "total_s": 9.0},
             {"event": "activation", "ts": 1, "source": "ptt", "interaction_id": "a" * 8},
             {"event": "stt", "ts": 2, "decode_s": 1.5, "audio_s": 0.0, "empty": True},
             {"event": "mic", "ts": 3, "state": "unavailable"}]
    _write_events(tmp_path / "perf", evts)
    data = report.build_report(report.load_events(tmp_path / "perf"))
    assert data["llm_failures"] == {"ConnectionError": 1}
    assert data["complete_status"] == {"completed": 1, "failed": 1}
    assert data["stt"]["zero_audio"] == 1 and data["stt"]["empty"] == 1
    assert data["stages"]["llm.duration_s"]["n"] == 30 and "p99" not in data["stages"]["llm.duration_s"]
    assert data["mic"] == {"unavailable": 1} and data["interactions"] == 1
    text = report.format_report(data)
    assert "llm.duration_s" in text and "p99 shown only when n >= 300" in text


def test_report_loads_rotated_backups_oldest_first_and_skips_garbage(tmp_path):
    d = tmp_path / "perf"
    _write_events(d, [{"event": "mic", "ts": 3, "state": "c"}], "perf.jsonl")
    _write_events(d, [{"event": "mic", "ts": 2, "state": "b"}], "perf.jsonl.1")
    _write_events(d, [{"event": "mic", "ts": 1, "state": "a"}], "perf.jsonl.2")
    with open(d / "perf.jsonl", "a", encoding="utf-8") as fh:
        fh.write("not json\n[1]\n")
    assert [e["state"] for e in report.load_events(d)] == ["a", "b", "c"]
    assert report.load_events(tmp_path / "nowhere") == []


def test_legacy_log_is_parsed_without_content(tmp_path):
    log = tmp_path / "void.log"
    log.write_text(
        "2026-05-01 10:00:00,000 INFO void.core.agent: LLM_CALL_DONE attempt=1 duration=2.50s tool_calls=1\n"
        "2026-05-01 10:00:03,000 WARNING void.core.agent: LLM_CALL_FAILED attempt=1 duration=0.10s ConnectionError\n"
        "2026-05-01 10:00:04,000 INFO void.core.agent: TOOL_CALL_DONE name=open_app risk=LOW ok=True duration=0.30s\n"
        "2026-05-01 10:00:05,000 INFO void.voice.session: STT_STARTED (audio_samples=32000)\n"
        "2026-05-01 10:00:07,500 INFO void.voice.session: STT_DONE empty=False\n"
        "2026-05-01 10:00:08,000 WARNING void.voice.runtime: MIC_UNAVAILABLE_DETECTED silence_s=9.0\n"
        "some unrelated line\n", encoding="utf-8")
    evts = report.parse_legacy_log(log)
    kinds = [(e["event"], e.get("state") or e.get("ok")) for e in evts]
    assert ("llm", True) in kinds and ("llm", False) in kinds and ("mic", "unavailable") in kinds
    stt = next(e for e in evts if e["event"] == "stt")
    assert stt["decode_s"] == pytest.approx(2.5) and stt["audio_s"] == pytest.approx(2.0)
    data = report.build_report(evts)
    assert data["stages"]["tool.duration_s"]["n"] == 1
    assert report.parse_legacy_log(tmp_path / "missing.log") == []


# ------------------------------------------------------------------ CLI
def test_cli_doctor_exit_codes_and_read_only(tmp_path, capsys):
    good = _fresh_state(tmp_path)
    before = _tree(tmp_path)
    assert cli.main(["doctor", "--state-dir", str(good), "--no-probe"]) == 0
    out = capsys.readouterr().out
    assert "[ ok ]" in out and "microphone" in out
    assert _tree(tmp_path) == before

    bad = tmp_path / "bad"
    bad.mkdir()
    health.write_health(bad, {"state": "idle", "mic": {"supervised": True, "broker_running": False,
                                                       "broker_closed": False, "healthy": False,
                                                       "recovery_attempts": 3, "backoff_s": 8.0}})
    assert cli.main(["doctor", "--state-dir", str(bad), "--no-probe"]) == 2
    assert "[FAIL]" in capsys.readouterr().out

    assert cli.main(["doctor", "--state-dir", str(tmp_path / "absent"), "--no-probe"]) == 1
    assert not (tmp_path / "absent").exists()


def test_cli_perf_report_and_empty_case(tmp_path, capsys):
    assert cli.main(["perf", "report", "--state-dir", str(tmp_path)]) == 1
    assert "No performance data" in capsys.readouterr().out
    _write_events(tmp_path / "perf", [{"event": "llm", "ts": 1, "ok": True, "duration_s": 2.0}])
    before = _tree(tmp_path)
    assert cli.main(["perf", "report", "--state-dir", str(tmp_path)]) == 0
    assert "llm.duration_s" in capsys.readouterr().out
    assert cli.main(["perf", "report", "--state-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stages"]["llm.duration_s"]["n"] == 1
    assert _tree(tmp_path) == before


def test_cli_perf_report_falls_back_to_the_legacy_log(tmp_path, capsys):
    (tmp_path / "void.log").write_text(
        "2026-05-01 10:00:00,000 INFO void.core.agent: LLM_CALL_DONE attempt=1 duration=2.50s tool_calls=0\n",
        encoding="utf-8")
    assert cli.main(["perf", "report", "--state-dir", str(tmp_path)]) == 0
    assert "legacy" in capsys.readouterr().out


def test_peek_state_dir_never_creates(tmp_path):
    from void.config import Config

    cfg = Config({"app": {"state_dir": ".void-peek-test"}})
    p = cfg.peek_state_dir()
    assert not p.exists()                                   # HOME is the test sandbox
    assert Path(os.path.expanduser("~")) in p.parents
    cfg.state_dir()
    assert p.exists()                                       # the creating variant still creates


# ------------------------------------------------------------------ gateway stats
def test_gateway_emits_aggregate_counters_without_identifiers(tmp_path):
    from tests.test_gateway_availability import _gateway

    path = perf.configure(tmp_path / "perf")
    gw_obj = _gateway(tmp_path, stats_period_s=0.05)
    try:
        gw_obj._bump("DEVICE_REQUEST_OK")
        gw_obj._bump("DEVICE_REQUEST_OK")
        gw_obj._bump("DEVICE_REQUEST_RATE_LIMITED")
        gw_obj._bump("connections_dropped_over_cap")
        deadline = time.time() + 3
        while time.time() < deadline and not Path(path).exists():
            time.sleep(0.05)
        time.sleep(0.2)
    finally:
        gw_obj.stop()
        perf.shutdown()
    recs = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]
    gws = [r for r in recs if r["event"] == "gateway"]
    assert gws, "no gateway aggregate emitted"
    assert sum(r["ok"] for r in gws) == 2 and sum(r["rate_limited"] for r in gws) == 1
    assert sum(r["dropped"] for r in gws) == 1
    assert all(set(r) <= {"ts", "event", "period_s", "ok", "rejected", "rate_limited", "paired",
                          "dropped", "conn_errors"} for r in gws)
