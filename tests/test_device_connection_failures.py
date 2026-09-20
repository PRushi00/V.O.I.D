"""Field-report matrix: what a companion client actually observes, layer by
layer, when it "cannot connect".

The Android app reported only "failed to connect to /10.x.x.x:8765" for
situations that are really different: nothing listening, wrong address,
wrong pinned certificate, or a reachable gateway that no longer knows the
device. These tests pin down - over REAL sockets and real TLS - that each of
those is distinguishable at the layer where it happens, and that none of
them can be "fixed" by weakening a security control:

  B  stale address, gateway running   -> TCP refusal  (not TLS, not auth)
  C  correct address, gateway stopped -> TCP refusal  (not TLS, not auth)
  D  correct address, wrong pinned fp -> TLS handshake OK, PIN check fails
  E  reachable, registry lost device  -> 401 unknown_device (TLS+TCP fine);
                                         no fallback; only a fresh pairing
                                         token restores it
  G  "Update Connection" then request -> same identity, new endpoint, works,
                                         and the failed attempt changed nothing

(Case A - correct address + running gateway - and F - IP change - are
covered by tests/test_device_gateway.py.)
"""
import http.client
import json
import socket
import ssl
import time

import pytest

from void.actions.base import Tool, ToolResult
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device import auth, cert
from void.device.gateway import PAIR_PATH, REQUEST_PATH, DeviceGateway
from void.device.identity import secret_key_for
from void.security import secrets as secret_store
from void.security.risk import RiskGate, RiskLevel


_paired_ids: list[str] = []


@pytest.fixture(autouse=True)
def _remove_test_secrets_from_the_real_keyring():
    """Pairing stores each device's shared secret in the real OS keyring (the
    same store production uses); don't leave this file's test devices behind."""
    _paired_ids.clear()
    yield
    for device_id in _paired_ids:
        secret_store.delete_secret(secret_key_for(device_id))
    _paired_ids.clear()


def _make_gateway(state_dir, host="127.0.0.1"):
    tools = ToolRegistry()
    tools.register(Tool(name="launch_app", description="d",
                       parameters={"type": "object"},
                       handler=lambda name: ToolResult.success(f"Launched {name}."),
                       risk=RiskLevel.LOW))
    config = Config({"app": {"name": "V.O.I.D", "version": "1.0"},
                    "voice": {"enabled": False}})
    gateway = DeviceGateway(config, tools, RiskGate(), KillSwitch(), state_dir,
                           host=host, port=0)
    gateway.start()
    gateway.serve_in_background()
    return gateway


@pytest.fixture
def gw(tmp_path):
    gateway = _make_gateway(tmp_path)
    yield gateway
    gateway.stop()


def _connect(host, port):
    """TCP + TLS handshake with NO certificate verification, returning the
    live connection and the fingerprint the server actually presented. This
    is the Python analogue of the Android client's connect-then-pin step."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, context=context, timeout=3)
    conn.connect()
    presented = cert.fingerprint_from_der(conn.sock.getpeercert(binary_form=True))
    return conn, presented


def _pinned_connect(host, port, pinned_fingerprint):
    conn, presented = _connect(host, port)
    if presented != pinned_fingerprint:
        conn.close()
        raise ssl.SSLCertVerificationError("fingerprint mismatch")
    return conn


def _post(conn, path, body: dict, signature=None):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if signature:
        headers[auth.SIGNATURE_HEADER] = signature
    conn.request("POST", path, body=raw, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode())


def _pair_over_tls(gateway, name="Phone"):
    token = gateway.pairing.begin(name)
    conn = _pinned_connect("127.0.0.1", gateway.port, gateway.fingerprint)
    status, payload = _post(conn, PAIR_PATH,
                            {"protocol": 1, "token": token.token, "name": name})
    assert status == 200 and payload["ok"] is True
    _paired_ids.append(payload["result"]["device_id"])
    return payload["result"]["device_id"], payload["result"]["shared_secret"]


def _get_status(host, port, fingerprint, device_id, secret):
    body = {"protocol": 1, "request_id": f"r-{time.time_ns()}", "device_id": device_id,
            "operation": "get_status", "parameters": {}, "timestamp": time.time()}
    raw = json.dumps(body).encode()
    conn = _pinned_connect(host, port, fingerprint)
    conn.request("POST", REQUEST_PATH, body=raw,
                 headers={"Content-Type": "application/json",
                          auth.SIGNATURE_HEADER: auth.sign(secret, raw)})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode())


def _is_pure_tcp_failure(exc: BaseException) -> bool:
    """A connection-level failure that is NOT a TLS/cert problem - the class
    the Android UI must be able to tell apart from a pin mismatch."""
    return isinstance(exc, OSError) and not isinstance(exc, ssl.SSLError)


# --- B: stale address, gateway running ------------------------------------

def test_stale_address_with_gateway_running_is_a_tcp_failure_not_tls_or_auth(gw):
    # The gateway is bound to 127.0.0.1 only; 127.0.0.2 is "the address the
    # phone still has saved, where the laptop no longer is".
    with pytest.raises(OSError) as excinfo:
        _connect("127.0.0.2", gw.port)
    assert _is_pure_tcp_failure(excinfo.value)
    assert isinstance(excinfo.value, (ConnectionRefusedError, socket.timeout, TimeoutError))


# --- C: correct address, gateway stopped -----------------------------------

def test_stopped_gateway_is_a_tcp_failure_not_tls_or_auth(tmp_path):
    gateway = _make_gateway(tmp_path)
    port = gateway.port
    gateway.stop()
    with pytest.raises(OSError) as excinfo:
        _connect("127.0.0.1", port)
    assert _is_pure_tcp_failure(excinfo.value)


# --- D: correct address, wrong TLS fingerprint -----------------------------

def test_wrong_fingerprint_completes_tcp_and_tls_then_fails_the_pin_check(gw):
    wrong = "00:" * 31 + "00"
    assert wrong != gw.fingerprint
    # Transport is healthy: TCP + TLS handshake succeed and a certificate is
    # presented - so this failure is unmistakably a PIN failure, and the
    # client (which must refuse) never gets as far as sending anything.
    conn, presented = _connect("127.0.0.1", gw.port)
    conn.close()
    assert presented == gw.fingerprint
    with pytest.raises(ssl.SSLError):
        _pinned_connect("127.0.0.1", gw.port, wrong)


def test_fingerprint_that_differs_only_in_format_is_the_same_pin(gw):
    """The Android client normalizes colons/case/whitespace before comparing
    (Diagnostics.kt normalizeFingerprint); the canonical form the laptop
    prints must survive that normalization unchanged as 64 hex digits."""
    printed = gw.fingerprint
    hex_only = "".join(c for c in printed if c.isalnum()).lower()
    assert len(hex_only) == 64
    assert printed.replace(":", "").lower() == hex_only


# --- E: reachable gateway, registry no longer knows the device -------------

def test_reachable_gateway_that_lost_the_device_answers_unknown_device(gw, tmp_path):
    device_id, secret = _pair_over_tls(gw)
    status, payload = _get_status("127.0.0.1", gw.port, gw.fingerprint, device_id, secret)
    assert status == 200 and payload["ok"] is True

    # The field state: laptop-side registry file gone, phone still holds the
    # device_id + secret it was issued.
    (tmp_path / "devices.json").unlink()

    status, payload = _get_status("127.0.0.1", gw.port, gw.fingerprint, device_id, secret)
    assert status == 401
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_device"


def test_lost_registry_has_no_fallback_only_a_fresh_token_restores_access(gw, tmp_path):
    old_id, old_secret = _pair_over_tls(gw)
    (tmp_path / "devices.json").unlink()

    # No insecure self-healing: still refused, however many times it retries.
    for _ in range(3):
        status, payload = _get_status("127.0.0.1", gw.port, gw.fingerprint, old_id, old_secret)
        assert (status, payload["error"]["code"]) == (401, "unknown_device")

    # An expired/absent pairing window cannot re-admit it either.
    conn = _pinned_connect("127.0.0.1", gw.port, gw.fingerprint)
    status, payload = _post(conn, PAIR_PATH,
                            {"protocol": 1, "token": "not-a-real-token", "name": "Phone"})
    assert status != 200 and payload["ok"] is False

    # Deliberate re-pairing with a fresh single-use token works, and issues a
    # NEW identity - the old credentials stay dead.
    new_id, new_secret = _pair_over_tls(gw)
    assert new_id != old_id
    assert _get_status("127.0.0.1", gw.port, gw.fingerprint, new_id, new_secret)[0] == 200
    assert _get_status("127.0.0.1", gw.port, gw.fingerprint, old_id, old_secret)[0] == 401


# --- G: Update Connection, then Get Status ---------------------------------

def test_updating_the_endpoint_reuses_the_same_identity_and_failed_attempts_change_nothing(tmp_path):
    first = _make_gateway(tmp_path, host="127.0.0.1")
    fingerprint = first.fingerprint
    device_id, secret = _pair_over_tls(first)
    devices_before = (tmp_path / "devices.json").read_text()
    first.stop()

    second = _make_gateway(tmp_path, host="127.0.0.2")   # laptop got a new address
    try:
        # Phone still has the OLD endpoint saved: fails at TCP, nothing else.
        with pytest.raises(OSError) as stale:
            _connect("127.0.0.1", second.port)
        assert _is_pure_tcp_failure(stale.value)

        # "Update Connection": only host/port change; identity is untouched.
        status, payload = _get_status("127.0.0.2", second.port, fingerprint, device_id, secret)
        assert status == 200 and payload["ok"] is True

        # The failed attempt did not create, remove or alter any device.
        devices_after = json.loads((tmp_path / "devices.json").read_text())
        assert set(devices_after) == set(json.loads(devices_before))
    finally:
        second.stop()


# --- field report: phone lost its credentials, then hammered /pair ----------

def test_repeated_pair_attempts_with_no_open_window_leave_a_registered_device_untouched(gw, tmp_path):
    """Field evidence: after the phone's app dropped its saved credentials, it
    sent six /pair requests with no pairing window open. The gateway must
    reject each one (invalid_pairing_token) WITHOUT altering the registry - the
    device stays registered with the same capabilities and its existing
    credentials keep working - and without ever pairing anyone."""
    device_id, secret = _pair_over_tls(gw)
    gw.registry.grant(device_id, "launch_app")
    before = json.loads((tmp_path / "devices.json").read_text())

    for _ in range(6):
        conn = _pinned_connect("127.0.0.1", gw.port, gw.fingerprint)
        status, payload = _post(conn, PAIR_PATH,
                                {"protocol": 1, "token": "stale-token", "name": "My Android Phone"})
        assert status != 200 and payload["ok"] is False
        assert payload["error"]["code"] == "invalid_pairing_token"

    after = json.loads((tmp_path / "devices.json").read_text())
    assert set(after) == set(before)                       # nothing added or removed
    assert after[device_id]["capabilities"] == before[device_id]["capabilities"]
    status, payload = _get_status("127.0.0.1", gw.port, gw.fingerprint, device_id, secret)
    assert status == 200 and payload["ok"] is True         # old identity still works


def test_repeated_authenticated_launches_never_trip_replay_or_drop_the_device(gw):
    """Five launch requests in a row (a fresh request_id each, as the app does
    with a UUID) all succeed, and the device is still valid afterwards."""
    device_id, secret = _pair_over_tls(gw)
    gw.registry.grant(device_id, "launch_app")
    for _ in range(5):
        body = {"protocol": 1, "request_id": f"uuid-{time.time_ns()}", "device_id": device_id,
                "operation": "launch_app", "parameters": {"name": "notepad"},
                "timestamp": time.time()}
        raw = json.dumps(body).encode()
        conn = _pinned_connect("127.0.0.1", gw.port, gw.fingerprint)
        conn.request("POST", REQUEST_PATH, body=raw,
                     headers={"Content-Type": "application/json",
                              auth.SIGNATURE_HEADER: auth.sign(secret, raw)})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        assert resp.status == 200 and payload["ok"] is True
    assert gw.registry.get(device_id) is not None
