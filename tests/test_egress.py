"""The egress watcher: prove what left this machine, and never break what did not.

V.O.I.D's privacy claims were each defended over a single module - the screen watch calls no
provider, the invoice engine imports no transport. This is the first one made about the whole
program, so these tests are about the three ways a watcher like it goes wrong:

* it misses something, because it watched call sites instead of the thing they all must do;
* it breaks the program it was meant to protect;
* it tells a comfortable lie, because its "honest list" was written by hand, or read out of its own
  prose rather than out of its code.

The socket hooks are exercised against a REAL HTTP server on loopback rather than a mock, because a
watcher that only works against a fake socket watches nothing. The off-machine DECISION is a pure
function and is tested as one - no test here binds a service to a routable interface.
"""
from __future__ import annotations

import http.server
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from void import egress
from void.egress import (MAX_FRAMES, MAX_NAMES, NETWORK_IMPORTS, UNIX_SOCKET, Connection, Drill,
                         Feature, Ledger, Watcher, drill, is_loopback, needs_network, reason)


# --------------------------------------------------------------------------------------------
# A real server on loopback. Nothing here is reachable from off this machine.
# --------------------------------------------------------------------------------------------
class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802 - BaseHTTPRequestHandler's name
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):                     # keep the test output readable
        return


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture
def watcher():
    """A started watcher that is always removed, even when the test fails part-way."""
    w = Watcher()
    w.start()
    try:
        yield w
    finally:
        w.stop()


# --------------------------------------------------------------------------------------------
# Loopback is a decision, not a guess
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "localhost", "LOCALHOST",
                                  "ip6-localhost", UNIX_SOCKET])
def test_these_hosts_never_reach_a_wire(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "example.com", "10.0.0.5", "192.168.1.9",
                                  "api.anthropic.com", "0.0.0.0", "::"])
def test_these_hosts_left_the_machine(host):
    assert is_loopback(host) is False


def test_a_private_lan_address_still_counts_as_leaving():
    """A packet to the router is still a packet off this machine. The honest answer is the strict
    one: only loopback stays."""
    assert is_loopback("192.168.0.1") is False
    assert Connection("192.168.0.1", 443).off_machine is True


def test_an_empty_host_is_not_quietly_called_safe():
    assert is_loopback("") is False


def test_a_name_that_is_not_an_address_is_treated_as_off_machine():
    """Guessing in the permissive direction is the one mistake this module exists to prevent."""
    assert is_loopback("some-host-we-cannot-classify") is False


# --------------------------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------------------------
def test_the_label_prefers_the_name_the_owner_would_recognise():
    assert Connection("160.79.104.10", 443, name="api.anthropic.com").label == "api.anthropic.com"
    assert Connection("160.79.104.10", 443).label == "160.79.104.10"


def test_the_line_names_the_reason_when_there_is_one_and_the_module_otherwise():
    assert "(the nightly re-index)" in Connection("x", 1, reason="the nightly re-index").line()
    assert "(void.research)" in Connection("x", 1, module="void.research").line()


def test_a_connection_is_frozen_so_a_record_cannot_be_edited_after_the_fact():
    conn = Connection("8.8.8.8", 53)
    with pytest.raises(Exception):
        conn.host = "127.0.0.1"                        # type: ignore[misc]


# --------------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------------
def test_the_ledger_keeps_what_it_is_given_in_order():
    ledger = Ledger()
    ledger.record(Connection("a", 1))
    ledger.record(Connection("b", 2))
    assert [c.host for c in ledger.all()] == ["a", "b"]


def test_the_ledger_drops_the_oldest_and_says_how_many():
    """A week-long run must not grow a list for ever, and a silent loss would be a lie by omission."""
    ledger = Ledger(limit=3)
    for i in range(6):
        ledger.record(Connection(str(i), i))
    assert [c.host for c in ledger.all()] == ["3", "4", "5"]
    assert ledger.dropped == 3
    assert len(ledger) == 3


def test_the_ledger_separates_what_left_from_what_did_not():
    ledger = Ledger()
    ledger.record(Connection("127.0.0.1", 80))
    ledger.record(Connection("8.8.8.8", 53))
    assert [c.host for c in ledger.off_machine()] == ["8.8.8.8"]


def test_clearing_resets_the_dropped_count_too():
    ledger = Ledger(limit=1)
    ledger.record(Connection("a", 1))
    ledger.record(Connection("b", 2))
    assert ledger.dropped == 1
    ledger.clear()
    assert len(ledger) == 0 and ledger.dropped == 0


def test_the_ledger_survives_many_threads_writing_at_once():
    """Connections are opened from the voice thread, the agent thread and the Qt timers."""
    ledger = Ledger(limit=10_000)

    def write():
        for i in range(200):
            ledger.record(Connection("127.0.0.1", i))

    threads = [threading.Thread(target=write) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ledger) == 1600


# --------------------------------------------------------------------------------------------
# The hook, against a real server
# --------------------------------------------------------------------------------------------
def test_a_real_http_request_is_recorded(watcher, server):
    urllib.request.urlopen(server, timeout=5).read()
    assert len(watcher.ledger) >= 1
    assert any(c.host == "127.0.0.1" for c in watcher.ledger.all())


def test_a_raw_socket_is_recorded_too(watcher, server):
    """Every library ends at connect(), which is why this watches connect() and not any library."""
    port = int(server.rsplit(":", 1)[1].rstrip("/"))
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
    finally:
        s.close()
    assert any(c.port == port for c in watcher.ledger.all())


def test_create_connection_is_recorded_without_being_hooked_separately(watcher, server):
    port = int(server.rsplit(":", 1)[1].rstrip("/"))
    socket.create_connection(("127.0.0.1", port), timeout=5).close()
    assert any(c.port == port for c in watcher.ledger.all())


def test_a_loopback_request_leaves_the_drill_passing(watcher, server):
    urllib.request.urlopen(server, timeout=5).read()
    assert watcher.ledger.off_machine() == []


def test_the_reason_reaches_the_record(watcher, server):
    with reason("the nightly re-index"):
        urllib.request.urlopen(server, timeout=5).read()
    assert any(c.reason == "the nightly re-index" for c in watcher.ledger.all())


def test_the_reason_is_restored_after_the_block(watcher, server):
    with reason("outer"):
        with reason("inner"):
            pass
        urllib.request.urlopen(server, timeout=5).read()
    assert any(c.reason == "outer" for c in watcher.ledger.all())


def test_a_connection_from_this_tree_is_attributed_to_its_module(watcher, server):
    """No call site was edited to be covered, so attribution has to come from the stack."""
    from void import cli                                # any module of this tree will do
    code = compile("import urllib.request as u; u.urlopen(url, timeout=5).read()", "<t>", "exec")
    eval(code, {"__name__": cli.__name__, "url": server})   # noqa: S307 - a frame with void's name
    assert any(c.module == "void.cli" for c in watcher.ledger.all())


def test_a_connection_from_outside_this_tree_is_not_attributed_to_it(watcher, server):
    urllib.request.urlopen(server, timeout=5).read()
    modules = {c.module for c in watcher.ledger.all()}
    assert not any(m.startswith("void.egress") for m in modules)


# --------------------------------------------------------------------------------------------
# The watcher must never break the program it watches
# --------------------------------------------------------------------------------------------
class _Hostile(Ledger):
    """A ledger that fails on every write, which is the worst thing recording can do."""

    def record(self, conn):
        raise RuntimeError("the ledger is broken")


def test_a_broken_recorder_does_not_break_the_connection(server):
    """An observer may fail. It may never take its subject down with it."""
    w = Watcher(ledger=_Hostile())
    with w:
        body = urllib.request.urlopen(server, timeout=5).read()
    assert body == b"ok"


def test_a_broken_recorder_does_not_break_a_name_lookup(server):
    w = Watcher(ledger=_Hostile())
    with w:
        assert socket.getaddrinfo("127.0.0.1", 80)


def test_the_real_socket_is_put_back_on_stop():
    before = socket.socket.connect
    w = Watcher()
    w.start()
    assert socket.socket.connect is not before
    w.stop()
    assert socket.socket.connect is before


def test_starting_twice_installs_one_hook_not_two():
    """A double install would double every record and a single stop would leave one behind."""
    before = socket.socket.connect
    w = Watcher()
    w.start()
    hooked = socket.socket.connect
    w.start()
    assert socket.socket.connect is hooked
    w.stop()
    assert socket.socket.connect is before


def test_stopping_a_watcher_that_never_started_is_safe():
    before = socket.socket.connect
    Watcher().stop()
    assert socket.socket.connect is before


def test_the_context_manager_removes_the_hook_even_when_the_body_raises():
    before = socket.socket.connect
    w = Watcher()
    with pytest.raises(ValueError):
        with w:
            raise ValueError("boom")
    assert socket.socket.connect is before
    assert w.installed is False


def test_getaddrinfo_still_returns_what_it_always_returned(watcher):
    real = socket.getaddrinfo("127.0.0.1", 80)
    assert real and real[0][4][0] == "127.0.0.1"


def test_a_resolved_name_becomes_the_label(watcher, server):
    socket.getaddrinfo("localhost", 80)
    assert watcher.name_for("127.0.0.1") in ("localhost", "")


def test_the_name_map_is_bounded():
    w = Watcher()
    for i in range(MAX_NAMES + 50):
        w._remember(f"host-{i}", [(None, None, None, None, (f"10.0.{i // 256}.{i % 256}", 0))])
    assert len(w._names) <= MAX_NAMES


# --------------------------------------------------------------------------------------------
# The drill
# --------------------------------------------------------------------------------------------
def test_the_drill_returns_what_the_work_returned(server):
    result, value = drill(lambda: urllib.request.urlopen(server, timeout=5).read())
    assert value == b"ok"
    assert result.passed is True


def test_the_drill_does_not_swallow_a_failure():
    """A drill that hid the exception would be reporting on something that did not happen."""
    def boom():
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        drill(boom)


def test_the_drill_removes_its_hook_even_when_the_work_raises():
    before = socket.socket.connect
    with pytest.raises(RuntimeError):
        drill(lambda: (_ for _ in ()).throw(RuntimeError("no")))
    assert socket.socket.connect is before


def test_a_drill_passes_only_when_nothing_left():
    assert Drill((Connection("127.0.0.1", 80),)).passed is True
    assert Drill((Connection("8.8.8.8", 53),)).passed is False


def test_the_drill_lists_each_escaped_host_once_in_the_order_first_seen():
    result = Drill((Connection("8.8.8.8", 53), Connection("127.0.0.1", 80),
                    Connection("1.1.1.1", 53), Connection("8.8.8.8", 53)))
    assert result.hosts() == ["8.8.8.8", "1.1.1.1"]


def test_the_report_says_so_plainly_either_way():
    assert Drill(()).report() == "Nothing left this machine."
    bad = Drill((Connection("8.8.8.8", 53, reason="research"),)).report()
    assert "8.8.8.8" in bad and "research" in bad


def test_a_drill_over_nothing_passes():
    result, _ = drill(lambda: None)
    assert result.passed and result.connections == ()


# --------------------------------------------------------------------------------------------
# The honest list, derived rather than remembered
# --------------------------------------------------------------------------------------------
def _tree(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "void"
    root.mkdir(exist_ok=True)
    (root / f"{name}.py").write_text(body, encoding="utf-8")
    return root


def test_a_module_that_imports_a_transport_is_listed(tmp_path):
    root = _tree(tmp_path, "sender", "import requests\n")
    found = {f.module: f for f in needs_network(root)}
    assert "void.sender" in found
    assert "requests" in found["void.sender"].imports


def test_a_url_in_a_docstring_is_prose_and_is_not_counted(tmp_path):
    """The lesson that cost three rewrites: these files explain at length what they do NOT send.

    A grep cannot tell an explanation from a call. This walks the syntax tree and skips docstrings,
    so a module whose whole point is that it sends nothing does not get listed for saying so.
    """
    root = _tree(tmp_path, "innocent",
                 '"""This module never talks to https://api.anthropic.com or anything else."""\n'
                 "VALUE = 1\n")
    assert [f.module for f in needs_network(root)] == []


def test_a_url_in_real_code_is_counted(tmp_path):
    root = _tree(tmp_path, "caller", 'ENDPOINT = "https://api.example.org/v1"\n')
    found = {f.module: f for f in needs_network(root)}
    assert found["void.caller"].hosts == ("api.example.org",)


def test_a_loopback_url_is_not_a_reason_to_list_a_module(tmp_path):
    root = _tree(tmp_path, "localonly", 'OLLAMA = "http://127.0.0.1:11434/v1"\n')
    assert [f.module for f in needs_network(root)] == []


def test_a_class_and_function_docstring_are_skipped_too(tmp_path):
    root = _tree(tmp_path, "documented",
                 "class A:\n"
                 '    """Never posts to https://evil.example.com."""\n'
                 "    def go(self):\n"
                 '        """Nor https://other.example.com."""\n'
                 "        return 1\n")
    assert [f.module for f in needs_network(root)] == []


def test_a_package_is_named_without_its_init(tmp_path):
    root = tmp_path / "void"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("import httpx\n", encoding="utf-8")
    assert [f.module for f in needs_network(root)] == ["void.pkg"]


def test_a_file_that_will_not_parse_is_skipped_rather_than_crashing_the_report(tmp_path):
    root = _tree(tmp_path, "broken", "def (:\n")
    assert needs_network(root) == []


def test_a_module_with_nothing_to_say_is_left_out(tmp_path):
    root = _tree(tmp_path, "quiet", "VALUE = 2\n")
    assert needs_network(root) == []


def test_the_watcher_itself_is_not_listed_as_a_feature_that_needs_the_internet():
    """It imports socket in order to watch socket. Listing it would be exactly backwards."""
    assert not any(f.module.startswith("void.egress") for f in needs_network())


def test_the_real_tree_lists_the_modules_that_genuinely_reach_outward():
    """Not a fixed number - that would break on every feature. The named ones must be there."""
    modules = {f.module for f in needs_network()}
    assert "void.research" in modules
    assert "void.providers.openai_provider" in modules


def test_a_shared_description_is_not_printed_twice():
    line = Feature("void.x", ("urllib", "urllib.request"), ()).line()
    assert line.count("opens URLs") == 1


def test_a_feature_with_only_a_host_still_explains_itself():
    assert "reaches outward" in Feature("void.x", (), ("api.example.com",)).line()


def test_every_description_in_the_import_table_is_a_sentence_a_person_can_read():
    """The report is read by the owner, so "opens URLs" earns its place and "urllib" does not.

    Naming the library inside the description is fine, and often clearest ("the requests library").
    What is not fine is a description that is only the import name echoed back.
    """
    for name, why in NETWORK_IMPORTS.items():
        assert why and why[0].islower()
        assert why != name
        assert " " in why


# --------------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------------
def test_an_ipv4_address_is_read_as_host_and_port():
    assert egress._address_of(("1.2.3.4", 443)) == ("1.2.3.4", 443)


def test_an_ipv6_address_is_read_from_its_four_tuple():
    assert egress._address_of(("::1", 443, 0, 0)) == ("::1", 443)


@pytest.mark.parametrize("address", ["/run/test.sock", b"/run/test.sock", Path("/run/test.sock")])
def test_a_unix_socket_has_no_host_and_never_leaves(address):
    host, port = egress._address_of(address)
    assert host == UNIX_SOCKET and port == 0
    assert Connection(host, port).off_machine is False


def test_an_address_shape_nobody_expected_does_not_raise():
    assert egress._address_of(None)[0] == "unknown"
    assert egress._address_of(())[0] == "unknown"


def test_the_frame_walk_is_bounded():
    """This runs on every connection; an unbounded walk through a deep stack costs for nothing."""
    assert isinstance(MAX_FRAMES, int) and 0 < MAX_FRAMES <= 100
