"""Camera capabilities: the gate's state, turning it on and off, and taking one look.

Four tools, and their risk levels *are* the security design, so they are worth stating plainly:

``get_camera_status`` LOW. Reading whether the camera is on changes nothing, and the owner must be able to
ask it freely - a privacy indicator that needs permission to read is not an indicator.

``enable_camera`` HIGH. Opening the camera is the decision that matters, so it is the one the owner is asked
about, through :class:`~void.security.risk.RiskGate` like any other HIGH action. In an unattended run the
gate has no confirmer and denies by default, so the camera cannot come on with nobody there.

``disable_camera`` LOW. Turning the camera off is never the risky direction, and making it cheap means
"V.O.I.D, stop looking" always works immediately.

``look`` MEDIUM. Within a window the owner just authorised, each frame is audited but not re-confirmed -
asking again every few seconds would train the owner to click yes without reading. The control on capture is
the gate, which was confirmed at HIGH and expires by itself; MEDIUM here is the honest level for "a thing
that happens inside something already agreed to". Outside an active window ``look`` is refused outright, not
escalated: there is no path from "no session" to "a frame" that does not go through ``enable_camera``.

On cloud egress. A frame leaves this machine only when ``camera.allow_cloud_analysis`` is true *and* the
owner asked for a description that needs it. When it does, the answer says so. When it is false, ``look``
returns what can be read locally and names what it cannot do, rather than quietly returning less than was
asked for. This machine has no local vision model (Ollama holds a text-only model, and the headless OpenCV
build ships no classifiers), so local analysis is geometry and light - honestly limited, and labelled.

These tools are deliberately NOT exposed through MCP. An MCP client is a program, not the owner, and the
data in question is a picture of whoever is sitting at the machine; "the owner confirmed a camera session"
does not transfer to a different caller. See docs/V2_DOMAINS.md for that decision.
"""
from __future__ import annotations

import logging

from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel
from void.vision import ACTIVE, DISABLED, OFF, CameraDenied, CameraGate, CameraPolicy
from void.vision import camera as camera_backend

_log = logging.getLogger(__name__)


class VisionActions:
    """The camera tools, over one :class:`~void.vision.CameraGate` shared for the process.

    The gate is held here, so activation state lives for as long as V.O.I.D runs and no longer. Nothing
    persists it: a restart leaves the camera off, which is the right default to come back to.
    """

    def __init__(self, gate: CameraGate | None = None, config=None):
        if gate is None:
            policy = CameraPolicy.from_config(config) if config is not None else CameraPolicy()
            gate = CameraGate(policy)
        self._gate = gate

    @property
    def gate(self) -> CameraGate:
        return self._gate

    # -- status --
    def get_camera_status(self) -> ToolResult:
        """Whether the camera is off, active, or disabled in configuration - and for how long."""
        status = self._gate.status()
        state = status["state"]
        present = camera_backend.devices_present()
        status["hardware_present"] = present
        if state == DISABLED:
            summary = ("The camera is switched off in V.O.I.D's configuration, so I cannot use it at all. "
                       "You can enable it by setting camera.enabled in your local config.")
        elif state == OFF:
            summary = "The camera is not active. Nothing is being captured."
            if not present:
                summary += " This machine also reports no camera hardware."
        else:
            remaining = status["seconds_remaining"]
            summary = (f"The camera is active for another {remaining:.0f} seconds. "
                       f"{status['captures_this_session']} frame(s) taken so far.")
        if state != DISABLED:
            summary += (" Sending images to a cloud model is "
                        + ("allowed." if status["cloud_analysis_allowed"] else "switched off."))
        return ToolResult.success(summary, data=status)

    # -- activation --
    def enable_camera(self, seconds: float | None = None) -> ToolResult:
        """Turn the camera on for a bounded window.

        RiskGate has already asked the owner by the time this runs - this tool is HIGH precisely so that it
        does. What happens here is recording the decision and starting the clock.
        """
        try:
            window = self._gate.activate(seconds)
        except CameraDenied as denied:
            return ToolResult.failure(str(denied))
        return ToolResult.success(
            f"The camera is on for {window:.0f} seconds. It will switch itself off after that, and you can "
            f"tell me to stop at any time.", data=self._gate.status())

    def disable_camera(self) -> ToolResult:
        """Turn the camera off now."""
        was_active = self._gate.state == ACTIVE
        self._gate.deactivate("owner asked")
        return ToolResult.success(
            "The camera is off." if was_active else "The camera was already off.",
            data=self._gate.status())

    # -- capture --
    def look(self, question: str | None = None) -> ToolResult:
        """Take one frame and describe what can honestly be said about it.

        With cloud analysis switched off this reports the geometry and lighting it measured and says plainly
        that describing the contents needs a vision model it does not have locally. It never guesses at
        content, because a confident wrong answer about what the camera can see is worse than no answer.
        """
        try:
            frame = camera_backend.capture(self._gate)
        except CameraDenied as denied:
            return ToolResult.failure(str(denied))
        except camera_backend.CameraUnavailable as broken:
            return ToolResult.failure(str(broken))
        facts = camera_backend.local_facts(frame)
        data = {"frame": facts, "camera": self._gate.status(), "sent_to_cloud": False}
        if question:
            # Recorded so the answer can be matched to what was asked; never used to build a command.
            data["question"] = str(question)[:200]
        if facts.get("looks_dark"):
            summary = (f"I took a {frame.width}x{frame.height} image and it is almost completely dark - "
                       f"the lens may be covered, or the room is unlit.")
        elif facts.get("looks_featureless"):
            summary = (f"I took a {frame.width}x{frame.height} image but it has almost no detail in it, "
                       f"which usually means the camera is covered or pointed at a blank surface.")
        else:
            summary = (f"I took a {frame.width}x{frame.height} image and the camera is working "
                       f"(brightness {facts.get('mean_brightness')}, contrast {facts.get('contrast')}).")
        if self._gate.policy.allow_cloud_analysis:
            # The policy permits egress, but the capability that would use it is not built yet. Saying so
            # is the honest answer; claiming a description would be a fabricated one.
            summary += (" Describing what is in the picture needs a vision model; that is not wired up "
                        "yet, so nothing was sent.")
        else:
            summary += (" I cannot tell you what is in the picture: that needs a vision model, and "
                        "sending images to a cloud one is switched off in your configuration.")
        return ToolResult.success(summary, data=data)

    # -- registration --
    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="get_camera_status",
                description=(
                    "Report whether V.O.I.D's camera access is off, active, or disabled in configuration, "
                    "how long any active session has left, and whether sending images to a cloud model is "
                    "permitted. Reads state only - it does not open the camera."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.get_camera_status,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="enable_camera",
                description=(
                    "Turn the camera on for a short, self-expiring session so that it can be looked "
                    "through. Always requires the owner's explicit confirmation. The session ends by "
                    "itself; it cannot be made permanent."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number",
                                    "description": ("How long to keep the camera on. Capped by the "
                                                    "owner's configured session limit.")},
                    },
                    "required": [],
                },
                handler=self.enable_camera,
                # The decision that matters: opening a camera on the owner's machine.
                risk=RiskLevel.HIGH,
            ),
            Tool(
                name="disable_camera",
                description="Turn the camera off immediately, ending any active session.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.disable_camera,
                risk=RiskLevel.LOW,
                terminal_on_success=True,
            ),
            Tool(
                name="look",
                description=(
                    "Take a single still image through the camera and report what can be determined from "
                    "it. Requires an active camera session (see enable_camera) and will refuse without "
                    "one. Captures one frame only - it cannot record video, and the image is not saved."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "What the owner wants to know about the view."},
                    },
                    "required": [],
                },
                handler=self.look,
                # Inside a window the owner confirmed at HIGH, and audited. See the module docstring.
                risk=RiskLevel.MEDIUM,
            ),
        ]
