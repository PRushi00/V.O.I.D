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


# --------------------------------------------------------------------------------------------
# Providers/fixtures that make the INFORMATION PATH observable (memory vs. filesystem).
# --------------------------------------------------------------------------------------------
FS_CANARY = "FS-CANARY-STRUCTURE-NOTES.md"


def seed_project(work_dir) -> Path:
    """A project folder that makes filesystem discovery tempting (and identifiable in any answer)."""
    proj = Path(work_dir) / "V.O.I.D"
    (proj / "void").mkdir(parents=True, exist_ok=True)
    (proj / "README.md").write_text("# V.O.I.D\nV1 baseline, V2 in progress\n", encoding="utf-8")
    (proj / FS_CANARY).write_text("structure notes\n", encoding="utf-8")
    return proj


class EagerToolProvider:
    """A stand-in for the model behaviour observed live: given tools it goes searching the machine
    (and answers from what it finds); the memory is only usable when NO tools are offered.

    Every call is recorded (tools offered, whether a memory block was present), so a test can assert
    which source the answer came from instead of grepping the final text."""
    name = "fake"

    def __init__(self, root):
        self.root = str(root)
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def generate(self, messages, tools=None):
        from void.providers.base import LLMResponse, ToolCall
        names = {t.name for t in (tools or [])}
        memory_msgs = [m["content"] for m in messages if "RETRIEVED MEMORY" in (m.get("content") or "")]
        self.calls.append({"tools_offered": len(names), "has_memory_block": bool(memory_msgs),
                           "has_engine_note": any("engine note" in (m.get("content") or "") for m in messages),
                           "roles": [m["role"] for m in messages]})
        if "list_directory" in names:                       # the observed failure mode
            if any(m["role"] == "tool" for m in messages):
                return LLMResponse(text=f"I looked in the folder and found {FS_CANARY}")
            return LLMResponse(tool_calls=[ToolCall(name="list_directory", arguments={"path": self.root})])
        if memory_msgs:                                     # answer strictly from the memory block
            line = next(ln for ln in memory_msgs[0].splitlines() if ln.startswith("- ("))
            return LLMResponse(text="From memory: " + line.split(") ", 1)[1])
        return LLMResponse(text="I don't have that stored.")


class HallucinatingProvider:
    """Ignores the (empty) tool list and calls a destructive tool anyway."""
    name = "fake"

    def __init__(self, path):
        self.path, self.calls = str(path), 0

    def available(self) -> bool:
        return True

    def generate(self, messages, tools=None):
        from void.providers.base import LLMResponse, ToolCall
        self.calls += 1
        return LLMResponse(text=None, tool_calls=[ToolCall(name="delete_file", arguments={"path": self.path})])
