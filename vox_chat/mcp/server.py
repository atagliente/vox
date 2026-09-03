"""VOX as an MCP server: its workspace tools, offered to another client.

The tools in ``tools.py`` are the natural thing to publish. They resolve every
path inside one directory and refuse anything that leaves it, they split
commands without a shell, and they are the best-tested part of this codebase.

But the confinement was written against a model **that VOX confirms for**.
Every write, patch and command in the TUI is authorised by a person looking
at a diff. On this side of the protocol there is no person: the caller is
another program, and a confirmation prompt would either hang for ever or be
answered by whatever is driving it.

So the answer here is not "confirm" but "do not offer":

- **Read-only by default.** ``list_files``, ``read_file`` and ``search_text``
  and nothing else. A client that connects gets tools that cannot change the
  machine, which is a promise it can check by reading ``tools/list``.
- **Writes are opt-in on the command line**, by the person starting the
  server, once, for a workspace they name. ``--allow-write`` is a decision
  taken with a shell prompt in front of you, which is the closest thing to a
  confirmation available here.
- **Commands are opt-in separately**, because "edit a file in this directory"
  and "run anything" are not the same risk and should not share a switch.
  ``--allow-run`` also honours ``agent.commands`` from the configuration, so
  a deny list written for the TUI applies here too.

The transport is stdio, one JSON object per line: the same thing
``mcp/transport.py`` reads, so VOX can be pointed at itself.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ..authorisation import Limits, Policy
from ..logging_setup import get_logger
from ..tools import (
    COMMAND_TOOLS,
    TOOL_SCHEMAS,
    WRITE_TOOLS,
    ToolError,
    Workspace,
    execute,
    split_command,
)
from .client import CLIENT_INFO, PROTOCOL_VERSION

log = get_logger("mcp.server")

SERVER_INFO = {"name": "vox", "version": CLIENT_INFO["version"]}

# Tools that only look. Everything else has to be asked for.
READ_ONLY = frozenset({"list_files", "read_file", "search_text"})


@dataclass
class Offer:
    """What this server is willing to do, decided once at startup."""

    workspace: Workspace
    allow_write: bool = False
    allow_run: bool = False
    agent_config: dict[str, Any] = field(default_factory=dict)

    def offers(self, name: str) -> bool:
        """Is this tool one this server was started willing to run?"""
        if name in READ_ONLY:
            return True
        if name in WRITE_TOOLS:
            return self.allow_write
        if name in COMMAND_TOOLS:
            return self.allow_run
        # web_search and fetch_url reach the network on the caller's behalf
        # and need a web configuration this side has not been given. They are
        # not refused so much as absent.
        return False

    def names(self) -> list[str]:
        """The tools offered, in the order they appear in TOOL_SCHEMAS."""
        return [
            schema["function"]["name"]
            for schema in TOOL_SCHEMAS
            if self.offers(schema["function"]["name"])
        ]

    def schemas(self) -> list[dict[str, Any]]:
        """The MCP tool descriptors, with the annotations a client acts on."""
        offered = set(self.names())
        out: list[dict[str, Any]] = []
        for schema in TOOL_SCHEMAS:
            function = schema["function"]
            name = function["name"]
            if name not in offered:
                continue
            out.append(
                {
                    "name": name,
                    "description": function.get("description", ""),
                    "inputSchema": function.get("parameters", {}),
                    # The same hints VOX reads from other people's servers,
                    # so a client can make the same decision about these.
                    "annotations": {
                        "readOnlyHint": name in READ_ONLY,
                        "destructiveHint": name in COMMAND_TOOLS or name in WRITE_TOOLS,
                    },
                }
            )
        return out

    def refuse(self, name: str) -> str | None:
        """Why this call will not be made, or None if it will."""
        if self.offers(name):
            return None
        if name in WRITE_TOOLS:
            return (
                f"{name} is not offered: this server was started read-only. "
                "Restart it with --allow-write if that is what you want."
            )
        if name in COMMAND_TOOLS:
            return (
                f"{name} is not offered: this server was started without --allow-run."
            )
        return f"no tool named {name}"

    def describe(self) -> str:
        parts = [f"workspace {self.workspace.root}"]
        parts.append("writes allowed" if self.allow_write else "read-only")
        if self.allow_run:
            policy = Policy.from_config(self.agent_config)
            parts.append(f"commands allowed ({policy.describe()})")
        return "  ·  ".join(parts)


class Server:
    """One stdio session, answering JSON-RPC until the input closes."""

    def __init__(
        self,
        offer: Offer,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.offer = offer
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout

    # ------------------------------------------------------------ the loop

    def serve(self) -> int:
        """Read requests until stdin closes. Returns the exit code."""
        log.info("MCP server ready: %s", self.offer.describe())
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # Not JSON at all: there is no id to answer with, so there is
                # nothing to say back. Saying nothing is the protocol.
                log.debug("ignoring a line that is not JSON")
                continue
            if not isinstance(message, dict):
                continue
            reply = self.handle(message)
            if reply is not None:
                self.send(reply)
        return 0

    def send(self, message: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(message) + "\n")
        self.stdout.flush()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one message, or None when it was a notification."""
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if request_id is None:
            # A notification. initialized is the only one worth noting.
            log.debug("notification: %s", method)
            return None

        if method == "initialize":
            return self._ok(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "Workspace tools confined to one directory. "
                        + self.offer.describe()
                    ),
                },
            )
        if method == "tools/list":
            return self._ok(request_id, {"tools": self.offer.schemas()})
        if method == "tools/call":
            return self._call(request_id, params)
        if method == "ping":
            return self._ok(request_id, {})
        return self._error(request_id, -32601, f"method not found: {method}")

    # ----------------------------------------------------------- the call

    def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        refusal = self.offer.refuse(name)
        if refusal is not None:
            # A refusal is a result, not a protocol error: the caller asked a
            # reasonable question and the answer is no, which is something a
            # model can read and act on.
            return self._ok(request_id, _content(refusal, is_error=True))

        if name in COMMAND_TOOLS:
            # "deny" is the only level that means anything here. "ask" has
            # nobody to ask, and --allow-run was the answer to that question
            # already; a deny list, on the other hand, was written by the
            # same person and still says no.
            command = str(arguments.get("command", ""))
            if _command_level(command, self.offer.agent_config) == "deny":
                return self._ok(
                    request_id,
                    _content(
                        f"{command!r} is on this machine's deny list "
                        "(agent.commands) and was not run.",
                        is_error=True,
                    ),
                )

        try:
            result = execute(
                name,
                arguments,
                self.offer.workspace,
                command_timeout=int(
                    self.offer.agent_config.get("command_timeout_seconds", 60)
                ),
                max_output=int(self.offer.agent_config.get("max_output_bytes", 8192)),
                limits=Limits.from_config(self.offer.agent_config),
            )
        except ToolError as exc:
            return self._ok(request_id, _content(str(exc), is_error=True))
        except KeyError as exc:
            return self._ok(
                request_id, _content(f"missing argument: {exc}", is_error=True)
            )
        return self._ok(request_id, _content(result.as_text(), is_error=not result.ok))

    # --------------------------------------------------------------- JSON-RPC

    def _ok(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def _content(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _command_level(command: str, agent_config: dict[str, Any]) -> str:
    try:
        argv = split_command(command)
    except ValueError:
        return "deny"
    return Policy.from_config(agent_config).level_for(command, argv)


def build_offer(
    workspace: Path | str,
    allow_write: bool = False,
    allow_run: bool = False,
    agent_config: dict[str, Any] | None = None,
) -> Offer:
    return Offer(
        workspace=Workspace(workspace),
        allow_write=allow_write,
        allow_run=allow_run,
        agent_config=dict(agent_config or {}),
    )
