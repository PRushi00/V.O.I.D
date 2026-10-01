"""What does going through MCP cost, against calling the same V.O.I.D capability directly?

Three layers are timed separately, because they answer different questions:

  direct      Agent.invoke_tool on the existing capability - the floor, and what V.O.I.D itself pays
  adapter     VoidMcpAdapter.<tool> - the floor plus argument rules and result shaping, no protocol
  protocol    a real MCP client round trip over the in-memory transport - the above plus JSON-RPC and validation

The point is to show that MCP is a protocol adapter and not another reasoning layer: no model is consulted at any
layer, and the overhead is serialisation, not inference.

Launches are recorded rather than performed, and the catalog is a fixture, so this measures V.O.I.D's own work and
not Windows' process creation. Nothing is launched and no path is opened.

usage:
    python scripts/bench/mcp_overhead_bench.py --repeats 200
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def q(values, frac):
    values = sorted(values)
    return values[min(len(values) - 1, int(frac * len(values)))]


def report(label, samples):
    print("%-44s p50=%7.3f ms  p90=%7.3f ms  max=%7.3f ms"
          % (label, statistics.median(samples) * 1000, q(samples, 0.9) * 1000, max(samples) * 1000))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=200)
    args = ap.parse_args()

    import tempfile

    from mcp.client.client import Client

    from tests.test_computer import FakeBackend, _exe
    from void.actions.apps import AppActions
    from void.actions.computer import AppCatalog, ComputerActions
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.app import Assistant
    from void.config import Config
    from void.mcp.adapter import VoidMcpAdapter
    from void.mcp.server import build_server
    from void.providers.base import LLMProvider, LLMResponse
    from void.providers.registry import ProviderRegistry
    from void.security.protected import EngineProtected

    class Counting(LLMProvider):
        def __init__(self, name):
            self.name, self.calls = name, 0

        def available(self):
            return True

        def generate(self, messages, tools=None):
            self.calls += 1
            return LLMResponse(text="(model)")

    tmp = Path(tempfile.mkdtemp())
    root = tmp / "allowed"
    root.mkdir()
    cfg = Config({"app": {"state_dir": str(tmp / ".void")}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(root)]}})
    assistant = Assistant(config=cfg)
    backend = FakeBackend(apps=[{"name": "WhatsApp", "kind": "exe", "target": _exe(tmp, "w.exe")}])
    catalog = AppCatalog(backend)
    launched: list = []
    fa = FileActions([root], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    assistant.tools = ToolRegistry()
    assistant.tools.register_all(fa.tools())
    assistant.tools.register_all(AppActions(fa, catalog=catalog,
                                            launcher=lambda k, t: launched.append(t)).tools())
    assistant.tools.register_all(ComputerActions(backend, catalog).tools())
    gemini, local = Counting("gemini"), Counting("local")
    assistant.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])

    adapter = VoidMcpAdapter(assistant=assistant)
    catalog.entries()                                  # discovery is start-up cost, measured elsewhere
    n = args.repeats
    print(f"repeats={n}  (launches recorded, catalog is a fixture)\n")

    # --- direct: the existing funnel, nothing else -----------------------------------------------
    app_id = adapter.find_application("whatsapp").resolved.app_id
    direct = []
    for _ in range(n):
        t0 = time.perf_counter()
        adapter._agent().invoke_tool("launch_app", {"name": app_id})
        direct.append(time.perf_counter() - t0)
    report("direct   Agent.invoke_tool(launch_app)", direct)

    # --- adapter: argument rules + resolution + result shaping ------------------------------------
    via_adapter = []
    for _ in range(n):
        t0 = time.perf_counter()
        adapter.launch_application("whatsapp")
        via_adapter.append(time.perf_counter() - t0)
    report("adapter  launch_application('whatsapp')", via_adapter)

    find_adapter = []
    for _ in range(n):
        t0 = time.perf_counter()
        adapter.find_application("whatsapp")
        find_adapter.append(time.perf_counter() - t0)
    report("adapter  find_application('whatsapp')", find_adapter)

    status = []
    for _ in range(max(10, n // 10)):
        t0 = time.perf_counter()
        adapter.get_system_info()
        status.append(time.perf_counter() - t0)
    report("adapter  get_system_info", status)

    # --- protocol: a real client round trip -------------------------------------------------------
    server = build_server(adapter)

    async def protocol(tool, payload, count):
        out = []
        async with Client(server) as c:
            await c.list_tools()                       # exclude first-call warm-up from the samples
            for _ in range(count):
                t0 = time.perf_counter()
                await c.call_tool(tool, payload)
                out.append(time.perf_counter() - t0)
        return out

    report("protocol tools/call launch_application", asyncio.run(protocol("launch_application",
                                                                         {"name": "whatsapp"}, n)))
    report("protocol tools/call find_application", asyncio.run(protocol("find_application",
                                                                       {"name": "whatsapp"}, n)))
    report("protocol tools/call get_system_info", asyncio.run(protocol("get_system_info", {},
                                                                      max(10, n // 10))))

    async def handshake(count):
        out = []
        for _ in range(count):
            t0 = time.perf_counter()
            async with Client(server) as c:
                await c.list_tools()
            out.append(time.perf_counter() - t0)
        return out

    report("protocol initialize + tools/list (per client)", asyncio.run(handshake(max(5, n // 20))))

    overhead = statistics.median(via_adapter) - statistics.median(direct)
    print(f"\nadapter overhead over the bare funnel: {overhead * 1000:+.3f} ms (p50)")
    print(f"launches recorded: {len(launched)} (nothing was started)")
    print(f"model calls across the whole run: gemini {gemini.calls}, ollama {local.calls}")


if __name__ == "__main__":
    main()
