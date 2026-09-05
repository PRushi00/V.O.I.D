"""Local provider - offline fallback via Ollama on the RTX 5070.

Talks to a local Ollama server (https://ollama.com) over HTTP using its
OpenAI-style /api/chat endpoint, which supports tool calling on recent
models (e.g. llama3.1). Degrades gracefully: if Ollama is not running,
``available()`` returns False and the registry falls through.
"""
from __future__ import annotations

import json

from void.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    ToolCall,
    ToolSpec,
)


class LocalProvider(LLMProvider):
    name = "local"

    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "llama3.1:8b", temperature: float = 0.2,
                 timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def _requests(self):
        try:
            import requests
        except ImportError as exc:
            raise ProviderUnavailable("requests is not installed.") from exc
        return requests

    def available(self) -> bool:
        try:
            requests = self._requests()
            resp = requests.get(f"{self.base_url}/api/tags", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _to_messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            if role in ("system", "user"):
                out.append({"role": role, "content": msg.get("content", "")})
            elif role == "assistant":
                if msg.get("tool_calls"):
                    out.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {"function": {"name": tc["name"],
                                          "arguments": tc.get("arguments", {})}}
                            for tc in msg["tool_calls"]
                        ],
                    })
                else:
                    out.append({"role": "assistant",
                                "content": msg.get("content", "")})
            elif role == "tool":
                out.append({"role": "tool", "content": msg.get("content", ""),
                            "name": msg.get("name", "tool")})
        return out

    def _to_tools(self, tools: list[ToolSpec] | None):
        if not tools:
            return None
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in tools]

    def _parse(self, payload: dict) -> LLMResponse:
        message = payload.get("message", {}) or {}
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
        text = message.get("content") or None
        return LLMResponse(text=text, tool_calls=tool_calls, raw=payload)

    def generate(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        requests = self._requests()
        body = {
            "model": self.model,
            "messages": self._to_messages(messages),
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        tool_payload = self._to_tools(tools)
        if tool_payload:
            body["tools"] = tool_payload
        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=body,
                                 timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:
            raise ProviderUnavailable(f"Local model request failed: {exc}") from exc
        return self._parse(resp.json())
