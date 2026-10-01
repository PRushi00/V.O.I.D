"""Local provider - offline fallback via Ollama on the RTX 5070.

Talks to a local Ollama server (https://ollama.com) over HTTP using its
OpenAI-style /api/chat endpoint, which supports tool calling on recent
models (e.g. llama3.1). Degrades gracefully: if Ollama is not running,
``available()`` returns False and the registry falls through.

Request shaping (measured on this machine, qwen3:8b, tool-call prompts):

* ``think``      - a thinking-capable model reasons for seconds before it answers: **~9.5 s p50 with thinking on vs ~2.7 s
                   with it off**. The switch is explicit and configurable (``llm.local.think``: false / true / null to
                   leave the model's own default). If a model does not support the option, Ollama rejects the request;
                   the provider then omits ``think`` for that model and repeats the request ONCE (no retry loop).
* ``num_ctx``    - the context window is set explicitly instead of inheriting the server default. V.O.I.D's system
                   prompt plus tool schemas is ~2.3k tokens; 8192 leaves room without the VRAM cost of a huge window.
* ``keep_alive`` - how long the model stays loaded after a request (cold start was 7.8 s, warm 2.7 s).

Nothing here pulls or changes models: ``available()`` only READS ``/api/tags``.
"""
from __future__ import annotations

import json
import logging

from void.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    ToolCall,
    ToolSpec,
)

_log = logging.getLogger("void.providers.local")

_DEFAULT_NUM_CTX = 8192
_DEFAULT_KEEP_ALIVE = "10m"
_CONNECT_TIMEOUT_S = 3.0


def _as_think(value):
    """Config value -> True / False / None (None = do not send the option)."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "null", "none", "default", "auto"):
        return None
    if text in ("false", "off", "no", "0"):
        return False
    if text in ("true", "on", "yes", "1"):
        return True
    _log.warning("LOCAL_THINK_INVALID value not understood; leaving the model default")
    return None


def _positive_int(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


_LOCALHOST_HOSTS = ("localhost",)


def _prefer_ipv4(base_url: str) -> str:
    """Rewrite a ``localhost`` Ollama URL to 127.0.0.1.

    "localhost" resolves to the IPv6 ``::1`` before IPv4 on a default Windows install, and Ollama listens only on
    IPv4. Each request therefore opens a connection to ``::1``, waits for it to fail, and only then retries on
    IPv4 - measured at **2.0 s of dead time per call** on this machine (readiness probe 2110 ms -> 16 ms,
    generation 4.32 s -> 2.17 s once the name was pinned).

    Only the exact hostname "localhost" is rewritten. A deliberate ``[::1]``, a real hostname or a remote address
    is left exactly as configured, so this cannot redirect anyone's traffic somewhere they did not ask for.
    """
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(base_url)
        if (parts.hostname or "").lower() not in _LOCALHOST_HOSTS:
            return base_url
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        if parts.username:                    # preserve credentials if somebody put them in the URL
            userinfo = parts.username + (f":{parts.password}" if parts.password else "")
            netloc = f"{userinfo}@{netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:                         # noqa: BLE001 - a URL we cannot parse is used unchanged
        return base_url


class LocalProvider(LLMProvider):
    name = "local"

    def __init__(self, base_url: str = "http://127.0.0.1:11434",
                 model: str = "llama3.1:8b", temperature: float = 0.2,
                 timeout: float = 120.0, think=None, num_ctx: int | None = _DEFAULT_NUM_CTX,
                 keep_alive: str | None = _DEFAULT_KEEP_ALIVE):
        self.base_url = _prefer_ipv4(base_url).rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.think = _as_think(think)
        self.num_ctx = _positive_int(num_ctx, _DEFAULT_NUM_CTX) if num_ctx is not None else None
        self.keep_alive = str(keep_alive).strip() if keep_alive not in (None, "") else None
        self._omit_think = False          # set once a model has rejected the option
        self._normalised = base_url.rstrip("/") != self.base_url

    @property
    def host_was_normalised(self) -> bool:
        """True when "localhost" was rewritten to 127.0.0.1 (observable by tests; nothing depends on it)."""
        return self._normalised

    def _requests(self):
        try:
            import requests
        except ImportError as exc:
            raise ProviderUnavailable("requests is not installed.") from exc
        return requests

    @staticmethod
    def _model_present(configured: str, names: list[str]) -> bool:
        want = configured.strip().lower()
        have = {n.strip().lower() for n in names}
        return want in have or (":" not in want and f"{want}:latest" in have)

    def available(self) -> bool:
        """The server answers AND the configured model is installed (read-only; never pulls a model)."""
        try:
            requests = self._requests()
            resp = requests.get(f"{self.base_url}/api/tags", timeout=2.0)
            if resp.status_code != 200:
                return False
            names = [m.get("name", "") for m in (resp.json().get("models") or []) if isinstance(m, dict)]
            return self._model_present(self.model, names)
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

    def _build_body(self, messages: list[dict], tools: list[ToolSpec] | None) -> dict:
        """The exact /api/chat request (a pure function of configuration, so it can be tested without a server)."""
        options: dict = {"temperature": self.temperature}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        body: dict = {
            "model": self.model,
            "messages": self._to_messages(messages),
            "stream": False,
            "options": options,
        }
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        if self.think is not None and not self._omit_think:
            body["think"] = self.think
        tool_payload = self._to_tools(tools)
        if tool_payload:
            body["tools"] = tool_payload
        return body

    def _post(self, body: dict):
        requests = self._requests()
        return requests.post(f"{self.base_url}/api/chat", json=body,
                             timeout=(_CONNECT_TIMEOUT_S, self.timeout))

    def generate(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        body = self._build_body(messages, tools)
        try:
            resp = self._post(body)
            if (resp.status_code == 400 and "think" in body
                    and "think" in (getattr(resp, "text", "") or "").lower()):
                # This model does not support the option: remember, omit it, and repeat exactly once.
                _log.info("LOCAL_THINK_UNSUPPORTED model=%s (omitting the option)", self.model)
                self._omit_think = True
                resp = self._post(self._build_body(messages, tools))
            if resp.status_code == 404:
                raise ProviderUnavailable(
                    f"Local model request failed: model '{self.model}' is not installed in Ollama.")
            resp.raise_for_status()
        except ProviderUnavailable:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name:
                raise ProviderUnavailable(
                    f"Local model request failed: no answer within {self.timeout:g}s.") from exc
            raise ProviderUnavailable(f"Local model request failed: {name}") from exc
        return self._parse(resp.json())
