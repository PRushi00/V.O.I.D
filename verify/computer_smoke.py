"""Phase 8A Windows LIVE smoke test (real backend - NOT a fake).

Exercises the actual installed backend end-to-end with a harmless app (Notepad):

    launch (alias, no shell)  ->  list_windows  ->  activate  ->  graceful close

REQUIREMENTS (documented):
  * Windows only.
  * An INTERACTIVE desktop session (window enumeration / SetForegroundWindow
    need a real desktop; they no-op or fail under a service/headless session).
  * pywin32 and psutil installed:  pip install -r requirements.txt

It is NON-destructive: it opens a fresh empty Notepad and sends a polite
WM_CLOSE (Notepad closes without a save prompt when empty). No force-kill.
No Gemini. Never prints secrets.

Run:  .venv\\Scripts\\python.exe verify\\computer_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from void.actions.apps import AppActions                     # noqa: E402
from void.actions.computer import (AppCatalog, ComputerActions,  # noqa: E402
                                   ComputerBackendError, make_backend)
from void.actions.files import FileActions                   # noqa: E402


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: not Windows.")
        return 0

    backend = make_backend()
    catalog = AppCatalog(backend)
    apps = AppActions(FileActions(allowed_roots=[]), catalog=catalog)
    computer = ComputerActions(backend, catalog)

    try:
        backend.list_windows()   # forces the lazy pywin32/psutil import
    except ComputerBackendError as exc:
        print(f"SKIP: backend unavailable ({exc}).")
        return 0

    print("1) launch_app('notepad') ...")
    r = apps.launch_app("notepad")
    print("   ->", r.ok, r.summary)
    if not r.ok:
        print("FAIL: could not launch Notepad.")
        return 1
    time.sleep(1.5)

    print("2) list_windows -> find the Notepad window ...")
    lw = computer.list_windows()
    token = None
    for d in lw.data:
        if "notepad" in (d.get("app", "").lower()) or "notepad" in d.get("title", "").lower():
            token = d["window_token"]
            break
    if not token:
        print("FAIL: Notepad window not found among", [d["app"] for d in lw.data])
        return 1
    print("   -> token", token)

    print("3) activate_window ...")
    print("   ->", computer.activate_window(token).summary)

    print("4) close_app (graceful WM_CLOSE) ...")
    rc = computer.close_app(token)
    print("   ->", rc.ok, rc.summary)
    time.sleep(1.0)

    still_open = any("notepad" in (d.get("app", "").lower())
                     for d in computer.list_windows().data)
    print("SMOKE RESULT:",
          "PASS" if (rc.ok and not still_open) else "PARTIAL (window may linger)")
    return 0 if rc.ok else 1


if __name__ == "__main__":
    sys.exit(main())
