"""Structured local state: preferences, system snapshots, detected changes, maintenance runs.

Deliberately separate from :mod:`void.memory`. Memory is encrypted, reviewed and owner-accepted;
this is plain, structured, diffable knowledge about the computer. See :mod:`void.state.store` for
why that line exists and what it forbids.
"""
from __future__ import annotations

from void.state.store import (
    MAX_PAYLOAD, MAX_TEXT, SCHEMA_VERSION, Change, MaintenanceRun, Observation, Preference,
    StateError, StateStore, clean, is_sunday, looks_secret, week_key,
)

__all__ = [
    "Change", "MaintenanceRun", "Observation", "Preference", "StateError", "StateStore",
    "SCHEMA_VERSION", "MAX_TEXT", "MAX_PAYLOAD", "clean", "is_sunday", "looks_secret", "week_key",
]
