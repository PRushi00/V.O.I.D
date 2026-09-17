"""Tests for the versioned device protocol: strict schema validation,
nothing partially accepted, nothing beyond the fixed field set."""
import json

import pytest

from void.device import protocol


def _valid_request(**overrides):
    body = {
        "protocol": protocol.PROTOCOL_VERSION,
        "request_id": "req-1",
        "device_id": "dev-1",
        "operation": "get_status",
        "parameters": {},
        "timestamp": 1234567890.0,
    }
    body.update(overrides)
    return body


def test_parses_a_well_formed_request():
    req = protocol.parse_request(json.dumps(_valid_request()))
    assert req.protocol == 1
    assert req.request_id == "req-1"
    assert req.device_id == "dev-1"
    assert req.operation == "get_status"
    assert req.parameters == {}
    assert req.timestamp == 1234567890.0


def test_parses_bytes_body_too():
    raw = json.dumps(_valid_request()).encode("utf-8")
    req = protocol.parse_request(raw)
    assert req.device_id == "dev-1"


@pytest.mark.parametrize("bad", ["not json", "[]", "42", '"a string"'])
def test_rejects_non_object_or_unparseable_bodies(bad):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(bad)
    assert exc.value.code == protocol.ErrorCode.MALFORMED


def test_rejects_missing_required_field():
    body = _valid_request()
    del body["operation"]
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body))
    assert exc.value.code == protocol.ErrorCode.MALFORMED


def test_rejects_unknown_field():
    body = _valid_request(extra_field="not part of the protocol")
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body))
    assert exc.value.code == protocol.ErrorCode.MALFORMED


def test_rejects_unsupported_protocol_version():
    body = _valid_request(protocol=999)
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body))
    assert exc.value.code == protocol.ErrorCode.UNSUPPORTED_VERSION


@pytest.mark.parametrize("field,value", [
    ("request_id", 5), ("request_id", ""), ("device_id", None),
    ("operation", 3.5), ("timestamp", "not-a-number"), ("timestamp", True),
    ("protocol", "1"), ("protocol", True),
])
def test_rejects_wrong_types(field, value):
    body = _valid_request(**{field: value})
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body))
    assert exc.value.code in (protocol.ErrorCode.MALFORMED,
                              protocol.ErrorCode.UNSUPPORTED_VERSION)


def test_rejects_oversized_body():
    huge = _valid_request(parameters={"pad": "x" * (protocol.MAX_BODY_BYTES + 1)})
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(huge))
    assert exc.value.code == protocol.ErrorCode.TOO_LARGE


def test_rejects_non_object_parameters():
    body = _valid_request(parameters="not an object")
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body))
    assert exc.value.code == protocol.ErrorCode.MALFORMED


def test_missing_parameters_defaults_to_empty_dict():
    body = _valid_request()
    del body["parameters"]
    req = protocol.parse_request(json.dumps(body))
    assert req.parameters == {}


def test_device_response_success_json_shape():
    resp = protocol.DeviceResponse(request_id="req-1", ok=True, result={"a": 1})
    data = json.loads(resp.to_json())
    assert data == {"protocol": 1, "request_id": "req-1", "ok": True, "result": {"a": 1}}


def test_device_response_error_json_shape():
    exc = protocol.ProtocolError(protocol.ErrorCode.DENIED, "nope")
    resp = protocol.error_response("req-1", exc)
    data = json.loads(resp.to_json())
    assert data["ok"] is False
    assert data["error"] == {"code": "action_denied", "message": "nope"}


def test_parse_pair_request_happy_path():
    body = {"protocol": 1, "token": "abc123", "name": "My Phone"}
    req = protocol.parse_pair_request(json.dumps(body))
    assert req.token == "abc123"
    assert req.name == "My Phone"


def test_parse_pair_request_rejects_unknown_field():
    body = {"protocol": 1, "token": "abc", "name": "x", "device_id": "sneaky"}
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_pair_request(json.dumps(body))
    assert exc.value.code == protocol.ErrorCode.MALFORMED
