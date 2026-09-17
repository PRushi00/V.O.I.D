"""Tests for the closed capability allow-list and its dispatch into the
existing RiskGate / KillSwitch / ToolRegistry - the exact security boundary
the device-communication task existed to build. These tests prove a network
request can reach ONLY get_status and launch_app, and that even a
(hypothetical, directly-invoked) HIGH-risk tool or an engaged KillSwitch
is refused, never silently executed."""
import pytest

from void.actions.base import Tool, ToolResult
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device import capabilities as caps
from void.device.protocol import ErrorCode, ProtocolError
from void.security.risk import RiskGate, RiskLevel


def _config():
    return Config({"app": {"name": "V.O.I.D", "version": "9.9.9"},
                  "voice": {"enabled": True}})


def _ctx(tools=None, risk_gate=None, kill_switch=None):
    return caps.GatewayContext(
        config=_config(),
        tools=tools or ToolRegistry(),
        risk_gate=risk_gate or RiskGate(),
        kill_switch=kill_switch or KillSwitch(),
    )


def test_get_status_returns_expected_shape_and_needs_no_tool():
    ctx = _ctx()
    result = caps.dispatch("get_status", {}, ["get_status"], ctx)
    assert result == {"name": "V.O.I.D", "version": "9.9.9", "voice_enabled": True}


def test_unknown_operation_rejected():
    ctx = _ctx()
    with pytest.raises(ProtocolError) as exc:
        caps.dispatch("execute_shell", {}, ["execute_shell"], ctx)
    assert exc.value.code == ErrorCode.UNKNOWN_OPERATION


def test_operation_not_granted_to_this_device_rejected():
    ctx = _ctx()
    with pytest.raises(ProtocolError) as exc:
        caps.dispatch("launch_app", {"name": "notepad"}, ["get_status"], ctx)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED


def test_the_allow_list_never_exposes_execute_style_operations():
    forbidden = {"execute_command", "execute_shell", "arbitrary_code",
                "unrestricted_tool", "run_goal", "delete_file", "write_file"}
    assert forbidden.isdisjoint(caps.ALL_CAPABILITIES)


def test_launch_app_success_flows_through_the_real_tool():
    calls = []

    def handler(name):
        calls.append(name)
        return ToolResult.success(f"Launched {name}.")

    registry = ToolRegistry()
    registry.register(Tool(name="launch_app", description="d",
                           parameters={"type": "object"}, handler=handler,
                           risk=RiskLevel.LOW))
    ctx = _ctx(tools=registry)
    result = caps.dispatch("launch_app", {"name": "notepad"}, ["launch_app"], ctx)
    assert calls == ["notepad"]
    assert result == {"summary": "Launched notepad."}


def test_launch_app_tool_failure_surfaces_as_denied():
    registry = ToolRegistry()
    registry.register(Tool(
        name="launch_app", description="d", parameters={"type": "object"},
        handler=lambda name: ToolResult.failure("Unknown application."),
        risk=RiskLevel.LOW))
    ctx = _ctx(tools=registry)
    with pytest.raises(ProtocolError) as exc:
        caps.dispatch("launch_app", {"name": "nope"}, ["launch_app"], ctx)
    assert exc.value.code == ErrorCode.DENIED


def test_kill_switch_engaged_blocks_a_tool_capability():
    registry = ToolRegistry()
    registry.register(Tool(name="launch_app", description="d",
                           parameters={"type": "object"},
                           handler=lambda name: ToolResult.success("ok"),
                           risk=RiskLevel.LOW))
    ks = KillSwitch()
    ks.engage(reason="test stop")
    ctx = _ctx(tools=registry, kill_switch=ks)
    with pytest.raises(ProtocolError) as exc:
        caps.dispatch("launch_app", {"name": "notepad"}, ["launch_app"], ctx)
    assert exc.value.code == ErrorCode.DENIED


def test_a_high_risk_tool_is_never_executed_over_the_gateway_even_if_reachable():
    """Belt-and-suspenders: even if a future capability mapping pointed at a
    HIGH-risk tool, RiskGate must still deny it - a headless gateway has no
    confirm_fn, so it fails safe rather than silently running the action."""
    registry = ToolRegistry()
    calls = []
    registry.register(Tool(
        name="dangerous", description="d", parameters={"type": "object"},
        handler=lambda: calls.append("ran") or ToolResult.success("ran"),
        risk=RiskLevel.HIGH))
    ctx = _ctx(tools=registry)
    with pytest.raises(ProtocolError) as exc:
        caps._run_tool_capability("dangerous", {}, ctx)
    assert exc.value.code == ErrorCode.DENIED
    assert calls == []   # never actually invoked


def test_default_granted_capabilities_is_read_only_status_only():
    assert caps.DEFAULT_GRANTED_CAPABILITIES == ["get_status"]
