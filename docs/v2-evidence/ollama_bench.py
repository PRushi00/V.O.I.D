"""Measure local-model (Ollama) latency using V1's REAL system prompt and REAL
tool schemas. Read-only w.r.t. V.O.I.D state: builds only tool *specs* (no
Assistant/TaskStore/keyring), sends stdout-only results. Nothing is persisted.
"""
import json, os, sys, time, statistics as st
from pathlib import Path

WT = r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6"
sys.path.insert(0, WT)
import requests

from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, ComputerActions, make_backend
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import SYSTEM_PROMPT
from void.providers.local_provider import LocalProvider

fake_ws = Path(os.environ["USERPROFILE"]) / "VOID" / "workspace"
fa = FileActions(allowed_roots=[fake_ws], delete_to_recycle_bin=True, protected_roots=[])
backend = make_backend()
catalog = AppCatalog(backend)
aa = AppActions(fa, catalog=catalog)
ca = ComputerActions(backend, catalog, protected_processes=[])
reg = ToolRegistry()
reg.register_all(fa.tools()); reg.register_all(aa.tools()); reg.register_all(ca.tools())
specs = reg.specs()
lp = LocalProvider(model="qwen3:8b")
tools_payload = lp._to_tools(specs)
print(f"tools={len(specs)}  system_prompt_chars={len(SYSTEM_PROMPT)}  tool_schema_json_chars={len(json.dumps(tools_payload))}")

BASE = "http://localhost:11434"
print("ollama version:", requests.get(BASE + "/api/version", timeout=5).json())

GOALS = [
    ("simple-launch", "open notepad"),
    ("multi-step",    "find my cybersecurity notes and open them"),
    ("no-tool-chat",  "hi, what can you help me with?"),
]

def run(goal, think):
    body = {"model": "qwen3:8b", "stream": False,
            "options": {"temperature": 0.2},
            "messages": lp._to_messages([{"role": "system", "content": SYSTEM_PROMPT},
                                          {"role": "user", "content": goal}]),
            "tools": tools_payload}
    if think is not None:
        body["think"] = think
    t0 = time.perf_counter()
    r = requests.post(BASE + "/api/chat", json=body, timeout=240)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    p = r.json()
    m = p.get("message", {}) or {}
    calls = [(c.get("function", {}) or {}).get("name") for c in (m.get("tool_calls") or [])]
    ns = 1e9
    return dict(wall=wall, load=p.get("load_duration", 0) / ns,
                prompt_tokens=p.get("prompt_eval_count"),
                prompt_s=p.get("prompt_eval_duration", 0) / ns,
                out_tokens=p.get("eval_count"), gen_s=p.get("eval_duration", 0) / ns,
                calls=calls, has_thinking=bool(m.get("thinking")),
                content_len=len(m.get("content") or ""))

# Warm the model first so 'load' is reported separately from steady-state.
t0 = time.perf_counter()
requests.post(BASE + "/api/chat", json={"model": "qwen3:8b", "stream": False, "think": False,
              "messages": [{"role": "user", "content": "ok"}], "options": {"num_predict": 1}}, timeout=240)
print(f"cold model load + 1 token: {time.perf_counter()-t0:.2f}s\n")

hdr = f"{'goal':14s} {'think':7s} {'wall':>6s} {'prompt_tok':>10s} {'prompt_s':>8s} {'out_tok':>7s} {'gen_s':>6s} {'tok/s':>6s}  tool_calls / thinking"
print(hdr); print("-" * len(hdr))
results = {}
for name, goal in GOALS:
    for think in (None, False):
        for rep in (1, 2):
            try:
                d = run(goal, think)
            except Exception as e:
                print(f"{name:14s} think={think!s:5s} rep{rep} ERROR {type(e).__name__}: {e}")
                continue
            results.setdefault((name, think), []).append(d)
            tps = (d["out_tokens"] / d["gen_s"]) if d["gen_s"] else 0
            print(f"{name:14s} {str(think):7s} {d['wall']:6.2f} {d['prompt_tokens']!s:>10s} {d['prompt_s']:8.2f} "
                  f"{d['out_tokens']!s:>7s} {d['gen_s']:6.2f} {tps:6.1f}  {d['calls']} think={d['has_thinking']}")

print("\nSummary (median wall seconds, warm):")
for (name, think), ds in results.items():
    print(f"  {name:14s} think={str(think):6s} median_wall={st.median(x['wall'] for x in ds):6.2f}s  "
          f"tool_calls={ds[-1]['calls']}")
