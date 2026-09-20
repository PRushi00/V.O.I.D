"""``void perf report``: latency/reliability tables from the perf stream (and, optionally,
the legacy ``void.log`` markers written before V2.0). Strictly read-only.

Statistics policy (no claim beyond the data): p50 and p95 are always computed; p99 is
reported ONLY when n >= P99_MIN_SAMPLES; a summary with n < LOW_N_THRESHOLD is flagged
``low_n`` so nobody mistakes a handful of samples for a distribution.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

P99_MIN_SAMPLES = 300
LOW_N_THRESHOLD = 20


# ------------------------------------------------------------------ statistics
def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile of an already-sorted list (p in 0..100)."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo))


def summarize(values) -> dict | None:
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    out = {"n": len(vals), "min": vals[0], "p50": percentile(vals, 50),
           "p95": percentile(vals, 95), "max": vals[-1]}
    if len(vals) >= P99_MIN_SAMPLES:
        out["p99"] = percentile(vals, 99)
    if len(vals) < LOW_N_THRESHOLD:
        out["low_n"] = True
    return out


# ------------------------------------------------------------------ loading
def load_events(perf_dir) -> list[dict]:
    """All events from ``perf.jsonl`` and its rotated backups, oldest first."""
    perf_dir = Path(perf_dir)
    if not perf_dir.is_dir():
        return []
    # Backups are perf.jsonl.1 (newest) .. perf.jsonl.N (oldest): read oldest first.
    files = sorted((p for p in perf_dir.glob("perf.jsonl.*") if p.suffix[1:].isdigit()),
                   key=lambda p: int(p.suffix[1:]), reverse=True)
    live = perf_dir / "perf.jsonl"
    if live.exists():
        files.append(live)
    events: list[dict] = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and "event" in rec:
                    events.append(rec)
        except OSError:
            continue
    events.sort(key=lambda r: r.get("ts", 0))
    return events


_TS = r"(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \w+ [\w.]+: "
_LEGACY = [
    ("llm_ok", re.compile(_TS + r"LLM_CALL_DONE attempt=(?P<attempt>\d+) duration=(?P<d>[\d.]+)s tool_calls=(?P<tc>\d+)")),
    ("llm_fail", re.compile(_TS + r"LLM_CALL_FAILED attempt=(?P<attempt>\d+) duration=(?P<d>[\d.]+)s (?P<cls>\w+)")),
    ("tool", re.compile(_TS + r"TOOL_CALL_DONE name=(?P<name>\S+) risk=(?P<risk>\w+) ok=(?P<ok>\w+) duration=(?P<d>[\d.]+)s")),
    ("stt_start", re.compile(_TS + r"STT_STARTED \(audio_samples=(?P<n>-?\d+)\)")),
    ("stt_done", re.compile(_TS + r"STT_DONE empty=(?P<empty>\w+)")),
    ("wake", re.compile(_TS + r"WAKE_DETECTED accepted")),
    ("endpoint", re.compile(_TS + r"COMMAND_ENDPOINT reason=(?P<reason>\w+)")),
    ("mic_unavail", re.compile(_TS + r"MIC_UNAVAILABLE_DETECTED")),
    ("mic_attempt", re.compile(_TS + r"MIC_RECOVERY_ATTEMPT attempt=(?P<a>\d+)")),
    ("mic_ok", re.compile(_TS + r"MIC_RECOVERY_SUCCEEDED")),
    ("mic_fail", re.compile(_TS + r"MIC_RECOVERY_FAILED")),
]


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def parse_legacy_log(path) -> list[dict]:
    """Turn pre-V2.0 ``void.log`` stage markers into perf-shaped events. Read-only;
    the log holds no transcripts by design, so neither do these events."""
    events: list[dict] = []
    pending_stt = None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        for kind, rx in _LEGACY:
            m = rx.match(line)
            if not m:
                continue
            ts = _epoch(m.group("ts"))
            if kind == "llm_ok":
                events.append({"event": "llm", "ts": ts, "ok": True, "duration_s": float(m["d"]),
                               "tool_calls": int(m["tc"]), "attempt": int(m["attempt"]), "legacy": True})
            elif kind == "llm_fail":
                events.append({"event": "llm", "ts": ts, "ok": False, "duration_s": float(m["d"]),
                               "error_class": m["cls"], "attempt": int(m["attempt"]), "legacy": True})
            elif kind == "tool":
                events.append({"event": "tool", "ts": ts, "name": m["name"], "risk": m["risk"],
                               "ok": m["ok"] == "True", "duration_s": float(m["d"]), "legacy": True})
            elif kind == "stt_start":
                pending_stt = (ts, int(m["n"]))
            elif kind == "stt_done" and pending_stt is not None:
                start, n = pending_stt
                events.append({"event": "stt", "ts": ts, "decode_s": round(ts - start, 3),
                               "audio_s": max(n, 0) / 16000.0, "empty": m["empty"] == "True", "legacy": True})
                pending_stt = None
            elif kind == "wake":
                events.append({"event": "activation", "ts": ts, "source": "wake", "legacy": True})
            elif kind == "endpoint":
                events.append({"event": "endpoint", "ts": ts, "reason": m["reason"], "legacy": True})
            elif kind == "mic_unavail":
                events.append({"event": "mic", "ts": ts, "state": "unavailable", "legacy": True})
            elif kind == "mic_attempt":
                events.append({"event": "mic", "ts": ts, "state": "recovery_attempt", "legacy": True})
            elif kind == "mic_ok":
                events.append({"event": "mic", "ts": ts, "state": "recovered", "legacy": True})
            elif kind == "mic_fail":
                events.append({"event": "mic", "ts": ts, "state": "recovery_failed", "legacy": True})
            break
    return events


# ------------------------------------------------------------------ report
def build_report(events: list[dict]) -> dict:
    stages: dict[str, list[float]] = defaultdict(list)
    tools: dict[str, list[float]] = defaultdict(list)
    activations, statuses, mic, llm_fail = Counter(), Counter(), Counter(), Counter()
    stt = Counter()
    ids = set()
    for e in events:
        if e.get("interaction_id"):
            ids.add(e["interaction_id"])
        kind = e.get("event")
        if kind == "stt":
            stt["total"] += 1
            stt["empty"] += bool(e.get("empty"))
            stt["zero_audio"] += (e.get("audio_s", 1) == 0)
            if "decode_s" in e:
                stages["stt.decode_s"].append(e["decode_s"])
            if "audio_s" in e and e["audio_s"] > 0:
                stages["stt.audio_s"].append(e["audio_s"])
        elif kind == "endpoint" and "capture_s" in e:
            stages["endpoint.capture_s"].append(e["capture_s"])
        elif kind == "llm":
            if e.get("ok") and "duration_s" in e:
                stages["llm.duration_s"].append(e["duration_s"])
                stages["llm.duration_s[tool_calls]" if e.get("tool_calls") else "llm.duration_s[text]"].append(e["duration_s"])
            elif e.get("ok") is False:
                llm_fail[e.get("error_class", "unknown")] += 1
        elif kind == "tool" and "duration_s" in e:
            stages["tool.duration_s"].append(e["duration_s"])
            tools[e.get("name", "?")].append(e["duration_s"])
        elif kind == "complete":
            statuses[e.get("status", "?")] += 1
            if "total_s" in e:
                stages["complete.total_s"].append(e["total_s"])
        elif kind == "speak" and "dur_s" in e:
            stages["speak.dur_s"].append(e["dur_s"])
        elif kind == "activation":
            activations[e.get("source", "?")] += 1
        elif kind == "mic":
            mic[e.get("state", "?")] += 1
    return {
        "events": len(events),
        "interactions": len(ids),
        "activations": dict(activations),
        "stages": {k: summarize(v) for k, v in sorted(stages.items())},
        "tools": {k: summarize(v) for k, v in sorted(tools.items())},
        "llm_failures": dict(llm_fail),
        "stt": dict(stt),
        "complete_status": dict(statuses),
        "mic": dict(mic),
    }


def format_report(report: dict) -> str:
    lines = [f"events: {report['events']}   interactions (with id): {report['interactions']}"]
    if report["activations"]:
        lines.append("activations: " + ", ".join(f"{k}={v}" for k, v in sorted(report["activations"].items())))
    if report["stt"]:
        s = report["stt"]
        lines.append(f"stt runs: {s.get('total', 0)} (empty {s.get('empty', 0)}, zero-audio {s.get('zero_audio', 0)})")
    if report["complete_status"]:
        lines.append("completions: " + ", ".join(f"{k}={v}" for k, v in sorted(report["complete_status"].items())))
    if report["llm_failures"]:
        lines.append("llm failures: " + ", ".join(f"{k}={v}" for k, v in sorted(report["llm_failures"].items())))
    if report["mic"]:
        lines.append("mic events: " + ", ".join(f"{k}={v}" for k, v in sorted(report["mic"].items())))
    lines.append("")
    lines.append(f"{'stage (seconds)':34s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s}")
    rows = list(report["stages"].items()) + [(f"tool[{k}]", v) for k, v in report["tools"].items()]
    for name, s in rows:
        if not s:
            continue
        p99 = f"{s['p99']:8.2f}" if "p99" in s else f"{'-':>8s}"
        flag = " *" if s.get("low_n") else ""
        lines.append(f"{name:34s} {s['n']:6d} {s['p50']:8.2f} {s['p95']:8.2f} {p99} {s['max']:8.2f}{flag}")
    lines.append("")
    lines.append(f"* n < {LOW_N_THRESHOLD}: too few samples to treat as a distribution.  "
                 f"p99 shown only when n >= {P99_MIN_SAMPLES}.")
    return "\n".join(lines)
