"""Gemini provider - the default V1 brain for planning and tool-calling.

Translates V.O.I.D's neutral message/tool types to and from the
``google-genai`` SDK. The API key is read from the OS secret store,
never from config or the repo.

NOTE: exercising this provider requires the ``google-genai`` package,
network access, and a stored Gemini API key. The agent loop is unit-tested
against a fake provider; this class is validated live on the laptop.

Automatic function calling is explicitly disabled: Gemini may only *propose*
tool calls. V.O.I.D's Agent / RiskGate / ToolRegistry remain the executor.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from void.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    ToolCall,
    ToolSpec,
)
from void.security import secrets
from void.security.credentials import (
    CredentialMissing,
    CredentialPool,
    CredentialsExhausted,
)


def _coerce_args(args) -> dict:
    """Turn a Gemini args map into a plain dict."""
    if not args:
        return {}
    try:
        return {k: _coerce_value(v) for k, v in args.items()}
    except AttributeError:
        return dict(args)


def _coerce_value(v):
    if hasattr(v, "items"):
        return {k: _coerce_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_coerce_value(x) for x in v]
    return v


def _encode_signature(raw) -> str | None:
    """Checkpoint-safe encoding of a *real* thought_signature payload.

    Passes through whatever Gemini (or a test double with the same attribute
    shape) provided. Never invents, hashes, or synthesizes a signature.
    """
    if raw is None or raw is False:
        return None
    if isinstance(raw, str):
        if not raw:
            return None
        raw = raw.encode("latin-1")
    elif isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif not isinstance(raw, (bytes, bytearray)):
        return None
    if not raw:
        return None
    return base64.b64encode(bytes(raw)).decode("ascii")


def _decode_signature(sig: str | None) -> bytes | None:
    """Restore original signature bytes from the checkpointed base64 form."""
    if not sig:
        return None
    try:
        raw = base64.b64decode(sig)
    except Exception:
        return None
    return raw or None


# --- error classification & cooldown policy ----------------------------
#
# Cooldowns are in-memory for the life of a run. Because free-tier daily quota
# is the real blocker, we drop an exhausted/invalid credential for a
# conservative default so the current run rotates to other credentials instead
# of re-hammering the same one. An explicit retry delay from the SDK is honored
# only when it is LONGER than the floor (never shorter, to avoid re-hammering).
_QUOTA_COOLDOWN_S = 3600     # 429 RESOURCE_EXHAUSTED
_AUTH_COOLDOWN_S = 3600      # 401/403 invalid credential
_MISSING_COOLDOWN_S = 3600   # credential has no stored value


def _classify_error(exc: Exception) -> str:
    """Classify a Gemini SDK error as 'quota', 'auth', or 'other'.

    Uses the SDK's structured ``code`` (HTTP status) / ``status`` fields first
    (google.genai APIError exposes both), with a cautious message fallback.
    Never reads or emits the API key.
    """
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return "quota"
    if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return "auth"
    text = str(getattr(exc, "message", None) or exc)
    if "RESOURCE_EXHAUSTED" in text or " 429" in text:
        return "quota"
    return "other"


def _retry_delay_seconds(exc: Exception) -> float | None:
    """Best-effort, never-raising extraction of an explicit retry delay (s)."""
    details = getattr(exc, "details", None)
    try:
        if isinstance(details, dict):
            items = (details.get("error", {}).get("details")
                     or details.get("details") or [])
        elif isinstance(details, list):
            items = details
        else:
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            rd = item.get("retryDelay") or item.get("retry_delay")
            if isinstance(rd, str) and rd.endswith("s"):
                return float(rd[:-1])
            if isinstance(rd, (int, float)):
                return float(rd)
            if isinstance(rd, dict) and "seconds" in rd:
                return float(rd["seconds"])
    except Exception:
        return None
    return None


def _cooldown_until(now: datetime, base_seconds: int,
                    exc: Exception | None = None) -> datetime:
    seconds = float(base_seconds)
    if exc is not None:
        rd = _retry_delay_seconds(exc)
        if rd is not None and rd > seconds:
            seconds = rd
    return now + timedelta(seconds=seconds)


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str = "gemini-1.5-flash",
                 temperature: float = 0.2, max_output_tokens: int = 2048,
                 credential_pool: CredentialPool | None = None):
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._genai = None
        self._types = None
        self._credentials = credential_pool
        # No client/key is bound for the life of the provider: clients are
        # short-lived and rebuilt per credential during rotation. These stay
        # None (kept for backward compatibility / introspection).
        self._client = None
        self._api_key: str | None = None

    # --- availability --------------------------------------------------

    def _sdk(self):
        """Import google-genai. Does not require an API key (translation only)."""
        if self._genai is not None and self._types is not None:
            return self._genai, self._types
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderUnavailable(
                "google-genai is not installed."
            ) from exc
        self._genai = genai
        self._types = types
        return genai, types

    def _pool(self) -> CredentialPool:
        if self._credentials is None:
            self._credentials = CredentialPool()
        return self._credentials

    def _client_for(self, credential):
        """Build a short-lived client for one credential.

        The secret value is read on demand and handed straight to the SDK; it
        is never stored on the provider or logged. Raises CredentialMissing if
        the credential has no stored value.
        """
        genai, _types = self._sdk()
        key = self._pool().get_value(credential)  # raises CredentialMissing
        return genai.Client(api_key=key)

    def available(self) -> bool:
        """True if the SDK is importable and a usable credential exists.

        Does not construct a client or make any network call.
        """
        try:
            self._sdk()
        except ProviderUnavailable:
            return False
        pool = self._pool()
        try:
            cred = pool.get_next_available()
            pool.get_value(cred)  # confirm a value exists (no client/network)
            return True
        except (CredentialsExhausted, CredentialMissing, secrets.SecretStoreError):
            return False

    # --- translation ---------------------------------------------------

    def _function_call_part(self, tc: dict):
        """Rebuild one model Part from a neutral tool_call dict.

        thought_signature is placed on this same Part (never merged into a
        neighbour, never dropped when present). IDs are preserved when set.
        """
        _genai, types = self._sdk()
        name = tc["name"]
        args = tc.get("arguments", {}) or {}
        fc_kwargs: dict = {"name": name, "args": args}
        call_id = tc.get("id")
        if call_id:
            fc_kwargs["id"] = call_id
        part_kwargs: dict = {
            "function_call": types.FunctionCall(**fc_kwargs),
        }
        raw = _decode_signature(tc.get("signature"))
        if raw is not None:
            part_kwargs["thought_signature"] = raw
        return types.Part(**part_kwargs)

    def _to_contents(self, messages: list[dict]):
        """Neutral messages -> (system_instruction, list[types.Content])."""
        _genai, types = self._sdk()
        system_parts: list[str] = []
        contents: list = []
        # IDs from the most recent model function_call Parts, consumed in order
        # so function_response Parts can echo them without Agent changes.
        pending_calls: list[tuple[str, str | None]] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                if msg.get("content"):
                    system_parts.append(msg["content"])
            elif role == "user":
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=msg.get("content", "") or "")],
                ))
                pending_calls = []
            elif role == "assistant":
                parts = []
                text = msg.get("content")
                tool_calls = msg.get("tool_calls") or []
                # Keep text and function_call as separate Parts so a signature
                # on a function-call Part is not merged into the text Part.
                if text:
                    parts.append(types.Part(text=text))
                pending_calls = []
                for tc in tool_calls:
                    parts.append(self._function_call_part(tc))
                    pending_calls.append((tc.get("name", ""), tc.get("id")))
                if not parts:
                    parts.append(types.Part(text=text or ""))
                contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                name = msg.get("name", "tool")
                call_id = msg.get("tool_call_id") or msg.get("id")
                if not call_id and pending_calls:
                    for i, (pending_name, pending_id) in enumerate(pending_calls):
                        if pending_name == name:
                            call_id = pending_id
                            pending_calls.pop(i)
                            break
                    else:
                        call_id = pending_calls.pop(0)[1]
                fr_kwargs: dict = {
                    "name": name,
                    "response": {"result": msg.get("content", "")},
                }
                if call_id:
                    fr_kwargs["id"] = call_id
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(
                        function_response=types.FunctionResponse(**fr_kwargs),
                    )],
                ))
        system = "\n\n".join(system_parts) if system_parts else None
        return system, contents

    def _to_tools(self, tools: list[ToolSpec] | None):
        if not tools:
            return None
        _genai, types = self._sdk()
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters,
            )
            for t in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    def _generate_config(self, system: str | None, tools: list[ToolSpec] | None):
        """Build GenerateContentConfig with automatic function calling OFF."""
        _genai, types = self._sdk()
        kwargs: dict = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        }
        if system:
            kwargs["system_instruction"] = system
        tool_payload = self._to_tools(tools)
        if tool_payload:
            kwargs["tools"] = tool_payload
        return types.GenerateContentConfig(**kwargs)

    def _parse(self, response) -> LLMResponse:
        tool_calls: list[ToolCall] = []
        texts: list[str] = []
        try:
            parts = response.candidates[0].content.parts
        except (AttributeError, IndexError):
            parts = []
        if parts is None:
            parts = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                raw_sig = (getattr(part, "thought_signature", None)
                           or getattr(fc, "thought_signature", None))
                call_id = getattr(fc, "id", None) or None
                if call_id == "":
                    call_id = None
                tool_calls.append(ToolCall(
                    name=fc.name,
                    arguments=_coerce_args(getattr(fc, "args", None)),
                    id=call_id,
                    signature=_encode_signature(raw_sig),
                ))
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                texts.append(part.text)
        return LLMResponse(text="\n".join(texts) if texts else None,
                           tool_calls=tool_calls, raw=response)

    # --- generate ------------------------------------------------------

    def generate(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        """Generate one turn, rotating credentials on quota/auth failures.

        Rotation happens INSIDE a single call so the agent sees one provider
        operation. On a 429 the current credential is cooled and the next
        available one is tried; when all are exhausted, ProviderUnavailable is
        raised (the agent treats that as fail-fast, never re-hammering a key).
        Other (transient/permanent) errors are re-raised unchanged so existing
        retry behavior is preserved.
        """
        system, contents = self._to_contents(messages)
        config = self._generate_config(system, tools)
        pool = self._pool()
        last_exc: Exception | None = None

        # Bounded by the credential count so the loop always terminates: each
        # iteration either succeeds, re-raises, or cools one credential.
        for _ in range(len(pool) + 1):
            now = datetime.now(timezone.utc)
            try:
                cred = pool.get_next_available(now=now)
            except CredentialsExhausted as exc:
                raise ProviderUnavailable(
                    "All Gemini credentials are exhausted (quota/cooldown). "
                    "Add or wait for a credential."
                ) from (last_exc or exc)

            try:
                client = self._client_for(cred)
            except CredentialMissing as exc:
                # No value for this reference: cool it and rotate.
                pool.mark_unavailable(
                    cred.name, _cooldown_until(now, _MISSING_COOLDOWN_S))
                last_exc = exc
                continue

            try:
                response = client.models.generate_content(
                    model=self.model, contents=contents, config=config)
                return self._parse(response)
            except Exception as exc:
                kind = _classify_error(exc)
                if kind == "quota":
                    pool.mark_unavailable(
                        cred.name, _cooldown_until(now, _QUOTA_COOLDOWN_S, exc))
                    last_exc = exc
                    continue
                if kind == "auth":
                    # Invalid credential: cool it and try others - never retry
                    # the same invalid credential.
                    pool.mark_unavailable(
                        cred.name, _cooldown_until(now, _AUTH_COOLDOWN_S))
                    last_exc = exc
                    continue
                # Transient/permanent non-quota error: preserve existing
                # behavior (the agent's retry loop handles these).
                raise

        raise ProviderUnavailable(
            "All Gemini credentials are exhausted (quota/cooldown)."
        ) from last_exc
