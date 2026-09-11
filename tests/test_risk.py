"""Tests for risk classification and the confirmation gate."""
from void.actions.base import Tool, ToolResult
from void.security.risk import RiskGate, RiskLevel, deny_all


def test_parse_levels():
    assert RiskLevel.parse("high") is RiskLevel.HIGH
    assert RiskLevel.parse("LOW") is RiskLevel.LOW
    assert RiskLevel.parse(RiskLevel.MEDIUM) is RiskLevel.MEDIUM


def test_low_risk_runs_without_confirmation():
    gate = RiskGate(confirm_at_or_above="high")
    assert gate.authorize(RiskLevel.LOW, "search")
    assert gate.authorize(RiskLevel.MEDIUM, "edit")


def test_high_risk_denied_when_unattended():
    # No confirm_fn -> deny_all -> high risk is refused.
    gate = RiskGate(confirm_at_or_above="high")
    assert not gate.authorize(RiskLevel.HIGH, "delete file")


def test_high_risk_allowed_when_confirmed():
    gate = RiskGate(confirm_at_or_above="high", confirm_fn=lambda d: True)
    assert gate.authorize(RiskLevel.HIGH, "delete file")


def test_threshold_medium():
    gate = RiskGate(confirm_at_or_above="medium", confirm_fn=lambda d: False)
    assert gate.authorize(RiskLevel.LOW, "search")
    assert not gate.authorize(RiskLevel.MEDIUM, "edit")


def test_deny_all():
    assert deny_all("anything") is False


# --- fail-safe: risk classification can never be downgraded ------------

def test_write_file_effective_risk_missing_or_unusable_path_is_high(tmp_path):
    # write_file's dynamic risk_fn (_write_risk) must fail safe to HIGH - never
    # silently classify as LOW/MEDIUM - when it cannot determine whether the
    # target already exists.
    from void.actions.files import FileActions

    write_tool = next(t for t in FileActions(allowed_roots=[tmp_path]).tools()
                      if t.name == "write_file")

    # No 'path' key at all.
    assert write_tool.effective_risk({}) is RiskLevel.HIGH
    # Falsy / missing path values.
    assert write_tool.effective_risk({"path": None}) is RiskLevel.HIGH
    assert write_tool.effective_risk({"path": ""}) is RiskLevel.HIGH
    # Malformed (wrong-type) argument values that make os.path.expanduser /
    # Path(...).resolve() raise inside _write_risk's try/except - exercising
    # the exception branch specifically, not just the "no path" early return.
    assert write_tool.effective_risk({"path": 12345}) is RiskLevel.HIGH
    assert write_tool.effective_risk({"path": ["a", "b"]}) is RiskLevel.HIGH


def test_effective_risk_never_downgrades_when_risk_fn_raises():
    # A generic Tool-level guarantee, independent of any specific tool: if the
    # dynamic risk_fn blows up for ANY reason, effective_risk() must return
    # HIGH - an error must never silently downgrade a dangerous action.
    def _boom(_arguments):
        raise RuntimeError("risk classification exploded")

    tool = Tool(
        name="dangerous_thing", description="test tool with a raising risk_fn",
        parameters={"type": "object", "properties": {}},
        handler=lambda **_kw: ToolResult.success("ran"),
        risk=RiskLevel.LOW,       # static risk deliberately LOW/permissive
        risk_fn=_boom,
    )
    assert tool.effective_risk({}) is RiskLevel.HIGH
    assert tool.effective_risk({"anything": "at-all"}) is RiskLevel.HIGH
    # The static risk alone (no risk_fn) is unaffected - only a RAISING risk_fn
    # triggers the fail-safe; this isolates the guarantee being tested.
    safe_tool = Tool(name="safe_thing", description="d",
                     parameters={"type": "object", "properties": {}},
                     handler=lambda **_kw: ToolResult.success("ran"),
                     risk=RiskLevel.LOW)
    assert safe_tool.effective_risk({}) is RiskLevel.LOW


# --- KillSwitch engaged after a HIGH-risk step is already pending ------

def test_killswitch_after_pending_blocks_approved_execution(tmp_path):
    # A HIGH-risk delete defers to AWAITING_CONFIRMATION. The KillSwitch then
    # engages - AFTER the pending state was created, simulating an emergency
    # stop that arrives before the owner's approval is applied. The owner's
    # (now-stale) approval must NOT execute the delete: KillSwitch supersedes
    # a durable approval, exactly as it supersedes a live one.
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import Status, TaskStore
    from void.providers.base import LLMResponse

    from tests.helpers import FakeProvider, tool_call

    class _CountingRiskGate(RiskGate):
        """Records every authorize() call, to prove the delete never even
        reached authorization (not merely that it didn't execute)."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.authorize_calls = []

        def authorize(self, level, description, owner_decision=None):
            self.authorize_calls.append((level, description, owner_decision))
            return super().authorize(level, description,
                                     owner_decision=owner_decision)

    target = tmp_path / "keep.txt"
    target.write_text("precious")

    files = FileActions(allowed_roots=[tmp_path])
    tools = ToolRegistry()
    tools.register_all(files.tools())
    ks = KillSwitch()
    gate = _CountingRiskGate(confirm_at_or_above="high")   # headless: no confirm_fn
    provider = FakeProvider([
        LLMResponse(tool_calls=[tool_call("delete_file", path=str(target))]),
        LLMResponse(text="finished"),   # would be served only if resumed further
    ])
    agent = Agent(provider, tools, gate, ks, TaskStore(tmp_path / "t.sqlite"),
                  max_retries=0, defer_confirmation=True)

    result = agent.run("delete keep.txt")
    assert result.status == Status.AWAITING_CONFIRMATION
    assert target.exists()                          # nothing ran yet
    gate.authorize_calls.clear()                     # isolate what happens next

    # The KillSwitch engages strictly AFTER the pending state exists.
    ks.engage(reason="emergency stop before approval was applied")

    task = agent.store.load(result.task.id)
    assert task.status == Status.AWAITING_CONFIRMATION and task.pending

    r2 = agent.resume_pending(task, decision=True)    # owner's approval arrives too late

    # No side effect: the file is untouched, byte for byte.
    assert target.exists()
    assert target.read_text() == "precious"
    # The delete never even reached the risk gate - KillSwitch pre-empts it.
    assert gate.authorize_calls == []
    # The task is left in a safe, resumable (not completed, not silently
    # re-armed) state; the stale pending is cleared rather than re-offered.
    assert r2.status == Status.PAUSED
    final = agent.store.load(result.task.id)
    assert final.status == Status.PAUSED
    assert final.pending is None
    tool_msgs = [m for m in final.messages if m["role"] == "tool"]
    assert any("did not run" in m["content"] for m in tool_msgs)
