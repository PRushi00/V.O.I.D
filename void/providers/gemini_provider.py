"""Gemini provider - the default V1 brain for planning and tool-calling.

Translates V.O.I.D's neutral message/tool types to and from the
``google-generativeai`` SDK. The API key is read from the OS secret store,
never from config or the repo.

NOTE: exercising this provider requires the ``google-generativeai`` package,
network access, and a stored Gemini API key. The agent loop is unit-tested
against a fake provider; this class is validated live on the laptop.
"""
from __future__ import annotations

import base64

from void.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    ToolCall,
    ToolSpec,
)
from void.security import secrets


def _coerce_args(args) -> dict:
    """Turn a Gemini proto args map into a plain dict."""
    try:
        return {k: _coerce_value(v) for k, v in args.items()}
    except AttributeError:
        return dict(args) if args else {}


def _coerce_value(v):
    # Proto Struct values may be scalars, lists, or nested maps.
    if hasattr(v, "items"):
        return {k: _coerce_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_coerce_value(x) for x in v]
    return v


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str = "gemini-1.5-flash",
                 temperature: float = 0.2, max_output_tokens: int = 2048):
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._genai = None
        self._api_key: str | None = None

    # --- availability --------------------------------------------------

    def _load(self):
        if self._genai is not None:
            return self._genai
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ProviderUnavailable(
                "google-generativeai is not installed."
            ) from exc
        key = secrets.get_secret(secrets.GEMINI_API_KEY)
        if not key:
            raise ProviderUnavailable(
                "No Gemini API key stored. Set it with: "
                "python -m void set-key gemini"
            )
        genai.configure(api_key=key)
        self._genai = genai
        self._api_key = key
        return genai

    def available(self) -> bool:
        try:
            self._load()
            return True
        except ProviderUnavailable:
            return False

    # --- translation ---------------------------------------------------

    def _to_contents(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        system_parts: list[str] = []
        contents: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                if msg.get("content"):
                    system_parts.append(msg["content"])
            elif role == "user":
                contents.append({"role": "user",
                                 "parts": [{"text": msg.get("content", "")}]})
            elif role == "assistant":
                if msg.get("tool_calls"):
                    parts = []
                    for tc in msg["tool_calls"]:
                        name = tc["name"]
                        args = tc.get("arguments", {}) or {}
                        sig = tc.get("signature")
                        raw = None
                        if sig:
                            try:
                                raw = base64.b64decode(sig)
                            except Exception:
                                raw = None
                        part = None
                        # Preferred (live) path: build a real proto Part so the
                        # thought_signature (Gemini 3.x requirement) is carried
                        # natively - dict parts drop unknown keys.
                        if raw and self._genai is not None:
                            try:
                                part = self._genai.protos.Part(
                                    function_call=self._genai.protos.FunctionCall(
                                        name=name, args=args),
                                    thought_signature=raw,
                                )
                            except Exception:
                                part = None
                        if part is None:
                            # Fallback (also the unit-test path when the SDK is
                            # not loaded): dict form, still carrying the bytes.
                            part = {"function_call": {"name": name, "args": args}}
                            if raw is not None:
                                part["thought_signature"] = raw
                        parts.append(part)
                    contents.append({"role": "model", "parts": parts})
                else:
                    contents.append({"role": "model",
                                     "parts": [{"text": msg.get("content", "")}]})
            elif role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{
                        "function_response": {
                            "name": msg.get("name", "tool"),
                            "response": {"result": msg.get("content", "")},
                        }
                    }],
                })
        system = "\n\n".join(system_parts) if system_parts else None
        return system, contents

    def _to_tools(self, tools: list[ToolSpec] | None):
        if not tools:
            return None
        return [{
            "function_declarations": [
                {"name": t.name, "description": t.description,
                 "parameters": t.parameters}
                for t in tools
            ]
        }]

    def _parse(self, response) -> LLMResponse:
        tool_calls: list[ToolCall] = []
        texts: list[str] = []
        try:
            parts = response.candidates[0].content.parts
        except (AttributeError, IndexError):
            parts = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                # Gemini 3.x returns a thought_signature (bytes) on the part
                # (or the function call). Preserve it as base64 so it survives
                # JSON checkpointing and can be echoed back next turn.
                raw_sig = (getattr(part, "thought_signature", None)
                           or getattr(fc, "thought_signature", None))
                signature = (base64.b64encode(raw_sig).decode("ascii")
                             if raw_sig else None)
                tool_calls.append(ToolCall(name=fc.name,
                                           arguments=_coerce_args(fc.args),
                                           signature=signature))
            elif getattr(part, "text", None):
                texts.append(part.text)
        return LLMResponse(text="\n".join(texts) if texts else None,
                           tool_calls=tool_calls, raw=response)

    # --- generate ------------------------------------------------------

    def generate(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        genai = self._load()
        system, contents = self._to_contents(messages)
        model = genai.GenerativeModel(
            model_name=self.model,
            system_instruction=system,
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            },
        )
        response = model.generate_content(
            contents,
            tools=self._to_tools(tools),
        )
        return self._parse(response)
