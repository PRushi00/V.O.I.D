"""REPRODUCE (scratch, loopback only, temp state dir - NOT the live gateway).

Hypothesis from code reading: DeviceGateway wraps the LISTENING socket with TLS
(ssl_context.wrap_socket(server.socket, server_side=True)); SSLSocket.accept()
then performs the TLS handshake inside the single accept loop, and no
handshake/connection timeout is configured anywhere. So ONE idle TCP connection
that never sends a ClientHello should stall accept() for everyone else.
"""
import os, socket, ssl, sys, tempfile, time
sys.path.insert(0, r"C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6")

from pathlib import Path
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.kill_switch import KillSwitch
from void.device.gateway import DeviceGateway
from void.security.risk import RiskGate

state = Path(tempfile.mkdtemp(prefix="void_gw_repro_"))
gw = DeviceGateway(Config({"app": {"name": "V.O.I.D", "version": "x"}, "voice": {"enabled": False}}),
                   ToolRegistry(), RiskGate(), KillSwitch(), state, host="127.0.0.1", port=0)
gw.start(); gw.serve_in_background()
port = gw.port
print(f"isolated gateway on 127.0.0.1:{port} (state dir {state})")

def tls_handshake_seconds(timeout=4.0):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname="x") as tls:
                return time.perf_counter() - t0, "ok"
    except Exception as e:
        return time.perf_counter() - t0, type(e).__name__

d, r = tls_handshake_seconds()
print(f"[baseline] legit TLS handshake: {d*1000:.0f} ms ({r})")

# One idle TCP connection: connect, send nothing.
idle = socket.create_connection(("127.0.0.1", port))
time.sleep(0.5)
d, r = tls_handshake_seconds(timeout=4.0)
print(f"[with 1 idle TCP conn open] legit TLS handshake: {d*1000:.0f} ms ({r})")

# Does it stay stuck? wait longer, still holding the idle connection.
time.sleep(5)
d, r = tls_handshake_seconds(timeout=4.0)
print(f"[5+ s later, idle conn still open] legit TLS handshake: {d*1000:.0f} ms ({r})")

idle.close()
time.sleep(0.5)
d, r = tls_handshake_seconds()
print(f"[after closing the idle conn] legit TLS handshake: {d*1000:.0f} ms ({r})")
gw.stop()
