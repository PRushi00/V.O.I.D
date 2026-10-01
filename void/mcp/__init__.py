"""V.O.I.D's MCP adapter: a protocol surface over V.O.I.D's existing capabilities.

MCP is an interoperability protocol, NOT V.O.I.D's authorization boundary. Every call made through this package is
executed by the existing capability layer and passes the same kill switch, per-call risk level, ``RiskGate``
decision, protected-root enforcement and audit trail as any other V.O.I.D action.

See ``docs/MCP_V0_1.md``. Run the stdio server with ``python -m void.mcp``.
"""
from void.mcp.errors import ErrorCode, McpError

__all__ = ["ErrorCode", "McpError", "build_server", "VoidMcpAdapter"]


def __getattr__(name):
    # Imported lazily so ``void.mcp.errors``/``schemas`` stay usable (and testable) without the MCP SDK installed,
    # and so importing this package never pulls in starlette/uvicorn unless a server is actually being built.
    if name == "build_server":
        from void.mcp.server import build_server
        return build_server
    if name == "VoidMcpAdapter":
        from void.mcp.adapter import VoidMcpAdapter
        return VoidMcpAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
