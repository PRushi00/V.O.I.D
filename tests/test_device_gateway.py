"""Device Gateway tests.

One real end-to-end test opens an actual TLS socket on 127.0.0.1 and speaks
real HTTP/1.1 over it (the closest thing to "real Android/Windows
validation" available without physical hardware - see the final report for
what real-hotspot/Android validation this could NOT cover and the manual
procedure left for it). The rest of the edge cases (replay, rate limiting,
staleness, malformed/oversized bodies, unknown devices) call the gateway's
own request-handling methods directly - same registry/auth/capability code
path, without paying for a TLS handshake per case.
"""
import http.client
import json
import ssl
import time

import pytest

from void.actions.base import Tool, ToolResult
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device import auth, cert
from void.device.gateway import PAIR_PATH, REQUEST_PATH, DeviceGateway, running_port
from void.device.protocol import MAX_BODY_BYTES
from void.security.risk import RiskGate, RiskLevel


def _config():
    return Config({"app": {"name": "V.O.I.D", "version": "1.0"},
                  "voice": {"enabled": False}})


@pytest.fixture
def launched():
    return []


@pytest.fixture
def tools(launched):
    registry = ToolRegistry()
    registry.register(Tool(
        name="launch_app", description="d", parameters={"type": "object"},
        handler=lambda name: launched.append(name) or ToolResult.success(f"Launched {name}."),
        risk=RiskLevel.LOW))
    return registry


@pytest.fixture
def gw(tmp_path, tools):
    gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                           host="127.0.0.1", port=0)
    gateway.start()
    gateway.serve_in_background()
    yield gateway
    gateway.stop()


def _tls_connection(gw) -> http.client.HTTPSConnection:
    """Connect and PIN the server's certificate fingerprint - exactly what a
    real client must do (see void.device.cert): no CA, no hostname check,
    trust comes only from matching the fingerprint shown at pairing time."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", gw.port, context=context, timeout=5)
    conn.connect()
    der = conn.sock.getpeercert(binary_form=True)
    assert cert.fingerprint_from_der(der) == gw.fingerprint, (
        "fingerprint pinning check failed - would refuse to proceed for real")
    return conn


def _post(conn, path, raw: bytes, extra_headers=None):
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers or {})
    conn.request("POST", path, body=raw, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    return resp.status, payload


# --- real socket, real TLS, end-to-end -----------------------------------

def test_pair_then_authenticated_get_status_over_real_tls(gw):
    token = gw.pairing.begin("Test Phone")
    conn = _tls_connection(gw)

    pair_body = json.dumps({"protocol": 1, "token": token.token,
                            "name": "Test Phone"}).encode()
    status, payload = _post(conn, PAIR_PATH, pair_body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["result"]["capabilities"] == ["get_status"]
    device_id = payload["result"]["device_id"]
    secret = payload["result"]["shared_secret"]

    request = {"protocol": 1, "request_id": "r1", "device_id": device_id,
              "operation": "get_status", "parameters": {}, "timestamp": time.time()}
    raw = json.dumps(request).encode()
    sig = auth.sign(secret, raw)

    conn2 = _tls_connection(gw)
    status, payload = _post(conn2, REQUEST_PATH, raw, {auth.SIGNATURE_HEADER: sig})
    assert status == 200
    assert payload["ok"] is True
    assert payload["result"]["name"] == "V.O.I.D"


def test_launch_app_requires_explicit_grant_then_works(gw, launched):
    token = gw.pairing.begin("Test Phone")
    conn = _tls_connection(gw)
    _, pair_payload = _post(conn, PAIR_PATH, json.dumps(
        {"protocol": 1, "token": token.token, "name": "Test Phone"}).encode())
    device_id = pair_payload["result"]["device_id"]
    secret = pair_payload["result"]["shared_secret"]

    def signed_request(op, request_id):
        body = {"protocol": 1, "request_id": request_id, "device_id": device_id,
                "operation": op, "parameters": {"name": "notepad"},
                "timestamp": time.time()}
        raw = json.dumps(body).encode()
        return raw, auth.sign(secret, raw)

    raw, sig = signed_request("launch_app", "r-denied")
    conn2 = _tls_connection(gw)
    status, payload = _post(conn2, REQUEST_PATH, raw, {auth.SIGNATURE_HEADER: sig})
    assert status == 200
    assert payload["ok"] is False
    assert payload["error"]["code"] == "capability_not_authorized"
    assert launched == []

    gw.registry.grant(device_id, "launch_app")
    raw, sig = signed_request("launch_app", "r-granted")
    conn3 = _tls_connection(gw)
    status, payload = _post(conn3, REQUEST_PATH, raw, {auth.SIGNATURE_HEADER: sig})
    assert payload["ok"] is True
    assert launched == ["notepad"]


def test_wrong_pairing_token_rejected_over_real_tls(gw):
    gw.pairing.begin("Test Phone")
    conn = _tls_connection(gw)
    status, payload = _post(conn, PAIR_PATH, json.dumps(
        {"protocol": 1, "token": "wrong-token", "name": "Test Phone"}).encode())
    assert status == 401
    assert payload["ok"] is False


# --- cross-process pairing at the FULL gateway stack (not just the bare
# PairingManager class - see tests/test_device_pairing.py for that level).
# Root-causing the real-world "invalid_pairing_token / No pairing" report
# required proving the exact lifecycle a real deployment goes through: a
# separate `pair-start` process's PairingManager instance, pointed at the
# same state_dir, writing a token that an ALREADY-RUNNING (or since-
# restarted) gateway process's OWN, independently-constructed PairingManager
# instance then reads back and redeems - never the gateway reusing the same
# in-memory PairingManager object that created the token, which non-real-
# world unit tests could accidentally rely on without ever noticing. ----

def test_fresh_pair_start_token_accepted_by_an_already_running_gateway(gw):
    """`pair-start` is modeled here exactly as it is for real: a genuinely
    separate PairingManager instance over the SAME directory as the
    already-started, already-serving gateway's own - not gw.pairing itself."""
    from void.device.pairing import PairingManager

    pair_start_process = PairingManager(gw.state_dir,
                                        window_seconds=300)
    token = pair_start_process.begin("Real Phone")

    conn = _tls_connection(gw)
    status, payload = _post(conn, PAIR_PATH, json.dumps(
        {"protocol": 1, "token": token.token, "name": "Real Phone"}).encode())
    assert status == 200
    assert payload["ok"] is True
    assert "device_id" in payload["result"]


def test_pair_start_token_created_before_the_gateway_even_started(tmp_path, tools):
    """The reverse ordering: the pairing window is opened, THEN the gateway
    process is constructed and started - proving order between the two
    processes never matters, since the token lives in a file, not memory."""
    from void.device.pairing import PairingManager

    pair_start_process = PairingManager(tmp_path, window_seconds=300)
    token = pair_start_process.begin("Real Phone")

    gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                           host="127.0.0.1", port=0)
    gateway.start()
    gateway.serve_in_background()
    try:
        conn = _tls_connection(gateway)
        status, payload = _post(conn, PAIR_PATH, json.dumps(
            {"protocol": 1, "token": token.token, "name": "Real Phone"}).encode())
        assert status == 200
        assert payload["ok"] is True
    finally:
        gateway.stop()


def test_pairing_survives_a_gateway_restart(tmp_path, tools):
    """A gateway process restart (crash/manual restart/upgrade) must not
    lose an in-progress pairing window - it is file-backed specifically so
    restarting the SERVER never requires restarting the pairing flow too."""
    from void.device.pairing import PairingManager

    first_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                 host="127.0.0.1", port=0)
    first_gateway.start()
    first_gateway.serve_in_background()
    pairing_process = PairingManager(tmp_path, window_seconds=300)
    token = pairing_process.begin("Real Phone")
    first_gateway.stop()   # simulates a restart

    second_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                  host="127.0.0.1", port=0)
    second_gateway.start()
    second_gateway.serve_in_background()
    try:
        conn = _tls_connection(second_gateway)
        status, payload = _post(conn, PAIR_PATH, json.dumps(
            {"protocol": 1, "token": token.token, "name": "Real Phone"}).encode())
        assert status == 200
        assert payload["ok"] is True
    finally:
        second_gateway.stop()


def test_pair_rejection_reason_is_logged_distinctly_never_the_token(gw, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="void.device.gateway"):
        status, payload = gw.handle_pair(
            json.dumps({"protocol": 1, "token": "made-up-guess",
                       "name": "Phone"}).encode(),
            "127.0.0.1")
    assert status == 401
    assert payload["ok"] is False
    reject_lines = [r.message for r in caplog.records if "DEVICE_PAIR_REJECTED" in r.message]
    assert reject_lines
    # No window was ever opened on this gateway, so the reason is no_window -
    # never collapsed into a generic, undifferentiated rejection.
    assert "reason=no_window" in reject_lines[0]
    assert "made-up-guess" not in " ".join(reject_lines)   # never logs the token


def test_pair_rejection_reason_distinguishes_wrong_token_from_no_window(gw, caplog):
    import logging

    gw.pairing.begin("Phone")
    with caplog.at_level(logging.WARNING, logger="void.device.gateway"):
        status, payload = gw.handle_pair(
            json.dumps({"protocol": 1, "token": "made-up-guess",
                       "name": "Phone"}).encode(),
            "127.0.0.1")
    assert status == 401
    reject_lines = [r.message for r in caplog.records if "DEVICE_PAIR_REJECTED" in r.message]
    assert "reason=wrong_token" in reject_lines[0]


# --- persistent device trust: the pairing token is a bootstrap, not a
# permanent credential (see the redesigned pairing lifecycle) - a device
# that has ALREADY paired must keep working across gateway restarts, IP
# changes, and reconnects using ONLY its device_id + shared secret, with the
# original pairing token playing no further role at all. --------------------

def test_paired_device_survives_a_full_gateway_restart(tmp_path, tools):
    """Not just the pairing WINDOW (see test_pairing_survives_a_gateway_restart
    above) - an ALREADY-COMPLETED pairing's resulting device_id + shared
    secret must go on authenticating fine after the gateway process that
    issued them is gone and a brand new one has taken its place, because
    trust lives in the registry/keyring, never in the gateway object."""
    first_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                 host="127.0.0.1", port=0)
    first_gateway.start()
    first_gateway.serve_in_background()
    device_id, secret = _pair(first_gateway, name="Real Phone")
    first_gateway.stop()   # simulates a full restart - a NEW gateway object

    second_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                  host="127.0.0.1", port=0)
    second_gateway.start()
    second_gateway.serve_in_background()
    try:
        status, payload = _request(second_gateway, device_id, secret)
        assert status == 200
        assert payload["ok"] is True
        assert payload["result"]["name"] == "V.O.I.D"
    finally:
        second_gateway.stop()


def test_paired_device_survives_the_gateways_own_address_changing(tmp_path, tools):
    """Device identity must never be tied to WHICH network address the
    gateway happens to be reachable at - exactly the phone-hotspot-IP-change
    scenario this redesign exists for. Pairs against a gateway bound to one
    host, then authenticates against a second gateway instance (same state,
    a DIFFERENT bound address) with no re-pairing."""
    first_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                 host="127.0.0.1", port=0)
    first_gateway.start()
    first_gateway.serve_in_background()
    device_id, secret = _pair(first_gateway, name="Real Phone")
    first_gateway.stop()

    # A different bind address - standing in for "the laptop's hotspot IP
    # changed" (127.0.0.2 is a real, distinct loopback address on most OSes).
    second_gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                                  host="127.0.0.2", port=0)
    second_gateway.start()
    second_gateway.serve_in_background()
    try:
        status, payload = _request(second_gateway, device_id, secret)
        assert status == 200
        assert payload["ok"] is True
    finally:
        second_gateway.stop()


def test_reconnect_after_a_gap_needs_no_pairing_state_at_all(gw):
    """No session/connection state exists to expire between requests - two
    ordinary authenticated requests, separated in (simulated) time, both
    succeed using nothing but the persisted device_id + secret, modeling a
    phone that disconnected from the hotspot and reconnected later."""
    device_id, secret = _pair(gw, name="Real Phone")
    status1, payload1 = _request(gw, device_id, secret, request_id="r-before-gap")
    assert payload1["ok"] is True
    status2, payload2 = _request(
        gw, device_id, secret, request_id="r-after-gap",
        timestamp=time.time() + 5)   # "later", well within the skew window
    assert payload2["ok"] is True


def test_forgotten_device_is_rejected_and_its_old_secret_is_dead(gw):
    """The laptop-side counterpart to the Android app's "Forget Pairing":
    void.device.identity.DeviceRegistry.forget deletes both the registry
    entry and the keyring secret, so a revoked device cannot reconnect with
    its old credential even if it is replayed byte-for-byte."""
    device_id, secret = _pair(gw, name="Real Phone")
    status, payload = _request(gw, device_id, secret, request_id="before-forget")
    assert payload["ok"] is True

    assert gw.registry.forget(device_id) is True

    status, payload = _request(gw, device_id, secret, request_id="after-forget")
    assert status == 401
    assert payload["error"]["code"] == "unknown_device"


# --- a capability grant/revoke/forget made through a SEPARATE
# DeviceRegistry instance (exactly what `device grant`/`device revoke`/
# `device forget` do as their own CLI process) must be honored by an
# ALREADY-RUNNING gateway's OWN registry instance immediately - not only
# after that gateway restarts. Real bug found during physical-validation
# rehearsal: DeviceRegistry used to load its device list once at
# construction and cache it, so a grant made by a separate process was
# invisible to a long-lived gateway until it happened to restart - for a
# revocation specifically, that is a real security gap, not just staleness.

def test_capability_granted_by_a_separate_process_works_without_a_gateway_restart(gw):
    from void.device.identity import DeviceRegistry

    device_id, secret = _pair(gw, name="Real Phone")
    status, payload = _request(gw, device_id, secret, operation="launch_app",
                               parameters={"name": "notepad"}, request_id="before-grant")
    assert payload["error"]["code"] == "capability_not_authorized"

    # A genuinely separate DeviceRegistry object, over the same directory -
    # modeling `device grant` as its own CLI process, NOT gw.registry itself.
    cli_process_registry = DeviceRegistry(gw.state_dir / "devices.json")
    cli_process_registry.grant(device_id, "launch_app")

    status, payload = _request(gw, device_id, secret, operation="launch_app",
                               parameters={"name": "notepad"}, request_id="after-grant")
    assert status == 200
    assert payload["ok"] is True


def test_capability_revoked_by_a_separate_process_takes_effect_immediately(gw):
    from void.device.identity import DeviceRegistry

    device_id, secret = _pair(gw, name="Real Phone")
    gw.registry.grant(device_id, "launch_app")
    status, payload = _request(gw, device_id, secret, operation="launch_app",
                               parameters={"name": "notepad"}, request_id="before-revoke")
    assert payload["ok"] is True

    cli_process_registry = DeviceRegistry(gw.state_dir / "devices.json")
    cli_process_registry.revoke_capability(device_id, "launch_app")

    status, payload = _request(gw, device_id, secret, operation="launch_app",
                               parameters={"name": "notepad"}, request_id="after-revoke")
    assert payload["error"]["code"] == "capability_not_authorized"


def test_forget_by_a_separate_process_is_honored_by_an_already_running_gateway(gw):
    """The precise real-world scenario: the owner runs `device forget` in one
    terminal while `device serve` keeps running in another - the revoked
    device must be rejected on its VERY NEXT request, not after a restart."""
    from void.device.identity import DeviceRegistry

    device_id, secret = _pair(gw, name="Real Phone")
    status, payload = _request(gw, device_id, secret, request_id="before-forget")
    assert payload["ok"] is True

    cli_process_registry = DeviceRegistry(gw.state_dir / "devices.json")
    assert cli_process_registry.forget(device_id) is True

    status, payload = _request(gw, device_id, secret, request_id="after-forget")
    assert status == 401
    assert payload["error"]["code"] == "unknown_device"


def test_pairing_token_itself_is_never_a_usable_device_credential(gw):
    """The pairing token establishes trust ONCE; it must never double as a
    device_id or a shared secret for the ordinary authenticated endpoint -
    that would make it a de facto permanent credential, which this redesign
    explicitly forbids. Using the raw token string in place of device_id
    (with a made-up signature) must be rejected exactly like any other
    unknown device - the token has no standing at /void/v1/request at all."""
    token = gw.pairing.begin("Real Phone")
    body = {"protocol": 1, "request_id": "r1", "device_id": token.token,
            "operation": "get_status", "parameters": {}, "timestamp": time.time()}
    raw = json.dumps(body).encode()
    status, payload = gw.handle_request(raw, auth.sign("guessed-secret", raw), "127.0.0.1")
    assert status == 401
    assert payload["error"]["code"] == "unknown_device"
    # And the pairing window itself must still be intact - a failed attempt
    # to (ab)use it as a device credential does not consume it either.
    assert gw.handle_pair(
        json.dumps({"protocol": 1, "token": token.token, "name": "Real Phone"}).encode(),
        "127.0.0.1")[1]["ok"] is True


def test_successful_pairing_and_request_never_log_the_secret_or_token(gw, caplog):
    import logging

    token = gw.pairing.begin("Real Phone")
    with caplog.at_level(logging.INFO, logger="void.device.gateway"):
        _, pair_payload = gw.handle_pair(
            json.dumps({"protocol": 1, "token": token.token, "name": "Real Phone"}).encode(),
            "127.0.0.1")
        device_id = pair_payload["result"]["device_id"]
        secret = pair_payload["result"]["shared_secret"]
        gw.handle_request(*_signed(device_id, secret), "127.0.0.1")
    all_log_text = " ".join(r.message for r in caplog.records)
    assert secret not in all_log_text
    assert token.token not in all_log_text


def _signed(device_id, secret, operation="get_status", request_id="r1"):
    body = {"protocol": 1, "request_id": request_id, "device_id": device_id,
            "operation": operation, "parameters": {}, "timestamp": time.time()}
    raw = json.dumps(body).encode()
    return raw, auth.sign(secret, raw)


# --- direct method calls: fast coverage of every rejection path ----------

def _pair(gw, name="Phone"):
    token = gw.pairing.begin(name)
    status, payload = gw.handle_pair(
        json.dumps({"protocol": 1, "token": token.token, "name": name}).encode(),
        "127.0.0.1")
    assert status == 200 and payload["ok"] is True
    return payload["result"]["device_id"], payload["result"]["shared_secret"]


def _request(gw, device_id, secret, operation="get_status", request_id="r1",
            timestamp=None, parameters=None, bad_signature=False):
    body = {"protocol": 1, "request_id": request_id, "device_id": device_id,
            "operation": operation, "parameters": parameters or {},
            "timestamp": timestamp if timestamp is not None else time.time()}
    raw = json.dumps(body).encode()
    sig = "deadbeef" if bad_signature else auth.sign(secret, raw)
    return gw.handle_request(raw, sig, "127.0.0.1")


def test_bad_signature_rejected(gw):
    device_id, secret = _pair(gw)
    status, payload = _request(gw, device_id, secret, bad_signature=True)
    assert status == 401
    assert payload["error"]["code"] == "bad_signature"


def test_unknown_device_rejected(gw):
    status, payload = _request(gw, "no-such-device", "irrelevant-secret")
    assert status == 401
    assert payload["error"]["code"] == "unknown_device"


def test_stale_timestamp_rejected(gw):
    device_id, secret = _pair(gw)
    status, payload = _request(gw, device_id, secret,
                               timestamp=time.time() - 1000)
    assert status == 401
    assert payload["error"]["code"] == "stale_request"


def test_replayed_request_id_rejected_on_second_use(gw):
    device_id, secret = _pair(gw)
    status1, payload1 = _request(gw, device_id, secret, request_id="dup")
    assert payload1["ok"] is True
    status2, payload2 = _request(gw, device_id, secret, request_id="dup")
    assert status2 == 401
    assert payload2["error"]["code"] == "replayed_request"


def test_malformed_body_rejected(gw):
    status, payload = gw.handle_request(b"not json at all", "sig", "127.0.0.1")
    assert status == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "malformed_message"


def test_oversized_body_rejected(gw):
    huge = json.dumps({"protocol": 1, "request_id": "r1", "device_id": "d1",
                       "operation": "get_status",
                       "parameters": {"pad": "x" * (MAX_BODY_BYTES + 100)},
                       "timestamp": time.time()}).encode()
    status, payload = gw.handle_request(huge, "sig", "127.0.0.1")
    assert status == 400
    assert payload["error"]["code"] == "message_too_large"


def test_request_rate_limit_eventually_denies(gw):
    device_id, secret = _pair(gw)
    statuses = []
    for i in range(35):
        status, _ = _request(gw, device_id, secret, request_id=f"r-{i}")
        statuses.append(status)
    assert 429 in statuses


def test_pairing_rate_limit_eventually_denies(gw):
    statuses = []
    for i in range(15):
        gw.pairing.begin(f"Phone-{i}")
        status, _ = gw.handle_pair(
            json.dumps({"protocol": 1, "token": "wrong", "name": "x"}).encode(),
            "10.0.0.5")
        statuses.append(status)
    assert 429 in statuses


def test_unknown_capability_operation_rejected(gw):
    device_id, secret = _pair(gw)
    status, payload = _request(gw, device_id, secret, operation="execute_shell")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_operation"


# --- cross-process port discovery (pair-start reading a separately-running
# `device serve`'s actual bound port - see void.device.gateway.running_port) --

def test_running_port_none_when_nothing_is_running(tmp_path):
    assert running_port(tmp_path) is None


def test_running_port_reflects_the_actual_bound_port_while_running(gw):
    assert running_port(gw.state_dir) == gw.port


def test_stop_after_start_without_ever_serving_does_not_hang(tmp_path, tools):
    """Regression guard: stop() must never call BaseServer.shutdown() unless
    serve_forever()/serve_in_background() actually ran, or it blocks forever
    waiting for an acknowledgement that will never come (hit for real while
    writing these tests - see void.device.gateway.DeviceGateway.stop)."""
    gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                           host="127.0.0.1", port=0)
    gateway.start()
    gateway.stop()   # must return promptly, not hang


def test_running_port_cleared_after_stop(tmp_path, tools):
    gateway = DeviceGateway(_config(), tools, RiskGate(), KillSwitch(), tmp_path,
                           host="127.0.0.1", port=0)
    gateway.start()
    gateway.serve_in_background()
    assert running_port(tmp_path) == gateway.port
    gateway.stop()
    assert running_port(tmp_path) is None
