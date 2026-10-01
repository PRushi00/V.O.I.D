"""OpenAI provider - V.O.I.D's primary online brain (model ``gpt-5.6-sol``).

Translates V.O.I.D's neutral message/tool types to and from an OpenAI-compatible Chat Completions HTTP API (FreeModel's gateway,
https://work.freemodel.dev/v1), over ``requests``
(already a dependency; no SDK is added). The model may only *propose* tool calls: this class returns data and never
executes anything. The Agent / RiskGate / ToolRegistry remain the executor and the authority.

Credentials
-----------
Five credential *slots* for the SAME model, read on demand from the environment (the local injection mechanism), falling
back to the OS keyring under the lower-cased slot name:

    OPENAI_API_KEY (primary), OPENAI_API_KEY_BACKUP_1 .. OPENAI_API_KEY_BACKUP_4

They are held by the existing :class:`void.security.credentials.CredentialPool` (names + cooldowns only; a value is read
when a request is built, used for one HTTP call and dropped). The pool always offers the FIRST slot that is not cooling
down, so healthy traffic uses the primary and never spreads across the backups (no round-robin, no load balancing). A slot
is skipped only after a failure that is plausibly specific to that credential:

    401 authentication            cool 1 h     403 permission (not a region block)   cool 1 h
    429 insufficient_quota        cool 1 h     429 rate limit                        cool Retry-After (default 60 s, max 1 h)

One ``generate()`` makes at most five HTTP attempts (one per slot), each slot at most once. Every other failure is NOT a
reason to spend another credential: timeout, network error and 5xx are transient (one attempt; the Agent's bounded retry
handles them), a 400/422 malformed request or an unsupported model is permanent and fails fast, and a policy / tool /
RiskGate outcome never reaches this layer at all. Cooldowns are in memory only, so nothing needs a restart to recover, and
no request is ever made just to test a key.

Secrets never leave this module: the key is a local variable used to build one Authorization header. Exceptions and logs
carry only the slot label, model, HTTP status and a short error *category* - never the provider's error message (which
can echo a fragment of the key) and never a header, URL query or body.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from void import perf
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

_log = logging.getLogger("void.providers.openai")

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_S = 60.0
# The five credentials are FreeModel (WorkBuddy) keys, so requests go to FreeModel's OpenAI-compatible gateway. The endpoint is
# fixed in code on purpose: a key is never sent to a configuration- or speech-chosen host. ``api_base`` exists for tests only and
# is checked against the allow-list below (https + a known host), so it cannot be turned into a credential-exfiltration path.
FREEMODEL_BASE = "https://work.freemodel.dev/v1"
_API_BASE = FREEMODEL_BASE
_ALLOWED_HOSTS = frozenset({"work.freemodel.dev", "api.openai.com"})
_CONNECT_TIMEOUT_S = 5.0
_MIN_TIMEOUT_S, _MAX_TIMEOUT_S = 1.0, 300.0

# The five slots, primary first. Slot -> the short label used in logs/telemetry.
SLOT_NAMES = ("OPENAI_API_KEY", "OPENAI_API_KEY_BACKUP_1", "OPENAI_API_KEY_BACKUP_2",
              "OPENAI_API_KEY_BACKUP_3", "OPENAI_API_KEY_BACKUP_4")
SLOT_LABELS = dict(zip(SLOT_NAMES, ("primary", "backup_1", "backup_2", "backup_3", "backup_4")))
MAX_ATTEMPTS = len(SLOT_NAMES)
_MANIFEST_KEY = "openai_credential_slots"

_AUTH_COOLDOWN_S = 3600
_QUOTA_COOLDOWN_S = 3600
_RATE_COOLDOWN_DEFAULT_S = 60
_RATE_COOLDOWN_MAX_S = 3600
_MISSING_COOLDOWN_S = 300

# Failure categories. ROTATING ones move to the next slot; every other category is final for this call.
ROTATING = frozenset({"auth", "forbidden", "quota", "rate_limit"})
CATEGORIES = ("auth", "forbidden", "quota", "rate_limit", "timeout", "network", "invalid_request",
              "unsupported_model", "server", "malformed_response", "other")

_SAFE_TOKEN = re.compile(r"^[a-z0-9_.\-]{1,64}$")
_REGION_CODES = frozenset({"unsupported_country_region_territory"})


class ProviderTransientError(RuntimeError):
    """A failure that may succeed on a plain retry (5xx, network, unreadable reply). NOT a reason to change credential.
    Any exception other than ProviderUnavailable is retried by the Agent a bounded number of times; this is one of those.
    The message carries only a category and a status - never provider text."""


def slot_label(name: str, labels: dict | None = None) -> str:
    return (labels or SLOT_LABELS).get(name, "slot")


def _read_slot(name: str) -> str | None:
    """A slot's value: the environment first, then the OS keyring. Never logged, never retained by the caller."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    try:
        stored = secrets.get_secret(name.lower())
    except secrets.SecretStoreError:
        return None
    return stored.strip() if stored and stored.strip() else None


def make_pool(slot_names: tuple = SLOT_NAMES, manifest_key: str = _MANIFEST_KEY) -> CredentialPool:
    """A CredentialPool over fixed slot names (primary first). The manifest is fixed code; values come from ``_read_slot``."""
    def source(key: str):
        return json.dumps(list(slot_names)) if key == manifest_key else _read_slot(key)

    return CredentialPool(primary_name=slot_names[0], manifest_key=manifest_key, get_secret=source)


def normalise_timeout(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if seconds != seconds:                                    # NaN
        return DEFAULT_TIMEOUT_S
    return min(max(seconds, _MIN_TIMEOUT_S), _MAX_TIMEOUT_S)


def _safe(value) -> str:
    """A provider-supplied error type/code, kept only if it is a short lower-case identifier."""
    text = str(value).strip().lower() if value is not None else ""
    return text if _SAFE_TOKEN.match(text) else ""


_BALANCE_PHRASES = ("insufficient balance", "insufficient quota", "insufficient credit", "out of credit", "no balance",
                    "quota exceeded", "quota exhausted", "insufficient user quota", "quota not enough", "balance is not enough")
_BALANCE_CODE = re.compile(r"insufficient.*(quota|balance|credit)|quota_not_enough|quota_exceeded|balance_not_enough")


def _error_fields(resp) -> tuple[str, str]:
    """(type, code) from an error body, sanitised; ('', '') when unreadable.

    OpenAI sends ``{"error": {"type", "code", "message"}}``. FreeModel's gateway sends ``{"error": "Insufficient balance"}``
    (a bare string, HTTP 401). A bare string is never returned or logged: it is only matched against a fixed set of
    balance/quota phrases, which map to the ``insufficient_quota`` code; anything else yields no code."""
    try:
        err = (resp.json() or {}).get("error") or {}
        if isinstance(err, dict):
            typ, code = _safe(err.get("type")), _safe(err.get("code"))
            if _BALANCE_CODE.search(f"{typ} {code}"):
                code = "insufficient_quota"
            return typ, code
        if isinstance(err, str) and any(ph in err.lower() for ph in _BALANCE_PHRASES):
            return "", "insufficient_quota"
    except Exception:                                          # noqa: BLE001
        pass
    return "", ""


def classify_http(status: int, err_type: str = "", err_code: str = "") -> str:
    """Category for an HTTP error status. Pure; uses only the status and sanitised type/code identifiers."""
    both = f"{err_type} {err_code}"
    if status == 402 or "insufficient_quota" in both:            # a spent balance may arrive as 401/402/429 (gateways differ)
        return "quota"
    if status == 401:
        return "auth"
    if status == 403:
        return "other" if err_code in _REGION_CODES else "forbidden"
    if status == 429:
        return "quota" if ("insufficient_quota" in both or "billing" in both) else "rate_limit"
    if status == 404 or err_code in ("model_not_found", "unsupported_model") or "model_not_found" in both:
        return "unsupported_model"
    if status in (400, 413, 415, 422):
        return "invalid_request"
    if status == 408:
        return "timeout"
    if status >= 500:
        return "server"
    return "other"


def classify_exception(exc: BaseException) -> str:
    names = {c.__name__.lower() for c in type(exc).__mro__}
    if any("timeout" in n for n in names):
        return "timeout"
    if any(k in n for n in names for k in ("connectionerror", "connecterror", "chunkedencoding", "sslerror",
                                            "protocolerror", "gaierror")) or isinstance(exc, OSError):
        return "network"
    return "other"


def _retry_after(resp) -> float | None:
    try:
        raw = resp.headers.get("retry-after") if getattr(resp, "headers", None) else None
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


class OpenAIProvider(LLMProvider):
    """A client for an OpenAI-compatible Chat Completions gateway. The class attributes below are its IDENTITY (provider name,
    credential slots, fixed endpoint, host allow-list); a gateway with a different identity is a subclass that changes only
    those (see ``agentrouter_provider.AgentRouterProvider``), so the HTTP, rotation and classification logic exist once."""
    name = "openai"
    _display = "OpenAI"                    # how this gateway is named in messages (never claim to be another vendor)
    _slot_names: tuple = SLOT_NAMES
    _slot_labels: dict = SLOT_LABELS
    _manifest_key: str = _MANIFEST_KEY
    _default_base: str = _API_BASE
    _allowed_hosts: frozenset = _ALLOWED_HOSTS

    def __init__(self, model: str = DEFAULT_MODEL, timeout_s: float = DEFAULT_TIMEOUT_S,
                 credential_pool: CredentialPool | None = None, api_base: str | None = None):
        self.model = model
        self.timeout_s = normalise_timeout(timeout_s)
        self._credentials = credential_pool
        self._api_base = self._checked_base(api_base if api_base is not None else self._default_base)

    @classmethod
    def _checked_base(cls, api_base: str) -> str:
        from urllib.parse import urlsplit
        parts = urlsplit(str(api_base))
        if parts.scheme != "https" or parts.hostname not in cls._allowed_hosts or parts.username or parts.password:
            raise ValueError("The API endpoint must be an https URL on an allow-listed host.")
        return str(api_base).rstrip("/")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"

    __str__ = __repr__

    def _pool(self) -> CredentialPool:
        if self._credentials is None:
            self._credentials = make_pool(self._slot_names, self._manifest_key)
        return self._credentials

    def _label(self, cred_name: str) -> str:
        return slot_label(cred_name, self._slot_labels)

    # --- availability (no network, no quota) ------------------------------------------------------

    def available(self) -> bool:
        """True if some slot that is not cooling down has a stored value. Makes no request."""
        pool = self._pool()
        now = datetime.now(timezone.utc)
        for cred in pool.credentials():
            if not cred.is_available(now):
                continue
            try:
                pool.get_value(cred)
                return True
            except (CredentialMissing, secrets.SecretStoreError):
                continue
        return False

    def credential_status(self) -> list[dict]:
        """Safe metadata per slot: label, configured?, cooling down? Never a value."""
        pool = self._pool()
        now = datetime.now(timezone.utc)
        out = []
        for cred in pool.credentials():
            try:
                pool.get_value(cred)
                configured = True
            except (CredentialMissing, secrets.SecretStoreError):
                configured = False
            out.append({"slot": self._label(cred.name), "configured": configured,
                        "cooling_down": not cred.is_available(now)})
        return out

    # --- translation ----------------------------------------------------------------------------

    @staticmethod
    def _call_id(raw, seq: int) -> str:
        if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", raw):
            return raw
        return f"call_{seq}"

    def _to_messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        pending: list[tuple[str, str]] = []            # (tool name, call id) awaiting their tool message, in order
        seq = 0
        for msg in messages:
            role = msg.get("role")
            if role in ("system", "user"):
                out.append({"role": role, "content": msg.get("content", "") or ""})
                if role == "user":
                    pending = []
            elif role == "assistant":
                entry: dict = {"role": "assistant", "content": msg.get("content") or None}
                calls = msg.get("tool_calls") or []
                pending = []
                if calls:
                    entry["tool_calls"] = []
                    for tc in calls:
                        seq += 1
                        cid = self._call_id(tc.get("id"), seq)
                        pending.append((tc.get("name", ""), cid))
                        entry["tool_calls"].append({
                            "id": cid, "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}) or {})}})
                elif entry["content"] is None:
                    entry["content"] = ""
                out.append(entry)
            elif role == "tool":
                name = msg.get("name", "tool")
                cid = msg.get("tool_call_id") or msg.get("id")
                if not cid and pending:
                    for i, (pname, pid) in enumerate(pending):
                        if pname == name:
                            cid = pid
                            pending.pop(i)
                            break
                    else:
                        cid = pending.pop(0)[1]
                if not cid:
                    seq += 1
                    cid = f"call_{seq}"
                out.append({"role": "tool", "tool_call_id": cid, "content": msg.get("content", "") or ""})
        return out

    @staticmethod
    def _to_tools(tools: list[ToolSpec] | None):
        if not tools:
            return None
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools]

    def _build_body(self, messages: list[dict], tools: list[ToolSpec] | None) -> dict:
        body: dict = {"model": self.model, "messages": self._to_messages(messages)}
        payload = self._to_tools(tools)
        if payload:
            body["tools"] = payload
        return body

    def _parse(self, payload) -> LLMResponse:
        try:
            message = payload["choices"][0]["message"]
        except (TypeError, KeyError, IndexError):
            raise ProviderTransientError(f"{self._display} returned an unreadable response.") from None
        if not isinstance(message, dict):
            raise ProviderTransientError(f"{self._display} returned an unreadable response.")
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    _log.info("LLM_TOOL_ARGS_UNPARSEABLE name=%s", _safe(fn.get("name")) or "?")
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if fn.get("name"):
                calls.append(ToolCall(name=fn["name"], arguments=args, id=(tc.get("id") or None)))
        text = message.get("content")
        if not isinstance(text, str) or not text:
            text = message.get("refusal") if isinstance(message.get("refusal"), str) else None
        return LLMResponse(text=text or None, tool_calls=calls, raw=payload)

    # --- generate -------------------------------------------------------------------------------

    def _telemetry(self, label: str, attempt: int, t0: float, ok: bool, category: str | None) -> None:
        duration = round(time.monotonic() - t0, 3)
        _log.info("%s_CALL slot=%s model=%s attempt=%d ok=%s category=%s duration=%.2fs",
                  self.name.upper(), label, self.model, attempt, ok, category or "-", duration)
        fields = {"provider": self.name, "model": self.model, "slot": label, "attempt": attempt,
                  "duration_s": duration, "ok": ok}
        if category:
            fields["category"] = category
        perf.emit("provider_call", **fields)

    def generate(self, messages: list[dict], tools: list[ToolSpec] | None = None) -> LLMResponse:
        """One turn. At most ``MAX_ATTEMPTS`` HTTP attempts, each credential at most once; only a failure that is
        specific to a credential moves to the next one."""
        body = self._build_body(messages, tools)
        pool = self._pool()
        tried: set[str] = set()
        made_request = False
        attempts = 0
        last_category = "other"
        max_attempts = len(self._slot_names)
        while attempts < max_attempts:
            now = datetime.now(timezone.utc)
            try:
                cred = pool.get_next_available(now=now)
            except CredentialsExhausted:
                break
            if cred.name in tried:                          # defensive: a slot is never used twice in one call
                break
            tried.add(cred.name)
            label = self._label(cred.name)
            try:
                key = pool.get_value(cred)
            except (CredentialMissing, secrets.SecretStoreError):
                pool.mark_unavailable(cred.name, now + timedelta(seconds=_MISSING_COOLDOWN_S))
                continue                                    # not configured: no request, not an attempt
            attempts += 1
            made_request = True
            t0 = time.monotonic()
            category, resp = self._attempt(key, body)
            key = None                                      # noqa: F841 - the value does not outlive the attempt
            if category is None:
                try:
                    parsed = self._parse(resp.json())
                except Exception:                            # noqa: BLE001 - unreadable body: transient, same slot
                    self._telemetry(label, attempts, t0, False, "malformed_response")
                    raise ProviderTransientError(f"{self._display} returned an unreadable response.") from None
                self._telemetry(label, attempts, t0, True, None)
                return parsed
            self._telemetry(label, attempts, t0, False, category)
            last_category = category
            if category not in ROTATING:
                self._raise_final(category)
            pool.mark_unavailable(cred.name, now + timedelta(seconds=self._cooldown_s(category, resp)))
        if not made_request:
            raise ProviderUnavailable(f"No {self._display} credential is configured or available.")
        _log.warning("%s_CREDENTIALS_EXHAUSTED attempts=%d last=%s", self.name.upper(), attempts, last_category)
        raise ProviderUnavailable(
            f"All {self._display} credentials are unavailable (last failure: {last_category}).")

    def _attempt(self, key: str, body: dict):
        """One HTTP request. Returns (None, response) on success, else (category, response-or-None)."""
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            resp = requests.post(f"{self._api_base}/chat/completions", json=body, headers=headers,
                                 timeout=(_CONNECT_TIMEOUT_S, self.timeout_s), allow_redirects=False)
        except Exception as exc:                             # noqa: BLE001 - classified by class name only
            return classify_exception(exc), None
        status = resp.status_code
        if status < 400:
            return None, resp
        err_type, err_code = _error_fields(resp)
        return classify_http(status, err_type, err_code), resp

    @staticmethod
    def _cooldown_s(category: str, resp) -> float:
        if category == "rate_limit":
            wait = _retry_after(resp) if resp is not None else None
            return min(max(wait if wait is not None else _RATE_COOLDOWN_DEFAULT_S, 1.0), _RATE_COOLDOWN_MAX_S)
        return _QUOTA_COOLDOWN_S if category == "quota" else _AUTH_COOLDOWN_S

    def list_models(self) -> list[str]:
        """Model ids the gateway exposes (``GET /models``). One request on the first usable credential, no rotation, no
        redirects; raises ProviderUnavailable with a category on failure. Used only for connectivity/identity checks."""
        pool = self._pool()
        now = datetime.now(timezone.utc)
        try:
            cred = pool.get_next_available(now=now)
            key = pool.get_value(cred)
        except (CredentialsExhausted, CredentialMissing, secrets.SecretStoreError):
            raise ProviderUnavailable(f"No {self._display} credential is configured or available.") from None
        try:
            resp = requests.get(f"{self._api_base}/models", headers={"Authorization": f"Bearer {key}"},
                                timeout=(_CONNECT_TIMEOUT_S, self.timeout_s), allow_redirects=False)
        except Exception as exc:                                     # noqa: BLE001 - class name only
            raise ProviderUnavailable(f"{self._display} model list failed ({classify_exception(exc)}).") from None
        finally:
            key = None                                               # noqa: F841
        if resp.status_code >= 400:
            category = classify_http(resp.status_code, *_error_fields(resp))
            raise ProviderUnavailable(f"{self._display} model list failed ({category}).")
        try:
            data = resp.json()["data"]
            if not isinstance(data, list):
                raise ValueError("data is not a list")
            return [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
        except Exception:                                            # noqa: BLE001
            raise ProviderUnavailable(f"{self._display} returned an unreadable model list.") from None

    def _raise_final(self, category: str):
        """A failure that another credential cannot fix."""
        if category == "timeout":
            raise ProviderUnavailable(f"{self._display} did not answer within {self.timeout_s:g}s.")
        if category == "unsupported_model":
            raise ProviderUnavailable(f"The {self._display} model '{self.model}' is not available to this account.")
        if category == "invalid_request":
            raise ProviderUnavailable(f"{self._display} rejected the request as invalid.")
        raise ProviderTransientError(f"{self._display} request failed ({category}).")
