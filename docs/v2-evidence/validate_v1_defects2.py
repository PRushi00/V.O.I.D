"""Second scratch validation: (a) kill during an executing tool, with the REAL sleep
preserved; (b) can a file the AGENT creates (MEDIUM, autonomous) become a valid
pairing window?  Temp dirs + fakes only."""
import json, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")
REAL_SLEEP = time.sleep

from void.actions.base import Tool, ToolResult
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import TaskStore
from void.device.pairing import PairingManager
from void.providers.base import LLMProvider, LLMResponse, ToolCall
from void.security.risk import RiskGate, RiskLevel
import logging; logging.disable(logging.CRITICAL)

tmp = Path(tempfile.mkdtemp(prefix="void_validate2_"))

print("== Kill/abort while a tool is executing ==")
ks = KillSwitch(); started = threading.Event(); finished = {}
def slow(**kw):
    started.set(); REAL_SLEEP(1.5); finished["t"] = time.perf_counter(); return ToolResult.success("slow tool finished")
reg = ToolRegistry(); reg.register(Tool(name="slow_tool", description="d", parameters={"type": "object"}, handler=slow, risk=RiskLevel.LOW))
class Script(LLMProvider):
    name = "script"; n = 0
    def available(self): return True
    def generate(self, messages, tools=None):
        Script.n += 1
        return LLMResponse(tool_calls=[ToolCall(name="slow_tool", arguments={})]) if Script.n == 1 else LLMResponse(text="second LLM turn ran")
def killer():
    started.wait(); REAL_SLEEP(0.2); finished["kill_at"] = time.perf_counter(); ks.engage("scratch stop")
threading.Thread(target=killer, daemon=True).start()
t0 = time.perf_counter()
r = Agent(Script(), reg, RiskGate(), ks, TaskStore(tmp / "k.sqlite")).run("x")
total = time.perf_counter() - t0
print(f" stop engaged at +{finished['kill_at']-t0:.2f}s (tool started ~0s, needs 1.5s)")
print(f" agent returned at +{total:.2f}s | status={r.status} | LLM turns={Script.n} | ledger={[e['status'] for e in r.task.plan]}")
print(" => in-flight tool was NOT interrupted; it ran to completion, was committed, and the next LLM turn was prevented."
      if total >= 1.4 and Script.n == 1 else " => unexpected result; inspect")

print("\n== Can an agent-created file become a valid pairing window? ==")
state = tmp / "home" / ".void"; state.mkdir(parents=True)
fa = FileActions(allowed_roots=[tmp / "home"], delete_to_recycle_bin=True, protected_roots=[])
reg2 = ToolRegistry(); reg2.register_all(fa.tools())
planted = json.dumps({"token": "ATTACKER-CHOSEN-TOKEN", "name": "evil-phone", "expires_at": time.time() + 3600})
t = reg2.get("write_file"); args = {"path": str(state / "pairing_window.json"), "content": planted}
risk = t.effective_risk(args); allowed = RiskGate().authorize(risk, "write_file", None)
res = reg2.execute("write_file", args) if allowed else None
print(f" write_file(pairing_window.json, new): risk={risk.name} authorized_unattended={allowed} result_ok={bool(res and res.ok)}")
try:
    name = PairingManager(state).redeem("ATTACKER-CHOSEN-TOKEN")
    print(f" PairingManager.redeem(attacker token) -> SUCCESS, device name '{name}' (an attacker on the LAN could now pair)")
except Exception as e:
    print(" redeem failed:", type(e).__name__, e)
