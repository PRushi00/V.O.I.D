"""The V.O.I.D MCP server: protocol wiring, and nothing else.

This module contains no capability logic, no path policy, no application resolution and no authorization. Each tool
is a few lines that hand typed arguments to :class:`void.mcp.adapter.VoidMcpAdapter` and return its typed result.
Everything that decides whether something may happen lives behind the adapter, in V.O.I.D's existing capability and
security layer.

**MCP is an interoperability protocol, not V.O.I.D's authorization boundary.** A client that can speak to this
server gets exactly the six capabilities below, each subject to the same kill switch, risk level, ``RiskGate``
decision, protected-root enforcement and audit trail that applies when V.O.I.D acts on the owner's own voice
command. There is no path from an MCP request to a shell, a subprocess, an arbitrary executable or a credential
store.

Transport is **stdio only** in v0.1: the server speaks on stdin/stdout to a parent process and binds no socket.
Because every tool body is a thin delegation, adding a Streamable HTTP transport later is a change to
``main()`` alone - the capability layer and the security path do not move.

Run it with::

    python -m void.mcp
"""
from __future__ import annotations

import logging
import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from void import __version__ as _void_version
from void.mcp import schemas
from void.mcp.adapter import VoidMcpAdapter
from void.mcp.schemas import (FindApplicationResult, LaunchApplicationResult, ListApplicationsResult,
                              OpenPathResult, SystemInfo, VoidStatus)

_log = logging.getLogger("void.mcp.server")

SERVER_NAME = "void"
SERVER_TITLE = "V.O.I.D"

#: Shown to an MCP client on initialize. It says what the server will NOT do, because a client that understands the
#: boundary asks for the right things.
INSTRUCTIONS = """\
V.O.I.D's own capabilities, exposed over MCP. This is an adapter: every call is executed by V.O.I.D's existing
capability layer and is subject to its security controls (kill switch, per-call risk level, the owner's RiskGate
policy, protected roots, audit). MCP is not the authorization boundary and cannot raise its own privileges.

What this server can do: list and resolve installed applications, launch a resolved application, open a file or
folder that lies inside an owner-approved root (or an http/https URL), and report machine and runtime status.

What it deliberately cannot do, by construction and not by configuration: run a shell, a command line, PowerShell
or an arbitrary executable; launch anything by path; open a location outside the owner's approved roots or inside a
protected one; read or write files; reach credentials, keys or tokens; change security policy; or act while V.O.I.D
is stopped. Ask the owner directly for anything in that list.

An application is named the way a person would say it ("WhatsApp", "VS Code"). If several match, the call fails as
'ambiguous' and lists the candidates - choose with the owner rather than guessing.
"""


def build_server(adapter: VoidMcpAdapter | None = None) -> MCPServer:
    """Construct the server and register the v0.1 tools. Does not start a transport.

    Taking the adapter as an argument is what lets the tests drive the real protocol surface against a V.O.I.D built
    on test seams, instead of the live machine.
    """
    void = adapter if adapter is not None else VoidMcpAdapter()
    mcp = MCPServer(name=SERVER_NAME, title=SERVER_TITLE, version=_void_version,
                    instructions=INSTRUCTIONS)

    # ``readOnlyHint`` / ``destructiveHint`` are advisory metadata for the CLIENT's benefit. They are not enforcement
    # - enforcement is the risk level each underlying V.O.I.D tool already declares, evaluated by RiskGate.
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    acts = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @mcp.tool(
        name="list_applications",
        description=("List applications V.O.I.D has discovered on this machine. Read-only. Returns a bounded, "
                     "stably ordered sample - not a complete inventory. To resolve a specific name, use "
                     "find_application."),
        annotations=read_only,
    )
    def list_applications(
        limit: Annotated[int, Field(ge=1, le=schemas.MAX_APPLICATIONS,
                                    description="Maximum applications to return.")]
        = schemas.DEFAULT_APPLICATIONS,
    ) -> ListApplicationsResult:
        return void.list_applications(limit=limit)

    @mcp.tool(
        name="find_application",
        description=("Resolve a human application name to V.O.I.D's own identifier, without launching it. Give the "
                     "name as a person would say it. Fails as 'ambiguous' when several match - never guess."),
        annotations=read_only,
    )
    def find_application(
        name: Annotated[str, Field(min_length=1, max_length=200,
                                   description="Application name as a person would say it, e.g. 'WhatsApp'.")],
    ) -> FindApplicationResult:
        return void.find_application(name=name)

    @mcp.tool(
        name="launch_application",
        description=("Launch an installed application by name. V.O.I.D resolves the name itself and launches what "
                     "it resolved; a path, command line or executable cannot be supplied. Subject to the owner's "
                     "security policy and refused while V.O.I.D is stopped."),
        annotations=acts,
    )
    def launch_application(
        name: Annotated[str, Field(min_length=1, max_length=200,
                                   description="Application name as a person would say it, e.g. 'WhatsApp'.")],
    ) -> LaunchApplicationResult:
        return void.launch_application(name=name)

    @mcp.tool(
        name="open_path",
        description=("Open a file or folder with its default application, or an http/https URL in the browser. The "
                     "location must lie inside a root the owner has approved and outside every protected one; "
                     "V.O.I.D decides that, not the caller."),
        annotations=acts,
    )
    def open_path(
        path: Annotated[str, Field(min_length=1, max_length=400,
                                   description="A local file or folder path, or an http/https URL.")],
    ) -> OpenPathResult:
        return void.open_path(path=path)

    @mcp.tool(
        name="get_system_info",
        description=("A minimal machine summary: operating system family and release, Python and V.O.I.D versions, "
                     "CPU count and memory totals. Carries no hostname, user, path or environment value."),
        annotations=read_only,
    )
    def get_system_info() -> SystemInfo:
        return void.get_system_info()

    @mcp.tool(
        name="get_void_status",
        description=("V.O.I.D's runtime health: kill-switch state, active task count, configured providers and "
                     "whether each is reachable, voice state, and the result of V.O.I.D's own health checks."),
        annotations=read_only,
    )
    def get_void_status() -> VoidStatus:
        return void.get_void_status()

    return mcp


def main(argv: list[str] | None = None) -> int:
    """Serve on stdio. No socket is bound and no network transport is offered in v0.1."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    # stdout belongs to the protocol: anything printed there would corrupt the JSON-RPC stream, so logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        server = build_server()
    except Exception as exc:                          # noqa: BLE001 - a start-up failure must be legible, not a trace
        print(f"void-mcp: could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
