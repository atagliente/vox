"""One MCP server, as VOX talks to it.

The Model Context Protocol is JSON-RPC 2.0 with a handshake and a small set
of methods. VOX uses three of them — ``initialize``, ``tools/list`` and
``tools/call`` — because tools are what an agent loop can actually act on.
Prompts and resources are defined by the protocol and deliberately not read
here: a capability nothing consumes is a capability nobody can tell is broken.

What comes back from a server is **data**, on exactly the same footing as a
fetched web page. A tool description is written by somebody else and reaches
the model as text, so it is labelled and never treated as an instruction to
VOX itself.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..logging_setup import get_logger
from .transport import DEFAULT_TIMEOUT, McpError, Transport

log = get_logger("mcp")

PROTOCOL_VERSION = "2025-06-18"
# Read from the package, not written again here: a second copy of a
# version is a second version, and this one was already wrong.
CLIENT_INFO = {"name": "vox", "version": __version__}


@dataclass
class Tool:
    """One tool a server offers, as VOX needs to see it."""

    server: str
    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False
    destructive: bool = False

    @property
    def qualified(self) -> str:
        """The name the model sees.

        Prefixed with the server, because two servers may both offer
        ``search`` and the model has one namespace to work in.
        """
        return f"mcp__{self.server}__{self.name}"

    def to_schema(self) -> dict[str, Any]:
        """The OpenAI function schema for this tool."""
        parameters = self.schema or {"type": "object", "properties": {}}
        if "type" not in parameters:
            parameters = {**parameters, "type": "object"}
        return {
            "type": "function",
            "function": {
                "name": self.qualified,
                "description": (
                    f"[{self.server}] {self.description}".strip()
                    or f"The {self.name} tool from the {self.server} server."
                ),
                "parameters": parameters,
            },
        }


class McpClient:
    """A connected server: the handshake, the tool list, and calls."""

    def __init__(
        self, name: str, transport: Transport, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.name = name
        self.transport = transport
        self.timeout = timeout
        self.server_info: dict[str, Any] = {}
        self.tools: list[Tool] = []
        self._ids = itertools.count(1)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def describe(self) -> str:
        title = str(self.server_info.get("name", "")) or self.name
        version = str(self.server_info.get("version", ""))
        where = self.transport.describe
        return f"{title} {version}".strip() + f" · {where}"

    # ------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Open the channel and complete the handshake."""
        self.transport.start()
        try:
            result = self._call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            )
        except McpError:
            self.transport.close()
            raise
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        # The handshake is only finished once the server has been told so.
        self._notify("notifications/initialized")
        self._connected = True
        log.info("connected to MCP server %s (%s)", self.name, self.describe())

    def close(self) -> None:
        self._connected = False
        self.tools = []
        self.transport.close()

    # ----------------------------------------------------------------- tools

    def list_tools(self) -> list[Tool]:
        """Ask the server what it offers, following pagination to the end."""
        found: list[Tool] = []
        cursor: str | None = None
        # A server that keeps handing back cursors would otherwise loop for
        # ever; ten pages is far more than any real server returns.
        for _page in range(10):
            params = {"cursor": cursor} if cursor else {}
            result = self._call("tools/list", params)
            for item in result.get("tools", []):
                if isinstance(item, dict) and item.get("name"):
                    found.append(self._tool(item))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools = found
        return found

    def _tool(self, item: dict[str, Any]) -> Tool:
        annotations = item.get("annotations")
        annotations = annotations if isinstance(annotations, dict) else {}
        schema = item.get("inputSchema")
        return Tool(
            server=self.name,
            name=str(item["name"]),
            description=str(item.get("description", "")),
            schema=schema if isinstance(schema, dict) else {},
            read_only=bool(annotations.get("readOnlyHint", False)),
            destructive=bool(annotations.get("destructiveHint", False)),
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Run one tool. Returns ``(ok, text)``.

        A tool that fails says so in ``isError`` rather than by raising: that
        is a result the model can read and act on, not a transport failure,
        and the distinction is worth keeping.
        """
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        return not bool(result.get("isError")), render_content(result)

    # --------------------------------------------------------------- JSON-RPC

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        reply = self.transport.send(message, self.timeout)
        if reply is None:  # pragma: no cover - only notifications return None
            raise McpError(f"{self.name} did not answer {method}")
        if "error" in reply:
            error = reply["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError(f"{self.name} refused {method}: {detail}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.transport.send(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}, self.timeout
        )


def render_content(result: dict[str, Any]) -> str:
    """Flatten an MCP result into the text a model can read.

    Text comes through as itself. Anything else is named rather than dropped,
    because "there was an image here" is information and silence is not.
    """
    if isinstance(result.get("structuredContent"), dict | list):
        import json

        return json.dumps(result["structuredContent"], indent=2)[:100_000]
    parts: list[str] = []
    for item in result.get("content", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource")
            if isinstance(resource, dict) and "text" in resource:
                parts.append(str(resource["text"]))
            else:
                uri = (resource or {}).get("uri", "?") if resource else "?"
                parts.append(f"[resource: {uri}]")
        else:
            parts.append(f"[{kind or 'unknown'} content, not text]")
    return "\n".join(part for part in parts if part).strip()
