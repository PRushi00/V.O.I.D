"""Re-run ONLY the Gemini connectivity check.

Reuses the exact check from laptop_check.py (no architecture changes). The API
key is read from the OS credential store, never printed, and scrubbed from any
error text - identical guarantees to the full verifier.

Output: verify/gemini_report.txt
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

VERIFY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VERIFY_DIR.parent))  # project root
sys.path.insert(0, str(VERIFY_DIR))         # for importing laptop_check

import laptop_check as lc  # noqa: E402

lc.check_gemini()
r = lc.results[0]

lines = [
    "V.O.I.D GEMINI RE-VERIFICATION",
    f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
    f"Python: {sys.version.split()[0]}",
    "=" * 70,
    f"[{r['status']}] {r['name']}",
    f"    Test performed : {r['test']}",
    f"    Command/action : {r['command']}",
    f"    Expected       : {r['expected']}",
    f"    Actual         : {r['actual']}",
    f"    Error          : {r['error'] or '(none)'}",
    f"    Recommended fix: {r['fix'] or '(none)'}",
    "-" * 70,
]
report = "\n".join(lines)
(VERIFY_DIR / "gemini_report.txt").write_text(report, encoding="utf-8")
print(report)
print("GEMINI_CHECK_DONE")
