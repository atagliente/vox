"""The MCP servers this configuration asks for, and the tools they add.

One object owns every connection, so the rest of VOX asks it two questions:
what schemas go in the request, and who runs this call. A server that will not
start is reported once and then left alone — a broken entry in the
configuration must not stop the others working, and must not be retried on
every turn.

Confirmation is stricter here than for VOX's own tools, on purpose. A local
tool is code in this repository that a test covers; an MCP tool is somebody
else's program, described to the model by its own author. So every call is
confirmed unless the server marks the tool read-only, and a tool the server
marks destructive is confirmed whatever the setting says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger
from .client import McpClient, Tool
from .transport import DEFAULT_TIMEOUT, HttpTransport, McpError, StdioTransport

log = get_logger("mcp")

PREFIX = "mcp__"


@dataclass
class ServerStatus:
    """What happened when VOX tried to talk to one server."""

    name: str
    where: str
    connected: bool
    tools: int = 0
    error: str = ""


def settings_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """The ``mcp`` block, already defaulted."""
    section = config.get("mcp") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    servers = section.get("servers")
    return {
        "enabled": bool(section.get("enabled", False)),
        "confirm": bool(section.get("confirm", True)),
        "timeout_seconds": float(section.get("timeout_seconds", DEFAULT_TIMEOUT)),
        "servers": servers if isinstance(servers, dict) else {},
    }


def build_transport(spec: dict[str, Any]):
    """Choose a transport from one server entry.

    ``command`` means a subprocess, ``url`` means HTTP. Asking for both is a
    configuration mistake worth naming rather than resolving silently.
    """
    command = str(spec.get("command", "") or "")
    url = str(spec.get("url", "") or "")
    if command and url:
        raise McpError("a server takes either a command or a url, not both")
    if command:
        args = spec.get("args")
        env = spec.get("env")
        return StdioTransport(
            command,
            args=[str(a) for a in args] if isinstance(args, list) else [],
            env={str(k): str(v) for k, v in env.items()}
            if isinstance(env, dict)
            else {},
            cwd=spec.get("cwd") or None,
        )
    if url:
        headers = spec.get("headers")
        return HttpTransport(
            url,
            headers={str(k): str(v) for k, v in headers.items()}
            if isinstance(headers, dict)
            else {},
        )
    raise McpError("a server needs either a command or a url")


class McpRegistry:
    """Every connected server, and the tools they add to a turn."""

    def __init__(self) -> None:
        self.clients: dict[str, McpClient] = {}
        self.status: list[ServerStatus] = []
        self._confirm_default = True

    # ------------------------------------------------------------- lifecycle

    def connect_all(self, config: dict[str, Any]) -> list[ServerStatus]:
        """Bring up every configured server. Never raises: it reports."""
        self.close()
        settings = settings_from_config(config)
        self._confirm_default = bool(settings["confirm"])
        if not settings["enabled"]:
            return []
        timeout = float(settings["timeout_seconds"])
        report: list[ServerStatus] = []
        for name, spec in sorted(settings["servers"].items()):
            if not isinstance(spec, dict):
                report.append(ServerStatus(name, "?", False, error="not an object"))
                continue
            report.append(self._connect_one(str(name), spec, timeout))
        self.status = report
        return report

    def _connect_one(
        self, name: str, spec: dict[str, Any], timeout: float
    ) -> ServerStatus:
        try:
            transport = build_transport(spec)
        except McpError as exc:
            return ServerStatus(name, "?", False, error=str(exc))
        where = transport.describe
        client = McpClient(name, transport, timeout=timeout)
        try:
            client.connect()
            tools = client.list_tools()
        except McpError as exc:
            client.close()
            log.warning("MCP server %s did not start: %s", name, exc)
            return ServerStatus(name, where, False, error=str(exc))
        self.clients[name] = client
        return ServerStatus(name, where, True, tools=len(tools))

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self.status = []

    # ----------------------------------------------------------------- tools

    def tools(self) -> list[Tool]:
        return [tool for client in self.clients.values() for tool in client.tools]

    def schemas(self) -> list[dict[str, Any]]:
        """What goes in the request alongside VOX's own tools."""
        return [tool.to_schema() for tool in self.tools()]

    def find(self, qualified: str) -> Tool | None:
        return next((t for t in self.tools() if t.qualified == qualified), None)

    def owns(self, name: str) -> bool:
        return name.startswith(PREFIX)

    def needs_confirmation(self, qualified: str) -> bool:
        """Whether this call must be authorised first.

        A tool the server itself marks destructive is always confirmed: that
        is the server's own warning, and a setting that overrode it would be
        a setting for ignoring warnings.
        """
        tool = self.find(qualified)
        if tool is None:
            return True
        if tool.destructive:
            return True
        if tool.read_only:
            return False
        return self._confirm_default

    def describe_call(self, qualified: str, arguments: dict[str, Any]) -> str:
        """What the confirmation modal shows for an MCP call."""
        tool = self.find(qualified)
        if tool is None:
            return f"{qualified} {arguments}"
        client = self.clients.get(tool.server)
        where = client.transport.describe if client else "?"
        lines = [
            f"RUN {tool.name} ON {tool.server}",
            f"server: {where}",
        ]
        if tool.destructive:
            lines.append("the server marks this tool as destructive")
        lines.append("")
        lines.append(tool.description or "(the server offered no description)")
        if arguments:
            import json

            lines += ["", "arguments:", json.dumps(arguments, indent=2)[:4000]]
        return "\n".join(lines)

    def call(self, qualified: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Run a confirmed MCP call. Raises :class:`McpError` if it cannot."""
        tool = self.find(qualified)
        if tool is None:
            raise McpError(f"no MCP server offers {qualified}")
        client = self.clients.get(tool.server)
        if client is None or not client.connected:  # pragma: no cover - defensive
            raise McpError(f"the {tool.server} server is not connected")
        return client.call_tool(tool.name, arguments)
