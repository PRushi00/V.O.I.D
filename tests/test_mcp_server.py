"""The MCP protocol surface: initialize, tools/list, schemas, tools/call.

These drive the REAL protocol through the SDK's own client against the real server object, so what is asserted is
what an MCP client actually sees - not what the adapter would have returned if called directly. The adapter's
behaviour is covered in ``test_mcp_adapter.py`` and its hostile-input handling in ``test_mcp_security.py``.

``python -m void.mcp`` over genuine stdio is exercised at the end, as a subprocess, because "the server starts" is
not a claim a same-process test can make.

Note on test hygiene: this file never patches ``subprocess.Popen`` globally. ``asyncio.windows_utils`` subclasses it
at import time, so replacing it with a lambda breaks the event loop the MCP transport runs on. ``AppActions``'
launcher seam is the way in.
"""
import asyncio
import json
import sys

import pytest

from tests.test_computer import FakeBackend, _exe
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, ComputerActions
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.app import Assistant
from void.config import Config
from void.mcp.adapter import VoidMcpAdapter
from void.mcp.server import SERVER_NAME, build_server
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.protected import EngineProtected

mcp_client = pytest.importorskip("mcp.client.client", reason="the MCP SDK is an optional dependency")
Client = mcp_client.Client

TOOLS = {"list_applications", "find_application", "launch_application", "open_path",
         "get_system_info", "get_void_status"}


class _Counting(LLMProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text="(model)")


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A real MCPServer over a V.O.I.D built on test seams. Returns (server, state)."""
    root = tmp_path / "allowed"
    root.mkdir()
    cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(root)]}})
    a = Assistant(config=cfg)
    backend = FakeBackend(apps=[{"name": n, "kind": "exe", "target": _exe(tmp_path, t)}
                               for n, t in (("WhatsApp", "w.exe"), ("Discord", "d.exe"))])
    catalog = AppCatalog(backend)
    launched, opened = [], []
    fa = FileActions([root], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    monkeypatch.setattr("void.actions.apps.os.startfile", lambda t: opened.append(str(t)), raising=False)
    a.tools = ToolRegistry()
    a.tools.register_all(fa.tools())
    a.tools.register_all(AppActions(fa, catalog=catalog,
                                    launcher=lambda kind, target: launched.append(target)).tools())
    a.tools.register_all(ComputerActions(backend, catalog).tools())
    gemini, local = _Counting("gemini"), _Counting("local")
    a.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])
    state = {"launched": launched, "opened": opened, "providers": [gemini, local], "root": root}
    return build_server(VoidMcpAdapter(assistant=a)), state


def call(server, name, args=None):
    """One protocol round trip: initialize, tools/call, shut down."""
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})
    return asyncio.run(go())


def listing(server):
    async def go():
        async with Client(server) as c:
            return await c.list_tools(), c
    result, _ = asyncio.run(go())
    return result


# --- initialize -----------------------------------------------------------------------------------

def test_the_server_initializes(server):
    srv, _ = server

    async def go():
        async with Client(srv) as c:
            return c.server_info, c.protocol_version, c.instructions
    info, protocol, instructions = asyncio.run(go())
    assert info.name == SERVER_NAME
    assert protocol, "no protocol version was negotiated"
    assert instructions and "not the authorization boundary" in instructions.lower()


def test_the_instructions_tell_a_client_what_is_refused(server):
    srv, _ = server

    async def go():
        async with Client(srv) as c:
            return c.instructions
    text = asyncio.run(go()).lower()
    for promise in ("shell", "powershell", "credential", "protected", "stopped"):
        assert promise in text, f"the instructions do not mention {promise}"


# --- tools/list and schemas -----------------------------------------------------------------------

def test_tools_list_returns_exactly_the_six_planned_tools(server):
    srv, _ = server
    assert {t.name for t in listing(srv).tools} == TOOLS


def test_every_tool_has_a_description_and_an_object_input_schema(server):
    srv, _ = server
    for tool in listing(srv).tools:
        assert tool.description and len(tool.description) > 40, tool.name
        schema = tool.input_schema
        assert schema and schema.get("type") == "object", tool.name


@pytest.mark.parametrize("name,required", [
    ("list_applications", []),
    ("find_application", ["name"]),
    ("launch_application", ["name"]),
    ("open_path", ["path"]),
    ("get_system_info", []),
    ("get_void_status", []),
])
def test_each_tool_declares_the_arguments_it_takes(server, name, required):
    srv, _ = server
    tool = next(t for t in listing(srv).tools if t.name == name)
    assert tool.input_schema.get("required", []) == required


def test_the_limit_argument_is_bounded_in_the_schema_itself(server):
    """A client cannot even ASK for an unbounded list: the bound is in the published schema."""
    from void.mcp import schemas as s
    srv, _ = server
    tool = next(t for t in listing(srv).tools if t.name == "list_applications")
    limit = tool.input_schema["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["minimum"] == 1 and limit["maximum"] == s.MAX_APPLICATIONS
    assert limit["default"] == s.DEFAULT_APPLICATIONS


def test_string_arguments_are_length_bounded_in_the_schema(server):
    srv, _ = server
    for name, prop in (("find_application", "name"), ("launch_application", "name"), ("open_path", "path")):
        tool = next(t for t in listing(srv).tools if t.name == name)
        field = tool.input_schema["properties"][prop]
        assert field["type"] == "string"
        assert field.get("maxLength"), f"{name}.{prop} has no maximum length"
        assert field.get("minLength") == 1


def test_results_are_structured_so_a_client_can_branch_on_them(server):
    srv, _ = server
    for tool in listing(srv).tools:
        assert tool.output_schema, f"{tool.name} returns no structured output"
        assert "ok" in (tool.output_schema.get("properties") or {}), tool.name


def test_the_read_only_tools_are_annotated_as_such(server):
    srv, _ = server
    by_name = {t.name: t for t in listing(srv).tools}
    for name in ("list_applications", "find_application", "get_system_info", "get_void_status"):
        assert by_name[name].annotations.read_only_hint is True, name
    for name in ("launch_application", "open_path"):
        assert by_name[name].annotations.read_only_hint is False, name
        assert by_name[name].annotations.destructive_hint is False, name


# --- tools/call -----------------------------------------------------------------------------------

def test_a_status_call_returns_structured_content(server):
    srv, _ = server
    r = call(srv, "get_system_info")
    assert r.is_error is False
    assert r.structured_content["ok"] is True
    assert r.structured_content["os_family"]


def test_void_status_over_the_protocol(server):
    srv, _ = server
    r = call(srv, "get_void_status")
    assert r.is_error is False
    body = r.structured_content
    assert body["ok"] is True and body["kill_switch_engaged"] is False
    assert isinstance(body["providers"], list)


def test_finding_an_application_over_the_protocol(server):
    srv, _ = server
    r = call(srv, "find_application", {"name": "whatsapp"})
    assert r.is_error is False
    assert r.structured_content["resolved"]["name"] == "WhatsApp"


def test_launching_an_application_over_the_protocol(server):
    srv, state = server
    r = call(srv, "launch_application", {"name": "whatsapp"})
    assert r.is_error is False and r.structured_content["ok"] is True
    assert len(state["launched"]) == 1


def test_opening_an_allowed_path_over_the_protocol(server):
    srv, state = server
    target = state["root"] / "folder"
    target.mkdir()
    r = call(srv, "open_path", {"path": str(target)})
    assert r.is_error is False and r.structured_content["ok"] is True
    assert state["opened"] == [str(target)]


def test_a_refusal_is_a_normal_response_a_client_can_read(server):
    """A denial must not arrive as a protocol fault - the client has to be able to see WHY."""
    srv, state = server
    r = call(srv, "launch_application", {"name": r"C:\Windows\System32\cmd.exe"})
    assert r.is_error is False, "a refusal was raised instead of reported"
    body = r.structured_content
    assert body["ok"] is False and body["error"]["code"] == "invalid_input"
    assert state["launched"] == []


def test_an_out_of_range_argument_is_rejected_by_the_protocol_layer(server):
    srv, _ = server
    r = call(srv, "list_applications", {"limit": 9999})
    assert r.is_error is True, "the schema bound was not enforced"


def test_an_unknown_tool_is_rejected(server):
    srv, _ = server
    r = call(srv, "definitely_not_a_tool", {})
    assert r.is_error is True


def test_a_missing_required_argument_is_rejected(server):
    srv, _ = server
    assert call(srv, "find_application", {}).is_error is True
    assert call(srv, "open_path", {}).is_error is True


def test_an_unexpected_argument_cannot_influence_a_call(server):
    """Measured behaviour of mcp 2.2.0: an extra argument is IGNORED, not rejected.

    That is not a hole - an argument the function does not declare reaches nothing - but it is worth pinning,
    because the security claim rests on "only declared arguments have any effect". A smuggled ``path`` on
    ``find_application`` must not become a path operation, and a smuggled ``limit`` must not widen a bound.
    """
    srv, state = server
    plain = call(srv, "find_application", {"name": "whatsapp"})
    smuggled = call(srv, "find_application",
                    {"name": "whatsapp", "path": r"C:\Windows", "limit": 9999, "confirm": True})
    assert smuggled.is_error is False
    assert smuggled.structured_content == plain.structured_content
    assert state["opened"] == [] and state["launched"] == []


def test_no_protocol_call_reaches_a_model(server):
    srv, state = server
    for name, args in (("get_system_info", {}), ("get_void_status", {}),
                       ("find_application", {"name": "whatsapp"}),
                       ("launch_application", {"name": "discord"}),
                       ("list_applications", {"limit": 2})):
        call(srv, name, args)
    assert sum(p.calls for p in state["providers"]) == 0


def test_no_response_body_carries_a_secret_shaped_string(server):
    srv, _ = server
    bodies = []
    for name, args in (("get_system_info", {}), ("get_void_status", {}), ("list_applications", {"limit": 5}),
                       ("find_application", {"name": "whatsapp"})):
        bodies.append(json.dumps(call(srv, name, args).structured_content or {}))
    blob = " ".join(bodies)
    for leak in ("sk-", "AIza", "ghp_", "api_key", "Bearer ", "Traceback"):
        assert leak not in blob, f"{leak} appeared in an MCP response"


# --- the server actually starts, over real stdio ---------------------------------------------------

def test_the_module_serves_over_real_stdio_as_a_subprocess():
    """`python -m void.mcp` must initialize and list tools over genuine stdin/stdout.

    This is the only test that proves the shipped entry point works: it launches a real process, speaks the real
    transport to it, and shuts it down. It uses the machine's own V.O.I.D configuration, so it touches the real
    application catalog - but it calls no side-effecting tool.
    """
    from mcp.client.stdio import StdioServerParameters

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "void.mcp"], cwd=str(ROOT))
        async with Client(params, read_timeout_seconds=120) as c:
            tools = await c.list_tools()
            info = c.server_info
            status = await c.call_tool("get_system_info", {})
            return info, {t.name for t in tools.tools}, status
    info, names, status = asyncio.run(go())
    assert info.name == SERVER_NAME
    assert names == TOOLS
    assert status.is_error is False and status.structured_content["ok"] is True


ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
