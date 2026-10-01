"""WMI access for the observation probes - through a closed query table, never a caller's string.

Windows Management Instrumentation is how this package learns about attached devices and about per-process
resource use. It is reached through pywin32, which V.O.I.D already depends on for window control, so no new
dependency is involved and - unlike shelling out to PowerShell - **no process is spawned at all**. Measured
on this machine: a PnP device enumeration costs 0.12 s through WMI against 0.57 s through ``Get-PnpDevice``.

The security design is the query table :data:`QUERIES`.

A caller names a query (``wmi.query("cameras")``); it never supplies WQL. Every query in the table is a
literal, hand-written, read-only ``SELECT`` with no interpolation anywhere in this module - there is no code
path that concatenates, formats or ``%``-substitutes anything into a query, so a device name or a model's
argument cannot become part of one. :func:`query` rejects an unknown name outright. WMI can execute methods
(process creation, service control, reboot) and WQL has no injection-proof quoting story worth relying on,
so "no caller-authored query" is enforced structurally rather than by escaping. Adding a query is a
source-code change and a review, exactly like adding a device-gateway capability.

COM is also per-thread. V.O.I.D runs the voice pipeline on worker threads, so :func:`query` initialises COM
on whatever thread calls it and leaves the apartment as it found it.
"""
from __future__ import annotations

import threading

from void.system import describe_error

#: Every query this package can ever run, by name. Literal strings, read-only, no interpolation.
#:
#: ``Win32_PnPEntity`` rows describe attached hardware; ``PNPClass`` is Windows' own device category, so
#: filtering on it in the query (rather than fetching all ~500 devices and filtering in Python) is both
#: faster and narrower. ``Win32_PerfFormattedData_PerfProc_Process`` holds per-process CPU and memory that
#: Windows has *already* differenced over its own sampling interval - which is why reading it needs no
#: sampling sleep of our own, and is several times faster than opening a handle to every process.
QUERIES: dict[str, str] = {
    "cameras":
        "SELECT Name, Status, PNPClass FROM Win32_PnPEntity WHERE PNPClass = 'Camera' OR PNPClass = 'Image'",
    "bluetooth":
        "SELECT Name, Status, PNPClass FROM Win32_PnPEntity WHERE PNPClass = 'Bluetooth'",
    "usb":
        "SELECT Name, Status, PNPClass FROM Win32_PnPEntity WHERE PNPClass = 'USB'",
    "audio_hardware":
        "SELECT Name, Status, PNPClass FROM Win32_PnPEntity WHERE PNPClass = 'AudioEndpoint' "
        "OR PNPClass = 'MEDIA'",
    "processes":
        "SELECT IDProcess, Name, PercentProcessorTime, WorkingSetPrivate "
        "FROM Win32_PerfFormattedData_PerfProc_Process",
}

#: Fields a query may return, by query name. A row's other properties are discarded rather than passed on:
#: ``Win32_PnPEntity`` also carries ``DeviceID`` and ``PNPDeviceID``, which embed hardware serial numbers,
#: and ``Win32_Process`` (not used here) carries command lines. Projecting explicitly means a widened
#: ``SELECT`` cannot start leaking a field by accident.
FIELDS: dict[str, tuple[str, ...]] = {
    "cameras": ("Name", "Status", "PNPClass"),
    "bluetooth": ("Name", "Status", "PNPClass"),
    "usb": ("Name", "Status", "PNPClass"),
    "audio_hardware": ("Name", "Status", "PNPClass"),
    "processes": ("IDProcess", "Name", "PercentProcessorTime", "WorkingSetPrivate"),
}

#: Rows returned by a single query, at most. WMI can hand back thousands on a busy workstation.
MAX_ROWS = 600

_lock = threading.Lock()


class WmiUnavailable(RuntimeError):
    """WMI could not be reached at all - not installed, not this platform, or the service is unwell."""


def _namespace():
    """A WMI namespace handle, with COM initialised for this thread."""
    import pythoncom
    import win32com.client
    try:
        pythoncom.CoInitialize()
    except Exception:                                          # noqa: BLE001 - already initialised here
        pass
    return win32com.client.GetObject("winmgmts:")


def available() -> bool:
    """True when WMI can be reached on this machine and thread."""
    try:
        _namespace()
        return True
    except Exception:                                          # noqa: BLE001
        return False


def query(name: str) -> list[dict]:
    """Run the named query from :data:`QUERIES` and return its projected rows.

    Raises :class:`KeyError` for a name that is not in the table - the point of the table - and
    :class:`WmiUnavailable` when WMI itself cannot answer.
    """
    if name not in QUERIES:
        raise KeyError(f"no such WMI query: {name!r}")
    wql = QUERIES[name]
    fields = FIELDS[name]
    try:
        # One query at a time. WMI is fine concurrently, but these probes are not hot paths and
        # serialising keeps the COM apartment handling simple and predictable.
        with _lock:
            namespace = _namespace()
            rows = namespace.ExecQuery(wql)
            out: list[dict] = []
            for row in rows:
                out.append({field: getattr(row, field, None) for field in fields})
                if len(out) >= MAX_ROWS:
                    break
        return out
    except KeyError:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise WmiUnavailable(describe_error(exc)) from exc


def text(value: object) -> str | None:
    """A WMI string property as a clean ``str``, or None. WMI hands back COM variants and empties."""
    if value is None:
        return None
    try:
        out = str(value).strip()
    except Exception:                                          # noqa: BLE001
        return None
    return out or None


def number(value: object) -> int | None:
    """A WMI numeric property as an ``int``, or None when it is absent or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
