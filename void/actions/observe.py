"""Read-only observation capabilities: the machine's health, its attached devices, its network.

These are the tools behind questions like "why is my laptop slow?", "is my headset connected?" and "is
anything listening on this machine?". They are a thin capability layer over :mod:`void.system`, which does
the actual reading; what this module adds is the part that belongs to V.O.I.D rather than to the OS - tool
schemas, argument validation, bounded results, and a one-line summary a model can relay or the owner can
hear without reading a table.

Every tool here shares four properties, and they are asserted as a group in ``tests/test_observe.py`` so a
future tool cannot quietly join this module without them:

1. **Read-only.** Nothing in this module changes any state, on the machine or in V.O.I.D. All are
   :attr:`~void.security.risk.RiskLevel.LOW`, which is the honest classification for an observation that
   cannot be undone because it did nothing.
2. **Never fabricates.** An unreadable metric is reported as unavailable, with the reason. This machine, for
   example, cannot report CPU temperature at all, and says so rather than estimating one from load.
3. **No secrets, no content.** Process rows carry no command lines, network rows carry no payloads, and no
   file is read. See the module docstrings in :mod:`void.system.host` and :mod:`void.system.network` for why
   each of those is a deliberate omission rather than a gap.
4. **Bounded.** Fixed caps on every list and a timeout on every probe, so a wedged driver cannot stall the
   voice loop and a busy machine cannot flood a model's context.

The summaries are written to be *spoken*. A status answer that reads out twenty-four fields is useless from
a voice assistant, so each tool returns a sentence or two as its summary and puts the full reading in
``data`` for a model or the CLI to work through.
"""
from __future__ import annotations

from void import perf
from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel
from void.system import devices as device_probe
from void.system import gpu as gpu_probe
from void.system import host as host_probe
from void.system import network as network_probe

#: How many devices one ``find_device`` answer names in its summary before it stops listing them.
_SPOKEN_DEVICES = 3

#: How many processes a spoken slowness answer names.
_SPOKEN_PROCESSES = 3


def _unavailable_note(reading) -> str:
    """A short, honest tail listing what could not be read, or an empty string."""
    missing = sorted(reading.unavailable)
    if not missing:
        return ""
    shown = ", ".join(missing[:3])
    more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
    return f" Not available on this machine: {shown}{more}."


def _payload(reading, probe: str | None = None) -> dict:
    """A reading as tool ``data``: the facts, and an explicit record of the gaps.

    Also the one place observation telemetry is emitted, so every answer is accounted for exactly once.
    Counts and durations only - the allowlist in :mod:`void.perf.schema` could not carry a device name, a
    process name or an address even if this tried to send one.
    """
    if probe:
        perf.emit("observe", probe=probe, duration_s=round(reading.duration_s, 3),
                  values=len(reading.values), unavailable=len(reading.unavailable))
    return {"values": dict(reading.values),
            "unavailable": dict(reading.unavailable),
            "probe_duration_s": round(reading.duration_s, 3)}


class ObserveActions:
    """The observation tools. Holds no state: every call is a fresh reading.

    Not caching is deliberate. Every question these tools answer is about *now* - "is it busy", "is the
    headset plugged in", "am I online" - and a cached answer to those is a wrong answer. The probes are
    fast enough that there is nothing to buy: the whole host snapshot is under a second, devices and
    network are well under a quarter of one, measured.
    """

    # -- domain 3: system awareness --
    def get_system_status(self, include_processes: bool = True) -> ToolResult:
        """Processor, memory, storage, power, graphics and uptime, in one reading."""
        reading = host_probe.snapshot(include_processes=bool(include_processes))
        try:
            reading.merge(gpu_probe.graphics())
        except Exception as exc:                                # noqa: BLE001
            from void.system import describe_error
            reading.miss("gpu", describe_error(exc))
        parts: list[str] = []
        cpu = reading.get("cpu_percent")
        if cpu is not None:
            parts.append(f"CPU {cpu:.0f}%")
        mem_pct, mem_free = reading.get("memory_percent"), reading.get("memory_available_gb")
        if mem_pct is not None:
            free = f" ({mem_free:.1f} GB free)" if mem_free is not None else ""
            parts.append(f"memory {mem_pct:.0f}%{free}")
        gpu_pct = reading.get("gpu_utilisation_percent")
        if gpu_pct is not None:
            parts.append(f"GPU {gpu_pct:.0f}%")
        for disk in reading.get("disks") or []:
            parts.append(f"drive {disk['mount']} {disk['percent_used']:.0f}% full")
            break
        battery = reading.get("battery_percent")
        if battery is not None:
            plugged = reading.get("power_plugged")
            parts.append(f"battery {battery:.0f}%" + (" on mains" if plugged else " on battery"))
        if not parts:
            return ToolResult.failure(
                "I could not read anything about this machine's state." + _unavailable_note(reading))
        summary = "; ".join(parts) + "." + _unavailable_note(reading)
        return ToolResult.success(summary, data=_payload(reading, "system_status"))

    def list_processes(self, order: str = "cpu", limit: int = host_probe.MAX_PROCESSES) -> ToolResult:
        """The heaviest running processes, by processor or by memory use."""
        reading = host_probe.processes(limit=limit, order=order)
        rows = reading.get("processes") or []
        if not rows:
            return ToolResult.failure(
                "I could not read the process list." + _unavailable_note(reading))
        ordered_by = reading.get("ordered_by")
        named = ", ".join(
            f"{row['name']} ({row['cpu_percent']:.0f}% CPU)" if ordered_by == "cpu"
            else f"{row['name']} ({row['memory_mb']:.0f} MB)"
            for row in rows[:_SPOKEN_PROCESSES]
            if row.get("cpu_percent" if ordered_by == "cpu" else "memory_mb") is not None)
        total = reading.get("process_count")
        head = f"{total} processes are running" if total else f"{len(rows)} processes"
        tail = f"; heaviest by {'processor' if ordered_by == 'cpu' else 'memory'}: {named}" if named else ""
        return ToolResult.success(head + tail + "." + _unavailable_note(reading),
                                  data=_payload(reading, "processes"))

    def diagnose_slowness(self) -> ToolResult:
        """Collect the machine's vital signs and report what, if anything, is actually under strain.

        This is the measurement-and-analysis half of "why is my laptop slow?". It states what it observed
        and what crossed a threshold; it does not guess at causes it cannot see, and when nothing is under
        strain it says so plainly, which is a real answer rather than a failure to find one.
        """
        reading = host_probe.snapshot(include_processes=True)
        try:
            reading.merge(gpu_probe.graphics())
        except Exception:                                      # noqa: BLE001 - absence is normal
            pass
        notes = host_probe.pressure(reading)
        if notes:
            summary = " ".join(notes)
        else:
            observed: list[str] = []
            for label, key, unit in (("CPU", "cpu_percent", "%"), ("memory", "memory_percent", "%"),
                                     ("GPU", "gpu_utilisation_percent", "%")):
                value = reading.get(key)
                if value is not None:
                    observed.append(f"{label} at {value:.0f}{unit}")
            detail = ", ".join(observed)
            summary = ("Nothing on this machine looks overloaded right now"
                       + (f" - {detail}." if detail else "."))
        return ToolResult.success(summary + _unavailable_note(reading),
                                 data={**_payload(reading, "diagnose"), "observations": notes})

    # -- domain 6: connectivity and devices --
    def list_devices(self, kind: str = "all") -> ToolResult:
        """What is attached: audio inputs and outputs, cameras, Bluetooth peers, USB hardware.

        ``kind`` narrows the answer and is validated against a fixed set; anything unrecognised widens to
        ``all`` rather than reaching a probe, because this argument can come from a model.
        """
        wanted = (kind or "all").strip().lower()
        groups = {"audio": ("audio_inputs", "audio_outputs"),
                  "audio_input": ("audio_inputs",), "microphone": ("audio_inputs",),
                  "audio_output": ("audio_outputs",), "speaker": ("audio_outputs",),
                  "camera": ("cameras",), "bluetooth": ("bluetooth_peers",), "usb": ("usb",)}
        keys = groups.get(wanted, ("audio_inputs", "audio_outputs", "cameras",
                                   "bluetooth_peers", "usb"))
        reading = device_probe.snapshot()
        counts: list[str] = []
        kept: dict[str, list] = {}
        labels = {"audio_inputs": "microphone", "audio_outputs": "audio output",
                  "cameras": "camera", "bluetooth_peers": "Bluetooth device", "usb": "USB device"}
        for key in keys:
            rows = reading.get(key) or []
            kept[key] = rows
            if rows:
                label = labels[key]
                counts.append(f"{len(rows)} {label}" + ("" if len(rows) == 1 else "s"))
        if not counts:
            return ToolResult.failure(
                "I could not find any device of that kind on this machine." + _unavailable_note(reading))
        data = _payload(reading, "devices")
        data["values"] = kept
        return ToolResult.success("Attached: " + ", ".join(counts) + "." + _unavailable_note(reading),
                                  data=data)

    def find_device(self, name: str) -> ToolResult:
        """Whether a particular device is attached, by name or by kind ("my headset", "webcam").

        Answers with what was actually found rather than with a yes: a match on the *kind* of device is
        reported as such, so "is my headset connected?" cannot be answered by a device that merely happens
        to be an audio output.
        """
        query = (name or "").strip()
        if not query:
            return ToolResult.failure("No device name was given to look for.")
        reading = device_probe.snapshot()
        hits = device_probe.find(query, reading)
        if not hits:
            return ToolResult.success(
                f"I cannot see anything matching '{query}' attached to this machine."
                + _unavailable_note(reading),
                data={**_payload(reading, "find_device"), "query": query, "matches": []})
        shown = hits[:_SPOKEN_DEVICES]
        default = next((row for row in hits if row.get("is_default")), None)
        lead = shown[0]
        working = lead.get("working")
        if lead.get("matched_category"):
            # Nothing is named what the owner called it; be explicit that this is a device OF that kind.
            head = (f"I can see {len(hits)} device(s) of that kind. The closest is "
                    f"'{lead['name']}' ({lead['kind'].replace('_', ' ')})")
        else:
            head = f"'{lead['name']}' is attached ({lead['kind'].replace('_', ' ')})"
        if working is False:
            head += f", but Windows reports its status as {lead.get('status') or 'not working'}"
        elif default is not None and default is lead:
            head += ", and it is the system default"
        others = [row["name"] for row in shown[1:]]
        tail = f" Also matching: {', '.join(others)}." if others else ""
        return ToolResult.success(head + "." + tail + _unavailable_note(reading),
                                  data={**_payload(reading, "find_device"), "query": query, "matches": hits})

    # -- domain 7: network monitoring --
    def get_network_status(self) -> ToolResult:
        """How this machine is connected, and whether anything about it is worth a look.

        Reads local interface and socket state only. Sends nothing: no scan, no probe, no lookup.
        """
        reading = network_probe.snapshot(include_connections=True)
        if not reading.ok:
            return ToolResult.failure(
                "I could not read this machine's network state." + _unavailable_note(reading))
        notes = network_probe.anomalies(reading)
        summary = network_probe.describe(reading)
        if notes:
            summary += " " + " ".join(notes)
        return ToolResult.success(summary + _unavailable_note(reading),
                                 data={**_payload(reading, "network_status"), "observations": notes})

    def list_connections(self, limit: int = network_probe.MAX_CONNECTIONS) -> ToolResult:
        """Open network connections and listening sockets: which program, which port, which scope.

        Connection metadata only - what a connection carries is not readable from here at all.
        """
        reading = network_probe.connections(limit=limit)
        if not reading.ok:
            return ToolResult.failure(
                "I could not read the socket table." + _unavailable_note(reading))
        counts = reading.get("connection_counts") or {}
        summary = (f"{counts.get('established', 0)} open connection(s) "
                   f"({counts.get('public', 0)} to the internet, {counts.get('loopback', 0)} "
                   f"within this machine) and {counts.get('listening', 0)} listening socket(s).")
        return ToolResult.success(summary + _unavailable_note(reading), data=_payload(reading, "connections"))

    # -- registration --
    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="get_system_status",
                description=(
                    "Read this machine's current state: processor and memory use, disk space, graphics "
                    "card, battery and uptime. Use this for questions about how the computer itself is "
                    "doing. Reports which metrics are unavailable rather than guessing them."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "include_processes": {
                            "type": "boolean",
                            "description": ("Also sample the running processes (slower, ~0.6s). "
                                            "Default true."),
                        },
                    },
                    "required": [],
                },
                handler=self.get_system_status,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_processes",
                description=(
                    "List the running processes using the most processor time or the most memory. "
                    "Returns program name, process id and resource use - never command lines."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "order": {"type": "string", "enum": ["cpu", "memory"],
                                  "description": "Sort by processor use or memory use. Default cpu."},
                        "limit": {"type": "integer",
                                  "description": (f"How many to return, up to "
                                                  f"{host_probe.MAX_PROCESSES}.")},
                    },
                    "required": [],
                },
                handler=self.list_processes,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="diagnose_slowness",
                description=(
                    "Work out why this machine feels slow: collect processor, memory, swap, disk, "
                    "graphics and battery readings and report which of them are actually under strain, "
                    "and which programs are heaviest. Use this for 'why is my computer slow?'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.diagnose_slowness,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_devices",
                description=(
                    "List hardware attached to this machine: microphones, audio outputs, cameras, "
                    "Bluetooth peers and USB devices. Reads only - it does not connect to anything."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": ["all", "audio", "microphone", "speaker", "camera",
                                          "bluetooth", "usb"],
                                 "description": "Narrow to one kind of device. Default all."},
                    },
                    "required": [],
                },
                handler=self.list_devices,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="find_device",
                description=(
                    "Check whether a particular device is attached, by its name ('iQOO Neo 10') or by "
                    "the kind of thing it is ('headset', 'webcam', 'microphone'). Use this for 'is my "
                    "headset connected?'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Device name, or a kind of device."},
                    },
                    "required": ["name"],
                },
                handler=self.find_device,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="get_network_status",
                description=(
                    "Report how this machine is connected - which interface, whether it is online, how "
                    "many connections are open - and flag anything locally observable that is worth a "
                    "look, such as a program accepting connections from the network. Reads local state "
                    "only: it sends no traffic and scans nothing."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.get_network_status,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_connections",
                description=(
                    "List open network connections and listening sockets with the program that owns "
                    "each, its port, and whether the address is local or on the internet. Connection "
                    "metadata only - never the contents of any connection."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer",
                                  "description": (f"How many of each to return, up to "
                                                  f"{network_probe.MAX_CONNECTIONS}.")},
                    },
                    "required": [],
                },
                handler=self.list_connections,
                risk=RiskLevel.LOW,
            ),
        ]
