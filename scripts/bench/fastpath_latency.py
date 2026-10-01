"""Controlled LOCAL latency harness: the model path vs the deterministic fast path for "open <app>".

Compares, on the same real ``Assistant`` (real tool registry funnel, real RiskGate, real kill switch, real task store):

  A. STT -> LLM -> capability   the command goes to a scripted, zero-latency "model" that proposes launch_app
  B. STT -> intent -> capability the deterministic fast path (no model)

The transcript is injected (the STT is a fixed string), the model is an instant scripted stub, and the launch itself is
a stub (nothing is ever started). So this measures V.O.I.D's OWN work - intent routing, the gated capability call and
bookkeeping - and the number of model calls. It deliberately does NOT measure a real model, a microphone, an STT engine or
a speaker: a real model's latency is ADDED to path A and to nothing in path B, and is reported separately from the
earlier evaluation, never re-measured or invented here. This is not a mic-to-speaker latency.

Reported per path (ms): intent-routing (run() start -> the capability is entered), execution (the capability itself),
total local (run() start -> result), and model calls. Run:  python scripts/bench/fastpath_latency.py [--n 300] [--json out]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_HOME = tempfile.mkdtemp(prefix="void-fastpath-bench-")        # throw-away HOME: the owner's ~/.void is never touched
os.environ["USERPROFILE"] = os.environ["HOME"] = _HOME

import void.actions.apps as apps                                    # noqa: E402
from void.actions.apps import AppActions                           # noqa: E402
from void.actions.computer import AppCatalog, make_backend         # noqa: E402
from void.actions.files import FileActions                         # noqa: E402
from void.actions.registry import ToolRegistry                     # noqa: E402
from void.app import Assistant                                     # noqa: E402
from void.config import Config                                     # noqa: E402
from void.core.fast_path import FastPath                           # noqa: E402
from void.providers.base import LLMProvider, LLMResponse, ToolCall  # noqa: E402
from void.providers.registry import ProviderRegistry              # noqa: E402
from void.security.protected import EngineProtected                # noqa: E402
from tests.test_computer import FakeBackend                        # noqa: E402

# Reference model latencies from docs/v2-evidence/BRAIN_AND_VOICE_EVALUATION.md (measured there, quoted here).
REFERENCE_LLM_S = {
    "gemini-3.5-flash-lite (minimal), tool call p50": 1.30,
    "ollama qwen3:8b (think off), tool call p50": 2.70,
    "gemini-3.6-flash (current config), 'Open Notepad.' 7.5/9.0/6.4 s -> median": 7.5,
}


class ScriptedModel(LLMProvider):
    """Instant stand-in for the planner: proposes launch_app once, then a summary. Counts every call."""
    name = "scripted"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        if messages[-1].get("role") != "tool":
            return LLMResponse(tool_calls=[ToolCall(name="launch_app", arguments={"name": "notepad"})])
        return LLMResponse(text="Opened Notepad.")


def build(fast: bool):
    cfg = Config({"app": {"state_dir": ".void"}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(Path(_HOME) / "ws")]}, "fast_path": {"enabled": fast}})
    a = Assistant(config=cfg)
    marks: dict = {}

    def popen(argv, *args, **kw):                                   # the launch: a stub that only timestamps
        marks["enter"] = time.perf_counter()
        marks["exit"] = time.perf_counter()

    apps.subprocess.Popen = popen                                    # process-local; this script only
    apps.shutil.which = lambda c: r"C:\stub\notepad.exe" if c == "notepad" else None
    catalog = AppCatalog(FakeBackend(apps=[]))
    fa = FileActions([Path(_HOME) / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    a.tools = ToolRegistry()
    a.tools.register_all(AppActions(fa, catalog=catalog).tools())
    a._fast = FastPath(catalog) if fast else None
    model = ScriptedModel()
    a.providers = ProviderRegistry({"scripted": model}, ["scripted"])
    return a, model, marks


def measure(fast: bool, n: int):
    a, model, marks = build(fast)
    for _ in range(10):                                              # warm-up (imports, sqlite, caches)
        a.run("open notepad")
    model.calls = 0
    routing, execution, total = [], [], []
    for _ in range(n):
        marks.clear()
        t0 = time.perf_counter()
        r = a.run("open notepad")
        t1 = time.perf_counter()
        assert r.status == "completed", r
        routing.append((marks["enter"] - t0) * 1000)
        execution.append((marks["exit"] - marks["enter"]) * 1000)
        total.append((t1 - t0) * 1000)
    return {"routing_ms": routing, "execution_ms": execution, "total_ms": total, "llm_calls": model.calls, "n": n}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summary(xs):
    return {"p50": round(statistics.median(xs), 3), "p95": round(pct(xs, 95), 3), "max": round(max(xs), 3)}


def catalog_cost():
    """One-time cost of building the REAL application catalog (read-only Start Menu discovery); paid only by names
    that are not in the fixed alias map, and only on the first such command of a process."""
    t0 = time.perf_counter()
    try:
        n = len(AppCatalog(make_backend()).entries())
    except Exception as exc:                                         # noqa: BLE001
        return {"error": type(exc).__name__}
    return {"entries": n, "build_ms": round((time.perf_counter() - t0) * 1000, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    res = {"model_path": measure(False, args.n), "fast_path": measure(True, args.n)}
    out = {"n": args.n, "catalog_discovery_once": catalog_cost(), "reference_model_latency_s": REFERENCE_LLM_S, "paths": {}}
    for name, r in res.items():
        out["paths"][name] = {"llm_calls_total": r["llm_calls"], "llm_calls_per_command": r["llm_calls"] / r["n"],
                              **{k[:-3]: summary(r[k]) for k in ("routing_ms", "execution_ms", "total_ms")}}
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    assert out["paths"]["fast_path"]["llm_calls_total"] == 0, "the fast path must make ZERO model calls"


if __name__ == "__main__":
    main()
