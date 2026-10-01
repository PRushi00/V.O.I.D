"""Live validation of V.O.I.D's configured Gemini provider (the REAL config: model, deadline, thinking, credential pool with its
existing rotation). Opt-in tooling, never run by pytest. Prints ONLY safe metadata - latency, categories, the model the config
requests, exact-match and tool-call correctness, safe credential state - never a key, a header, or a response body.

    python scripts/bench/gemini_validate.py [--n 5] [--json out.json]

Steps: (1) exact-reply completion; (2) a real tool round trip through the Agent (kill switch -> RiskGate -> capability) with one
harmless read-only tool in a throw-away folder; (3) a small latency sample (simple + tool-call completions).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway_validate import EXPECTED, LIST, SYSTEM, pct                  # noqa: E402
from void.actions.files import FileActions                                  # noqa: E402
from void.actions.registry import ToolRegistry                              # noqa: E402
from void.config import Config                                              # noqa: E402
from void.core.agent import Agent                                           # noqa: E402
from void.core.kill_switch import KillSwitch                                # noqa: E402
from void.core.task import TaskStore                                        # noqa: E402
from void.providers.gemini_provider import classify_failure                 # noqa: E402
from void.providers.registry import ProviderRegistry                        # noqa: E402
from void.security.protected import EngineProtected                         # noqa: E402
from void.security.risk import RiskGate                                     # noqa: E402


def cred_state(p):
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    out = []
    for i, c in enumerate(p._pool().credentials()):
        try:
            p._pool().get_value(c)
            configured = True
        except Exception:                                                     # noqa: BLE001
            configured = False
        out.append({"slot": "primary" if i == 0 else f"backup_{i}", "configured": configured,
                    "cooling_down": not c.is_available(now)})
    return out


def cat(exc) -> str:
    try:
        return classify_failure(exc)
    except Exception:                                                         # noqa: BLE001
        return type(exc).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    cfg = Config.load()
    reg = ProviderRegistry.from_config(cfg)
    p = reg.get("gemini")
    out = {"provider": "gemini", "requested_model": p.model, "timeout_s": p.timeout_s,
           "thinking": p.thinking_setting, "configured_primary": cfg.get("llm.primary"),
           "credentials_before": cred_state(p), "steps": {}}

    def finish(status):
        out["credentials_after"] = cred_state(p)
        out["status"] = status
        print(json.dumps(out, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")

    # Like the Agent, tolerate a transient server error a bounded number of times (2 retries, short backoff); a permanent or
    # credential failure stops immediately. Attempts and per-attempt categories are recorded.
    attempts = []
    r = None
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            r = p.generate([SYSTEM, {"role": "user", "content": "Reply with exactly: V.O.I.D_GEMINI_38_OK"}])
            break
        except Exception as exc:                                              # noqa: BLE001
            attempts.append({"category": cat(exc), "latency_s": round(time.perf_counter() - t0, 3)})
            if cat(exc) not in ("server", "network"):
                break
            time.sleep(2.0 * (attempt + 1))
    if r is None:
        out["steps"]["completion"] = {"ok": False, "failed_attempts": attempts}
        return finish("STOPPED: completion failed (%s)" % attempts[-1]["category"])
    usage = getattr(r.raw, "usage_metadata", None)
    out["steps"]["completion"] = {
        "ok": True, "latency_s": round(time.perf_counter() - t0, 3), "failed_attempts_before_success": attempts,
        "exact_match": (r.text or "").strip() == "V.O.I.D_GEMINI_38_OK",
        "response_model_version": getattr(r.raw, "model_version", None) if isinstance(getattr(r.raw, "model_version", None), str) else None,
        "usage": {k: getattr(usage, k) for k in ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")
                  if isinstance(getattr(usage, k, None), int)} if usage else None}

    with tempfile.TemporaryDirectory(prefix="void-gem-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        (root / "hello.txt").write_text("hi", encoding="utf-8")
        state = root / ".void"
        state.mkdir()
        fa = FileActions([root], engine_protected=EngineProtected.default(state_dir=state))
        tools = ToolRegistry()
        tools.register_all([t for t in fa.tools() if t.name == "list_directory"])
        agent = Agent(provider=p, tools=tools, risk_gate=RiskGate("high"), kill_switch=KillSwitch(),
                      store=TaskStore(root / "t.sqlite"), max_retries=2)
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
            "final_answer_mentions_file": "hello" in (res.result or "").lower(),
            "llm_calls_in_task": sum(1 for m in res.task.messages if m.get("role") == "assistant")}

    simple, tool, failures = [], [], []
    for _ in range(max(1, args.n)):
        for bucket, msgs, tls in (
                (simple, [SYSTEM, {"role": "user", "content": "In one short sentence, what can you help me with?"}], None),
                (tool, [SYSTEM, {"role": "user", "content": "List the folder C:\\example."}], [LIST])):
            t0 = time.perf_counter()
            try:
                p.generate(msgs, tools=tls)
                bucket.append(time.perf_counter() - t0)
            except Exception as exc:                                          # noqa: BLE001
                failures.append(cat(exc))
    for name, xs in (("simple_completion", simple), ("tool_call_completion", tool)):
        out["steps"][name] = {"n": len(xs), "p50_s": round(statistics.median(xs), 3) if xs else None,
                              "p95_s": pct(xs, 95) if len(xs) >= 20 else "n/a (n<20)",
                              "max_s": round(max(xs), 3) if xs else None}
    if failures:
        out["latency_failures"] = failures
    return finish("COMPLETED")


if __name__ == "__main__":
    main()
