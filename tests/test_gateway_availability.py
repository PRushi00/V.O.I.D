"""D-03 / D-09 (T0.5): the gateway stays available under idle / half-open / slow-loris
clients and floods, its pre-auth state and logs are bounded, and NONE of the
authentication controls were weakened to get there.

Real loopback sockets; short timeouts (Config ``device.connection_timeout_s``) keep it fast."""
import json
import socket
import ssl
import time

import pytest

from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device import auth
from void.device.gateway import DeviceGateway, _LogSampler
from void.security import secrets
from void.security.risk import RiskGate


def _gateway(tmp_path, **device_cfg):
    cfg = Config({"app": {"name": "V.O.I.D", "version": "t"}, "voice": {"enabled": False},
                  "device": {"connection_timeout_s": 1.0, "max_connections": 8, **device_cfg}})
    gw = DeviceGateway(cfg, ToolRegistry(), RiskGate(), KillSwitch(), tmp_path,
                       host="127.0.0.1", port=0)
    gw.start()
    gw.serve_in_background()
    return gw


def _tls_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _legit(port, timeout=3.0):
    body = json.dumps({"protocol": 1, "token": "wrong", "name": "t"}).encode()
    t0 = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw, \
                _tls_ctx().wrap_socket(raw, server_hostname="x") as tls:
            tls.sendall(b"POST /void/v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            status = tls.recv(4096).split(b"\r\n", 1)[0].decode(errors="replace")
    except Exception as exc:                          # noqa: BLE001
        status = type(exc).__name__
    return status, time.perf_counter() - t0


def _closed_by_server(sock, wait=3.0):
    """True once the server has closed (or reset) the connection on its own."""
    sock.settimeout(wait)
    try:
        return sock.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


@pytest.fixture
def gw(tmp_path):
    g = _gateway(tmp_path)
    yield g
    g.stop()


# ------------------------------------------------------------------ availability
@pytest.mark.real_socket
def test_idle_client_does_not_block_a_legitimate_client(gw):
    idle = socket.create_connection(("127.0.0.1", gw.port))
    try:
        time.sleep(0.2)
        status, elapsed = _legit(gw.port)
        assert status.startswith("HTTP/1.1 401") and elapsed < 1.0
    finally:
        idle.close()


@pytest.mark.real_socket
def test_half_open_tls_handshakes_do_not_block_and_are_closed_by_the_server(gw):
    half = []
    for _ in range(3):
        s = socket.create_connection(("127.0.0.1", gw.port))
        s.sendall(b"\x16\x03\x01\x00\xc8")            # start of a TLS record, then silence
        half.append(s)
    try:
        time.sleep(0.2)
        status, elapsed = _legit(gw.port)
        assert status.startswith("HTTP/1.1 401") and elapsed < 1.0
        assert all(_closed_by_server(s) for s in half), "server kept a stalled handshake open"
    finally:
        for s in half:
            s.close()


@pytest.mark.real_socket
def test_slow_loris_body_is_cut_off_and_does_not_block_others(gw):
    raw = socket.create_connection(("127.0.0.1", gw.port))
    tls = _tls_ctx().wrap_socket(raw, server_hostname="x")
    try:
        tls.sendall(b"POST /void/v1/pair HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")  # body never sent
        time.sleep(0.2)
        status, elapsed = _legit(gw.port)
        assert status.startswith("HTTP/1.1 401") and elapsed < 1.0
        assert _closed_by_server(tls), "slow-loris connection was never cut off"
    finally:
        tls.close()


def _connect_from(source_ip, port):
    s = socket.socket()
    s.settimeout(1.0)
    s.bind((source_ip, 0))
    s.connect(("127.0.0.1", port))
    return s


def _second_loopback_source_available():
    try:
        s = socket.socket()
        s.bind(("127.0.0.2", 0))
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.real_socket
@pytest.mark.skipif(not _second_loopback_source_available(),
                    reason="needs a second loopback source address (127.0.0.2) to model a different client")
def test_single_source_flood_cannot_starve_a_different_client(tmp_path):
    """One flooding source is capped per-IP, so a client at another address is still
    served DETERMINISTICALLY (a global cap alone let one source hold every slot)."""
    g = _gateway(tmp_path, max_connections=8, max_connections_per_ip=3)
    flood = []
    try:
        for _ in range(60):
            try:
                flood.append(_connect_from("127.0.0.2", g.port))
            except OSError:
                pass
        time.sleep(0.4)
        status, elapsed = _legit(g.port)              # 127.0.0.1: a different source
        assert status.startswith("HTTP/1.1 401") and elapsed < 1.0, status
        assert g.stats_snapshot().get("connections_dropped_per_ip", 0) > 0
    finally:
        for s in flood:
            s.close()
        g.stop()


@pytest.mark.real_socket
def test_global_handler_cap_bounds_threads_under_a_flood(tmp_path):
    """The global cap bounds resource use even when the per-source cap is out of the
    picture (documented limit: a many-source flood can hold slots for one timeout)."""
    import threading
    g = _gateway(tmp_path, max_connections=8, max_connections_per_ip=1000)
    before = threading.active_count()
    flood = []
    try:
        for _ in range(60):
            try:
                flood.append(socket.create_connection(("127.0.0.1", g.port), timeout=1.0))
            except OSError:
                pass
        time.sleep(0.4)
        assert g.stats_snapshot().get("connections_dropped_over_cap", 0) > 0
        assert threading.active_count() - before <= 8 + 2, "handler threads were not bounded"
    finally:
        for s in flood:
            s.close()
        g.stop()


@pytest.mark.real_socket
def test_slots_are_released_so_the_server_recovers_after_a_flood(tmp_path):
    g = _gateway(tmp_path, max_connections=4, max_connections_per_ip=1000)
    flood = []
    try:
        for _ in range(20):
            try:
                flood.append(socket.create_connection(("127.0.0.1", g.port), timeout=1.0))
            except OSError:
                pass
        time.sleep(1.6)                               # handler timeout (1 s) frees every slot
        status, _ = _legit(g.port)
        assert status.startswith("HTTP/1.1 401"), f"server did not recover after the flood: {status}"
    finally:
        for s in flood:
            s.close()
        g.stop()


# ------------------------------------------------------ security preserved (no weakening)
@pytest.mark.real_socket
def test_plaintext_http_is_still_refused_tls_is_mandatory(gw):
    s = socket.create_connection(("127.0.0.1", gw.port))
    try:
        s.sendall(b"POST /void/v1/pair HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
        s.settimeout(3.0)
        try:
            data = s.recv(4096)
        except OSError:
            data = b""
        assert b"HTTP/1.1 2" not in data and b"HTTP/1.1 4" not in data, "plaintext HTTP was answered"
    finally:
        s.close()


@pytest.mark.real_socket
def test_server_still_requires_tls_1_2_or_newer(tmp_path):
    g = _gateway(tmp_path)
    try:
        assert g._server.socket.context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert g._server.socket.do_handshake_on_connect is False   # lazy, but still TLS
    finally:
        g.stop()


# ------------------------------------------------------------------ limiter bounds (D-09)
def test_rate_limiter_key_set_is_bounded_and_lru_evicted():
    lim = auth.RateLimiter(5, 60.0, max_keys=8)
    for i in range(100):
        lim.allow(f"k{i}", now=1000.0 + i * 0.001)
    assert len(lim._events) == 8
    assert "k99" in lim._events and "k0" not in lim._events


def test_rate_limiter_still_enforces_per_key_limits():
    lim = auth.RateLimiter(3, 60.0, max_keys=8)
    assert [lim.allow("a", now=1.0) for _ in range(5)] == [True, True, True, False, False]
    assert lim.allow("a", now=100.0) is True          # window slid


def test_recently_used_key_survives_eviction():
    lim = auth.RateLimiter(100, 60.0, max_keys=3)
    for k in ("a", "b", "c"):
        lim.allow(k, now=1.0)
    lim.allow("a", now=2.0)                           # touch a
    lim.allow("d", now=3.0)                           # evicts the LRU (b)
    assert set(lim._events) == {"a", "c", "d"}


def test_pre_auth_ip_limiter_throttles_a_flood_before_device_state_is_touched(tmp_path):
    g = _gateway(tmp_path, ip_rate_per_minute=5)
    try:
        body = json.dumps({"protocol": 1, "request_id": "r", "device_id": "d", "operation": "get_status",
                           "parameters": {}, "timestamp": time.time()}).encode()
        codes = [g.handle_request(body, "sig", "203.0.113.5")[0] for _ in range(20)]
        assert codes[:5].count(429) == 0 and codes[5:].count(429) == 15
        assert len(g._request_limiter._events) <= 1   # throttled requests never reached device state
    finally:
        g.stop()


# ------------------------------------------------------------------ log sampling (D-09)
def test_log_sampler_admits_first_suppresses_burst_then_reports_the_count():
    now = {"t": 100.0}
    s = _LogSampler(1.0, clock=lambda: now["t"])
    assert s.admit("k") == 0                          # first: admitted, nothing suppressed
    assert [s.admit("k") for _ in range(5)] == [None] * 5
    now["t"] += 1.5
    assert s.admit("k") == 5                          # next window: admitted, reports 5 dropped
    assert s.admit("other") == 0                      # independent key


def test_distinct_rejection_reasons_are_each_logged(tmp_path, caplog):
    import logging
    g = _gateway(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="void.device.gateway"):
            g.handle_pair(json.dumps({"protocol": 1, "token": "x", "name": "P"}).encode(), "127.0.0.1")
            g.pairing.begin("P")
            g.handle_pair(json.dumps({"protocol": 1, "token": "y", "name": "P"}).encode(), "127.0.0.2")
        lines = " ".join(r.message for r in caplog.records)
        assert "reason=no_window" in lines and "reason=wrong_token" in lines
    finally:
        g.stop()


def test_counters_record_rejections_without_identifiers(tmp_path):
    g = _gateway(tmp_path)
    try:
        body = json.dumps({"protocol": 1, "request_id": "r", "device_id": "secret-looking-id",
                           "operation": "get_status", "parameters": {}, "timestamp": time.time()}).encode()
        g.handle_request(body, "sig", "203.0.113.5")
        snap = g.stats_snapshot()
        assert any("unknown_device" in k for k in snap)
        assert "secret-looking-id" not in json.dumps(snap) and "203.0.113.5" not in json.dumps(snap)
    finally:
        g.stop()


# ------------------------------------------------------------------ PIN comparison (D-09)
def test_stop_pin_is_compared_in_constant_time(monkeypatch):
    calls = []
    import void.core.kill_switch as ks_mod
    real = ks_mod.hmac.compare_digest
    monkeypatch.setattr(ks_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    secrets.set_secret(secrets.STOP_PIN, "4321")
    ks = KillSwitch(require_pin=True)
    assert ks.engage("t", pin="0000") is False
    assert ks.engage("t", pin="4321") is True
    assert len(calls) == 2


def test_stop_pin_semantics_unchanged_including_non_ascii_and_missing_pin():
    secrets.set_secret(secrets.STOP_PIN, "pässwörd")
    ks = KillSwitch(require_pin=True)
    assert ks.engage("t", pin="pässwörd") is True     # non-ASCII must not raise
    ks2 = KillSwitch(require_pin=True)
    assert ks2.engage("t", pin=None) is False         # PIN required, none supplied
    assert ks2.engage("t", pin="") is False
    secrets.delete_secret(secrets.STOP_PIN)
    assert KillSwitch(require_pin=True).engage("t") is True   # fail-safe: no PIN configured => stop allowed
