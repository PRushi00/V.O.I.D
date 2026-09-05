"""V.O.I.D laptop verification - runs the four unverified checks on Windows.

Harmless and reversible only: no personal files are read, modified, moved, or
deleted. The only files written are inside the project's verify/ folder plus a
temp file in the OS temp dir (which this script deletes). The Gemini API key is
read from the OS credential store, is NEVER printed, and is scrubbed from any
error text before it is written anywhere.

Outputs (in verify/):
    verify_report.json   machine-readable results
    verify_report.txt    human-readable report
    widget_render.png    rendered image of the circular widget (check 4)
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
VERIFY_DIR = Path(__file__).resolve().parent

results: list[dict] = []


def add(name, test, command, expected, actual, passed, error="", fix=""):
    results.append({
        "name": name, "test": test, "command": command,
        "expected": expected, "actual": actual,
        "status": "PASS" if passed else "FAIL",
        "error": error, "fix": fix,
    })


# --- Check 1: Gemini connectivity -------------------------------------

def check_gemini():
    name = "1. Gemini connectivity"
    command = ("secrets.get_secret('gemini_api_key') -> "
               "genai.GenerativeModel('<model>').generate_content('Reply with exactly: OK')")
    expected = "A non-empty text response from Gemini (e.g. 'OK')"
    key = None
    try:
        from void.security import secrets
        from void.config import Config
        cfg = Config.load()
        model_name = cfg.get("llm.gemini.model", "gemini-1.5-flash")
        key = secrets.get_secret(secrets.GEMINI_API_KEY)

        def scrub(s: str) -> str:
            return s.replace(key, "***REDACTED***") if key else s

        if not key:
            add(name,
                "Read key from Windows Credential Manager; make a minimal live call",
                command, expected,
                "No Gemini API key found in the OS credential store; a live call "
                "cannot be made without one.",
                False, error="no key stored",
                fix="Run 'python -m void set-key gemini' (the key is entered "
                    "hidden and never printed), then re-run this check.")
            return
        try:
            import google.generativeai as genai
        except ImportError as e:
            add(name, "Import google-generativeai", command, expected,
                "google-generativeai is not installed in this environment.",
                False, error=str(e),
                fix="pip install -r requirements.txt")
            return

        genai.configure(api_key=key)
        model = genai.GenerativeModel(model_name)
        t0 = time.time()
        resp = model.generate_content("Reply with exactly: OK")
        dt = time.time() - t0
        text = (getattr(resp, "text", "") or "").strip()
        add(name,
            f"Minimal live generate_content call on '{model_name}' using the "
            f"stored key (key never printed)",
            command, expected,
            f"HTTP call succeeded in {dt:.2f}s; response text = {text!r}",
            bool(text))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if key:
            msg = msg.replace(key, "***REDACTED***")
        add(name, "Minimal live generate_content call", command, expected,
            f"Call failed: {type(e).__name__}",
            False, error=msg[:600],
            fix="Confirm the stored key is valid and network is reachable; "
                "check the model name in config.")


# --- Check 2: Local Ollama connectivity -------------------------------

def check_ollama():
    name = "2. Local Ollama connectivity"
    from void.config import Config
    cfg = Config.load()
    base = cfg.get("llm.local.base_url", "http://localhost:11434")
    url = f"{base}/api/tags"
    command = f"HTTP GET {url}"
    expected = "HTTP 200 with a JSON list of locally installed models"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            status = r.status
            data = json.loads(r.read().decode("utf-8"))
        models = [m.get("name") for m in data.get("models", [])]
        add(name, "HTTP GET /api/tags on the local Ollama server",
            command, expected,
            f"HTTP {status}; installed models: {models}",
            status == 200)
    except Exception as e:  # noqa: BLE001
        add(name, "HTTP GET /api/tags on the local Ollama server",
            command, expected,
            f"Could not connect: {type(e).__name__}",
            False, error=str(e)[:300],
            fix="Ollama is the OPTIONAL offline fallback (Gemini is primary). "
                "To enable: install Ollama (free, local), run 'ollama serve', "
                "then 'ollama pull llama3.1:8b'.")


# --- Check 3: Windows app launching / os.startfile --------------------

def check_windows_launch():
    name = "3. Windows app launching / os.startfile"
    command = ("AppActions.open_path(<temp .txt>) [os.startfile]; "
               "AppActions.launch_app('calc')")
    expected = ("Both return ok=True (a text viewer and Calculator open); "
                "no personal files touched")
    tmp = Path(tempfile.gettempdir()) / "void_verify_open.txt"
    try:
        from void.actions.files import FileActions
        from void.actions.apps import AppActions
        tmp.write_text("V.O.I.D verification temp file - safe to delete.",
                       encoding="utf-8")
        fa = FileActions(allowed_roots=[Path(tempfile.gettempdir())])
        aa = AppActions(fa)
        r1 = aa.open_path(str(tmp))
        r2 = aa.launch_app("calc")
        ok = r1.ok and r2.ok
        add(name,
            "Open a fresh temp .txt via os.startfile and launch Calculator, "
            "both through void's real AppActions code",
            command, expected,
            f"open_path -> ok={r1.ok} ({r1.summary!r}); "
            f"launch_app('calc') -> ok={r2.ok} ({r2.summary!r})",
            ok,
            fix="" if ok else "Verify os.startfile works and Calculator is "
                              "installed/on PATH.")
    except Exception as e:  # noqa: BLE001
        add(name, "Open temp file + launch Calculator via AppActions",
            command, expected,
            f"Exception: {type(e).__name__}",
            False, error=str(e)[:400],
            fix="Investigate the traceback in setup_log.txt.")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# --- Check 4: PySide6 circular widget ---------------------------------

def check_widget():
    name = "4. PySide6 circular widget"
    png = VERIFY_DIR / "widget_render.png"
    command = "VoidWidget().grab().save('verify/widget_render.png')"
    expected = "Widget class constructs and renders; a PNG image is produced"
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from void.ui.widget import VoidWidget
        w = VoidWidget()
        w.resize(340, 300)
        pix = w.grab()  # renders offscreen; no visible window needed
        saved = bool(pix.save(str(png)))
        size = png.stat().st_size if png.exists() else 0
        add(name,
            "Construct QApplication + VoidWidget and render it to a PNG",
            command, expected,
            f"Constructed OK; PNG saved={saved}; file={png.name}; "
            f"bytes={size}; pixmap={pix.width()}x{pix.height()}",
            saved and size > 0)
        try:
            app.quit()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        add(name, "Construct + render VoidWidget", command, expected,
            f"Exception: {type(e).__name__}",
            False, error=str(e)[:400],
            fix="Ensure PySide6 is installed (pip install -r requirements.txt) "
                "and a desktop session is available.")


def main() -> int:
    check_gemini()
    check_ollama()
    check_windows_launch()
    check_widget()

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "results": results,
    }
    (VERIFY_DIR / "verify_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "V.O.I.D LAPTOP VERIFICATION REPORT",
        f"Time: {report['timestamp']}",
        f"Python: {report['python']}  (venv: {report['in_venv']})",
        f"Executable: {report['executable']}",
        f"Platform: {report['platform']}",
        "=" * 70,
    ]
    for r in results:
        lines += [
            f"[{r['status']}] {r['name']}",
            f"    Test performed : {r['test']}",
            f"    Command/action : {r['command']}",
            f"    Expected       : {r['expected']}",
            f"    Actual         : {r['actual']}",
            f"    Error          : {r['error'] or '(none)'}",
            f"    Recommended fix: {r['fix'] or '(none)'}",
            "-" * 70,
        ]
    (VERIFY_DIR / "verify_report.txt").write_text(
        "\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    print("VERIFY_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
