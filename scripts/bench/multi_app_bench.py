"""What does one sentence naming several applications cost, and how many model calls does it make?

Launches are RECORDED, not performed: actually starting programs measures Windows' process creation (and leaves a
dozen windows open), which is neither V.O.I.D's cost nor something it can improve. The same decision, and the same
reason, as scripts/bench/response_bench.py.

The comparison that matters is one multi-target sentence against the same applications named one sentence at a time,
because that is what the owner would otherwise have to do.

usage:
    python scripts/bench/multi_app_bench.py --repeats 10
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from void.config import Config                                       # noqa: E402
from void.providers.base import LLMProvider, LLMResponse              # noqa: E402
from void.providers.registry import ProviderRegistry                  # noqa: E402

CASES = [
    ("single", ["open chrome"]),
    ("two", ["open vs code and whatsapp"]),
    ("three", ["open chrome, vs code and file explorer"]),
    ("two, separately", ["open vs code", "open whatsapp"]),
    ("three, separately", ["open chrome", "open vs code", "open file explorer"]),
    ("partial (one missing)", ["open vs code, whatsapp and notepad++"]),
    ("all missing", ["open notepad++ and someunknownthing"]),
    ("folder + application", ["open workspace and terminal"]),
    ("glued verb", ["openchat gpt"]),
]


class Counting(LLMProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text="(the model answered)")


def record_launches():
    """Record launches instead of performing them - the engine's work is what this measures."""
    import void.actions.apps as apps_mod
    done: list = []
    apps_mod.subprocess.Popen = lambda argv, *a, **k: done.append(tuple(argv))
    apps_mod.os.startfile = lambda target: done.append(("startfile", target))
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=10)
    args = ap.parse_args()

    launched = record_launches()
    from void.app import Assistant
    a = Assistant(config=Config.load())
    gemini, local = Counting("gemini"), Counting("local")
    a.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])

    # Warm the catalogs first: discovery is start-up cost, not per-command cost (and is measured elsewhere).
    t0 = time.perf_counter()
    a._fast.catalog.entries()
    apps_ms = (time.perf_counter() - t0) * 1000
    folders_ms = 0.0
    if a._fast.folders is not None:
        t0 = time.perf_counter()
        a._fast.folders.entries()
        folders_ms = (time.perf_counter() - t0) * 1000
    print(f"start-up, once: application discovery {apps_ms:.0f} ms, folder scan {folders_ms:.0f} ms\n")

    print(f"{'case':24} {'sentences':>9} {'p50':>9} {'p90':>9} {'max':>9} {'opened':>7} {'model':>6}")
    print("-" * 80)
    for label, sentences in CASES:
        times, opened_n = [], 0
        g0, l0 = gemini.calls, local.calls
        for _ in range(args.repeats):
            launched.clear()
            t0 = time.perf_counter()
            for s in sentences:
                a.run(s)
            times.append((time.perf_counter() - t0) * 1000)
            opened_n = len(launched)
        times.sort()
        p50 = statistics.median(times)
        p90 = times[min(len(times) - 1, int(0.9 * len(times)))]
        print(f"{label:24} {len(sentences):9d} {p50:8.2f}ms {p90:8.2f}ms {times[-1]:8.2f}ms "
              f"{opened_n:7d} {(gemini.calls - g0) + (local.calls - l0):6d}")

    print(f"\ntotal model calls across the whole run: gemini {gemini.calls}, ollama {local.calls}")
    print(f"launches recorded (nothing was started): {len(launched)} in the last case")


if __name__ == "__main__":
    main()
