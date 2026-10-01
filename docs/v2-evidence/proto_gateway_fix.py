"""PROTOTYPE (scratch only; repo untouched): candidate D-03 fix = lazy TLS handshake in the handler thread
+ handler socket timeout + bounded concurrent handlers. Loopback + temp state dir. Compares against V1."""
import json, socket, ssl, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")
import logging; logging.disable(logging.CRITICAL)
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device.gateway import DeviceGateway, _GatewayHandler
from void.security.risk import RiskGate

class FixedHandler(_GatewayHandler):
    timeout = 2.0                                   # StreamRequestHandler.setup() applies this to the socket

class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *a, max_handlers=32, **k):
        super().__init__(*a, **k); self._sem = threading.BoundedSemaphore(max_handlers); self.rejected = 0
    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):      # over the cap: drop immediately
            self.rejected += 1
            try: request.close()
            except OSError: pass
            return
        super().process_request(request, client_address)
    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self._sem.release()
    def handle_error(self, request, client_address): pass       # timeouts/aborted handshakes are expected; don't spam stderr

class FixedGateway(DeviceGateway):
    def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(self.cert_path), keyfile=str(self.key_path))
        srv = BoundedServer((self.host, self.port), FixedHandler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)   # <- the change
        srv.gateway = self; self._server = srv; self.port = srv.server_address[1]

def make(cls):
    state = Path(tempfile.mkdtemp(prefix="void_gwproto_"))
    gw = cls(Config({"app": {"name": "x", "version": "1"}, "voice": {"enabled": False}}), ToolRegistry(), RiskGate(),
             KillSwitch(), state, host="127.0.0.1", port=0)
    gw.start(); gw.serve_in_background(); return gw

def legit_seconds(port, timeout=4.0):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname="x") as tls:
            body = json.dumps({"protocol": 1, "token": "wrong", "name": "t"}).encode()
            tls.sendall(b"POST /void/v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            data = tls.recv(4096)
        return time.perf_counter() - t0, data.split(b"\r\n", 1)[0].decode(errors="replace")
    except Exception as e:
        return time.perf_counter() - t0, type(e).__name__

def scenario(name, gw, prep):
    conns = prep(gw.port)
    time.sleep(0.4)
    d, r = legit_seconds(gw.port)
    print(f"  [{name}] legit request: {d*1000:7.0f} ms -> {r}")
    return conns

for label, cls in (("V1 baseline gateway", DeviceGateway), ("PROTOTYPE (lazy handshake + timeout + cap)", FixedGateway)):
    print(f"\n### {label}")
    gw = make(cls)
    d, r = legit_seconds(gw.port); print(f"  [no attack]           legit request: {d*1000:7.0f} ms -> {r}")
    idle = scenario("1 idle TCP conn      ", gw, lambda p: [socket.create_connection(("127.0.0.1", p))])
    half = scenario("+ half-open ClientHello", gw, lambda p: [(lambda s: (s.sendall(b"\x16\x03\x01\x00\xc8"), s)[1])(socket.create_connection(("127.0.0.1", p))) for _ in range(3)])
    time.sleep(3.0)                                   # let handler timeouts fire
    d, r = legit_seconds(gw.port); print(f"  [3 s later, attackers still holding sockets] legit: {d*1000:7.0f} ms -> {r}")
    closed = 0
    for c in idle + half:
        try:
            c.settimeout(0.3); closed += 1 if c.recv(1) == b"" else 0
        except (socket.timeout, ConnectionResetError, OSError) as e:
            closed += 1 if not isinstance(e, socket.timeout) else 0
    print(f"  server closed {closed}/{len(idle)+len(half)} attacker connections on its own")
    flood = []
    for _ in range(150):
        try: flood.append(socket.create_connection(("127.0.0.1", gw.port), timeout=0.5))
        except OSError: pass                      # a stalled/backlogged server refuses or times out: that IS the result
    time.sleep(0.5); d, r = legit_seconds(gw.port)
    print(f"  [150 idle conns: {len(flood)} accepted by the OS] legit: {d*1000:7.0f} ms -> {r} | active threads={threading.active_count()}"
          + (f" | rejected over cap={gw._server.rejected}" if hasattr(gw._server, 'rejected') else ""))
    for c in flood + idle + half:
        try: c.close()
        except OSError: pass
    gw.stop()
print("\ndone")
