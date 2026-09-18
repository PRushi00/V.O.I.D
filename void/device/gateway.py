"""The Device Gateway: an explicitly-started, TLS-only local HTTPS server
that is the ONLY network-reachable surface this feature adds to V.O.I.D.

It is never started by the voice runtime or autostart - only by ``python -m
void device serve`` - so V.O.I.D's default posture (no listening port at
all) is unchanged unless the owner deliberately opts in. Every request goes
through, in order: TLS, body-size cap, protocol/schema validation, device
lookup, HMAC signature verification, replay check, rate limiting, then the
closed capability allow-list (void.device.capabilities) - which is the only
thing that can reach RiskGate/KillSwitch/ToolRegistry, exactly the same way
the Agent does. Nothing here ever executes a raw string from the network.

Threading model: one thread per connection (``ThreadingHTTPServer``), but
all state mutation (device registry, replay guard, rate limiter, pairing)
is serialized behind one lock - correct and simple for a single-user
personal gateway; not tuned for throughput, which this has no need for.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device import auth, cert
from void.device.capabilities import GatewayContext, dispatch
from void.device.identity import DeviceRegistry
from void.device.pairing import PairingError, PairingManager
from void.device.protocol import (
    MAX_BODY_BYTES, DeviceResponse, ErrorCode, ProtocolError, error_response,
    parse_pair_request, parse_request,
)
from void.security.risk import RiskGate

_log = logging.getLogger("void.device.gateway")

PAIR_PATH = "/void/v1/pair"
REQUEST_PATH = "/void/v1/request"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
ADDRESS_FILENAME = "gateway_address.json"


def running_port(state_dir: Path) -> int | None:
    """The port a currently-running gateway process actually bound, read
    from the small state file it writes on start() - or None if no gateway
    appears to be running (the file is absent/stale-cleared on stop()).
    Lets `device pair-start` (a separate process) report the real port even
    when `device serve` was started with an overriding --port."""
    path = state_dir / ADDRESS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

_REQUEST_RATE_MAX = 30
_REQUEST_RATE_WINDOW_S = 60.0
_PAIR_RATE_MAX = 10
_PAIR_RATE_WINDOW_S = 300.0


def _client_ip(address) -> str:
    try:
        return str(address[0])
    except Exception:
        return "unknown"


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "VOIDDeviceGateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr logging;
        pass                            # we log ourselves, privacy-safely.

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> bytes | None:
        """Reject an oversized body based on Content-Length BEFORE reading
        it, so an attacker cannot force this process to buffer an arbitrarily
        large payload. Returns None (and already responded) on rejection."""
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False,
                                  "error": {"code": ErrorCode.MALFORMED.value,
                                            "message": "Missing/invalid Content-Length."}})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False,
                                  "error": {"code": ErrorCode.TOO_LARGE.value,
                                            "message": "Request body too large."}})
            return None
        return self.rfile.read(length)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        gateway: DeviceGateway = self.server.gateway  # type: ignore[attr-defined]
        client_ip = _client_ip(self.client_address)

        if self.path == PAIR_PATH:
            body = self._read_body()
            if body is None:
                return
            status, payload = gateway.handle_pair(body, client_ip)
            self._send_json(status, payload)
            return

        if self.path == REQUEST_PATH:
            body = self._read_body()
            if body is None:
                return
            signature = self.headers.get(auth.SIGNATURE_HEADER, "")
            status, payload = gateway.handle_request(body, signature, client_ip)
            self._send_json(status, payload)
            return

        self._send_json(404, {"ok": False,
                              "error": {"code": ErrorCode.MALFORMED.value,
                                        "message": "Unknown endpoint."}})

    def do_GET(self):  # noqa: N802
        self._send_json(404, {"ok": False,
                              "error": {"code": ErrorCode.MALFORMED.value,
                                        "message": "This endpoint only accepts POST."}})


class DeviceGateway:
    """Owns the HTTPS socket and all gateway-local state. Construct one per
    process (``python -m void device serve``); ``serve_forever``/``shutdown``
    mirror ``http.server``'s own lifecycle so it is trivially testable
    without touching a real network interface (bind to 127.0.0.1:0 in
    tests)."""

    def __init__(self, config: Config, tools: ToolRegistry, risk_gate: RiskGate,
                kill_switch: KillSwitch, state_dir: Path,
                host: str | None = None, port: int | None = None):
        self.config = config
        self.state_dir = state_dir
        self.registry = DeviceRegistry(state_dir / "devices.json")
        self.pairing = PairingManager(
            state_dir, window_seconds=config.get("device.pairing_window_seconds", 300))
        self._replay = auth.ReplayGuard()
        self._request_limiter = auth.RateLimiter(_REQUEST_RATE_MAX, _REQUEST_RATE_WINDOW_S)
        self._pair_limiter = auth.RateLimiter(_PAIR_RATE_MAX, _PAIR_RATE_WINDOW_S)
        self._lock = threading.Lock()
        self._ctx = GatewayContext(config=config, tools=tools, risk_gate=risk_gate,
                                   kill_switch=kill_switch)

        self.host = host or config.get("device.host", DEFAULT_HOST)
        self.port = port if port is not None else config.get("device.port", DEFAULT_PORT)

        self.cert_path, self.key_path = cert.ensure_cert(state_dir)
        self.fingerprint = cert.fingerprint(self.cert_path)

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._serving = False

    # --- lifecycle ---------------------------------------------------

    def start(self) -> None:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(certfile=str(self.cert_path), keyfile=str(self.key_path))

        server = ThreadingHTTPServer((self.host, self.port), _GatewayHandler)
        server.daemon_threads = True
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
        server.gateway = self  # type: ignore[attr-defined]
        self._server = server
        self.port = server.server_address[1]
        self._write_address_file()
        _log.info("DEVICE_GATEWAY_STARTED host=%s port=%s fingerprint=%s",
                  self.host, self.port, self.fingerprint)

    def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        self._serving = True
        self._server.serve_forever()

    def serve_in_background(self) -> None:
        assert self._server is not None, "call start() first"
        self._serving = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # BaseServer.shutdown() blocks waiting for serve_forever()'s loop to
        # acknowledge the stop request; calling it when serve_forever() was
        # NEVER entered would hang forever (nothing would ever acknowledge
        # it), so only ask for a real shutdown once serving actually started.
        if self._server is not None:
            if self._serving:
                self._server.shutdown()
            self._server.server_close()
            _log.info("DEVICE_GATEWAY_STOPPED")
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._clear_address_file()

    def _address_path(self) -> Path:
        return self.state_dir / ADDRESS_FILENAME

    def _write_address_file(self) -> None:
        """Record the port actually bound (which may differ from config if
        the owner passed --port) so `device pair-start` - a SEPARATE process
        invocation - can report the real, currently-running port instead of
        just repeating the configured default. Same cross-process-file
        pattern as the pairing window and KillSwitch's stop file."""
        try:
            self._address_path().write_text(json.dumps({"port": self.port}),
                                            encoding="utf-8")
        except OSError:
            pass  # best-effort; pairing falls back to the configured default

    def _clear_address_file(self) -> None:
        self._address_path().unlink(missing_ok=True)

    # --- pairing endpoint ---------------------------------------------

    def handle_pair(self, body: bytes, client_ip: str) -> tuple[int, dict]:
        with self._lock:
            if not self._pair_limiter.allow(client_ip):
                _log.warning("DEVICE_PAIR_RATE_LIMITED ip=%s", client_ip)
                return 429, {"ok": False,
                             "error": {"code": ErrorCode.RATE_LIMITED.value,
                                       "message": "Too many pairing attempts."}}
            try:
                req = parse_pair_request(body)
            except ProtocolError as exc:
                _log.warning("DEVICE_PAIR_REJECTED ip=%s code=%s", client_ip, exc.code.value)
                return 400, {"ok": False,
                             "error": {"code": exc.code.value, "message": exc.message}}
            try:
                name = self.pairing.redeem(req.token)
            except PairingError as exc:
                # exc.reason distinguishes WHY (no_window/expired/wrong_token)
                # for diagnostics only - the response to the caller is
                # unchanged (still a generic invalid_pairing_token, never
                # revealing which case it was to an unauthenticated caller).
                # Before this, every rejection logged identically, so a
                # genuine "the gateway never saw a pairing window at all"
                # bug (e.g. two processes resolving different state
                # directories) was indistinguishable from an ordinary wrong
                # guess or a stale token - undiagnosable after the fact.
                _log.warning("DEVICE_PAIR_REJECTED ip=%s reason=%s", client_ip, exc.reason)
                return 401, {"ok": False,
                             "error": {"code": ErrorCode.INVALID_TOKEN.value,
                                       "message": str(exc)}}
            device, shared_secret = self.registry.pair(name=req.name or name)
            _log.info("DEVICE_PAIRED device_id=%s ip=%s", device.device_id, client_ip)
            return 200, {
                "ok": True,
                "result": {
                    "device_id": device.device_id,
                    "shared_secret": shared_secret,
                    "capabilities": device.capabilities,
                    "server_fingerprint": self.fingerprint,
                },
            }

    # --- authenticated request endpoint --------------------------------

    def handle_request(self, body: bytes, signature: str,
                       client_ip: str) -> tuple[int, dict]:
        with self._lock:
            try:
                req = parse_request(body)
            except ProtocolError as exc:
                _log.warning("DEVICE_REQUEST_REJECTED ip=%s code=%s",
                            client_ip, exc.code.value)
                return 400, json.loads(error_response("", exc).to_json())

            if not self._request_limiter.allow(req.device_id):
                _log.warning("DEVICE_REQUEST_RATE_LIMITED device_id=%s", req.device_id)
                return 429, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.RATE_LIMITED, "Too many requests.")).to_json())

            device = self.registry.get(req.device_id)
            secret = self.registry.get_secret_value(req.device_id) if device else None
            if device is None or not secret:
                _log.warning("DEVICE_REQUEST_REJECTED device_id=%s code=unknown_device",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.UNKNOWN_DEVICE, "Unknown device.")).to_json())

            if not auth.verify(secret, body, signature):
                _log.warning("DEVICE_REQUEST_REJECTED device_id=%s code=bad_signature",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.BAD_SIGNATURE, "Invalid signature")).to_json())

            if auth.is_stale(req.timestamp):
                _log.warning("DEVICE_REQUEST_REJECTED device_id=%s code=stale",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.STALE, "Request timestamp out of range")).to_json())

            if not self._replay.check_and_record(req.device_id, req.request_id):
                _log.warning("DEVICE_REQUEST_REJECTED device_id=%s code=replayed",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.REPLAYED, "Duplicate request_id")).to_json())

            self.registry.touch(req.device_id)

            try:
                result = dispatch(req.operation, req.parameters, device.capabilities,
                                  self._ctx)
            except ProtocolError as exc:
                _log.info("DEVICE_REQUEST_DENIED device_id=%s operation=%s code=%s",
                         req.device_id, req.operation, exc.code.value)
                return 200, json.loads(error_response(req.request_id, exc).to_json())

            _log.info("DEVICE_REQUEST_OK device_id=%s operation=%s",
                     req.device_id, req.operation)
            resp = DeviceResponse(request_id=req.request_id, ok=True, result=result)
            return 200, json.loads(resp.to_json())
