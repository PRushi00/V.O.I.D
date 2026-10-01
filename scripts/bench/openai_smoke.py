"""Minimal real-API check of the OpenAI provider (gpt-5.6-sol). Uses ONLY the primary credential by default (one slot, no rotation; --allow-backups applies the normal policy),
makes a handful of tiny requests, and prints only safe metadata: model, slot, latency, success, error category and
tool-call correctness. It never prints, logs or writes a key, a header or a response body.

    python scripts/bench/openai_smoke.py [--reps 3] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from void.providers.base import ProviderUnavailable, ToolSpec           # noqa: E402
from void.providers.openai_provider import (                            # noqa: E402
    SLOT_NAMES, OpenAIProvider, _read_slot)
from void.security.credentials import CredentialPool                    # noqa: E402

LAUNCH = ToolSpec(name="launch_app", description="Launch a known application by alias, e.g. notepad.",
                  parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
SYSTEM = {"role": "system", "content": "You are V.O.I.D, a concise desktop assistant. Use tools when asked to act."}
CASES = {
    "conversational": ([SYSTEM, {"role": "user", "content": "In one short sentence, what can you help me with?"}], None, None),
    "tool_selection": ([SYSTEM, {"role": "user", "content": "Please open notepad."}], [LAUNCH], "launch_app"),
    "structured_tool_call": ([SYSTEM, {"role": "user", "content": "Launch the app called calc."}], [LAUNCH], "launch_app"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json", default="")
    ap.add_argument("--allow-backups", action="store_true",
                    help="use the normal credential policy (primary first, backups only after a credential-specific failure) "
                         "instead of the primary alone; use when the primary is known to be unavailable")
    args = ap.parse_args()
    if args.allow_backups:
        p = OpenAIProvider()
    else:
        pool = CredentialPool(primary_name=SLOT_NAMES[0], manifest_key="_smoke_none",
                              get_secret=lambda k: _read_slot(k) if k == SLOT_NAMES[0] else None)
        p = OpenAIProvider(credential_pool=pool)
    out = {"model": p.model, "slots": "policy (primary first)" if args.allow_backups else "primary only", "cases": {}}
    for name, (msgs, tools, want) in CASES.items():
        rows = []
        for _ in range(args.reps):
            t0 = time.perf_counter()
            row = {"ok": False, "category": None, "correct": None}
            try:
                r = p.generate(msgs, tools=tools)
                row["ok"] = True
                if want:
                    row["correct"] = bool(r.tool_calls) and r.tool_calls[0].name == want and isinstance(
                        r.tool_calls[0].arguments.get("name"), str)
                else:
                    row["correct"] = bool(r.text)
            except ProviderUnavailable as exc:
                row["category"] = "unavailable"
            except Exception as exc:                                     # noqa: BLE001
                row["category"] = type(exc).__name__
            row["latency_s"] = round(time.perf_counter() - t0, 3)
            rows.append(row)
        lat = [r["latency_s"] for r in rows if r["ok"]]
        out["cases"][name] = {"n": len(rows), "ok": sum(r["ok"] for r in rows), "correct": sum(bool(r["correct"]) for r in rows),
                              "p50_s": round(statistics.median(lat), 3) if lat else None,
                              "max_s": max(lat) if lat else None, "categories": sorted({r["category"] for r in rows if r["category"]})}
    out["slot_state_after"] = p.credential_status()
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
