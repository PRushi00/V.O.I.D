"""Scratch: AES-GCM availability, state-dir override semantics, D-13 (silent non-COMPLETED outcomes)."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")
import logging; logging.disable(logging.CRITICAL)

print("== AES-256-GCM (cryptography) ==")
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import cryptography
key = AESGCM.generate_key(bit_length=256); n = os.urandom(12)
ct = AESGCM(key).encrypt(n, b"owner's project folder is X", b"item-id-1")
print(" cryptography", cryptography.__version__, "| roundtrip ok:", AESGCM(key).decrypt(n, ct, b"item-id-1") == b"owner's project folder is X",
      "| wrong AAD rejected:", end=" ")
try: AESGCM(key).decrypt(n, ct, b"other-id"); print("NO (bad)")
except Exception as e: print("yes", type(e).__name__)
import base64; print(" key as keyring string length:", len(base64.b64encode(key)), "bytes (Credential Manager blob limit is 2560)")

print("\n== state-dir override: is an ABSOLUTE app.state_dir honoured? ==")
from void.config import Config
tmp = Path(tempfile.mkdtemp(prefix="void_dev_state_"))
cfg = Config({"app": {"state_dir": str(tmp / "dev-state")}})
print(" state_dir() ->", cfg.state_dir(), "| under home?", str(cfg.state_dir()).startswith(os.path.expanduser("~")))

print("\n== D-13: what does the voice runtime SPEAK when a task is not COMPLETED? ==")
from void.voice.session import VoiceSession
from void.voice.adapters import STT, TTS
from void.core.kill_switch import KillSwitch
from void.core.task import Status
class Rec(TTS):
    def __init__(self): self.spoke = []
    @property
    def is_speaking(self): return False
    def speak(self, t): self.spoke.append(t)
    def stop(self): pass
class Res:
    def __init__(self, status, result, error=None): self.status = status; self.result = result; self.task = type("T", (), {"error": error})()
class Asst:
    def __init__(self, r): self.r = r
    def run(self, transcript): return self.r
class Cap:
    is_open = False
    def open(self): pass
    def stop(self): return []
    def close(self): pass
for label, res in [("COMPLETED with text", Res(Status.COMPLETED, "Launched Notepad.")),
                   ("AWAITING_CONFIRMATION (needs owner approval)", Res(Status.AWAITING_CONFIRMATION, None)),
                   ("FAILED (e.g. provider unreachable)", Res(Status.FAILED, None, "ConnectionError")),
                   ("BLOCKED (ambiguous folder, needs choice)", Res(Status.BLOCKED, None))]:
    tts = Rec(); s = VoiceSession(Asst(res), KillSwitch(), capture=Cap(), stt=type("S", (STT,), {"transcribe": lambda self, a: "x"})(), tts=tts)
    s._last_transcript = "do something"; s._state = "dispatched"
    s._run_dispatch(s.generation)
    import time; time.sleep(0.15)
    print(f" {label:48s} -> spoken: {tts.spoke!r} | session state after: {s.state}")
