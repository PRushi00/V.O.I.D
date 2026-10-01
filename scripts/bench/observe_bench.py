"""What does each V2 observation cost, and what can this machine actually not tell us?

Two questions, because they are the two that matter for a voice assistant:

  latency      a spoken answer waits for these, so a probe measured in seconds is a product defect
  honesty      every metric that could NOT be read, with the reason - the thing a benchmark usually hides

Three layers are timed separately:

  probe        void.system.* - the raw read from the OS
  tool         void.actions.observe - the probe plus argument rules and the spoken summary
  funnel       Agent.invoke_tool - the above plus kill switch, risk, registry and telemetry

Nothing here changes anything: every call is read-only, no application is launched, no path is opened, and
the camera is not touched. No model is consulted at any layer, which the run asserts rather than assumes.

usage:
    python scripts/bench/observe_bench.py --repeats 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def q(values, frac):
    values = sorted(values)
    return values[min(len(values) - 1, int(frac * len(values)))]


def report(label, samples):
    print("%-46s p50=%8.1f ms  p90=%8.1f ms  max=%8.1f ms"
          % (label, statistics.median(samples) * 1000, q(samples, 0.9) * 1000, max(samples) * 1000))


def timeit(fn, repeats):
    out = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5,
                    help="samples per measurement (these probes are hundreds of ms, not microseconds)")
    args = ap.parse_args()
    n = max(1, args.repeats)

    from void.actions.observe import ObserveActions
    from void.app import Assistant
    from void.core.agent import Agent
    from void.providers.base import LLMProvider, LLMResponse
    from void.providers.registry import ProviderRegistry
    from void.system import devices as device_probe
    from void.system import gpu as gpu_probe
    from void.system import host as host_probe
    from void.system import network as net_probe

    class Counting(LLMProvider):
        def __init__(self, name):
            self.name, self.calls = name, 0

        def available(self):
            return True

        def generate(self, messages, tools=None):
            self.calls += 1
            return LLMResponse(text="(model)")

    print(f"repeats={n}  (every call is read-only; nothing is launched, opened or captured)\n")

    # --- layer 1: the raw probes ------------------------------------------------------------------
    print("-- probes (void.system) --")
    probes = [
        ("host.operating_system", host_probe.operating_system),
        ("host.processor", host_probe.processor),
        ("host.memory", host_probe.memory),
        ("host.storage", host_probe.storage),
        ("host.power", host_probe.power),
        ("host.uptime", host_probe.uptime),
        ("host.temperatures", host_probe.temperatures),
        ("host.processes", host_probe.processes),
        ("host.snapshot (incl. processes)", host_probe.snapshot),
        ("gpu.graphics", gpu_probe.graphics),
        ("devices.audio", device_probe.audio),
        ("devices.cameras", device_probe.cameras),
        ("devices.bluetooth_peers", device_probe.bluetooth_peers),
        ("devices.usb", device_probe.usb),
        ("devices.snapshot", device_probe.snapshot),
        ("network.interfaces", net_probe.interfaces),
        ("network.traffic", net_probe.traffic),
        ("network.connections", net_probe.connections),
        ("network.snapshot", net_probe.snapshot),
    ]
    unavailable: dict[str, str] = {}
    for label, probe in probes:
        report(label, timeit(probe, n))
        try:
            unavailable.update(probe().unavailable)
        except Exception as exc:                                # noqa: BLE001
            unavailable[label] = f"the probe raised {type(exc).__name__}"

    # --- layer 2: the capability tools -----------------------------------------------------------
    print("\n-- tools (void.actions.observe) --")
    actions = ObserveActions()
    tools = [
        ("get_system_status", actions.get_system_status),
        ("diagnose_slowness", actions.diagnose_slowness),
        ("list_processes", actions.list_processes),
        ("list_devices", actions.list_devices),
        ("find_device('headset')", lambda: actions.find_device("headset")),
        ("get_network_status", actions.get_network_status),
        ("list_connections", actions.list_connections),
    ]
    for label, call in tools:
        report(label, timeit(call, n))

    # --- layer 3: through the real funnel --------------------------------------------------------
    print("\n-- funnel (Agent.invoke_tool: kill switch + risk + registry + telemetry) --")
    # A SANDBOXED state directory, never the owner's. The kill switch latches to a file under the state
    # directory and survives a restart by design, so a benchmark pointed at the real one can leave V.O.I.D
    # stopped - which is exactly what happened the first time this script was run.
    import tempfile

    from void.config import Config
    sandbox = Path(tempfile.mkdtemp()) / "state"
    assistant = Assistant(config=Config({"app": {"state_dir": str(sandbox)},
                                         "memory": {"enabled": False}}))
    gemini, local = Counting("gemini"), Counting("local")
    assistant.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])
    agent = Agent(provider=gemini, tools=assistant.tools, risk_gate=assistant.risk_gate,
                  kill_switch=assistant.kill_switch, store=assistant.store,
                  on_event=lambda _m: None, defer_confirmation=True)
    for name, arguments in (("get_system_status", {}), ("diagnose_slowness", {}),
                            ("list_processes", {}), ("list_devices", {}),
                            ("find_device", {"name": "headset"}), ("get_network_status", {}),
                            ("list_connections", {}), ("get_active_window", {}),
                            ("get_camera_status", {})):
        report(f"invoke_tool({name})", timeit(lambda n=name, a=arguments: agent.invoke_tool(n, a), n))

    # --- what this machine cannot answer ----------------------------------------------------------
    print("\n-- metrics this machine does NOT expose (reported, never guessed) --")
    if unavailable:
        for metric, reason in sorted(unavailable.items()):
            print(f"  {metric}: {reason}")
    else:
        print("  (everything the probes ask for is readable here)")

    print(f"\nmodel calls across the whole run: gemini {gemini.calls}, ollama {local.calls}")
    assert gemini.calls == 0 and local.calls == 0, "an observation consulted a model"
    print("every answer above is a measurement, not an inference.")


if __name__ == "__main__":
    main()
