"""The camera gate: the only thing that can permit a frame to be taken, and the state the owner can see.

Everything about this package is arranged around one fact: a camera on a personal machine points at the
owner. So the question it answers is not "can V.O.I.D see?" but "can the owner tell, at any moment, whether
V.O.I.D can see, and did they say so?"

Four controls, each independent of the others, and each tested:

**A master switch in configuration.** ``camera.enabled`` is ``false`` by default. While it is false the
capability does not exist: not for the model, not for a tool call, not for MCP, not for an owner
confirmation. Turning it on is an edit to the owner's own config file, which no part of V.O.I.D can write.
This is the same deny-by-default shape as ``security.allowed_roots``.

**An explicit, owner-confirmed activation.** Even with the master switch on, the camera is off until
something asks for it and :class:`~void.security.risk.RiskGate` gets the owner's answer. The activating
tool is HIGH risk, so in an unattended run - where there is no confirmer and the gate's default is to deny -
the camera can never come on at all.

**A session that expires by itself.** An activation lasts :attr:`CameraPolicy.session_timeout_s` and then
lapses. There is no way to activate indefinitely. This is what makes "no uncontrolled background capture"
structural rather than a promise: a forgotten "yes" becomes "off" on its own, and every capture re-checks.

**No recording, at all.** There is no function in this package that records video, and no function that
writes a frame to disk. A capture produces one image in memory, used for one answer, and dropped. "No
silent recording" is not a policy here - it is the absence of the capability.

Separately: sending a frame to a cloud model is its own decision, with its own switch
(``camera.allow_cloud_analysis``, also false by default), because it is a different risk from looking at a
frame locally. See :mod:`void.actions.vision` for how that is surfaced and reported.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from void import perf

_log = logging.getLogger(__name__)

#: How long an activation lasts before it lapses, in seconds. Short on purpose: long enough to ask a
#: follow-up question about what the camera can see, far too short to become a monitoring session.
DEFAULT_SESSION_TIMEOUT_S = 120.0

#: An activation may never be requested for longer than this, whatever a caller asks for.
MAX_SESSION_TIMEOUT_S = 600.0

#: States the gate reports. ``disabled`` means configuration forbids the capability entirely; ``off`` means
#: it is permitted but not active; ``active`` means a frame can be taken right now.
DISABLED, OFF, ACTIVE = "disabled", "off", "active"


class CameraDenied(RuntimeError):
    """A capture was refused. The message is the reason, and is safe to say out loud."""


@dataclass(frozen=True)
class CameraPolicy:
    """What the owner's configuration permits. Read once at construction; never written by V.O.I.D."""

    enabled: bool = False
    session_timeout_s: float = DEFAULT_SESSION_TIMEOUT_S
    allow_cloud_analysis: bool = False
    device_index: int = 0
    #: Frames are downscaled to at most this width before anything looks at them. Smaller images carry
    #: less incidental detail (a document on the desk, a face in the background) and cost less to send.
    max_width: int = 640

    @classmethod
    def from_config(cls, config) -> "CameraPolicy":
        """Build from V.O.I.D's config, clamping anything out of range rather than trusting it."""
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default
        timeout = get("camera.session_timeout_s", DEFAULT_SESSION_TIMEOUT_S)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_SESSION_TIMEOUT_S
        index = get("camera.device_index", 0)
        try:
            index = max(0, int(index))
        except (TypeError, ValueError):
            index = 0
        width = get("camera.max_width", 640)
        try:
            width = max(160, min(int(width), 1920))
        except (TypeError, ValueError):
            width = 640
        return cls(enabled=bool(get("camera.enabled", False)),
                   session_timeout_s=max(5.0, min(timeout, MAX_SESSION_TIMEOUT_S)),
                   allow_cloud_analysis=bool(get("camera.allow_cloud_analysis", False)),
                   device_index=index,
                   max_width=width)


class CameraGate:
    """Holds whether the camera may be used right now, and why.

    The gate is the only authority on that question. :mod:`void.vision.camera` will not open a device
    without being handed a gate that says yes, and the tools in :mod:`void.actions.vision` ask the gate
    before every capture - not once per session - so an activation that has lapsed stops the next frame
    rather than the next session.

    ``on_audit`` receives a short, non-sensitive line for every state change and every capture. It is how
    "no silent recording" becomes checkable after the fact: a capture that left no audit line is a bug, and
    there is a test for it.
    """

    def __init__(self, policy: CameraPolicy | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 on_audit: Callable[[str], None] | None = None):
        self._policy = policy or CameraPolicy()
        self._clock = clock
        self._audit = on_audit or (lambda line: _log.info("CAMERA %s", line))
        self._active_until: float | None = None
        self._captures = 0
        self._cloud_calls = 0

    @property
    def policy(self) -> CameraPolicy:
        return self._policy

    @property
    def captures(self) -> int:
        """How many frames have been taken through this gate since it was created."""
        return self._captures

    @property
    def state(self) -> str:
        """``disabled``, ``off`` or ``active`` - evaluated now, so a lapsed session reads as ``off``."""
        if not self._policy.enabled:
            return DISABLED
        if self._active_until is None or self._clock() >= self._active_until:
            return OFF
        return ACTIVE

    @property
    def seconds_remaining(self) -> float | None:
        """Seconds left in the current activation, or None when the camera is not active."""
        if self.state != ACTIVE or self._active_until is None:
            return None
        return max(0.0, self._active_until - self._clock())

    def activate(self, seconds: float | None = None) -> float:
        """Permit captures for a bounded window. Returns the window's length in seconds.

        This does *not* authorize anything by itself - it records that authorization happened. The
        owner's decision is made by RiskGate, before this is called; see
        :meth:`void.actions.vision.VisionActions.enable_camera`.
        """
        if not self._policy.enabled:
            raise CameraDenied(
                "The camera is switched off in V.O.I.D's configuration. Only you can turn it on, by "
                "setting camera.enabled in your local config.")
        window = self._policy.session_timeout_s if seconds is None else float(seconds)
        window = max(5.0, min(window, self._policy.session_timeout_s))
        self._active_until = self._clock() + window
        self._audit(f"ACTIVATED for {window:.0f}s")
        perf.emit("camera", op="activate", state=ACTIVE, session_s=round(window, 1),
                  cloud=bool(self._policy.allow_cloud_analysis))
        return window

    def deactivate(self, reason: str = "owner") -> None:
        """Turn the camera off now. Always allowed: stopping is never the risky direction."""
        was = self.state
        self._active_until = None
        if was == ACTIVE:
            self._audit(f"DEACTIVATED ({reason})")
            perf.emit("camera", op="deactivate", state=OFF, captures=self._captures)

    def check(self) -> None:
        """Raise :class:`CameraDenied` unless a frame may be taken this instant."""
        state = self.state
        if state != ACTIVE:
            perf.emit("camera", op="denied", state=state)
        if state == DISABLED:
            raise CameraDenied(
                "The camera is switched off in V.O.I.D's configuration, so I cannot use it.")
        if state == OFF:
            if self._active_until is not None:
                # Distinguishing these two matters: the owner who said yes two minutes ago should be told
                # the session lapsed, not that they never agreed.
                raise CameraDenied(
                    "The camera session has expired. Ask me to turn the camera on again if you want me "
                    "to look.")
            raise CameraDenied(
                "The camera is not active. Ask me to turn the camera on first - I will check with you.")

    def note_capture(self) -> None:
        """Record that one frame was taken. Called by the capture path, never by a caller."""
        self._captures += 1
        remaining = self.seconds_remaining
        fields = {"op": "capture", "state": self.state, "captures": self._captures}
        if remaining is not None:
            fields["session_s"] = round(remaining, 1)
        perf.emit("camera", **fields)
        self._audit(f"CAPTURE #{self._captures}"
                    + (f" ({remaining:.0f}s left in session)" if remaining is not None else ""))

    def note_cloud_analysis(self, sent_bytes: int, *, ok: bool) -> None:
        """Record that one frame left this machine for a cloud vision model.

        The single most privacy-significant thing the camera can do, so it gets its own audit line and its
        own telemetry event rather than being folded into the capture. Called by the capability layer after
        the bytes have actually gone, so the record describes what happened and not what was intended -
        ``ok`` says whether an answer came back, but the egress is recorded either way.

        Counts only: the number of bytes and whether it worked. There is no field here that could carry the
        image, the description, or what the owner asked about it.
        """
        self._cloud_calls += 1
        perf.emit("camera", op="cloud_analysis", state=self.state, captures=self._captures,
                  cloud=True, bytes_sent=int(sent_bytes))
        self._audit(f"CLOUD_ANALYSIS #{self._cloud_calls} sent={int(sent_bytes)}B answered={bool(ok)}")

    @property
    def cloud_calls(self) -> int:
        """How many frames have been sent to a cloud vision model through this gate."""
        return self._cloud_calls

    def status(self) -> dict:
        """A description of the gate for the owner: state, time left, and what is permitted."""
        return {"state": self.state,
                "enabled_in_config": self._policy.enabled,
                "seconds_remaining": (round(self.seconds_remaining, 1)
                                      if self.seconds_remaining is not None else None),
                "session_timeout_s": self._policy.session_timeout_s,
                "cloud_analysis_allowed": self._policy.allow_cloud_analysis,
                "captures_this_session": self._captures,
                "cloud_analyses_this_session": self._cloud_calls}
