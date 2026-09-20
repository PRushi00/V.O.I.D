"""Shared helpers for the persistent-memory tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from void.memory import crypto
from void.memory.service import MemoryService, Settings

CANARY = "CANARY-7f3a91 the owner keeps the launch codebook in the blue drawer"


class FixedKeys:
    """An in-memory key provider (independent of the keyring)."""

    def __init__(self, key: bytes | None = None):
        self.key = key

    def get(self):
        return self.key

    def create(self):
        self.key = crypto.new_key()
        return self.key


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float = 0.0, seconds: float = 0.0) -> None:
        self.t += days * 86400.0 + seconds


def make_service(tmp_path, *, keys=None, clock=None, name="memory.sqlite", **settings) -> MemoryService:
    return MemoryService(Path(tmp_path) / name, key_provider=keys or FixedKeys(), now=clock or Clock(),
                         settings=Settings(**settings) if settings else None)


def db_bytes(path) -> bytes:
    return Path(path).read_bytes()


def sql(path, statement, params=()):
    """Direct (attacker-style) access to the database file."""
    con = sqlite3.connect(str(path))
    try:
        cur = con.execute(statement, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()
