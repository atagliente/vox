"""Talking to Model Context Protocol servers.

MCP is how a client reaches tools somebody else wrote — a database, an issue
tracker, a browser — without VOX having to know about any of them. A server
is either a subprocess or a URL; either way it declares what it offers and
VOX puts those declarations in front of the model beside its own tools.

Everything a server says is data. A tool description is written by its author
and reaches the model as text, so it is labelled by server and never treated
as an instruction to VOX itself, on the same footing as a fetched page.
"""

from __future__ import annotations

from .client import McpClient, Tool, render_content
from .registry import PREFIX, McpRegistry, ServerStatus, settings_from_config
from .transport import HttpTransport, McpError, StdioTransport

__all__ = [
    "PREFIX",
    "HttpTransport",
    "McpClient",
    "McpError",
    "McpRegistry",
    "ServerStatus",
    "StdioTransport",
    "Tool",
    "render_content",
    "settings_from_config",
]
