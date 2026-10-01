"""Response-timing audit: what every category of request actually costs, and what it calls to get there.

Providers are FAKES that count calls and answer instantly. That is deliberate and it is the point: this benchmark
answers "does a local command reach a model at all, and how much work does the engine do", which is a routing
question. Real provider latency is not something V.O.I.D controls and was measured separately
(docs/PROVIDER_FALLBACK_2026-09-24.md: Gemini successful calls p50 6.88 s, Ollama warm ~2 s). Running these
hundreds of times against live Gemini would burn the owner's quota to learn nothing new.

Stage timings come from the engine's own telemetry (void/perf) plus wall-clock around Assistant.run, so nothing is
instrumented twice.

usage:
    python scripts/bench/response_bench.py [--repeats N]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from void import perf
from void.app import Assistant
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry

LOCAL = ["Open VS Code", "Open WhatsApp", "Open File Explorer", "Open Windows Terminal",
         "Open ChatGPT", "Open Notepad", "Open Opera GX", "Open Claude"]
MISSING = ["Open Notepad++", "Open Photoshop"]
INFORMATIONAL = ["Explain ARP", "What is DNS?", "Explain the TCP handshake"]
CONVERSATIONAL = ["thanks, that is all for now"]


class CountingProvider(LLMProvider):
    """Answers instantly and records every call, so routing mistakes are impossible to miss."""

    def __init__(self, name, text="(answer)"):
        self.name = name
        self._text = text
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text=self._text)


class Recorder:
    """Collects the engine's own telemetry for one run without writing to the owner's perf log."""

    def __init__(self, monkey_target=perf):
        self.events: list[tuple[str, dict]] = []
        self._target = monkey_target
        self._original = None

    def __enter__(self):
        self._original = self._target.emit
        self._target.emit = lambda event, **fields: self.events.append((event, dict(fields)))
        return self

    def __exit__(self, *exc):
        self._target.emit = self._original

    def stage(self, event, field):
        for name, fields in self.events:
            if name == event and field in fields:
                return fields[field]
        return None

    def count(self, event):
        return sum(1 for name, _ in self.events if name == event)

    def kinds(self):
        return [f.get("kind") for n, f in self.events if n == "respond"]


def pct(values, q):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def run_category(label, commands, repeats, assistant, gemini, ollama):
    rows, model_calls, ollama_calls, tool_calls, respond_kinds, failures = [], 0, 0, 0, Counter(), 0
    for _ in range(repeats):
        for said in commands:
            before_g, before_o = gemini.calls, ollama.calls
            with Recorder() as rec:
                t0 = time.perf_counter()
                result = assistant.run(said)
                total_ms = (time.perf_counter() - t0) * 1000
            rows.append(total_ms)
            model_calls += gemini.calls - before_g
            ollama_calls += ollama.calls - before_o
            tool_calls += rec.count("tool")
            for k in rec.kinds():
                respond_kinds[k] += 1
            if getattr(result, "status", "") not in ("completed",):
                failures += 1
            time.sleep(0.05)
    print(f"{label:22} {pct(rows,.5):8.1f} {pct(rows,.9):8.1f} {pct(rows,.95):8.1f} {max(rows):8.1f} "
          f"{model_calls:7} {ollama_calls:8} {tool_calls:7} {failures:8}  {dict(respond_kinds)}")
    return rows


def record_launches():
    """Record launches instead of performing them.

    The engine's work is what this benchmark is for. Actually starting an application measures Windows' process
    creation (and leaves a dozen windows open), which is neither V.O.I.D's cost nor something it can improve.
    Real launch latency is reported separately, once per application, by --real-launch.
    """
    import void.actions.apps as apps_mod
    launched: list = []
    apps_mod.subprocess.Popen = lambda argv, *a, **k: launched.append(tuple(argv))
    apps_mod.os.startfile = lambda target: launched.append(("startfile", target))
    return launched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--real-launch", action="store_true",
                    help="actually start each application once, to time the OS rather than the engine")
    args = ap.parse_args()
    launched = [] if args.real_launch else record_launches()

    gemini = CountingProvider("gemini", "ARP maps an IP address to a MAC address.")
    ollama = CountingProvider("local", "(local answer)")

    # --- cold start: the very first request of a fresh process ----------------------------------
    t0 = time.perf_counter()
    assistant = Assistant()
    construct_ms = (time.perf_counter() - t0) * 1000
    assistant.providers = ProviderRegistry({"gemini": gemini, "local": ollama}, ["gemini", "local"])

    with Recorder() as rec:
        t0 = time.perf_counter()
        assistant.run(LOCAL[0])
        cold_ms = (time.perf_counter() - t0) * 1000
    print(f"Assistant() construction           {construct_ms:8.1f} ms")
    print(f"COLD first local command           {cold_ms:8.1f} ms   "
          f"(model calls {gemini.calls}, tools {rec.count('tool')})")
    t0 = time.perf_counter()
    assistant.run(LOCAL[0])
    print(f"WARM same command                  {(time.perf_counter()-t0)*1000:8.1f} ms\n")

    print(f"{'category':22} {'p50':>8} {'p90':>8} {'p95':>8} {'max':>8} "
          f"{'model':>7} {'ollama':>8} {'tools':>7} {'failed':>8}  respond kinds")
    run_category("LOCAL actions", LOCAL, args.repeats, assistant, gemini, ollama)
    run_category("MISSING application", MISSING, args.repeats, assistant, gemini, ollama)
    run_category("INFORMATIONAL", INFORMATIONAL, args.repeats, assistant, gemini, ollama)
    run_category("CONVERSATIONAL", CONVERSATIONAL, args.repeats, assistant, gemini, ollama)
    run_category("REPEATED (one cmd)", [LOCAL[0]], args.repeats * 4, assistant, gemini, ollama)

    print(f"\ntotal fake-Gemini calls: {gemini.calls}   total fake-Ollama calls: {ollama.calls}")
    if not args.real_launch:
        print(f"launches recorded (no program was started): {len(launched)}")

    # --- where the time goes inside one local command --------------------------------------------
    print("\nstages inside one warm LOCAL command (engine telemetry + wall clock):")
    with Recorder() as rec:
        t0 = time.perf_counter()
        assistant.run("Open WhatsApp")
        total = (time.perf_counter() - t0) * 1000
    route = [f for n, f in rec.events if n == "route"]
    tools = [f for n, f in rec.events if n == "tool"]
    complete = [f for n, f in rec.events if n == "complete"]
    print(f"  route events      {route}")
    print(f"  tool events       {[{k: v for k, v in t.items() if k != 'interaction_id'} for t in tools]}")
    print(f"  complete          {[{k: v for k, v in c.items() if k != 'interaction_id'} for c in complete]}")
    print(f"  wall clock        {total:.2f} ms")


if __name__ == "__main__":
    main()
