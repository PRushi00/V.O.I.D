"""Live validation of an OpenAI-compatible gateway provider (default: AgentRouter, model gpt-5.6-sol). Opt-in tooling, never run
by pytest. It prints ONLY safe metadata: HTTP-level category, latency, the model id the response reports, whether the exact
expected text came back, and tool-call / argument correctness. It never prints, logs or writes a key, a header or a response body.

    python scripts/bench/gateway_validate.py [--provider agentrouter|openai] [--n 5] [--json out.json]

Steps: (1) GET /models and check the requested model is listed; (2) one exact-reply completion, recording the response `model`
field; (3) a real tool round trip through the Agent (kill switch -> RiskGate -> capability) with one harmless read-only tool
in a throw-away directory; (4) a small latency sample. It stops at the first step that cannot work (no credential, spent
balance, ...) instead of retrying, and reports the safe category.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from void.actions.files import FileActions                                  # noqa: E402
from void.actions.registry import ToolRegistry                              # noqa: E402
from void.core.agent import Agent                                           # noqa: E402
from void.core.kill_switch import KillSwitch                                # noqa: E402
from void.core.task import TaskStore                                        # noqa: E402
from void.providers.agentrouter_provider import AgentRouterProvider         # noqa: E402
from void.providers.base import ProviderUnavailable, ToolSpec               # noqa: E402
from void.providers.openai_provider import OpenAIProvider                   # noqa: E402
from void.security.protected import EngineProtected                         # noqa: E402
from void.security.risk import RiskGate                                     # noqa: E402

EXPECTED = "V.O.I.D_PROVIDER_TEST_OK"
SYSTEM = {"role": "system", "content": "You are V.O.I.D, a concise desktop assistant. Use tools when asked to act."}
LIST = ToolSpec(name="list_directory", description="List a folder.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


def category(exc) -> str:
    text = str(exc)
    for word in ("quota", "auth", "rate_limit", "timeout", "network", "server", "invalid", "not available", "unreadable",
                 "No AgentRouter credential", "No OpenAI credential"):
        if word.lower() in text.lower():
            return word
    return type(exc).__name__


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="agentrouter", choices=["agentrouter", "openai"])
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    p = AgentRouterProvider() if args.provider == "agentrouter" else OpenAIProvider()
    out: dict = {"provider": p.name, "endpoint": p._api_base, "requested_model": p.model,
                 "slots": p.credential_status(), "steps": {}}

    def finish(status):
        out["status"] = status
        print(json.dumps(out, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")

    # 1. model discovery
    t0 = time.perf_counter()
    try:
        ids = p.list_models()
        out["steps"]["models"] = {"ok": True, "latency_s": round(time.perf_counter() - t0, 3), "count": len(ids),
                                  "requested_model_listed": p.model in ids}
    except ProviderUnavailable as exc:
        out["steps"]["models"] = {"ok": False, "category": category(exc), "latency_s": round(time.perf_counter() - t0, 3)}
        return finish("STOPPED: model discovery failed (%s)" % category(exc))

    # 2. exact completion + model identity evidence
    t0 = time.perf_counter()
    try:
        r = p.generate([SYSTEM, {"role": "user", "content": f"Reply with exactly: {EXPECTED}"}])
    except Exception as exc:                                                 # noqa: BLE001
        out["steps"]["completion"] = {"ok": False, "category": category(exc), "latency_s": round(time.perf_counter() - t0, 3)}
        return finish("STOPPED: completion failed (%s)" % category(exc))
    raw = r.raw if isinstance(r.raw, dict) else {}
    out["steps"]["completion"] = {
        "ok": True, "latency_s": round(time.perf_counter() - t0, 3), "exact_match": (r.text or "").strip() == EXPECTED,
        "response_model_field": raw.get("model") if isinstance(raw.get("model"), str) else None,
        "response_model_matches_request": raw.get("model") == p.model,
        "usage": {k: v for k, v in raw["usage"].items() if isinstance(v, int)} if isinstance(raw.get("usage"), dict) else None}

    # 3. a real tool round trip through Agent -> RiskGate -> capability (read-only, throw-away directory)
    with tempfile.TemporaryDirectory(prefix="void-gw-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        (root / "hello.txt").write_text("hi", encoding="utf-8")
        state = root / ".void"
        state.mkdir()
        fa = FileActions([root], engine_protected=EngineProtected.default(state_dir=state))
        reg = ToolRegistry()
        reg.register_all([t for t in fa.tools() if t.name == "list_directory"])
        agent = Agent(provider=p, tools=reg, risk_gate=RiskGate("high"), kill_switch=KillSwitch(),
                      store=TaskStore(root / "t.sqlite"), max_retries=0)
        t0 = time.perf_counter()
        res = agent.run(f"List the files in the folder {root}. Then tell me the file name you saw.")
        proposed = [m for m in res.task.messages if m.get("role") == "assistant" and m.get("tool_calls")]
        args_ok = bool(proposed) and all(isinstance(tc.get("arguments"), dict) and isinstance(tc["arguments"].get("path"), str)
                                         for tc in proposed[0]["tool_calls"])
        out["steps"]["tool_round_trip"] = {
            "status": res.status, "latency_s": round(time.perf_counter() - t0, 3),
            "tool_call_proposed": bool(proposed), "tool_name": proposed[0]["tool_calls"][0]["name"] if proposed else None,
            "arguments_parsed_and_valid_per_schema": args_ok,
            "tool_executed_through_riskgate": any(m.get("role") == "tool" and m.get("name") == "list_directory"
                                                  for m in res.task.messages),
            "final_answer_mentions_file": "hello" in (res.result or "").lower()}

    # 4. small latency sample (only reached after a completion succeeded)
    simple, tool = [], []
    for _ in range(max(1, args.n)):
        for bucket, msgs, tools in (
                (simple, [SYSTEM, {"role": "user", "content": "In one short sentence, what can you help me with?"}], None),
                (tool, [SYSTEM, {"role": "user", "content": "List the folder C:\\example."}], [LIST])):
            t0 = time.perf_counter()
            try:
                p.generate(msgs, tools=tools)
                bucket.append(time.perf_counter() - t0)
            except Exception as exc:                                         # noqa: BLE001
                out.setdefault("latency_failures", []).append(category(exc))
    for name, xs in (("simple_completion", simple), ("tool_call_completion", tool)):
        out["steps"][name] = {"n": len(xs), "p50_s": round(statistics.median(xs), 3) if xs else None,
                              "p95_s": pct(xs, 95) if len(xs) >= 20 else "n/a (n<20)",
                              "max_s": round(max(xs), 3) if xs else None}
    return finish("COMPLETED")


if __name__ == "__main__":
    main()
