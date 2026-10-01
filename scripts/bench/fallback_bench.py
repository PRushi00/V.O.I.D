"""Benchmark the Gemini -> Ollama failure path, stage by stage.

Gemini failures are INJECTED, never provoked: benchmarking must not spend the owner's free quota, and an injected
failure is reproducible where a real outage is not. Everything downstream of the failure - classification, provider
selection, readiness, model load, generation - is the real code talking to the real Ollama.

usage:
    python scripts/bench/fallback_bench.py            # every scenario
    python scripts/bench/fallback_bench.py --quick    # skip the cold-start scenario (it unloads the model)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from void.app import Assistant
from void.config import Config
from void.providers import failures
from void.providers.base import LLMProvider, ProviderUnavailable
from void.providers.registry import ProviderRegistry

# Distinct prompts per repetition: Ollama reuses its KV cache for an identical prompt, which makes a repeated
# benchmark look several times faster than the first real request ever would be.
PROMPTS = [
    "Explain ARP in one sentence.",
    "Explain DNS in one sentence.",
    "Explain DHCP in one sentence.",
    "Explain NAT in one sentence.",
    "Explain TLS in one sentence.",
    "Explain BGP in one sentence.",
]


class InjectedGemini(LLMProvider):
    """Fails the way the real provider fails, without touching the network or a credential."""

    name = "gemini"

    def __init__(self, exc, latency_s: float = 0.0):
        self._exc, self._latency = exc, latency_s
        self.calls = 0
        self.stamps: list[float] = []

    def available(self) -> bool:
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        self.stamps.append(time.perf_counter())
        if self._latency:
            time.sleep(self._latency)
        raise self._exc


def resident_models(base="http://127.0.0.1:11434"):
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=3) as r:
            return [m.get("name") for m in json.load(r).get("models", [])]
    except Exception:
        return []


def unload(cfg):
    """Drop the model from VRAM so the next call is genuinely cold."""
    from void.providers.local_provider import LocalProvider
    p = LocalProvider(base_url=cfg.get("llm.local.base_url", "http://127.0.0.1:11434"),
                      model=cfg.get("llm.local.model", "qwen3:8b"), think=False,
                      timeout=180, keep_alive="0")
    try:
        p.generate([{"role": "user", "content": "hi"}])
    except Exception:
        pass
    time.sleep(4.0)


def run_case(label, gemini, local_provider, repeats=3):
    cfg = Config.load()
    rows = []
    for rep in range(repeats):
        a = Assistant(config=cfg)
        providers = {"gemini": gemini}
        order = ["gemini"]
        if local_provider is not None:
            providers["local"] = local_provider
            order.append("local")
        a.providers = ProviderRegistry(providers, order)
        gemini.calls, gemini.stamps = 0, []
        t0 = time.perf_counter()
        result = a.run(PROMPTS[(run_case.seq + rep) % len(PROMPTS)])
        total = time.perf_counter() - t0
        first = (gemini.stamps[0] - t0) if gemini.stamps else 0.0
        tail = total - (gemini.stamps[-1] - t0) if gemini.stamps else total
        rows.append((total, first, gemini.calls, tail, result.status))
        time.sleep(0.5)
    run_case.seq += repeats
    med = statistics.median(r[0] for r in rows)
    _t, first, calls, tail, status = rows[-1]
    print(f"  {label:40} {med:6.2f}s  setup {first*1000:6.1f}ms  "
          f"attempts {calls}  after-last-failure {tail:5.2f}s  {status}")
    return med


run_case.seq = 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the cold-start scenario")
    args = ap.parse_args()

    cfg = Config.load()
    base = Assistant(config=cfg)
    local = base.providers.get("local")
    print(f"local: model={local.model} url={local.base_url} available={local.available()}")
    print(f"resident before: {resident_models() or 'none (cold)'}\n")

    print("classification (must be instant and correct):")
    for exc, want in ((RuntimeError("503 UNAVAILABLE"), "server"),
                      (RuntimeError("429 RESOURCE_EXHAUSTED"), "rate_limit"),
                      (RuntimeError("401 API key not valid"), "auth"),
                      (RuntimeError("You exceeded your current quota"), "quota"),
                      (TimeoutError("deadline exceeded"), "timeout"),
                      (ConnectionError("connection refused"), "network"),
                      (RuntimeError("400 invalid argument"), "invalid_request")):
        t = time.perf_counter()
        got = failures.classify(exc)
        attempts, over = failures.policy(got)
        ok = "ok " if got == want else "BAD"
        print(f"  {ok} {str(exc)[:34]:36} -> {got:15} attempts={attempts} failover={over} "
              f"({(time.perf_counter()-t)*1e6:.0f} us)")

    print("\nscenarios (median of 3):")
    run_case("gemini 503 -> warm ollama",
             InjectedGemini(RuntimeError("503 UNAVAILABLE. high demand")), local)
    run_case("gemini timeout -> warm ollama",
             InjectedGemini(TimeoutError("deadline exceeded")), local)
    run_case("gemini 429 -> warm ollama (no retry)",
             InjectedGemini(RuntimeError("429 RESOURCE_EXHAUSTED")), local)
    run_case("gemini auth -> warm ollama (no retry)",
             InjectedGemini(RuntimeError("401 API key not valid")), local)
    run_case("gemini quota -> warm ollama (no retry)",
             InjectedGemini(ProviderUnavailable("every credential is cooled")), local)
    run_case("gemini invalid request (must NOT fail over)",
             InjectedGemini(RuntimeError("400 invalid argument: bad schema")), local)
    run_case("gemini 503, no fallback configured",
             InjectedGemini(RuntimeError("503 UNAVAILABLE")), None)

    from void.providers.local_provider import LocalProvider
    dead = LocalProvider(base_url="http://127.0.0.1:1", model=local.model, timeout=5)
    run_case("gemini 503 -> ollama unreachable", InjectedGemini(RuntimeError("503 UNAVAILABLE")), dead)
    missing = LocalProvider(base_url=local.base_url, model="no-such-model:0b", timeout=20)
    run_case("gemini 503 -> model not installed", InjectedGemini(RuntimeError("503 UNAVAILABLE")), missing)

    if not args.quick:
        print("\n  (unloading the model for a genuinely cold measurement)")
        unload(cfg)
        print(f"  resident: {resident_models() or 'none (cold)'}")
        run_case("gemini 503 -> COLD ollama", InjectedGemini(RuntimeError("503 UNAVAILABLE")),
                 local, repeats=1)
        run_case("gemini 503 -> warm again", InjectedGemini(RuntimeError("503 UNAVAILABLE")), local)


if __name__ == "__main__":
    main()
