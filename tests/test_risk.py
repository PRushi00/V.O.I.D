"""Tests for risk classification and the confirmation gate."""
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
