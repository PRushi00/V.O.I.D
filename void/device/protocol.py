"""Versioned V.O.I.D device protocol - the message shape, not the transport.

A request/response is a small JSON object with a fixed, explicit set of
fields. Nothing here executes anything: :func:`parse_request` only ever
returns a validated :class:`DeviceRequest` or raises :class:`ProtocolError`
with one of the fixed :class:`ErrorCode` values - the gateway maps that
straight to a rejection response. Unknown fields, wrong types, missing
fields, an unsupported protocol version, and oversized payloads are all
rejected here, before any device/auth/capability logic ever sees the input.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

# The only protocol version this build understands. A future version bump is
# additive (new versions get their own parse path); this build must never
# guess at an unknown version's shape.
PROTOCOL_VERSION = 1

# Hard cap on a request/response body, enforced by the gateway BEFORE the
# body is even fully read (see void.device.gateway) and again here as a
# defense-in-depth check on the parsed JSON text.
MAX_BODY_BYTES = 16_384

# request_id doubles as the replay-protection nonce (void.device.auth) - one
# field, not two, since both need exactly the same "unique per request"
# property. Bounded length so it can't itself be used to bloat storage.
MAX_REQUEST_ID_LEN = 128
MAX_DEVICE_ID_LEN = 128
MAX_OPERATION_LEN = 64

_REQUEST_FIELDS = {"protocol", "request_id", "device_id", "operation",
                   "parameters", "timestamp"}
_REQUIRED_REQUEST_FIELDS = {"protocol", "request_id", "device_id",
                            "operation", "timestamp"}


class ErrorCode(str, enum.Enum):
    MALFORMED = "malformed_message"
    TOO_LARGE = "message_too_large"
    UNSUPPORTED_VERSION = "unsupported_protocol_version"
    UNKNOWN_OPERATION = "unknown_operation"
    INVALID_PARAMETERS = "invalid_parameters"
    UNKNOWN_DEVICE = "unknown_device"
    BAD_SIGNATURE = "bad_signature"
    INVALID_TOKEN = "invalid_pairing_token"
    REPLAYED = "replayed_request"
    STALE = "stale_request"
    NOT_AUTHORIZED = "capability_not_authorized"
    RATE_LIMITED = "rate_limited"
    DENIED = "action_denied"
    INTERNAL = "internal_error"


class ProtocolError(Exception):
    """A request was rejected before/instead of being dispatched."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DeviceRequest:
    protocol: int
    request_id: str
    device_id: str
    operation: str
    parameters: dict
    timestamp: float


@dataclass
class DeviceResponse:
    request_id: str
    ok: bool
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    protocol: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        body = {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "ok": self.ok,
        }
        if self.ok:
            body["result"] = self.result
        else:
            body["error"] = {"code": self.error_code, "message": self.error_message}
        return json.dumps(body)


def error_response(request_id: str, exc: ProtocolError) -> DeviceResponse:
    return DeviceResponse(request_id=request_id or "", ok=False,
                          error_code=exc.code.value, error_message=exc.message)


def _require_str(obj: dict, key: str, max_len: int) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ProtocolError(ErrorCode.MALFORMED, f"Invalid or missing '{key}'.")
    return value


MAX_NAME_LEN = 64
MAX_TOKEN_LEN = 64
_PAIR_FIELDS = {"protocol", "token", "name"}
_REQUIRED_PAIR_FIELDS = {"protocol", "token", "name"}


@dataclass
class PairRequest:
    protocol: int
    token: str
    name: str


def parse_pair_request(raw: bytes | str) -> PairRequest:
    """Parse and validate a pairing-endpoint body. Deliberately a separate,
    smaller schema from :func:`parse_request`: there is no device_id or
    signature yet at this point - only the token gates it (see
    void.device.pairing)."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BODY_BYTES:
            raise ProtocolError(ErrorCode.TOO_LARGE, "Request body too large.")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(ErrorCode.MALFORMED, "Body is not valid UTF-8.")
    if len(raw) > MAX_BODY_BYTES:
        raise ProtocolError(ErrorCode.TOO_LARGE, "Request body too large.")

    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        raise ProtocolError(ErrorCode.MALFORMED, "Body is not valid JSON.")
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.MALFORMED, "Body must be a JSON object.")

    unknown = set(obj) - _PAIR_FIELDS
    if unknown:
        raise ProtocolError(ErrorCode.MALFORMED,
                            f"Unknown field(s): {sorted(unknown)}")
    missing = _REQUIRED_PAIR_FIELDS - set(obj)
    if missing:
        raise ProtocolError(ErrorCode.MALFORMED,
                            f"Missing field(s): {sorted(missing)}")

    protocol = obj.get("protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise ProtocolError(ErrorCode.MALFORMED, "'protocol' must be an integer.")
    if protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"Unsupported protocol version {protocol}; this build speaks "
            f"{PROTOCOL_VERSION}.")

    token = _require_str(obj, "token", MAX_TOKEN_LEN)
    name = _require_str(obj, "name", MAX_NAME_LEN)
    return PairRequest(protocol=protocol, token=token, name=name)


def parse_request(raw: bytes | str) -> DeviceRequest:
    """Parse and strictly validate one request body. Raises
    :class:`ProtocolError` for anything that isn't an exact, well-formed,
    supported-version request - never guesses, never partially accepts."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BODY_BYTES:
            raise ProtocolError(ErrorCode.TOO_LARGE, "Request body too large.")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(ErrorCode.MALFORMED, "Body is not valid UTF-8.")
    if len(raw) > MAX_BODY_BYTES:
        raise ProtocolError(ErrorCode.TOO_LARGE, "Request body too large.")

    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        raise ProtocolError(ErrorCode.MALFORMED, "Body is not valid JSON.")
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.MALFORMED, "Body must be a JSON object.")

    unknown = set(obj) - _REQUEST_FIELDS
    if unknown:
        raise ProtocolError(ErrorCode.MALFORMED,
                            f"Unknown field(s): {sorted(unknown)}")
    missing = _REQUIRED_REQUEST_FIELDS - set(obj)
    if missing:
        raise ProtocolError(ErrorCode.MALFORMED,
                            f"Missing field(s): {sorted(missing)}")

    protocol = obj.get("protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise ProtocolError(ErrorCode.MALFORMED, "'protocol' must be an integer.")
    if protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"Unsupported protocol version {protocol}; this build speaks "
            f"{PROTOCOL_VERSION}.")

    request_id = _require_str(obj, "request_id", MAX_REQUEST_ID_LEN)
    device_id = _require_str(obj, "device_id", MAX_DEVICE_ID_LEN)
    operation = _require_str(obj, "operation", MAX_OPERATION_LEN)

    parameters = obj.get("parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ProtocolError(ErrorCode.MALFORMED, "'parameters' must be an object.")

    timestamp = obj.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ProtocolError(ErrorCode.MALFORMED, "'timestamp' must be a number.")

    return DeviceRequest(protocol=protocol, request_id=request_id,
                        device_id=device_id, operation=operation,
                        parameters=parameters, timestamp=float(timestamp))
