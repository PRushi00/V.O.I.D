"""Risk classification and the owner-confirmation gate.

Every action declares a *base* risk level. The gate decides, from config and
context, whether an action may run autonomously or needs the owner's explicit
go-ahead. This is the mechanism behind the principle "maximum practical
autonomy, not blind autonomy".
"""
from __future__ import annotations

import enum
from typing import Callable


class RiskLevel(enum.IntEnum):
    LOW = 1      # read-only or trivially reversible (search, read, open app)
    MEDIUM = 2   # modifies data but recoverable (edit a file, move to recycle)
    HIGH = 3     # dangerous / hard to reverse (permanent delete, run script)

    @classmethod
    def parse(cls, value: "str | RiskLevel") -> "RiskLevel":
        if isinstance(value, RiskLevel):
            return value
        return cls[str(value).strip().upper()]


# A ConfirmFn is asked to authorize a single action. It receives a short
# human-readable description and returns True to allow, False to deny.
ConfirmFn = Callable[[str], bool]


def deny_all(_description: str) -> bool:
    """Default confirmer for unattended/autonomous runs: refuse high-risk ops."""
    return False


class RiskGate:
    """Decides whether an action may proceed given its risk level."""

    def __init__(self, confirm_at_or_above: "str | RiskLevel" = RiskLevel.HIGH,
                 confirm_fn: ConfirmFn | None = None):
        self.threshold = RiskLevel.parse(confirm_at_or_above)
        # If no confirmer is supplied we assume unattended operation and deny
        # anything at/above the threshold rather than guessing.
        self.confirm_fn = confirm_fn or deny_all

    def requires_confirmation(self, level: RiskLevel) -> bool:
        return level >= self.threshold

    def authorize(self, level: RiskLevel, description: str) -> bool:
        """Return True if the action is allowed to run."""
        if not self.requires_confirmation(level):
            return True
        return bool(self.confirm_fn(description))
