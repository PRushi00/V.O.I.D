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

import collections
import json
import logging
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from void import perf
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


class _LogSampler:
    """Admit at most one log record per key per ``interval_s``; report how many
    similar records were suppressed since the last admitted one. Keys are the
    (finite) message templates, so memory is bounded. Rejections of
    UNAUTHENTICATED input are attacker-controlled volume: without sampling one
    probe wrote one WARNING into a non-rotating log (D-09)."""

    def __init__(self, interval_s: float = 1.0, clock=time.monotonic):
        self._interval = interval_s
        self._clock = clock
        self._last: dict[str, float] = {}
        self._suppressed: "collections.Counter[str]" = collections.Counter()
        self._lock = threading.Lock()

    def admit(self, key: str) -> int | None:
        """None => drop this record; otherwise the number suppressed before it."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                self._suppressed[key] += 1
                return None
            self._last[key] = now
            return self._suppressed.pop(key, 0)


class _BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with (a) a hard cap on concurrent handler threads and
    (b) quiet, sampled handling of per-connection errors.

    The cap matters because V1 spawned one thread per connection without limit;
    the error handler matters because a client that stalls or aborts the TLS
    handshake (now performed in the handler thread, see DeviceGateway.start) is
    expected and must neither spam stderr with tracebacks nor take the server
    down."""

    daemon_threads = True

    def __init__(self, *args, max_handlers: int = 32, max_per_ip: int = 4, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(max(1, int(max_handlers)))
        # A global cap alone lets ONE source hold every slot with idle connections
        # until the handler timeout. The per-source cap stops a single flooding
        # source from monopolising the pool. (A flood from >= max_handlers/max_per_ip
        # distinct sources can still occupy all slots for one timeout window: an
        # availability-only limit, bounded in time and threads.)
        self._max_per_ip = max(1, int(max_per_ip))
        self._per_ip: "collections.Counter[str]" = collections.Counter()
        self._per_ip_lock = threading.Lock()

    def _drop(self, request, counter: str):
        gw = getattr(self, "gateway", None)
        if gw is not None:
            gw._bump(counter)
        try:
            request.close()
        except OSError:
            pass

    def _release_ip(self, ip: str) -> None:
        with self._per_ip_lock:
            self._per_ip[ip] -= 1
            if self._per_ip[ip] <= 0:
                del self._per_ip[ip]

    def process_request(self, request, client_address):
        ip = _client_ip(client_address)
        with self._per_ip_lock:
            over_ip = self._per_ip[ip] >= self._max_per_ip
            if not over_ip:
                self._per_ip[ip] += 1
        if over_ip:
            self._drop(request, "connections_dropped_per_ip")
            return
        if not self._slots.acquire(blocking=False):
            self._release_ip(ip)
            self._drop(request, "connections_dropped_over_cap")
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            self._release_ip(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            self._release_ip(_client_ip(client_address))

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        gw = getattr(self, "gateway", None)
        if gw is None:
            return
        kind = ("timeout" if isinstance(exc, (TimeoutError, ssl.SSLError))
                else type(exc).__name__)
        gw._bump(f"connection_error:{kind}")
        gw._sampled_log(logging.INFO, "DEVICE_CONNECTION_ERROR kind=%s", kind,
                        key="DEVICE_CONNECTION_ERROR:" + kind)


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "VOIDDeviceGateway/1"
    protocol_version = "HTTP/1.1"

    def setup(self):
        # Per-connection timeout (D-03). StreamRequestHandler.setup applies
        # self.timeout to the socket; because the TLS handshake now happens lazily
        # on first read in THIS thread, the same timeout bounds a stalled or
        # half-open handshake as well as a slow-loris body.
        gw = getattr(self.server, "gateway", None)
        timeout = getattr(gw, "connection_timeout_s", None)
        if timeout:
            self.timeout = timeout
        super().setup()

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
        max_keys = int(config.get("device.limiter_max_keys", 1024))
        self._request_limiter = auth.RateLimiter(_REQUEST_RATE_MAX, _REQUEST_RATE_WINDOW_S,
                                                 max_keys=max_keys)
        self._pair_limiter = auth.RateLimiter(_PAIR_RATE_MAX, _PAIR_RATE_WINDOW_S,
                                              max_keys=max_keys)
        # Pre-auth, per-client-IP ceiling applied BEFORE the body is parsed, so an
        # unauthenticated flood is throttled without touching device state (D-09).
        self._ip_limiter = auth.RateLimiter(int(config.get("device.ip_rate_per_minute", 120)),
                                            60.0, max_keys=max_keys)
        # Availability limits (D-03): per-connection I/O timeout (also bounds a
        # stalled TLS handshake) and a cap on concurrently served connections.
        self.connection_timeout_s = float(config.get("device.connection_timeout_s", 10.0))
        self.max_connections = int(config.get("device.max_connections", 32))
        self.max_connections_per_ip = int(config.get("device.max_connections_per_ip", 4))
        self._sampler = _LogSampler(1.0)
        self._stats: "collections.Counter[str]" = collections.Counter()
        self._stats_lock = threading.Lock()
        self.stats_period_s = float(config.get("device.stats_period_s", 60.0))
        self._stats_stop = threading.Event()
        self._stats_thread: threading.Thread | None = None
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

        server = _BoundedServer((self.host, self.port), _GatewayHandler,
                                max_handlers=self.max_connections,
                                max_per_ip=self.max_connections_per_ip)
        # D-03: wrapping the LISTENING socket makes accept() perform the TLS
        # handshake inside the single accept loop, so one client that connects and
        # sends nothing blocked every other client. Defer the handshake to the first
        # read, which happens in the per-connection handler thread (bounded by the
        # handler timeout). TLS version, certificate and pinning are unchanged.
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True,
                                                do_handshake_on_connect=False)
        server.gateway = self  # type: ignore[attr-defined]
        self._server = server
        self.port = server.server_address[1]
        self._write_address_file()
        self._start_stats_loop()
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
        self._stats_stop.set()
        if self._stats_thread is not None:
            self._stats_thread.join(timeout=2)
            self._stats_thread = None
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

    # --- bounded, sampled logging and counters (D-09) -----------------

    def _bump(self, name: str) -> None:
        with self._stats_lock:
            self._stats[name] += 1

    def stats_snapshot(self) -> dict:
        """Counts only (no identifiers, no content) - for health/perf reporting."""
        with self._stats_lock:
            return dict(self._stats)

    def _start_stats_loop(self) -> None:
        """Emit aggregate counters (never identifiers) to the perf stream every
        ``stats_period_s`` while the gateway runs; silent when nothing happened."""
        if self.stats_period_s <= 0:
            return
        self._stats_stop.clear()
        self._stats_thread = threading.Thread(target=self._stats_loop, name="void-gw-stats", daemon=True)
        self._stats_thread.start()

    def _stats_loop(self) -> None:
        last: dict = {}
        while not self._stats_stop.wait(self.stats_period_s):
            snap = self.stats_snapshot()
            delta = {k: v - last.get(k, 0) for k, v in snap.items()}
            last = snap

            def total(*prefixes: str) -> int:
                return sum(v for k, v in delta.items() if k.startswith(prefixes))

            agg = {"ok": delta.get("DEVICE_REQUEST_OK", 0),
                   "rejected": total("DEVICE_REQUEST_REJECTED", "DEVICE_PAIR_REJECTED", "DEVICE_REQUEST_DENIED"),
                   "rate_limited": total("DEVICE_REQUEST_RATE_LIMITED", "DEVICE_PAIR_RATE_LIMITED"),
                   "paired": delta.get("DEVICE_PAIRED", 0),
                   "dropped": total("connections_dropped"),
                   "conn_errors": total("connection_error")}
            if any(agg.values()):
                perf.emit("gateway", period_s=self.stats_period_s, **agg)

    def _sampled_log(self, level: int, template: str, *args, key: str | None = None) -> None:
        """``key`` must be FINITE (never an id/IP): it bounds sampler memory and is
        what "similar" means. Defaults to the template."""
        suppressed = self._sampler.admit(key or template)
        if suppressed is None:
            return
        if suppressed:
            template += " (+%d similar suppressed)"
            args = args + (suppressed,)
        _log.log(level, template, *args)

    @staticmethod
    def _event_key(template: str, args: tuple) -> str:
        """Event name + the code/reason ONLY - never a device id or IP, so the key
        is finite and counters/sampling carry no identifiers."""
        words = template.split()
        tail = words[-1]
        if tail.startswith(("code=", "reason=")):
            tail = str(args[-1]) if tail.endswith("=%s") else tail.split("=", 1)[1]
            return f"{words[0]}:{tail}"
        return words[0]

    def _warn(self, template: str, *args) -> None:
        key = self._event_key(template, args)
        self._bump(key)
        self._sampled_log(logging.WARNING, template, *args, key=key)

    # --- pairing endpoint ---------------------------------------------

    def handle_pair(self, body: bytes, client_ip: str) -> tuple[int, dict]:
        with self._lock:
            if not self._pair_limiter.allow(client_ip):
                self._warn("DEVICE_PAIR_RATE_LIMITED ip=%s", client_ip)
                return 429, {"ok": False,
                             "error": {"code": ErrorCode.RATE_LIMITED.value,
                                       "message": "Too many pairing attempts."}}
            try:
                req = parse_pair_request(body)
            except ProtocolError as exc:
                self._warn("DEVICE_PAIR_REJECTED ip=%s code=%s", client_ip, exc.code.value)
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
                self._warn("DEVICE_PAIR_REJECTED ip=%s reason=%s", client_ip, exc.reason)
                return 401, {"ok": False,
                             "error": {"code": ErrorCode.INVALID_TOKEN.value,
                                       "message": str(exc)}}
            device, shared_secret = self.registry.pair(name=req.name or name)
            self._bump("DEVICE_PAIRED")
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
            if not self._ip_limiter.allow(client_ip):
                self._warn("DEVICE_REQUEST_RATE_LIMITED ip=%s", client_ip)
                return 429, json.loads(error_response(
                    "", ProtocolError(ErrorCode.RATE_LIMITED, "Too many requests.")).to_json())
            try:
                req = parse_request(body)
            except ProtocolError as exc:
                self._warn("DEVICE_REQUEST_REJECTED ip=%s code=%s",
                            client_ip, exc.code.value)
                return 400, json.loads(error_response("", exc).to_json())

            if not self._request_limiter.allow(req.device_id):
                self._warn("DEVICE_REQUEST_RATE_LIMITED device_id=%s", req.device_id)
                return 429, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.RATE_LIMITED, "Too many requests.")).to_json())

            device = self.registry.get(req.device_id)
            secret = self.registry.get_secret_value(req.device_id) if device else None
            if device is None or not secret:
                self._warn("DEVICE_REQUEST_REJECTED device_id=%s code=unknown_device",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.UNKNOWN_DEVICE, "Unknown device.")).to_json())

            if not auth.verify(secret, body, signature):
                self._warn("DEVICE_REQUEST_REJECTED device_id=%s code=bad_signature",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.BAD_SIGNATURE, "Invalid signature")).to_json())

            if auth.is_stale(req.timestamp):
                self._warn("DEVICE_REQUEST_REJECTED device_id=%s code=stale",
                            req.device_id)
                return 401, json.loads(error_response(
                    req.request_id,
                    ProtocolError(ErrorCode.STALE, "Request timestamp out of range")).to_json())

            if not self._replay.check_and_record(req.device_id, req.request_id):
                self._warn("DEVICE_REQUEST_REJECTED device_id=%s code=replayed",
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

            self._bump("DEVICE_REQUEST_OK")
            _log.info("DEVICE_REQUEST_OK device_id=%s operation=%s",
                     req.device_id, req.operation)
            resp = DeviceResponse(request_id=req.request_id, ok=True, result=result)
            return 200, json.loads(resp.to_json())
