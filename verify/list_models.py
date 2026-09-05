"""Read-only capability probe: list Gemini models available to the stored key.

Calls google-generativeai's list_models() (a read-only ListModels API call) and
reports every model that supports generateContent, with its display name,
supported methods, and token limits. The API key is read from the OS credential
store, never printed, and scrubbed from any error text.

Output: verify/models_report.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

VERIFY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VERIFY_DIR.parent))  # project root

from void.security import secrets  # noqa: E402

OUT = VERIFY_DIR / "models_report.txt"


def main() -> int:
    key = secrets.get_secret(secrets.GEMINI_API_KEY)
    if not key:
        OUT.write_text("NO KEY STORED", encoding="utf-8")
        print("NO KEY STORED")
        return 1

    def scrub(s: str) -> str:
        return s.replace(key, "***REDACTED***") if key else s

    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        rows = []
        for m in genai.list_models():
            methods = list(getattr(m, "supported_generation_methods", []) or [])
            if "generateContent" not in methods:
                continue
            rows.append({
                "name": m.name,
                "display": getattr(m, "display_name", "") or "",
                "methods": methods,
                "in_limit": getattr(m, "input_token_limit", ""),
                "out_limit": getattr(m, "output_token_limit", ""),
            })
    except Exception as e:  # noqa: BLE001
        msg = scrub(str(e))
        OUT.write_text(f"ERROR: {type(e).__name__}: {msg}", encoding="utf-8")
        print(f"ERROR: {type(e).__name__}: {msg}")
        return 1

    lines = [
        "GEMINI MODELS AVAILABLE TO THIS KEY (support generateContent)",
        f"count={len(rows)}",
        "=" * 90,
    ]
    for r in rows:
        lines.append(
            f"{r['name']}  |  {r['display']}  |  in={r['in_limit']} "
            f"out={r['out_limit']}  |  methods={','.join(r['methods'])}"
        )
    report = "\n".join(lines)
    OUT.write_text(report, encoding="utf-8")
    print(report)
    print("LISTMODELS_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
