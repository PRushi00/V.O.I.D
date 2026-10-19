"""A browser V.O.I.D owns: fresh profile, loopback debugging port, closed and deleted afterwards.

Why a second browser path exists at all, when ``void/browser/`` is already V.O.I.D's browser layer:
those two layers want opposite things. The existing one deliberately **attaches to the owner's
running browser**, because reusing their real session is the capability - that is what makes "open the
tab I was reading" work. A teardown must do the reverse. It needs a browser with none of the owner's
cookies or logins in it, and it needs one whose cache is empty, because the REA pack's own measurement
is the argument: with a reused profile `igloo.inc` weighed 0.4 MB and with a fresh one 14.7 MB. A
reused browser lies about weight.

So this module launches its own, and the rules it follows are all from that one fact:

**A brand-new profile directory per job, deleted afterwards.** Never the owner's profile, never a
reused one. The teardown runs in a ``finally`` that terminates the process and removes the directory,
and it reports a cleanup that failed rather than swallowing it - a teardown must never leave a second
browser running.

**Observation only.** The DevTools client has exactly two jobs: ``Runtime.evaluate`` for read-only
page facts, and ``Input.dispatchMouseEvent`` with a wheel delta to scroll. It cannot click, type,
navigate or submit, because those methods are not implemented - the pack's "REA observes, it never
drives" enforced by absence rather than by policy. Scrolling uses wheel events rather than
``scrollTo`` because smooth-scroll libraries listen for the wheel and a programmatic jump fires none
of the effects a teardown exists to watch.

**Standard library only.** The WebSocket client below is about a hundred lines and adds no dependency,
which is the same trade V.O.I.D already makes for its BM25 index. Playwright is a heavier tool aimed
at a different job, and it is already in the project for that job.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

#: Loopback port the investigation browser listens on. Loopback only, and a port V.O.I.D's own
#: device gateway does not use.
DEBUG_PORT = 9333

#: Window the browser opens at. A fixed size so two runs of the same site are comparable, and large
#: enough that a desktop layout renders rather than a mobile one.
WINDOW = (1280, 820)

#: How long to wait for the browser to answer /json/version, and how long for the page to settle.
BROWSER_READY_S = 25.0
LOAD_TIMEOUT_S = 30.0
#: Extra settle after ``readyState`` is complete: an intro animation and a first render are not the
#: same thing as a loaded document, and REA sees nothing that has not happened yet.
INTRO_SETTLE_S = 2.5

#: One wheel step, and how many a scroll pass sends. Sixteen steps is roughly a long page.
WHEEL_DELTA = 120
SCROLL_STEPS = 16
SCROLL_PAUSE_S = 0.12

#: Hard ceiling on a single DevTools reply. A page can hold a very large bundle, and a teardown must
#: not be a way to exhaust memory on the owner's machine.
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: Where investigation profiles live. Outside the project and outside the owner's browser data.
PROFILE_ROOT = Path.home() / ".void" / "teardown-profiles"

#: Candidate browser executables, in the order they are tried. Chromium-family only: the DevTools
#: protocol is what this module speaks.
CHROME_CANDIDATES = (
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux, and the Playwright-managed build when one is present
    "/opt/pw-browsers/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

#: Environment variable an owner can set to name a browser explicitly.
CHROME_ENV = "VOID_TEARDOWN_CHROME"


class TeardownBrowserError(RuntimeError):
    """The investigation browser could not be started, reached, or read."""


def find_chrome() -> str | None:
    """Path to a Chromium-family browser, or None. ``VOID_TEARDOWN_CHROME`` wins."""
    explicit = os.environ.get(CHROME_ENV, "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ======================================================================== the WebSocket client

def _host_of(url: object) -> str:
    """The host of a URL, accepting a bare host too ("example.com", "127.0.0.1:8731").

    A bare host has no scheme, so ``urlsplit`` reads the whole thing as a path and reports no
    hostname at all - which silently matched every target rather than none.
    """
    text = str(url or "").strip()
    host = urlsplit(text).hostname or urlsplit("//" + text.lstrip("/")).hostname or ""
    return host.lower().removeprefix("www.")


def _mask(payload: bytes) -> bytes:
    key = os.urandom(4)
    masked = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return key + masked


class _WebSocket:
    """The smallest client that can speak CDP: text frames out, text frames in.

    Deliberately not a general WebSocket implementation. It sends masked text frames (which the
    protocol requires of a client), reassembles continuation frames, answers pings, and ignores
    everything else. It is enough because CDP is a request/response protocol over text.
    """

    def __init__(self, url: str, timeout_s: float = 20.0):
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise TeardownBrowserError(f"Expected a ws:// debugger URL, got {parts.scheme!r}.")
        host = parts.hostname or "127.0.0.1"
        if host not in ("127.0.0.1", "localhost", "::1"):
            # The debugger is ours and is on loopback. Anything else is not a browser we launched.
            raise TeardownBrowserError("The debugger URL is not on loopback.")
        self._sock = socket.create_connection((host, parts.port or 80), timeout=timeout_s)
        self._sock.settimeout(timeout_s)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self._sock.sendall(handshake.encode())
        header = self._read_until(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise TeardownBrowserError("The browser refused the debugger handshake.")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise TeardownBrowserError("The debugger connection closed during the handshake.")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head + marker

    def _recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            if count > MAX_FRAME_BYTES:
                raise TeardownBrowserError("A debugger frame exceeded the size cap.")
            chunk = self._sock.recv(min(1 << 20, count - len(self._buffer)))
            if not chunk:
                raise TeardownBrowserError("The debugger connection closed.")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        self._sock.sendall(header + _mask(payload))

    def _frame(self) -> tuple[int, bytes]:
        first, second = struct.unpack("!BB", self._recv_exact(2))
        opcode, final = first & 0x0F, bool(first & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_FRAME_BYTES:
            raise TeardownBrowserError("A debugger frame exceeded the size cap.")
        if second & 0x80:                              # a server frame must not be masked
            self._recv_exact(4)
        return (opcode if final else -opcode), self._recv_exact(length)

    def recv(self) -> str:
        """One complete text message, reassembling continuation frames."""
        parts: list[bytes] = []
        while True:
            opcode, payload = self._frame()
            if opcode == 0x9:                          # ping -> pong, or the browser hangs up
                self._sock.sendall(struct.pack("!BB", 0x8A, 0x80 | len(payload)) + _mask(payload))
                continue
            if opcode == 0xA:                          # pong
                continue
            if opcode == 0x8:
                raise TeardownBrowserError("The debugger closed the connection.")
            parts.append(payload)
            if opcode >= 0:                            # final frame of the message
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.sendall(struct.pack("!BB", 0x88, 0x80) + _mask(b""))
        with contextlib.suppress(OSError):
            self._sock.close()


# ======================================================================== the page client

@dataclass
class PageFacts:
    """What a read-only pass over the page established."""

    url: str = ""
    title: str = ""
    elements: int = 0
    height_screens: float = 0.0
    load_s: float = 0.0
    #: Every resource the page loaded, from the browser's own timing log: see ``resources``.
    resources: list[dict] = field(default_factory=list)


class InvestigationPage:
    """A CDP connection to one page, with two verbs: evaluate, and scroll.

    There is no click, no type, no navigate and no submit on this class. That is the enforcement of
    "observe, never drive": a caller cannot drive the page because the methods do not exist.
    """

    def __init__(self, websocket_url: str, timeout_s: float = 20.0):
        self._ws = _WebSocket(websocket_url, timeout_s=timeout_s)
        self._next_id = 0

    def _call(self, method: str, **params) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise TeardownBrowserError(
                        f"{method}: {message['error'].get('message', 'failed')}")
                return message.get("result", {})
            # Anything else is an event from the page; a read-only pass has no use for them.

    def evaluate(self, expression: str):
        """Evaluate a read-only expression and return its JSON value."""
        result = self._call("Runtime.evaluate", expression=expression, returnByValue=True,
                            awaitPromise=True)
        payload = result.get("result", {})
        if payload.get("subtype") == "error":
            raise TeardownBrowserError(f"The page raised: {payload.get('description', '')[:200]}")
        return payload.get("value")

    def wait_for_load(self, timeout_s: float = LOAD_TIMEOUT_S) -> float:
        """Block until the document is complete, plus the intro settle. Returns seconds waited."""
        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            with contextlib.suppress(TeardownBrowserError):
                if self.evaluate("document.readyState") == "complete":
                    time.sleep(INTRO_SETTLE_S)
                    return time.monotonic() - started
            time.sleep(0.25)
        raise TeardownBrowserError(f"The page did not finish loading within {timeout_s:.0f}s.")

    def scroll(self, steps: int = SCROLL_STEPS, *, up: bool = False) -> None:
        """Scroll with wheel events, which is what smooth-scroll libraries listen for."""
        delta = -WHEEL_DELTA if up else WHEEL_DELTA
        for _ in range(max(1, int(steps))):
            with contextlib.suppress(TeardownBrowserError):
                self._call("Input.dispatchMouseEvent", type="mouseWheel",
                           x=WINDOW[0] // 2, y=WINDOW[1] // 2, deltaX=0, deltaY=delta)
            time.sleep(SCROLL_PAUSE_S)

    def facts(self) -> PageFacts:
        """The page's own description of itself. Every value comes from the page, so it is untrusted."""
        raw = self.evaluate(
            "({url: location.href, title: document.title,"
            " elements: document.getElementsByTagName('*').length,"
            " height: Math.round((document.documentElement.scrollHeight ||0)"
            " / Math.max(1, window.innerHeight) * 10) / 10})") or {}
        return PageFacts(url=str(raw.get("url", ""))[:2000], title=str(raw.get("title", ""))[:300],
                         elements=int(raw.get("elements") or 0),
                         height_screens=float(raw.get("height") or 0.0))

    def resources(self) -> list[dict]:
        """Every file the page loaded, from the browser's own resource timing log.

        This is the gap the REA pack reports hitting first: REA attaches to a page that has already
        loaded and so reports zero network requests, while the browser's own log has all of them.
        Both are read, and the note says which saw what.
        """
        raw = self.evaluate(
            "performance.getEntriesByType('resource').map(e => ({"
            " name: e.name, kind: e.initiatorType,"
            " size: e.transferSize || e.encodedBodySize || 0 }))") or []
        out = []
        for entry in raw if isinstance(raw, list) else []:
            name = str((entry or {}).get("name", ""))[:500]
            if not name:
                continue
            out.append({"url": name, "kind": str(entry.get("kind", ""))[:40],
                        "bytes": int(entry.get("size") or 0),
                        "origin": (urlsplit(name).hostname or "")[:200]})
        return out

    def scripts(self) -> list[str]:
        """Absolute URLs of the page's own script files, for the static read."""
        raw = self.evaluate(
            "Array.from(document.scripts).map(s => s.src).filter(Boolean)") or []
        return [str(item)[:500] for item in (raw if isinstance(raw, list) else [])][:60]

    def close(self) -> None:
        self._ws.close()


# ======================================================================== the browser itself

@dataclass
class Launched:
    """A running investigation browser."""

    process: subprocess.Popen
    profile: Path
    port: int
    job_id: str


class CleanBrowser:
    """Launch a browser with a brand-new profile, and always take it down again.

    Used as a context manager. The teardown happens in a ``finally``: the process is terminated, then
    killed if it will not go, and the profile directory is removed. :attr:`cleanup_error` is set when
    any of that fails, so a caller can report it rather than leave the owner with a stray browser and
    no idea it is there.
    """

    def __init__(self, chrome: str | None = None, *, headless: bool = True,
                 port: int = DEBUG_PORT, profile_root: Path | None = None):
        self.chrome = chrome or find_chrome()
        self.headless = bool(headless)
        self.port = int(port)
        self.profile_root = Path(profile_root) if profile_root else PROFILE_ROOT
        self.launched: Launched | None = None
        self.cleanup_error = ""

    # --- lifecycle -----------------------------------------------------

    def __enter__(self) -> "CleanBrowser":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self, url: str) -> Launched:
        if not self.chrome:
            raise TeardownBrowserError(
                "No Chromium-family browser was found. Install Chrome, Edge or Chromium, or set "
                f"{CHROME_ENV} to its path.")
        job_id = uuid.uuid4().hex[:12]
        profile = self.profile_root / job_id
        profile.mkdir(parents=True, exist_ok=True)
        argv = [self.chrome,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--mute-audio",
                "--no-service-autorun", "--password-store=basic", "--use-mock-keychain",
                f"--window-size={WINDOW[0]},{WINDOW[1]}"]
        if self.headless:
            argv += ["--headless=new", "--disable-gpu"]
        if os.name != "nt" and os.geteuid() == 0:      # a root container has no sandbox to use
            argv.append("--no-sandbox")
        argv.append(url)
        _log.info("TEARDOWN_BROWSER_LAUNCH headless=%s port=%d", self.headless, self.port)
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.launched = Launched(process=process, profile=profile, port=self.port, job_id=job_id)
        self._wait_ready()
        return self.launched

    def _wait_ready(self) -> dict:
        deadline = time.monotonic() + BROWSER_READY_S
        last = ""
        while time.monotonic() < deadline:
            if self.launched and self.launched.process.poll() is not None:
                raise TeardownBrowserError(
                    f"The browser exited immediately (code {self.launched.process.returncode}).")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = type(exc).__name__
                time.sleep(0.25)
        raise TeardownBrowserError(
            f"The browser did not answer its debugger port within {BROWSER_READY_S:.0f}s ({last}).")

    def version(self) -> dict:
        return self._wait_ready()

    def page_target(self, url: str) -> dict:
        """The page target for ``url``'s host, allowing www and redirects.

        Matched on host rather than on the exact address because a site may redirect, add a locale
        prefix or append a query - and because a target picked by index is whichever tab happened to
        be created first.
        """
        wanted = _host_of(url)
        deadline = time.monotonic() + BROWSER_READY_S
        while time.monotonic() < deadline:
            for target in self.targets():
                if target.get("type") != "page":
                    continue
                host = _host_of(target.get("url", ""))
                if wanted and (host == wanted or wanted in host or host in wanted):
                    return target
            time.sleep(0.4)
        raise TeardownBrowserError(f"No page target for {wanted or url} appeared.")

    def targets(self) -> list[dict]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=5) as response:
                found = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TeardownBrowserError(f"The debugger port could not be listed: {exc}") from exc
        return found if isinstance(found, list) else []

    def page(self, url: str) -> InvestigationPage:
        target = self.page_target(url)
        socket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not socket_url:
            raise TeardownBrowserError("The page target has no debugger URL.")
        return InvestigationPage(socket_url)

    def close(self) -> None:
        """Terminate the browser and delete its profile. Records, never raises."""
        launched, self.launched = self.launched, None
        if launched is None:
            return
        problems = []
        try:
            launched.process.terminate()
            try:
                launched.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                launched.process.kill()
                launched.process.wait(timeout=5)
        except Exception as exc:
            problems.append(f"the browser process could not be stopped: {exc}")
        try:
            shutil.rmtree(launched.profile, ignore_errors=False)
        except OSError as exc:
            problems.append(f"the profile directory {launched.profile} could not be removed: {exc}")
        if launched.profile.exists():
            problems.append(f"the profile directory {launched.profile} is still present")
        self.cleanup_error = "; ".join(problems)
        if problems:
            _log.warning("TEARDOWN_CLEANUP_INCOMPLETE problems=%d", len(problems))


def stray_profiles(profile_root: Path | None = None) -> list[str]:
    """Investigation profiles left on disk by a crashed run. A preflight check reads this."""
    root = Path(profile_root) if profile_root else PROFILE_ROOT
    if not root.is_dir():
        return []
    return sorted(str(child) for child in root.iterdir() if child.is_dir())
