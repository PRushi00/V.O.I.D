"""Failing-first regression tests for the confirmed V1 defects (V2.0 T0.2).

Each ``xfail(strict=True)`` test asserts the INTENDED behaviour and is known to
fail on the V1 baseline FOR THE STATED REASON (checked with ``--runxfail``). When
the fixing task lands, the xfail marker is removed and the test becomes a
permanent regression guard; ``strict=True`` makes an unexpected pass loud.

Every xfail has a passing "harness sanity" companion so that a broken fixture can
never be mistaken for the defect it is meant to expose.

  D-01  mic supervisor abandons recovery after one failed restart      (fixed by T0.3)
  D-02  PTT ``ctrl+space`` hooks bare ``space``                        (fixed by T0.4)
  D-03  one idle TCP connection stalls the device gateway              (fixed by T0.5)
  D-04b agent-created pairing_window.json is redeemable                (fixed by T1.1)
  D-05  open_path treats a data-carrying URL as LOW risk               (fixed by T1.5)
  D-07  no provider failover when the cloud is unreachable             (fixed by T2.5)
  D-09  unbounded limiter state / log flood from unauthenticated input (fixed by T0.5)
  D-13  voice is silent when a task is not COMPLETED                   (fixed by T0.10)
"""
import json
import logging
import os
import socket
import ssl
import sys
import time
import types
from pathlib import Path

import pytest

from void.actions.apps import AppActions
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, TaskStore
from void.device.gateway import DeviceGateway
from void.device.pairing import PairingError, PairingManager
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.risk import RiskGate, RiskLevel
from void.voice.adapters import STT, TTS, PTTActivation
from void.voice.session import VoiceSession

from tests.test_voice_mic_recovery import (
    FailingStartBackend, _MIC_SILENCE_TIMEOUT_S, _rig,
)


def _xfail(defect: str, why: str):
    return pytest.mark.xfail(strict=True, reason=f"{defect}: {why}")


# ---------------------------------------------------------------- D-01
def _silent_then_failed_first_restart():
    backend = FailingStartBackend(fail_on_call=2)     # start #2 = the recovery attempt raises
    ctrl, broker, backend, _session, states, clock = _rig(backend=backend)
    broker.start()
    backend.emit()
    broker.drain()
    clock.advance(_MIC_SILENCE_TIMEOUT_S + 0.1)
    ctrl.poll_once()                                  # recovery attempt #1 raises inside
    return ctrl, broker, backend, clock


def test_d01_harness_first_restart_failure_is_reached():
    ctrl, broker, backend, _clock = _silent_then_failed_first_restart()
    assert backend._call == 2                         # the failing restart really happened
    assert ctrl._mic_healthy is False                 # supervisor noticed the silence


def test_d01_supervisor_keeps_retrying_after_a_failed_restart():          # fixed by T0.3
    ctrl, broker, backend, clock = _silent_then_failed_first_restart()
    for _ in range(600):                              # 10 simulated minutes of monitor ticks
        clock.advance(1.0)
        ctrl.poll_once()
    assert backend._call >= 3, (
        f"supervisor never retried: backend.start() called {backend._call} time(s); "
        f"broker.running={broker.running}")


def test_d01_mic_eventually_recovers_when_device_returns():               # fixed by T0.3
    ctrl, broker, backend, clock = _silent_then_failed_first_restart()
    recovered = False
    for _ in range(600):
        clock.advance(1.0)
        ctrl.poll_once()
        backend.emit()
        if ctrl._mic_healthy:
            recovered = True
            break
    assert recovered, (f"mic never recovered although the device is available again; "
                       f"broker.running={broker.running} starts={backend._call}")


# ---------------------------------------------------------------- D-02
def _fake_keyboard(monkeypatch, held: set):
    kb = types.SimpleNamespace(callbacks={})
    kb.on_press_key = lambda key, cb: kb.callbacks.__setitem__(("down", key), cb)
    kb.on_release_key = lambda key, cb: kb.callbacks.__setitem__(("up", key), cb)
    kb.is_pressed = lambda name: name in held
    kb.unhook_all = lambda: None
    monkeypatch.setitem(sys.modules, "keyboard", kb)
    return kb


def test_d02_harness_chord_with_ctrl_held_starts_ptt(monkeypatch):
    held = {"ctrl", "space"}
    kb = _fake_keyboard(monkeypatch, held)
    events = []
    ptt = PTTActivation(lambda: events.append("down"), lambda: events.append("up"),
                        hotkey="ctrl+space")
    ptt.start()
    kb.callbacks[("down", "space")](None)
    assert events == ["down"]


@_xfail("D-02", "bare Space (Ctrl NOT held) starts a voice session because only the last chord token is hooked")
def test_d02_bare_space_does_not_start_ptt(monkeypatch):
    kb = _fake_keyboard(monkeypatch, held=set())      # Ctrl is NOT down
    events = []
    ptt = PTTActivation(lambda: events.append("down"), lambda: events.append("up"),
                        hotkey="ctrl+space")
    ptt.start()
    kb.callbacks[("down", "space")](None)
    assert events == [], f"bare Space activated PTT: {events}"


# ---------------------------------------------------------------- gateway helpers
def _make_gateway(state_dir, start=True):
    gw = DeviceGateway(
        Config({"app": {"name": "V.O.I.D", "version": "test"}, "voice": {"enabled": False}}),
        ToolRegistry(), RiskGate(), KillSwitch(), state_dir, host="127.0.0.1", port=0)
    if start:
        gw.start()
        gw.serve_in_background()
    return gw


def _legit_pair_attempt(port, timeout):
    """One well-formed TLS request that the gateway answers with HTTP 401 (unknown token)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps({"protocol": 1, "token": "wrong", "name": "t"}).encode()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw, \
                ctx.wrap_socket(raw, server_hostname="x") as tls:
            tls.sendall(b"POST /void/v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            return tls.recv(4096).split(b"\r\n", 1)[0].decode(errors="replace")
    except Exception as exc:                          # noqa: BLE001 - the type is the evidence
        return type(exc).__name__


# ---------------------------------------------------------------- D-03
@pytest.mark.real_socket
def test_d03_harness_legit_request_works_without_an_attacker(tmp_path):
    gw = _make_gateway(tmp_path)
    try:
        assert _legit_pair_attempt(gw.port, timeout=3.0).startswith("HTTP/1.1 401")
    finally:
        gw.stop()


@pytest.mark.real_socket
@_xfail("D-03", "the TLS handshake runs inside the single accept loop, so one idle TCP connection blocks everyone")
def test_d03_idle_tcp_connection_does_not_block_legitimate_clients(tmp_path):
    gw = _make_gateway(tmp_path)
    idle = socket.create_connection(("127.0.0.1", gw.port))     # connects, sends nothing
    try:
        time.sleep(0.3)
        outcome = _legit_pair_attempt(gw.port, timeout=2.5)
        assert outcome.startswith("HTTP/1.1 401"), (
            f"legitimate client was not served while one idle connection was open: {outcome}")
    finally:
        idle.close()          # unblock the stalled accept loop so stop() cannot hang
        gw.stop()


# ---------------------------------------------------------------- D-04b
def test_d04b_harness_owner_written_pairing_window_is_redeemable(tmp_path):
    """Sanity: the PairingManager itself redeems a window written by the owner path."""
    state = tmp_path / "state"
    token = PairingManager(state).begin("phone").token
    assert PairingManager(state).redeem(token) == "phone"


@_xfail("D-04b", "an agent-created pairing_window.json (MEDIUM, autonomous) becomes a valid pairing window")
def test_d04b_agent_cannot_plant_a_pairing_window_in_the_state_dir():
    home = Path(os.path.expanduser("~"))              # sandboxed by the T0.1 fixture
    state = Config({}).state_dir()                    # the (sandboxed) ~/.void
    fa = FileActions(allowed_roots=[home], delete_to_recycle_bin=True)   # broad roots, like the owner's C:\
    planted = json.dumps({"token": "ATTACKER-CHOSEN-TOKEN", "name": "evil-phone",
                          "expires_at": time.time() + 3600})
    result = fa.write(str(state / "pairing_window.json"), planted)
    assert not result.ok, "write_file created a file inside V.O.I.D's own state directory"
    with pytest.raises(PairingError):
        PairingManager(state).redeem("ATTACKER-CHOSEN-TOKEN")


# ---------------------------------------------------------------- D-05
def _open_path_tool():
    home = Path(os.path.expanduser("~"))
    fa = FileActions(allowed_roots=[home], delete_to_recycle_bin=True)
    return {t.name: t for t in AppActions(fa).tools()}["open_path"]


def test_d05_harness_open_path_tool_is_reachable_and_low_for_a_plain_url():
    assert _open_path_tool().effective_risk({"target": "https://example.com/"}) == RiskLevel.LOW


@_xfail("D-05", "open_path rates a data-carrying URL LOW, so an injected instruction can exfiltrate via the query string")
def test_d05_open_path_high_entropy_query_is_not_low_risk():
    url = "https://attacker.example/collect?d=" + "Zk3v9QpX1LmT7uYb4NcR8eWa2SdF6gHj" * 2
    risk = _open_path_tool().effective_risk({"target": url})
    assert risk >= RiskLevel.HIGH, f"open_path rated a data-carrying URL {risk.name}"


# ---------------------------------------------------------------- D-07
class _Cloud(LLMProvider):
    name = "gemini"
    calls = 0

    def available(self):
        return True                                   # what V1 reports: SDK + key present

    def generate(self, messages, tools=None):
        _Cloud.calls += 1
        raise ConnectionError("network unreachable (simulated)")


class _Local(LLMProvider):
    name = "local"
    calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        _Local.calls += 1
        return LLMResponse(text="done locally")


def _run_agent_with_dead_cloud(monkeypatch, tmp_path):
    _Cloud.calls = 0
    _Local.calls = 0
    monkeypatch.setattr("void.core.agent.time.sleep", lambda s: None)   # V1 sleeps 1+2+4 s
    reg = ProviderRegistry({"gemini": _Cloud(), "local": _Local()}, ["gemini", "local"])
    agent = Agent(reg.select(), ToolRegistry(), RiskGate(), KillSwitch(),
                  TaskStore(tmp_path / "t.sqlite"), defer_confirmation=True)
    return agent.run("hello")


def test_d07_harness_dead_cloud_is_actually_attempted(monkeypatch, tmp_path):
    _run_agent_with_dead_cloud(monkeypatch, tmp_path)
    assert _Cloud.calls >= 1


@_xfail("D-07", "select() runs once per run; the dead cloud is retried 3x and the task FAILS while the local provider is never used")
def test_d07_unreachable_cloud_fails_over_instead_of_failing_the_task(monkeypatch, tmp_path):
    result = _run_agent_with_dead_cloud(monkeypatch, tmp_path)
    assert _Local.calls >= 1 or result.status == Status.PAUSED, (
        f"no failover: cloud attempts={_Cloud.calls}, local calls={_Local.calls}, status={result.status}")


# ---------------------------------------------------------------- D-09
def _unauthenticated_body(i):
    return json.dumps({"protocol": 1, "request_id": f"r{i}", "device_id": f"attacker-{i}",
                       "operation": "get_status", "parameters": {}, "timestamp": time.time()}).encode()


def test_d09_harness_unauthenticated_request_is_rejected_401(tmp_path):
    gw = _make_gateway(tmp_path, start=False)
    status, _ = gw.handle_request(_unauthenticated_body(0), "sig", "203.0.113.9")
    assert status == 401


@_xfail("D-09", "the request limiter keeps one key per unauthenticated device_id forever")
def test_d09_limiter_state_is_bounded_under_unauthenticated_load(tmp_path):
    gw = _make_gateway(tmp_path, start=False)
    for i in range(3000):
        gw.handle_request(_unauthenticated_body(i), "sig", "203.0.113.9")
    keys = len(gw._request_limiter._events)
    assert keys <= 2048, f"limiter holds {keys} keys after 3000 unauthenticated requests"


@_xfail("D-09", "every unauthenticated rejection writes its own WARNING (non-rotating log flood)")
def test_d09_rejection_logging_is_sampled(tmp_path, caplog):
    gw = _make_gateway(tmp_path, start=False)
    with caplog.at_level(logging.WARNING, logger="void.device.gateway"):
        for i in range(2000):
            gw.handle_request(_unauthenticated_body(i), "sig", "203.0.113.9")
    n = sum(1 for r in caplog.records if r.name == "void.device.gateway")
    assert n <= 100, f"{n} log records for 2000 unauthenticated requests"


# ---------------------------------------------------------------- D-13
class _RecTTS(TTS):
    def __init__(self):
        self.spoke = []

    @property
    def is_speaking(self):
        return False

    def speak(self, text):
        self.spoke.append(text)

    def stop(self):
        pass


class _NullCapture:
    is_open = False

    def open(self): pass
    def stop(self): return []
    def close(self): pass


class _FakeResult:
    def __init__(self, status, result, error=None):
        self.status = status
        self.result = result
        self.task = types.SimpleNamespace(error=error)


class _NullSTT(STT):
    def transcribe(self, audio):
        return "x"


def _dispatch(result):
    tts = _RecTTS()
    session = VoiceSession(types.SimpleNamespace(run=lambda transcript: result), KillSwitch(),
                           capture=_NullCapture(), stt=_NullSTT(), tts=tts)
    session._last_transcript = "do something"
    session._state = "dispatched"
    session._run_dispatch(session.generation)
    return tts.spoke


def test_d13_harness_completed_text_is_spoken():
    assert _dispatch(_FakeResult(Status.COMPLETED, "Launched Notepad.")) == ["Launched Notepad."]


@pytest.mark.parametrize("status", [Status.AWAITING_CONFIRMATION, Status.BLOCKED,
                                    Status.FAILED, Status.PAUSED])
@_xfail("D-13", "the voice runtime speaks nothing when a task is awaiting approval / blocked / failed / paused")
def test_d13_non_completed_outcomes_speak_a_constant_phrase(status):
    spoke = _dispatch(_FakeResult(status, None, error="SECRET-ERROR-TEXT"))
    assert len(spoke) == 1, f"expected one spoken status phrase, got {spoke!r}"
    assert "SECRET-ERROR-TEXT" not in spoke[0]
