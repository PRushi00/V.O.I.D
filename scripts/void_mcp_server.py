"""Start V.O.I.D's MCP server on stdio, without needing ``-m``.

``python -m void.mcp`` is the canonical entry point. This script exists because some MCP hosts and development tools
parse a leading ``-m`` as one of their OWN options and never forward it, so a plain script path is the portable way
to name the server in a client configuration:

    {"command": "C:/V.O.I.D/.venv/Scripts/python.exe",
     "args": ["C:/V.O.I.D/scripts/void_mcp_server.py"]}

It adds no behaviour: stdio transport, the same six tools, the same security path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.mcp.server import main  # noqa: E402

raise SystemExit(main([]))
