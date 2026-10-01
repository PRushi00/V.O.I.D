"""The machine's own vital signs, from psutil: processor, memory, storage, power, uptime, processes.

Everything here is a read. Nothing is cached between calls, because the questions this answers ("is it
busy *now*?") are about the present moment and a stale CPU figure is worse than a slow one.

Two deliberate omissions, both security decisions rather than oversights:

*Command lines are never read.* ``psutil.Process.cmdline()`` is where API keys, tokens and private paths
live on a developer's machine - ``--api-key=...`` in an argv is routine. A process listing that V.O.I.D
can speak aloud, write to a log, or hand to a cloud model must not be able to carry those, so this module
reads ``name()`` and never ``cmdline()`` or ``environ()``.

*Only the owner's own processes are described in detail.* Enumerating every process on the machine is
allowed (it is public information on Windows), but anything that raises ``AccessDenied`` is skipped
quietly rather than retried with more privilege. V.O.I.D does not escalate to observe.
"""
from __future__ import annotations

import os
import platform
import time

from void.system import PROBE_TIMEOUT_S, Reading, describe_error, timed
from void.system import wmi

#: How many processes a listing may return. A machine with 400 processes is normal; handing 400 rows to a
#: language model is not, and reading 400 rows aloud is absurd.
MAX_PROCESSES = 15

#: Processes are sampled twice to get a CPU percentage - the first psutil reading is always 0.0 because it
#: has no previous sample to difference against. This is how long we wait between the two.
CPU_SAMPLE_S = 0.25

#: Rows in the Windows process counter set that are not processes: the all-process total, and the
#: processor-idle pseudo-process. Reporting either as "busy" would be actively misleading.
_NOT_A_PROCESS = frozenset({"_Total", "Idle"})

#: A utilisation at or above this is worth mentioning when explaining slowness.
BUSY_CPU_PCT = 70.0
BUSY_MEMORY_PCT = 85.0
BUSY_DISK_PCT = 90.0
LOW_BATTERY_PCT = 20.0


def _psutil():
    """psutil or None. It is a declared dependency, but a probe must not be the thing that crashes."""
    try:
        import psutil
        return psutil
    except Exception:                                          # noqa: BLE001
        return None


def operating_system() -> Reading:
    """Which OS and Python this is. Deliberately omits hostname, user name and domain."""
    with timed() as r:
        try:
            r.set("os_family", platform.system() or None)
            r.set("os_release", platform.release() or None)
            r.set("os_version", platform.version() or None)
            r.set("machine", platform.machine() or None)
            r.set("python_version", platform.python_version())
        except Exception as exc:                                # noqa: BLE001
            r.miss("operating_system", describe_error(exc))
        try:
            from void import __version__
            r.set("void_version", __version__)
        except Exception:                                      # noqa: BLE001
            pass
    return r


def processor() -> Reading:
    """Processor utilisation and shape. ``cpu_percent`` blocks for :data:`CPU_SAMPLE_S`."""
    with timed() as r:
        ps = _psutil()
        try:
            r.set("cpu_logical", os.cpu_count())
        except Exception as exc:                                # noqa: BLE001
            r.miss("cpu_logical", describe_error(exc))
        if ps is None:
            return r.miss("cpu_percent", "psutil is not installed")
        try:
            r.set("cpu_percent", round(ps.cpu_percent(interval=CPU_SAMPLE_S), 1))
        except Exception as exc:                                # noqa: BLE001
            r.miss("cpu_percent", describe_error(exc))
        try:
            r.set("cpu_physical", ps.cpu_count(logical=False))
        except Exception as exc:                                # noqa: BLE001
            r.miss("cpu_physical", describe_error(exc))
        try:
            freq = ps.cpu_freq()
            if freq is not None and freq.current:
                r.set("cpu_mhz", round(float(freq.current), 0))
                if freq.max:
                    r.set("cpu_mhz_max", round(float(freq.max), 0))
        except Exception as exc:                                # noqa: BLE001
            r.miss("cpu_mhz", describe_error(exc))
        return r


def memory() -> Reading:
    """Physical memory and swap, in GiB and percent."""
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("memory", "psutil is not installed")
        try:
            vm = ps.virtual_memory()
            r.set("memory_total_gb", round(vm.total / 2 ** 30, 1))
            r.set("memory_available_gb", round(vm.available / 2 ** 30, 1))
            r.set("memory_percent", round(float(vm.percent), 1))
        except Exception as exc:                                # noqa: BLE001
            r.miss("memory", describe_error(exc))
        try:
            sw = ps.swap_memory()
            if sw.total:
                r.set("swap_total_gb", round(sw.total / 2 ** 30, 1))
                r.set("swap_percent", round(float(sw.percent), 1))
        except Exception as exc:                                # noqa: BLE001
            r.miss("swap", describe_error(exc))
        return r


def storage() -> Reading:
    """Usage for each mounted, readable filesystem.

    Removable and unreadable mounts are skipped rather than reported as an error: an empty card reader
    raising on ``disk_usage`` is normal, not a fault worth telling the owner about.
    """
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("storage", "psutil is not installed")
        disks: list[dict] = []
        try:
            partitions = ps.disk_partitions(all=False)
        except Exception as exc:                                # noqa: BLE001
            return r.miss("storage", describe_error(exc))
        for part in partitions:
            try:
                usage = ps.disk_usage(part.mountpoint)
            except Exception:                                  # noqa: BLE001 - empty drive, no permission
                continue
            disks.append({"mount": part.mountpoint,
                          "filesystem": part.fstype or None,
                          "total_gb": round(usage.total / 2 ** 30, 1),
                          "free_gb": round(usage.free / 2 ** 30, 1),
                          "percent_used": round(float(usage.percent), 1)})
        if disks:
            r.set("disks", disks)
        else:
            r.miss("storage", "no readable filesystem was found")
        return r


def power() -> Reading:
    """Battery charge and whether it is on mains. A desktop reports no battery, which is not a failure."""
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("battery", "psutil is not installed")
        try:
            battery = ps.sensors_battery()
        except Exception as exc:                                # noqa: BLE001
            return r.miss("battery", describe_error(exc))
        if battery is None:
            return r.miss("battery", "this machine reports no battery")
        r.set("battery_percent", round(float(battery.percent), 1))
        r.set("power_plugged", bool(battery.power_plugged))
        secs = getattr(battery, "secsleft", None)
        # psutil signals "charging" and "unknown" with negative sentinels; a negative number of minutes
        # remaining would be a fabricated fact.
        if isinstance(secs, int) and secs >= 0:
            r.set("battery_minutes_left", int(secs // 60))
        return r


def temperatures() -> Reading:
    """Hardware temperatures where the platform exposes them without privilege.

    On Windows psutil has no sensor support at all, so this honestly reports nothing rather than guessing
    from CPU load. GPU temperature comes from :mod:`void.system.gpu`, which has a real source for it.
    """
    with timed() as r:
        ps = _psutil()
        sensors = getattr(ps, "sensors_temperatures", None) if ps is not None else None
        if sensors is None:
            return r.miss("temperatures",
                          "this platform exposes no temperature sensors to an unprivileged process")
        try:
            found = sensors()
        except Exception as exc:                                # noqa: BLE001
            return r.miss("temperatures", describe_error(exc))
        readings = [{"sensor": name, "label": entry.label or None,
                     "celsius": round(float(entry.current), 1)}
                    for name, entries in (found or {}).items() for entry in entries
                    if entry.current is not None]
        if readings:
            r.set("temperatures", readings)
        else:
            r.miss("temperatures", "no sensor reported a reading")
        return r


def uptime() -> Reading:
    """How long the machine has been up, in seconds and hours."""
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("uptime", "psutil is not installed")
        try:
            seconds = max(0.0, time.time() - ps.boot_time())
            r.set("uptime_s", int(seconds))
            r.set("uptime_hours", round(seconds / 3600.0, 1))
        except Exception as exc:                                # noqa: BLE001
            r.miss("uptime", describe_error(exc))
        return r


def _wmi_processes() -> list[dict] | None:
    """Per-process CPU and memory from Windows' own performance counters, or None if unreadable.

    Preferred over psutil because Windows has already differenced the CPU figures over its own sampling
    interval: one query, no sampling sleep. Measured on this machine, 386 processes:

        this path          534-811 ms, single call
        psutil two-pass   2100 ms of handle opening, plus a 250 ms sleep between the passes

    The cost in psutil is opening a handle per process (~2.7 ms each here, and the same whether one
    attribute is read or four), which no amount of batching avoids.
    """
    try:
        rows = wmi.query("processes")
    except Exception:                                          # noqa: BLE001 - WmiUnavailable, or no pywin32
        return None
    cores = os.cpu_count() or 1
    out: list[dict] = []
    for row in rows:
        name = wmi.text(row.get("Name"))
        pid = wmi.number(row.get("IDProcess"))
        if name is None or pid is None:
            continue
        # The counter set carries two pseudo-rows that are not processes: "_Total" (the sum of everything)
        # and "Idle" (pid 0, the processor doing nothing). Reporting "Idle is using 94% of your CPU" as an
        # answer to "why is my laptop slow" would be worse than saying nothing, so neither is a row here.
        if name in _NOT_A_PROCESS or pid == 0:
            continue
        # Windows disambiguates same-named processes as "chrome#3". The suffix identifies a counter
        # instance, not the program, and the pid is the real identity - so it is dropped.
        base, _, _ = name.partition("#")
        cpu = wmi.number(row.get("PercentProcessorTime"))
        working_set = wmi.number(row.get("WorkingSetPrivate"))
        out.append({"pid": pid,
                    "name": base or name,
                    # The counter is per-core (a fully busy 24-core machine reads 2400), normalised here to
                    # a share of the whole machine - which is what "using 50% of the CPU" means to a person.
                    "cpu_percent": round(cpu / cores, 1) if cpu is not None else None,
                    "memory_mb": round(working_set / 2 ** 20, 1) if working_set is not None else None})
    return out or None


def _psutil_processes(deadline: float) -> tuple[list[dict], str | None]:
    """Fallback listing when the performance counters are unreadable: ``(rows, caveat)``.

    One pass only. A second pass would be needed for CPU percentages and costs another full second of
    handle opening, so this reports memory and process identity and says CPU is unavailable rather than
    making the owner wait twice as long or filling the column with zeroes.
    """
    ps = _psutil()
    if ps is None:
        return [], "psutil is not installed"
    rows: list[dict] = []
    try:
        procs = ps.process_iter(["pid", "name", "memory_info"])
    except Exception as exc:                                    # noqa: BLE001
        return [], describe_error(exc)
    truncated = False
    for proc in procs:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            info = proc.info
            mem = info.get("memory_info")
            rows.append({"pid": info.get("pid"),
                         "name": info.get("name") or "(unknown)",
                         "cpu_percent": None,
                         "memory_mb": round(mem.rss / 2 ** 20, 1) if mem is not None else None})
        except Exception:                                      # noqa: BLE001 - exited mid-walk
            continue
    caveat = ("the listing was cut short by the probe timeout" if truncated else None)
    return rows, caveat


def processes(limit: int = MAX_PROCESSES, order: str = "cpu") -> Reading:
    """The heaviest processes, by ``cpu`` or ``memory``.

    Returns name, pid, CPU percent and private working set - and nothing else. See the module docstring for
    why the command line is not here.

    ``order`` is validated against a fixed pair; an unrecognised value falls back to ``cpu`` rather than
    reaching anything, because this argument can originate from a model.
    """
    order = order if order in ("cpu", "memory") else "cpu"
    limit = max(1, min(int(limit or MAX_PROCESSES), MAX_PROCESSES))
    with timed() as r:
        deadline = time.monotonic() + PROBE_TIMEOUT_S
        rows = _wmi_processes()
        if rows is None:
            rows, caveat = _psutil_processes(deadline)
            if caveat:
                r.miss("processes_complete", caveat)
            if rows:
                r.miss("process_cpu_percent",
                       "the Windows performance counters were unreadable, so only memory could be sampled")
        if not rows:
            return r.miss("processes", "no process could be read on this machine")
        r.set("process_count", len(rows))
        if order == "cpu" and all(row.get("cpu_percent") is None for row in rows):
            order = "memory"                                    # never sort by a column that is all None
        key = "cpu_percent" if order == "cpu" else "memory_mb"
        rows.sort(key=lambda row: (row.get(key) or 0.0), reverse=True)
        r.set("processes", rows[:limit])
        r.set("ordered_by", order)
        return r


def snapshot(*, include_processes: bool = True) -> Reading:
    """One combined reading of the host: OS, processor, memory, storage, power, uptime, temperatures.

    This is what the ``get_system_status`` tool returns, and what "why is my laptop slow?" reasons over.
    Probes are independent: one failing source leaves the rest of the answer intact and lands in
    ``unavailable`` so the gap is visible rather than silent.
    """
    with timed() as r:
        for probe in (operating_system, processor, memory, storage, power, uptime, temperatures):
            try:
                r.merge(probe())
            except Exception as exc:                            # noqa: BLE001
                r.miss(probe.__name__, describe_error(exc))
        if include_processes:
            try:
                r.merge(processes())
            except Exception as exc:                            # noqa: BLE001
                r.miss("processes", describe_error(exc))
        return r


def pressure(reading: Reading) -> list[str]:
    """Plain-language observations about what, in this reading, is actually under strain.

    This is the analysis half of "why is my laptop slow?": thresholds applied to measured numbers, and
    nothing more. It states what it observed; it does not advise, and it never claims a cause it cannot
    see. An empty list means nothing crossed a threshold - a real and useful answer.
    """
    notes: list[str] = []
    cpu = reading.get("cpu_percent")
    if isinstance(cpu, (int, float)) and cpu >= BUSY_CPU_PCT:
        notes.append(f"The processor is busy, at {cpu:.0f}%.")
    mem_pct = reading.get("memory_percent")
    if isinstance(mem_pct, (int, float)) and mem_pct >= BUSY_MEMORY_PCT:
        free = reading.get("memory_available_gb")
        tail = f", {free:.1f} GB free" if isinstance(free, (int, float)) else ""
        notes.append(f"Memory is nearly full, at {mem_pct:.0f}%{tail}.")
    swap = reading.get("swap_percent")
    if isinstance(swap, (int, float)) and swap >= BUSY_MEMORY_PCT:
        notes.append(f"The machine is swapping heavily ({swap:.0f}% of the page file is in use), "
                     f"which slows everything down.")
    for disk in reading.get("disks") or []:
        used = disk.get("percent_used")
        if isinstance(used, (int, float)) and used >= BUSY_DISK_PCT:
            notes.append(f"Drive {disk.get('mount')} is {used:.0f}% full "
                         f"({disk.get('free_gb')} GB free).")
    gpu_pct = reading.get("gpu_utilisation_percent")
    if isinstance(gpu_pct, (int, float)) and gpu_pct >= BUSY_CPU_PCT:
        notes.append(f"The GPU is busy, at {gpu_pct:.0f}%.")
    battery = reading.get("battery_percent")
    plugged = reading.get("power_plugged")
    if isinstance(battery, (int, float)) and battery <= LOW_BATTERY_PCT and plugged is False:
        notes.append(f"The battery is low at {battery:.0f}% and not charging; Windows may be throttling "
                     f"the processor to save power.")
    heavy = [p for p in (reading.get("processes") or []) if (p.get("cpu_percent") or 0) >= 10.0]
    if heavy:
        named = ", ".join(f"{p['name']} ({p['cpu_percent']:.0f}%)" for p in heavy[:3])
        notes.append(f"Busiest right now: {named}.")
    return notes
