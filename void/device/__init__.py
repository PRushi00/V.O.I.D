"""V.O.I.D device communication foundation (V1).

Secure, minimal plumbing that lets a paired personal device (the future
Android companion) talk to the V.O.I.D runtime on this laptop over a local
network (a phone's Wi-Fi hotspot in the common case). The laptop is the
server; a device connects to it, never the other way around, so discovery is
just "type in the laptop's address" (see :mod:`void.device.pairing`) and the
attack surface is exactly one process listening on one port, opt-in only.

Layers (deliberately separate, per the architecture review that preceded
this code - see the module docstrings for why each exists):

    transport   void.device.cert      TLS, self-signed cert + pinned fingerprint
    identity    void.device.identity  paired-device registry (no secrets in it)
    auth        void.device.auth     per-request HMAC signing + replay/rate guards
    protocol    void.device.protocol  versioned, size-capped, schema-validated messages
    capability  void.device.capabilities  closed allow-list, never the raw ToolRegistry
    gateway     void.device.gateway   the HTTPS server wiring all of the above together

None of this is wired into the voice runtime or autostart: the gateway only
runs when explicitly started (``python -m void device serve``), and nothing
here weakens RiskGate, KillSwitch, allowed_roots, or secret storage - it reuses
them exactly as the CLI/Agent already do.
"""
