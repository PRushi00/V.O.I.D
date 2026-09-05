"""Test helpers: a scripted fake LLM provider."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from void.providers.base import LLMProvider, LLMResponse, ToolCall


class FakeProvider(LLMProvider):
    """Returns a scripted sequence of LLMResponses, one per generate() call."""
    name = "fake"

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self.seen_messages = []

    def available(self) -> bool:
        return True

    def generate(self, messages, tools=None):
        self.seen_messages.append(list(messages))
        if self.calls >= len(self._script):
            resp = LLMResponse(text="done")
        else:
            resp = self._script[self.calls]
        self.calls += 1
        return resp


def tool_call(name, **args):
    return ToolCall(name=name, arguments=args)
