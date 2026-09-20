"""Constant spoken phrases for task outcomes that need the owner's attention (D-13).

In V1 a task that stopped at ``awaiting_confirmation`` / ``blocked`` / ``failed`` /
``paused`` was silent: the owner said something, heard nothing and had no way to know
that the assistant was waiting for approval.

Security rule: these are FIXED strings chosen by status alone. Nothing the model, a tool,
a file, a window title or an error message produced is ever interpolated - that text is
untrusted, and speaking it would hand an attacker a voice channel. The phrases also
point the owner to the command line: voice can announce that approval is needed but can
never grant it.
"""
from __future__ import annotations

from void.core.task import Status

ENGINE_STATUS_PHRASES: dict[str, str] = {
    Status.AWAITING_CONFIRMATION: "That needs your approval. Please use the command line to approve or deny it.",
    Status.BLOCKED: "I need more information to continue. Please check the command line.",
    Status.FAILED: "That task failed. Please check the command line for details.",
    Status.PAUSED: "That task was paused. You can resume it from the command line.",
}


def phrase_for(status: object) -> str | None:
    """The constant phrase for ``status``, or None when the outcome is not one of the
    attention-needing statuses (e.g. completed: the normal response path applies)."""
    return ENGINE_STATUS_PHRASES.get(status) if isinstance(status, str) else None
