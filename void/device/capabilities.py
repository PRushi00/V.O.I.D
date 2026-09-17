"""The closed capability allow-list - the boundary between "a paired device"
and "the ToolRegistry".

This is a fixed, hand-written mapping, never derived from
``ToolRegistry.names()``: a paired, capability-granted device can reach
EXACTLY the operations listed here, nothing else, even if the ToolRegistry
grows new (possibly higher-risk) tools later. Adding a new device-reachable
capability is a source-code change and review, not a runtime grant.

``get_status`` is a gateway-native, read-only capability that never touches
the Agent/ToolRegistry at all. ``launch_app`` re-uses the EXISTING,
already-validated ``launch_app`` tool (void.actions.apps) via the SAME
authorization path the Agent itself uses -
:class:`~void.core.kill_switch.KillSwitch` then
:class:`~void.security.risk.RiskGate` then :class:`~void.actions.registry.ToolRegistry`
- so a network request can never take a shortcut around either control.
``launch_app`` is LOW risk and already refuses arbitrary commands/paths (see
its docstring); nothing here raises its risk or loosens its own validation.

Deliberately NOT here, and never to be added without a full new security
review: execute_command, execute_shell, arbitrary file writes/deletes, or
any HIGH-risk tool - RiskGate.authorize() would deny those anyway (a headless
gateway has no confirm_fn, so it fails safe to "denied"), but keeping the
allow-list itself closed means that fact is never load-bearing on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch, StopRequested
from void.device.protocol import ErrorCode, ProtocolError
from void.security.risk import RiskGate

STATUS = "get_status"

# capability name -> backing tool name, for capabilities that dispatch into
# the existing ToolRegistry.
TOOL_CAPABILITIES: dict[str, str] = {
    "launch_app": "launch_app",
}

# The full, fixed set of operation names a device can ever reach.
ALL_CAPABILITIES: frozenset[str] = frozenset({STATUS, *TOOL_CAPABILITIES})

# Capabilities granted automatically on pairing - read-only status only.
# Everything else (including launch_app) requires an explicit, separate
# owner grant (see void.device.identity.DeviceRegistry.grant / `void device
# grant` in the CLI).
DEFAULT_GRANTED_CAPABILITIES: list[str] = [STATUS]


@dataclass
class GatewayContext:
    """Everything a capability handler needs, and nothing more - notably not
    the raw Agent, and not direct socket/network access (see void.device
    package docstring: the Agent/gateway never touches sockets directly)."""
    config: Config
    tools: ToolRegistry
    risk_gate: RiskGate
    kill_switch: KillSwitch


def _get_status(parameters: dict, ctx: GatewayContext) -> dict:
    return {
        "name": ctx.config.get("app.name", "V.O.I.D"),
        "version": ctx.config.get("app.version", "0.0.0"),
        "voice_enabled": bool(ctx.config.get("voice.enabled", False)),
    }


def _run_tool_capability(tool_name: str, parameters: dict,
                         ctx: GatewayContext) -> dict:
    try:
        ctx.kill_switch.raise_if_engaged()
    except StopRequested as exc:
        raise ProtocolError(ErrorCode.DENIED, f"V.O.I.D is stopped: {exc}")

    tool = ctx.tools.get(tool_name)
    if tool is None:  # pragma: no cover - allow-list/registry drift, not reachable in tests
        raise ProtocolError(ErrorCode.INTERNAL, "Capability is temporarily unavailable.")

    description = f"{tool_name}({parameters}) [via device gateway]"
    risk = tool.effective_risk(parameters)
    # owner_decision=None: identical to the Agent's own call (void.core.agent) -
    # below the confirmation threshold, auto-allowed; at/above it, consulted
    # via confirm_fn, which the gateway never supplies, so it fails safe to
    # denied rather than silently executing a HIGH-risk action for a network
    # caller with nobody available to confirm it.
    if not ctx.risk_gate.authorize(risk, description, owner_decision=None):
        raise ProtocolError(
            ErrorCode.DENIED,
            f"Action requires owner confirmation and cannot be authorized "
            f"over the device gateway (risk={risk.name}).")

    result = ctx.tools.execute(tool_name, parameters)
    if not result.ok:
        raise ProtocolError(ErrorCode.DENIED, result.summary)
    return {"summary": result.summary}


def dispatch(operation: str, parameters: dict, granted_capabilities: list[str],
            ctx: GatewayContext) -> dict:
    """Validate ``operation`` against the closed allow-list AND the specific
    device's grants, then run it. Raises :class:`ProtocolError` for anything
    not explicitly permitted - default deny."""
    if operation not in ALL_CAPABILITIES:
        raise ProtocolError(ErrorCode.UNKNOWN_OPERATION,
                            f"Unknown operation: {operation!r}")
    if operation not in granted_capabilities:
        raise ProtocolError(
            ErrorCode.NOT_AUTHORIZED,
            f"This device is not authorized for '{operation}'.")

    if operation == STATUS:
        return _get_status(parameters, ctx)
    return _run_tool_capability(TOOL_CAPABILITIES[operation], parameters, ctx)
