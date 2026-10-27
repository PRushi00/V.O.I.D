"""Prove that nothing left this machine, instead of saying so.

V.O.I.D's privacy argument is made in a dozen places: the screen watch has no provider to call, the
invoice engine imports no transport, the focus organ cannot hold a window title. Each of those is
defended by a test over ONE module. None of them answers the owner's actual question, which is about
the whole program at once: *when I asked that, did anything leave this machine?*

Sixteen modules in this tree can open a connection - the providers, research, websites, the browser,
the device gateway, the MCP adapter, the teardown kit. Auditing them one at a time proves nothing
about the seventeenth, and the seventeenth is the one somebody adds next year.

So this module does not audit call sites. It records the only thing every one of them must do to
reach anything at all: **open a socket.** A hostname resolved in `urllib`, a TLS session set up by
`ssl`, a websocket, a library this tree has never heard of - all of them end at
``socket.socket.connect``, and that is the single place this watches.

Four properties, each one deliberate:

**It is off by default.** Installing a process-wide socket hook is a real change to how a 92k-line
program behaves, and a privacy tool that introduces its own risk is a bad trade. Nothing here runs
until something asks: the drill, ``void preflight``, or the owner through ``void egress``.

**It can never break a connection.** Every recording path is wrapped and the real call is made
either way. A watcher that could turn a working assistant into a broken one would be removed within
a week, and then nothing would be watched at all. The same rule the teardown panel follows: an
observer may fail, but never take its subject down with it.

**It says why, without anyone having to tell it.** The reason is read from the call stack - the
first frame belonging to this tree - so no call site had to be edited to be covered, and none can
drift out of coverage by being forgotten. :func:`reason` is there for the cases where a caller can
say something more useful than its own module name.

**The honest list is read from the code, not from memory.** :func:`needs_network` walks the syntax
trees of this tree to find what still reaches outward. It is deliberately not a hand-written list,
because a hand-written list is accurate on the day it is written and quietly wrong afterwards - and
because these files are full of prose about what they do not do, which is why this walks the AST
rather than grepping the text. An earlier generation of structural tests in this project failed on
their own docstrings; that lesson is applied here from the start.

What this is not: a firewall. It observes and reports. Deciding what is *allowed* to leave is the
risk gate's job and the owner's, and a watcher that quietly started blocking things would be making
a security decision nobody asked it to make.
"""
from __future__ import annotations

import ast
import contextlib
import contextvars
import ipaddress
import logging
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

#: How many connections the ledger keeps. An assistant that runs for a week must not grow a list for
#: ever, and the drill only ever asks about a single turn.
MAX_CONNECTIONS = 2048

#: How many name-to-address answers are remembered, so a report can say "api.anthropic.com" instead
#: of an address the owner has no way to recognise.
MAX_NAMES = 512

#: How far up the stack to look for a frame belonging to this tree. Bounded because this runs on
#: every connection, and an unbounded walk through a deep stack is a cost paid for nothing.
MAX_FRAMES = 40

#: A connection to a path on this filesystem, which has no host and reaches no wire.
UNIX_SOCKET = "unix-socket"
UNKNOWN = "unknown"

_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

_reason: contextvars.ContextVar[str] = contextvars.ContextVar("void_egress_reason", default="")


def is_loopback(host: str) -> bool:
    """True when ``host`` names this machine, so a packet to it never reaches a wire."""
    if not host:
        return False
    name = host.strip().lower()
    if name == UNIX_SOCKET or name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        # Not an address and not a name for this machine. Something had to resolve it to reach
        # anything, so it counts as off-machine: guessing in the permissive direction is exactly
        # the mistake this module exists to make impossible.
        return False


@dataclass(frozen=True)
class Connection:
    """One socket this process opened."""

    host: str
    port: int
    reason: str = ""
    module: str = UNKNOWN
    #: The hostname, when a lookup for this address was seen. Addresses are what `connect` is given;
    #: the name is what the owner recognises.
    name: str = ""

    @property
    def off_machine(self) -> bool:
        return not is_loopback(self.host)

    @property
    def label(self) -> str:
        return self.name or self.host

    def line(self) -> str:
        where = self.reason or self.module
        port = f":{self.port}" if self.port else ""
        return f"{self.label}{port}  ({where})"


class Ledger:
    """Every connection seen, newest last, bounded and safe to write from any thread."""

    def __init__(self, limit: int = MAX_CONNECTIONS):
        self._limit = max(1, int(limit))
        self._items: list[Connection] = []
        self._dropped = 0
        self._lock = threading.Lock()

    def record(self, conn: Connection) -> None:
        with self._lock:
            self._items.append(conn)
            if len(self._items) > self._limit:
                # Drop the OLDEST. A drill asks what just happened, so the recent end is the end
                # worth keeping, and the count is reported rather than silently losing history.
                overflow = len(self._items) - self._limit
                del self._items[:overflow]
                self._dropped += overflow

    def all(self) -> list[Connection]:
        with self._lock:
            return list(self._items)

    def off_machine(self) -> list[Connection]:
        return [c for c in self.all() if c.off_machine]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._dropped = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@contextlib.contextmanager
def reason(label: str):
    """Name why the connections opened inside this block are being made.

    Entirely optional - without it the watcher uses the calling module, which is why no call site in
    this tree had to be edited. Use it where a module name is not the useful answer ("the vault's
    nightly re-index" says more than "void.vault").
    """
    token = _reason.set(str(label))
    try:
        yield
    finally:
        _reason.reset(token)


def _calling_module() -> str:
    """The first frame belonging to this tree, which is the thing actually making the call."""
    try:
        frame = sys._getframe(1)
    except ValueError:                      # pragma: no cover - no stack to walk
        return UNKNOWN
    seen = 0
    while frame is not None and seen < MAX_FRAMES:
        name = frame.f_globals.get("__name__", "")
        if name == "void" or (name.startswith("void.") and not name.startswith("void.egress")):
            return name
        frame = frame.f_back
        seen += 1
    return UNKNOWN


def _address_of(address) -> tuple[str, int]:
    """``connect``'s argument, as a host and a port.

    AF_INET gives ``(host, port)``, AF_INET6 gives a four-tuple, AF_UNIX gives a path, and a path
    reaches nothing off this machine.
    """
    if isinstance(address, (str, bytes, Path)):
        return UNIX_SOCKET, 0
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 and isinstance(address[1], int) else 0
        return (str(host) if host is not None else UNKNOWN), port
    return UNKNOWN, 0


class Watcher:
    """Records every socket this process opens, for as long as it is started."""

    def __init__(self, ledger: Ledger | None = None):
        self.ledger = ledger if ledger is not None else Ledger()
        self._lock = threading.Lock()
        self._installed = False
        self._real_connect = None
        self._real_connect_ex = None
        self._real_getaddrinfo = None
        self._names: dict[str, str] = {}
        self._names_lock = threading.Lock()

    # -- the name map ----------------------------------------------------------------------
    def _remember(self, name: str, results) -> None:
        """Keep the address a name resolved to, so the ledger can print the name."""
        if not name:
            return
        with self._names_lock:
            for entry in results or ():
                try:
                    address = entry[4][0]
                except (IndexError, TypeError):
                    continue
                if not address:
                    continue
                if len(self._names) >= MAX_NAMES and str(address) not in self._names:
                    # Full. Keeping the first answers is as defensible as keeping the last, and it
                    # costs nothing; the label falls back to the address, which is still true.
                    continue
                self._names[str(address)] = str(name)

    def name_for(self, host: str) -> str:
        with self._names_lock:
            return self._names.get(host, "")

    # -- installation ----------------------------------------------------------------------
    def start(self) -> "Watcher":
        """Begin recording. Idempotent: starting twice installs one hook, not two."""
        with self._lock:
            if self._installed:
                return self
            self._real_connect = socket.socket.connect
            self._real_connect_ex = socket.socket.connect_ex
            self._real_getaddrinfo = socket.getaddrinfo
            socket.socket.connect = self._wrap(self._real_connect)
            socket.socket.connect_ex = self._wrap(self._real_connect_ex)
            socket.getaddrinfo = self._wrap_lookup(self._real_getaddrinfo)
            self._installed = True
        return self

    def stop(self) -> None:
        """Put the real socket back. Safe to call when not started."""
        with self._lock:
            if not self._installed:
                return
            socket.socket.connect = self._real_connect
            socket.socket.connect_ex = self._real_connect_ex
            socket.getaddrinfo = self._real_getaddrinfo
            self._installed = False

    @property
    def installed(self) -> bool:
        with self._lock:
            return self._installed

    def __enter__(self) -> "Watcher":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- the hooks -------------------------------------------------------------------------
    def _wrap_lookup(self, real):
        def getaddrinfo(host, port, *args, **kwargs):
            results = real(host, port, *args, **kwargs)
            try:
                self._remember(str(host) if host else "", results)
            except Exception:                   # noqa: BLE001 - a lookup must never fail because
                _log.debug("EGRESS_NAME_RECORD_FAILED", exc_info=True)   # we wanted to label it
            return results
        return getaddrinfo

    def _wrap(self, real):
        def connect(sock, address, *args, **kwargs):
            # Record BEFORE connecting. A connection that is refused still tried to leave, and an
            # attempt that failed is exactly as interesting to the owner as one that succeeded.
            try:
                self._record(address)
            except Exception:                   # noqa: BLE001 - see the module docstring: an
                _log.debug("EGRESS_RECORD_FAILED", exc_info=True)   # observer never breaks its subject
            return real(sock, address, *args, **kwargs)
        return connect

    def _record(self, address) -> None:
        host, port = _address_of(address)
        self.ledger.record(Connection(
            host=host,
            port=port,
            reason=_reason.get(""),
            module=_calling_module(),
            name=self.name_for(host),
        ))


@dataclass(frozen=True)
class Drill:
    """What happened while something ran."""

    connections: tuple[Connection, ...]
    dropped: int = 0

    @property
    def escaped(self) -> tuple[Connection, ...]:
        return tuple(c for c in self.connections if c.off_machine)

    @property
    def passed(self) -> bool:
        """True only when nothing left this machine."""
        return not self.escaped

    def hosts(self) -> list[str]:
        """Every off-machine host, once each, in the order first seen."""
        seen: list[str] = []
        for c in self.escaped:
            if c.label not in seen:
                seen.append(c.label)
        return seen

    def report(self) -> str:
        if self.passed:
            return "Nothing left this machine."
        lines = [f"{len(self.escaped)} connection(s) left this machine:"]
        lines.extend(f"  {c.line()}" for c in self.escaped)
        return "\n".join(lines)


def drill(run, *args, **kwargs) -> tuple[Drill, object]:
    """Run ``run`` under a fresh watcher and report what it opened.

    Returns the drill and whatever ``run`` returned, so a caller can check both what happened and
    what came back. Exceptions from ``run`` propagate - a drill that swallowed the failure would be
    reporting on something that did not happen.
    """
    watcher = Watcher()
    with watcher:
        result = run(*args, **kwargs)
    return Drill(tuple(watcher.ledger.all()), watcher.ledger.dropped), result


# ------------------------------------------------------------------------------------------------
# The honest list: what in this tree still reaches outward, read from the code rather than recalled.
# ------------------------------------------------------------------------------------------------

#: An import that gives a module the ability to open a connection, and what to tell the owner about
#: it. ``socket`` and ``ssl`` are here because a module that imports them can reach the network
#: whether or not it currently does - the list answers "what COULD leave", which is the question
#: worth asking before the Wi-Fi goes off.
NETWORK_IMPORTS = {
    "urllib.request": "opens URLs",
    "urllib": "opens URLs",
    "http.client": "speaks HTTP itself",
    "requests": "the requests library",
    "httpx": "the httpx library",
    "aiohttp": "the aiohttp library",
    "websocket": "a websocket client",
    "websockets": "a websocket client",
    "socket": "opens sockets directly",
    "ssl": "wraps a socket in TLS",
    "ftplib": "speaks FTP",
    "smtplib": "sends mail",
    "telnetlib": "speaks telnet",
}

_URL = __import__("re").compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://([^/\s\"'<>:)\]}]+)")


@dataclass(frozen=True)
class Feature:
    """One module in this tree that can reach off this machine."""

    module: str
    imports: tuple[str, ...]
    hosts: tuple[str, ...]

    def line(self) -> str:
        # Several imports can share a description (``urllib`` and ``urllib.request`` are both
        # "opens URLs"), and saying it twice reads like two findings instead of one.
        seen: list[str] = []
        for name in self.imports:
            why = NETWORK_IMPORTS.get(name, name)
            if why not in seen:
                seen.append(why)
        where = f" -> {', '.join(self.hosts)}" if self.hosts else ""
        return f"{self.module}: {', '.join(seen) or 'reaches outward'}{where}"


def _docstring_nodes(tree: ast.AST) -> set:
    """The ``id()`` of every string that is a docstring.

    These files explain at length what they do NOT send, and a URL inside that explanation is
    prose. Counting it would make the honest list dishonest in the one direction that matters.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _scan(path: Path, root: Path) -> Feature | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".")[0]
                if alias.name in NETWORK_IMPORTS:
                    imports.add(alias.name)
                elif root_name in NETWORK_IMPORTS:
                    imports.add(root_name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in NETWORK_IMPORTS:
                imports.add(node.module)
            else:
                root_name = node.module.split(".")[0]
                if root_name in NETWORK_IMPORTS:
                    imports.add(root_name)

    skip = _docstring_nodes(tree)
    hosts: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for match in _URL.findall(node.value):
            host = match.split("@")[-1]
            if host and not is_loopback(host) and "." in host:
                hosts.add(host)

    if not imports and not hosts:
        return None
    return Feature(_module_name(path, root), tuple(sorted(imports)), tuple(sorted(hosts)))


def needs_network(root: Path | str | None = None) -> list[Feature]:
    """Every module in this tree that can reach off this machine, derived from its syntax tree.

    This is the list the owner should be shown before being told "nothing leaves the machine",
    because it is the list of things that could make that false.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    out: list[Feature] = []
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # The watcher itself imports socket in order to watch it. Listing it as a feature that
        # needs the internet would be exactly backwards.
        if path.parent.name == "egress" and path.parent.parent == base:
            continue
        feature = _scan(path, base)
        if feature is not None:
            out.append(feature)
    return out
