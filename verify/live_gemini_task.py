"""Milestone verification: one LIVE Gemini tool-calling task, end to end.

Runs the REAL V.O.I.D agent (real config, real tools, real risk gate, real
Gemini brain) on a read-only, non-destructive goal and records the full
tool-calling transcript. The confirmer denies high-risk actions (fail-safe), so
even if the model tried something destructive it would be blocked - but the
goal only needs LOW-risk tools (search_files, open_path).

The API key is read from the OS credential store by the provider and is never
printed or written anywhere by this script.

Output: verify/live_task_report.txt
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

VERIFY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VERIFY_DIR.parent))  # project root

from void.app import Assistant  # noqa: E402
from void.providers.base import ProviderUnavailable  # noqa: E402

OUT = VERIFY_DIR / "live_task_report.txt"
GOAL = "Find the file named void_test_note.txt and open it."


def main() -> int:
    events: list[str] = []
    lines = [
        "V.O.I.D LIVE GEMINI TOOL-CALLING TASK",
        f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"Goal: {GOAL}",
        "=" * 72,
    ]
    try:
        # confirm_fn=None => high-risk actions are DENIED (fail-safe). The goal
        # only needs low-risk tools, so nothing should need confirmation.
        assistant = Assistant(confirm_fn=None, on_event=lambda m: events.append(m))
        lines.append(
            "Allowed roots: "
            + str([str(r) for r in assistant.config.allowed_roots()])
        )
        provider = assistant.providers.select()
        lines.append(f"Provider selected: {provider.name}")
        lines.append(f"Gemini model: {assistant.config.get('llm.gemini.model')}")
        lines.append("")
        result = assistant.run(GOAL)
    except ProviderUnavailable as exc:
        lines.append(f"PROVIDER UNAVAILABLE: {exc}")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 1
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ERROR: {type(exc).__name__}: {exc}")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 1

    lines.append("--- TOOL-CALLING SEQUENCE ---")
    step = 0
    for m in result.task.messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                step += 1
                # Diagnostic only: presence + byte length of the signature,
                # never the value itself.
                sig = tc.get("signature")
                if sig:
                    try:
                        sig_info = f"signature present ({len(base64.b64decode(sig))} bytes)"
                    except Exception:
                        sig_info = "signature present (undecodable)"
                else:
                    sig_info = "signature absent"
                lines.append(
                    f"{step}. GEMINI requested tool: {tc['name']}  "
                    f"args={tc.get('arguments')}  [{sig_info}]"
                )
        elif role == "tool":
            preview = " | ".join((m.get("content") or "").splitlines()[:4])[:400]
            lines.append(f"   -> TOOL RESULT [{m.get('name')}]: {preview}")
        elif role == "assistant" and m.get("content"):
            lines.append(f"GEMINI final answer: {m['content']}")

    lines.append("")
    lines.append(f"Status: {result.status}   steps: {result.steps}")
    if result.task.error:
        lines.append(f"Note: {result.task.error}")

    lines.append("")
    lines.append("--- EVENT LOG ---")
    lines += [f"  {e}" for e in events]

    report = "\n".join(lines)
    OUT.write_text(report, encoding="utf-8")
    print(report)
    print("LIVE_TASK_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
