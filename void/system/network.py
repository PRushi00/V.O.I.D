"""Defensive network observation: what this machine is connected through, and what it is talking to.

The architecture the owner asked for is OBSERVE -> ANALYZE -> DETECT -> ALERT -> ASK PERMISSION, and this
module is the first two stages and nothing beyond them. It reads local kernel state through psutil. It is
worth being explicit about what that means, because "network monitoring" covers a lot of ground this
deliberately does not touch:

* It **never sends a packet.** No scan, no probe, no ping, no connect, no DNS lookup. Every fact here comes
  from this machine's own interface table and socket table - the same information ``netstat`` prints,
  readable without privilege and without touching the network at all.
* It **never reads payloads.** Packet capture needs a driver and administrator rights; V.O.I.D asks for
  neither. What a connection *carries* is not observable here, by construction, so nothing downstream can
  leak it.
* It **never acts.** There is no block, no disconnect, no firewall change, no countermeasure. Detection
  produces a sentence for the owner; the owner decides.

The detections in :func:`anomalies` are therefore *indicators*, not verdicts. Each one is a measured local
fact phrased as something worth a look. None of them accuses a remote host of anything, because from here
there is no evidence that would support it.
"""
from __future__ import annotations

import ipaddress
import socket

from void.system import Reading, describe_error, timed

#: How many connections one listing may return. A browser alone holds dozens.
MAX_CONNECTIONS = 40

#: How many interfaces are described.
MAX_INTERFACES = 20

#: Windows' own networking, by process image name. Measured on this machine: 43 sockets listen and 21 of
#: them accept from the network, essentially all of them Windows RPC, SMB and service-host plumbing. An
#: alert that names ``lsass.exe`` and ``svchost.exe`` as suspicious is not a finding - it is noise, and
#: noise teaches the owner to ignore the next alert, which is the actual harm. So the detector separates
#: "Windows is doing what Windows does" from "something else here is accepting connections", and only the
#: second is reported. This is a *description* of normality, never an authorization: nothing on this list
#: gains any privilege, and a listener omitted from it is reported, not blocked.
_WINDOWS_SERVICES = frozenset({
    "system", "lsass.exe", "svchost.exe", "services.exe", "spoolsv.exe", "wininit.exe",
    "smss.exe", "csrss.exe", "msmpeng.exe", "searchindexer.exe", "wlanext.exe", "dashost.exe",
})

#: Listening on more than this many ports *after* Windows' own services are set aside is worth mentioning.
#: Set from the measurement above rather than from a guess: this machine has 22 non-Windows listeners under
#: normal use, almost all developer tooling bound to loopback.
MANY_LISTENERS = 25

#: Address families worth reporting. Link-layer (AF_LINK / AF_PACKET) entries are MAC addresses, which are
#: stable hardware identifiers useful for tracking and of no use in any question V.O.I.D answers, so they
#: are not collected at all.
_IP_FAMILIES = (socket.AF_INET, socket.AF_INET6)

#: Socket states that mean "a conversation is open right now".
_ESTABLISHED = "ESTABLISHED"
_LISTEN = "LISTEN"


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:                                          # noqa: BLE001
        return None


def _scope(address: str | None) -> str:
    """Where an address lives: ``loopback``, ``private``, ``public``, ``multicast``, or ``unknown``.

    Classifying by scope rather than reporting a bare address means a summary can say "three connections to
    the internet, the rest local" without the addresses themselves having to travel anywhere.
    """
    if not address:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address.split("%")[0])        # strip an IPv6 zone index
    except ValueError:
        return "unknown"
    if ip.is_loopback:
        return "loopback"
    if ip.is_multicast:
        return "multicast"
    if ip.is_link_local or ip.is_private:
        return "private"
    return "public"


def interfaces() -> Reading:
    """Every network interface, whether it is up, its speed, and its IP addresses.

    Reports IPv4/IPv6 addresses but never MAC addresses - see :data:`_IP_FAMILIES`.
    """
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("interfaces", "psutil is not installed")
        try:
            addresses = ps.net_if_addrs()
        except Exception as exc:                                # noqa: BLE001
            return r.miss("interfaces", describe_error(exc))
        try:
            stats = ps.net_if_stats()
        except Exception:                                      # noqa: BLE001
            stats = {}
        rows: list[dict] = []
        for name, entries in list(addresses.items())[:MAX_INTERFACES]:
            stat = stats.get(name)
            ips = [{"address": entry.address, "scope": _scope(entry.address),
                    "family": "ipv4" if entry.family == socket.AF_INET else "ipv6"}
                   for entry in entries if entry.family in _IP_FAMILIES and entry.address]
            rows.append({"name": name,
                         "up": bool(stat.isup) if stat is not None else None,
                         "speed_mbps": int(stat.speed) if stat is not None and stat.speed else None,
                         "mtu": int(stat.mtu) if stat is not None and stat.mtu else None,
                         "addresses": ips})
        r.set("interfaces", rows)
        active = [row for row in rows if row.get("up") and row.get("addresses")
                  and any(ip["scope"] != "loopback" for ip in row["addresses"])]
        r.set("active_interfaces", [row["name"] for row in active])
        r.set("online", bool(active))
        return r


def traffic() -> Reading:
    """Bytes and packets this machine has sent and received since boot, plus error and drop counters.

    These are cumulative totals, not a rate. A rate would need two samples separated by a wait, and no
    question V.O.I.D answers today needs one - but the error and drop counters are useful immediately:
    a link that is up and passing traffic while dropping packets is a real, diagnosable fault.
    """
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("traffic", "psutil is not installed")
        try:
            io = ps.net_io_counters()
        except Exception as exc:                                # noqa: BLE001
            return r.miss("traffic", describe_error(exc))
        if io is None:
            return r.miss("traffic", "this platform reports no traffic counters")
        r.set("bytes_sent_mb", round(io.bytes_sent / 2 ** 20, 1))
        r.set("bytes_received_mb", round(io.bytes_recv / 2 ** 20, 1))
        r.set("packets_sent", int(io.packets_sent))
        r.set("packets_received", int(io.packets_recv))
        r.set("errors_in", int(io.errin))
        r.set("errors_out", int(io.errout))
        r.set("dropped_in", int(io.dropin))
        r.set("dropped_out", int(io.dropout))
        return r


def connections(limit: int = MAX_CONNECTIONS) -> Reading:
    """Open sockets: which local process, which remote address and port, and in what state.

    Connection *metadata* only - never payloads, which are not readable from here at all. Sockets belonging
    to processes the owner cannot inspect are counted but not attributed, and no privilege is requested to
    change that.
    """
    limit = max(1, min(int(limit or MAX_CONNECTIONS), MAX_CONNECTIONS))
    with timed() as r:
        ps = _psutil()
        if ps is None:
            return r.miss("connections", "psutil is not installed")
        try:
            socks = ps.net_connections(kind="inet")
        except (PermissionError, OSError) as exc:
            return r.miss("connections",
                          f"the socket table is not readable by this process ({describe_error(exc)})")
        except Exception as exc:                                # noqa: BLE001
            return r.miss("connections", describe_error(exc))
        names: dict[int, str] = {}

        def owner(pid: int | None) -> str | None:
            if pid is None:
                return None
            if pid not in names:
                try:
                    names[pid] = ps.Process(pid).name()
                except Exception:                              # noqa: BLE001 - exited, or not ours
                    names[pid] = "(unknown)"
            return names[pid]

        established: list[dict] = []
        listening: list[dict] = []
        counts = {"established": 0, "listening": 0, "other": 0,
                  "public": 0, "private": 0, "loopback": 0}
        for sock in socks:
            status = getattr(sock, "status", None) or "NONE"
            remote = sock.raddr
            local = sock.laddr
            if status == _ESTABLISHED and remote:
                counts["established"] += 1
                scope = _scope(getattr(remote, "ip", None))
                counts[scope] = counts.get(scope, 0) + 1
                if len(established) < limit:
                    established.append({"process": owner(sock.pid),
                                        "pid": sock.pid,
                                        "remote_address": getattr(remote, "ip", None),
                                        "remote_port": getattr(remote, "port", None),
                                        "scope": scope})
            elif status == _LISTEN and local:
                counts["listening"] += 1
                if len(listening) < limit:
                    listening.append({"process": owner(sock.pid),
                                      "pid": sock.pid,
                                      "local_address": getattr(local, "ip", None),
                                      "local_port": getattr(local, "port", None),
                                      "scope": _scope(getattr(local, "ip", None)),
                                      # A listener bound to 0.0.0.0 / :: accepts from the network; one on
                                      # 127.0.0.1 cannot be reached from off the machine at all. That
                                      # distinction is the whole security content of a listener row.
                                      "reachable_from_network": getattr(local, "ip", None) in
                                      ("0.0.0.0", "::", "")})
            else:
                counts["other"] += 1
        r.set("established", established)
        r.set("listening", listening)
        r.set("connection_counts", counts)
        return r


def snapshot(*, include_connections: bool = True) -> Reading:
    """Interfaces, traffic counters and (by default) the socket table, in one reading."""
    with timed() as r:
        probes = [interfaces, traffic] + ([connections] if include_connections else [])
        for probe in probes:
            try:
                r.merge(probe())
            except Exception as exc:                            # noqa: BLE001
                r.miss(probe.__name__, describe_error(exc))
        return r


def _unique(rows: list[dict]) -> list[dict]:
    """One row per (program, port). A service bound to both IPv4 and IPv6 is one listener, not two."""
    out, seen = [], set()
    for row in rows:
        key = ((row.get("process") or "").lower(), row.get("local_port"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _is_windows_service(row: dict) -> bool:
    """Whether a listening socket belongs to Windows' own networking. See :data:`_WINDOWS_SERVICES`.

    Case is folded, because Windows reports image names inconsistently. Whitespace is deliberately **not**
    stripped: this list decides what gets *silenced*, so a near-miss must not match it. ``"svchost.exe "``
    is not svchost - and if a process really is reported under a padded name, that is itself worth seeing
    rather than quietly filtering out.
    """
    name = row.get("process")
    return isinstance(name, str) and name.lower() in _WINDOWS_SERVICES


def anomalies(reading: Reading) -> list[str]:
    """Indicators in this reading that the owner may want to look at.

    Every item is a local measurement phrased as something to check - never a conclusion about a remote
    party, which nothing observable from here could support. An empty list is the normal, healthy answer.
    """
    notes: list[str] = []
    if reading.get("online") is False:
        notes.append("No network interface is up with a usable address - this machine appears to be offline.")
    # The error and drop counters psutil exposes are machine-wide totals, not per-interface, so they are
    # reported once. (Reporting them inside a loop over interfaces repeated the same number per interface.)
    errors = (reading.get("errors_in") or 0) + (reading.get("errors_out") or 0)
    if isinstance(errors, int) and errors > 0:
        notes.append(f"The network stack has recorded {errors} error(s) since boot.")
    dropped = (reading.get("dropped_in") or 0) + (reading.get("dropped_out") or 0)
    if isinstance(dropped, int) and dropped > 0:
        notes.append(f"{dropped} packet(s) have been dropped since boot, which can show up as "
                     f"stalling or slow transfers.")
    # One service bound to both IPv4 and IPv6 is two rows and one fact, so the alert counts endpoints by
    # (program, port) rather than by socket - otherwise "3 programs" meant one database listening twice.
    exposed = _unique([row for row in reading.get("listening") or []
                       if row.get("reachable_from_network") and not _is_windows_service(row)])
    if exposed:
        named = ", ".join(f"{row.get('process') or 'an unidentified process'} on port {row.get('local_port')}"
                          for row in exposed[:4])
        more = f" and {len(exposed) - 4} more" if len(exposed) > 4 else ""
        notes.append(f"{len(exposed)} program(s) other than Windows' own services are accepting network "
                     f"connections ({named}{more}). That is expected for some software and worth "
                     f"checking for the rest.")
    others = _unique([row for row in reading.get("listening") or [] if not _is_windows_service(row)])
    if len(others) > MANY_LISTENERS:
        notes.append(f"{len(others)} sockets are listening besides Windows' own services, which is more "
                     f"than this machine usually has open.")
    return notes


def describe(reading: Reading) -> str:
    """One sentence about how this machine is connected, for a spoken answer."""
    if reading.get("online") is False:
        return "This machine is offline - no interface is up with a usable address."
    active = reading.get("active_interfaces") or []
    if not active:
        return "I could not determine how this machine is connected."
    counts = reading.get("connection_counts") or {}
    established = counts.get("established")
    tail = (f", with {established} open connection(s)"
            if isinstance(established, int) and established else "")
    return f"Connected through {active[0]}{tail}."
