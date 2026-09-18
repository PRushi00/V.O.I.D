"""Paired-device registry: identity and capability grants, no secrets.

A device is "paired" only after it completes the token-gated pairing
exchange (void.device.pairing). Being on the same hotspot, knowing the IP,
or knowing the port grants nothing by itself - only an entry in this
registry, created by that exchange, does.

This file (``<state_dir>/devices.json``) holds ONLY non-secret metadata:
device id, a human label, granted capability names, and timestamps. The
actual per-device shared secret used to authenticate requests lives in the
OS keyring (void.security.secrets), never here and never in source control -
so this file is safe to read, back up, or inspect without exposing anything
that would let someone impersonate a device.

Every read/write here re-loads the file from disk rather than trusting an
in-memory snapshot - the same reload-per-call design
:class:`~void.device.pairing.PairingManager` already uses, for the same
reason: ``device serve`` (a long-lived gateway process) and ``device
grant``/``device revoke``/``device forget`` (short-lived CLI invocations)
are ordinarily SEPARATE processes. A DeviceRegistry that cached its device
list at construction time would mean a capability grant, revocation, or
forget made through the CLI while the gateway is already running had no
effect until the gateway was restarted - for a revocation, that is a real
security gap (a device meant to be cut off immediately would keep working),
not just a staleness annoyance.
"""
from __future__ import annotations

import json
import os
import secrets as _pysecrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from void.security import secrets as secret_store

REGISTRY_FILENAME = "devices.json"


def secret_key_for(device_id: str) -> str:
    """The keyring key holding one device's shared secret."""
    return f"device_secret:{device_id}"


def new_device_id() -> str:
    return _pysecrets.token_hex(16)


def new_shared_secret() -> str:
    return _pysecrets.token_urlsafe(32)


@dataclass
class Device:
    device_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    paired_at: float = 0.0
    last_seen: float | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "capabilities": list(self.capabilities),
            "paired_at": self.paired_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        return cls(
            device_id=str(data["device_id"]),
            name=str(data.get("name", "")),
            capabilities=[str(c) for c in data.get("capabilities", []) or []],
            paired_at=float(data.get("paired_at", 0.0) or 0.0),
            last_seen=(float(data["last_seen"])
                      if data.get("last_seen") is not None else None),
        )


class DeviceRegistry:
    """JSON-file-backed store of paired devices. Not thread-safe internally;
    the gateway serializes access (see void.device.gateway). Holds NO
    device-list state across calls (see module docstring) - construction is
    cheap and never touches disk; every method call below reads (and, for
    mutations, atomically rewrites) the file fresh."""

    def __init__(self, path: Path, get_secret=None, set_secret=None,
                delete_secret=None):
        self._path = path
        self._get_secret = get_secret or secret_store.get_secret
        self._set_secret = set_secret or secret_store.set_secret
        self._delete_secret = delete_secret or secret_store.delete_secret

    def _load(self) -> dict[str, Device]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Device] = {}
        for did, data in raw.items():
            if isinstance(data, dict):
                try:
                    out[did] = Device.from_dict(data)
                except (KeyError, ValueError, TypeError):
                    continue
        return out

    def _save(self, devices: dict[str, Device]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {did: dev.to_dict() for did, dev in devices.items()}
        payload = json.dumps(data, indent=2)
        # Atomic write (temp file + os.replace), same rationale as
        # void.device.pairing.PairingManager: a concurrent reader (a
        # different process, or the gateway's own next request) must never
        # be able to observe a partially-written file.
        tmp_path = self._path.parent / f"{self._path.name}.tmp{os.getpid()}"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._path)

    # --- queries ---------------------------------------------------------

    def get(self, device_id: str) -> Device | None:
        return self._load().get(device_id)

    def list(self) -> list[Device]:
        return sorted(self._load().values(), key=lambda d: d.paired_at)

    def get_secret_value(self, device_id: str) -> str | None:
        """Read a device's shared secret from the OS keyring on demand -
        never cached on the Device object."""
        return self._get_secret(secret_key_for(device_id))

    # --- mutation (owner/pairing-flow only; never called from a network
    # request handler except via the gated pairing exchange itself) --------

    def pair(self, name: str, capabilities: list[str] | None = None) -> tuple[Device, str]:
        """Create a new paired device with a fresh id + shared secret.
        Returns (device, secret) - the ONLY moment the secret value exists
        outside the keyring, and it is handed straight back to the pairing
        response, never logged, never stored on the Device object."""
        if capabilities is None:
            from void.device.capabilities import DEFAULT_GRANTED_CAPABILITIES
            capabilities = DEFAULT_GRANTED_CAPABILITIES
        devices = self._load()
        device_id = new_device_id()
        secret = new_shared_secret()
        self._set_secret(secret_key_for(device_id), secret)
        device = Device(device_id=device_id, name=name,
                        capabilities=list(capabilities),
                        paired_at=time.time())
        devices[device_id] = device
        self._save(devices)
        return device, secret

    def touch(self, device_id: str) -> None:
        devices = self._load()
        dev = devices.get(device_id)
        if dev is not None:
            dev.last_seen = time.time()
            self._save(devices)

    def grant(self, device_id: str, capability: str) -> Device:
        devices = self._load()
        dev = devices.get(device_id)
        if dev is None:
            raise KeyError(f"Unknown device: {device_id}")
        if capability not in dev.capabilities:
            dev.capabilities.append(capability)
            self._save(devices)
        return dev

    def revoke_capability(self, device_id: str, capability: str) -> Device:
        devices = self._load()
        dev = devices.get(device_id)
        if dev is None:
            raise KeyError(f"Unknown device: {device_id}")
        if capability in dev.capabilities:
            dev.capabilities.remove(capability)
            self._save(devices)
        return dev

    def forget(self, device_id: str) -> bool:
        """Fully unpair a device: remove its registry entry AND its keyring
        secret, so a compromised/lost device's credential stops working
        immediately - including for an already-running gateway process that
        paired or granted it, since nothing here is cached in memory."""
        devices = self._load()
        existed = device_id in devices
        devices.pop(device_id, None)
        if existed:
            self._save(devices)
        self._delete_secret(secret_key_for(device_id))
        return existed
