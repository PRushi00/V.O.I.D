"""One view of a device's standing: is it known, is it here, and what is it allowed to do.

The blueprint asks for a device registry that distinguishes known, present, trusted, previously-authorized
and connected. Both halves of that already exist in V.O.I.D and neither is rebuilt here:

* :mod:`void.system.devices` observes what is **physically present** - radios, cameras, paired Bluetooth
  peers, USB. It reads; it never pairs or trusts anything.
* :class:`void.device.identity.DeviceRegistry` records what is **deliberately authorized** - a paired
  companion device, its capabilities, when it was paired, when it was last seen, with a per-device secret.

A second registry would have been the wrong answer: two stores of who is trusted is how a system ends up
trusting something twice over and revoking it once. So this module composes the two and nothing more. It
holds no state of its own, and it has no ``trust()`` method - **this module cannot grant anything**.

That is the important property. Trust comes from pairing, which is an explicit act with a shared secret,
and capabilities come from :meth:`DeviceRegistry.grant`. Presence is an observation and carries no weight
at all: a device being physically connected says nothing about whether it may do anything, which is
precisely the mistake that makes "just plug in a USB device" an attack. Here, presence can raise a device's
state only as far as PRESENT; nothing observed can make it TRUSTED.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from void.system.devices import clean_name

_log = logging.getLogger(__name__)

#: How recently a paired device must have checked in to count as connected rather than merely known.
CONNECTED_WINDOW_S = 300.0

#: Trust states, from weakest to strongest. Ordered so a caller can compare them.
UNKNOWN = "unknown"              # V.O.I.D has no record of it and cannot see it
PRESENT = "present"              # physically attached or in range, and NOT authorized for anything
KNOWN = "known"                  # paired at some point, not currently reachable
AUTHORIZED = "authorized"        # paired, with capabilities granted, not seen recently
CONNECTED = "connected"          # paired, granted, and in contact now

_ORDER = (UNKNOWN, PRESENT, KNOWN, AUTHORIZED, CONNECTED)


def rank(state: str) -> int:
    """Where a state sits in the ordering, for comparisons. Unknown states sort lowest."""
    try:
        return _ORDER.index(state)
    except ValueError:
        return 0


@dataclass(frozen=True)
class DeviceStanding:
    """What V.O.I.D knows about one device, from both sources, with the sources kept distinct.

    ``observed`` and ``paired`` are separate fields on purpose. Collapsing them into a single "trusted"
    boolean would lose the distinction that matters: whether V.O.I.D is reporting something it *saw* or
    something the owner *decided*.
    """

    name: str
    state: str = UNKNOWN
    #: True when hardware enumeration currently reports it. An OBSERVATION - untrusted, and never a reason
    #: to permit anything.
    observed: bool = False
    #: True when it exists in the pairing registry. A DECISION the owner made.
    paired: bool = False
    capabilities: tuple[str, ...] = ()
    device_id: str = ""
    last_seen: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", clean_name(self.name) or "")
        object.__setattr__(self, "detail", (clean_name(self.detail) or "")[:200])

    @property
    def may_act(self) -> bool:
        """True only when the device has been paired AND granted something.

        Deliberately ignores ``observed``. A device sitting on the USB port has no standing whatsoever.
        """
        return bool(self.paired and self.capabilities)

    def allows(self, capability: str) -> bool:
        """Whether this device was granted a specific capability. Exact match, no wildcards.

        No pattern matching and no "all" value: a capability a device holds is one the owner named.
        """
        wanted = (capability or "").strip().lower()
        if not wanted or not self.paired:
            return False
        return wanted in {str(held).strip().lower() for held in self.capabilities}

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "observed": self.observed,
                "paired": self.paired, "capabilities": list(self.capabilities),
                "device_id": self.device_id, "last_seen": self.last_seen,
                "may_act": self.may_act, "detail": self.detail}


def _state_for(paired: bool, capabilities, last_seen, observed: bool, now: float) -> str:
    """The single place the state is decided, so the rules are readable in one view."""
    if paired:
        fresh = last_seen is not None and (now - float(last_seen)) <= CONNECTED_WINDOW_S
        if capabilities and fresh:
            return CONNECTED
        if capabilities:
            return AUTHORIZED
        return KNOWN
    # Not paired. Presence is as far as an observation can carry a device - this is the line that keeps
    # "it is plugged in" from meaning "it is allowed".
    return PRESENT if observed else UNKNOWN


def standings(registry=None, reading=None, now: float | None = None) -> list[DeviceStanding]:
    """Join the pairing registry and the hardware reading into one list.

    Either source may be absent: with no registry, everything observed is PRESENT; with no reading, paired
    devices still report their authorization. A failure in either source is logged and skipped rather than
    raised, because "I could not enumerate USB" must not hide the paired devices.
    """
    moment = time.time() if now is None else now
    out: list[DeviceStanding] = []
    seen_names: set[str] = set()

    for device in _paired(registry):
        name = clean_name(getattr(device, "name", "")) or getattr(device, "device_id", "")
        capabilities = tuple(str(capability) for capability in
                             (getattr(device, "capabilities", None) or ()))
        last_seen = getattr(device, "last_seen", None)
        observed = _is_observed(name, reading)
        out.append(DeviceStanding(
            name=name,
            state=_state_for(True, capabilities, last_seen, observed, moment),
            observed=observed, paired=True, capabilities=capabilities,
            device_id=str(getattr(device, "device_id", "")), last_seen=last_seen,
            detail="paired companion device"))
        if name:
            seen_names.add(name.lower())

    for entry in _observed_entries(reading):
        name = clean_name(entry.get("name") or "") or ""
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        out.append(DeviceStanding(name=name, state=PRESENT, observed=True, paired=False,
                                  detail=str(entry.get("kind") or "")[:200]))

    out.sort(key=lambda standing: (-rank(standing.state), standing.name.lower()))
    return out


def _paired(registry) -> list:
    if registry is None:
        return []
    try:
        lister = getattr(registry, "list", None)
        return list(lister() or []) if callable(lister) else []
    except Exception as exc:                                    # noqa: BLE001
        _log.info("DEVICE_REGISTRY_UNREADABLE kind=%s", type(exc).__name__)
        return []


def _observed_entries(reading) -> list[dict]:
    """Flatten a devices reading into ``{name, kind}`` entries, tolerating its shape."""
    if reading is None:
        return []
    entries: list[dict] = []
    try:
        # A ``void.system`` Reading carries its contents in ``.values``: a mapping of kind
        # ("cameras", "usb", "bluetooth_peers", ...) to a list of ``{name, ...}``. Plain dicts and
        # ``.data`` are also accepted so a caller can hand in a reading from anywhere.
        data = None
        if isinstance(reading, dict):
            data = reading
        else:
            for attribute in ("values", "data"):
                candidate = getattr(reading, attribute, None)
                if callable(candidate):
                    candidate = candidate()
                if isinstance(candidate, (dict, list, tuple)) and candidate:
                    data = candidate
                    break
        if isinstance(data, dict):
            for kind, items in data.items():
                if not isinstance(items, (list, tuple)):
                    continue
                for item in items:
                    name = item.get("name") if isinstance(item, dict) else item
                    if name:
                        entries.append({"name": str(name), "kind": str(kind)})
        elif isinstance(data, (list, tuple)):
            for item in data:
                name = item.get("name") if isinstance(item, dict) else item
                if name:
                    entries.append({"name": str(name), "kind": ""})
    except Exception as exc:                                    # noqa: BLE001
        _log.info("DEVICE_READING_UNUSABLE kind=%s", type(exc).__name__)
        return []
    return entries


def _is_observed(name: str, reading) -> bool:
    if not name or reading is None:
        return False
    needle = name.lower()
    for entry in _observed_entries(reading):
        other = entry.get("name", "").lower()
        if needle and (needle in other or other in needle):
            return True
    return False
