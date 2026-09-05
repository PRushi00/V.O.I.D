"""Registry that holds all available tools and dispatches calls by name."""
from __future__ import annotations

from void.actions.base import Tool, ToolResult
from void.providers.base import ToolSpec


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        """Provider-neutral specs the LLM sees."""
        return [
            ToolSpec(name=t.name, description=t.description, parameters=t.parameters)
            for t in self._tools.values()
        ]

    def execute(self, name: str, arguments: dict) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(f"Unknown tool: {name}")
        return tool.run(**(arguments or {}))
