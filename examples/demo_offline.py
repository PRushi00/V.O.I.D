"""Offline end-to-end demo of the V.O.I.D agent loop - no API key required.

This wires the REAL agent, REAL file tools, REAL risk gate, task store, and
kill switch, but drives them with a *scripted* fake brain so you can watch the
whole loop work today with zero setup. It proves the machinery end-to-end;
swap the fake brain for Gemini/local and the same loop runs for real.

Run:  python examples/demo_offline.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import FakeProvider, tool_call  # scripted brain
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import TaskStore
from void.providers.base import LLMResponse
from void.security.risk import RiskGate


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="void_demo_"))
    notes = sandbox / "notes"
    notes.mkdir()
    (notes / "cybersecurity_notes.md").write_text(
        "# Cybersecurity\n- Enable MFA everywhere\n- Patch weekly\n"
    )
    (notes / "shopping.txt").write_text("eggs, coffee")

    print(f"Sandbox: {sandbox}\n")

    # Scripted plan the 'brain' will follow: find -> read -> summarize.
    script = [
        LLMResponse(tool_calls=[tool_call("search_files", query="cyber")]),
        LLMResponse(tool_calls=[tool_call(
            "read_file", path=str(notes / "cybersecurity_notes.md"))]),
        LLMResponse(text="Found your cybersecurity notes. They cover enabling "
                         "MFA everywhere and patching weekly."),
    ]

    files = FileActions(allowed_roots=[sandbox])
    tools = ToolRegistry()
    tools.register_all(files.tools())

    agent = Agent(
        provider=FakeProvider(script),
        tools=tools,
        risk_gate=RiskGate(confirm_at_or_above="high"),
        kill_switch=KillSwitch(),
        store=TaskStore(sandbox / "tasks.sqlite"),
        on_event=lambda m: print("  " + m),
    )

    result = agent.run("find my cybersecurity notes and summarize them")

    print("\n" + "=" * 60)
    print(f"Status: {result.status.upper()}  |  steps: {result.steps}")
    print(result.result)
    print("=" * 60)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
