"""The MCP client, against a server that really is a separate process.

The stdio tests spawn a small server written inline below and talk to it over
real pipes. That is the point: framing, the handshake, a reply arriving after
a notification, a server that dies — none of those are exercised by a fake
transport, and all of them are how MCP fails in practice.

The HTTP tests use the same seam every other network test here uses.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from email.message import EmailMessage

import pytest

from vox_chat import mcp
from vox_chat.mcp.client import McpClient, Tool, render_content
from vox_chat.mcp.registry import McpRegistry, build_transport
from vox_chat.mcp.transport import HttpTransport, McpError, StdioTransport

# ------------------------------------------------------------ a real server

SERVER = r"""
import json, sys

TOOLS = [
    {
        "name": "echo",
        "description": "Give back what it was handed.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wreck",
        "description": "Break something.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": True},
    },
]

def reply(id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        reply(message["id"], {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1.0"},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        # A notification first, to prove the client does not mistake it for
        # the answer it is waiting on.
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/message",
             "params": {"level": "info", "data": "listing"}}) + "\n")
        sys.stdout.flush()
        reply(message["id"], {"tools": TOOLS})
    elif method == "tools/call":
        name = message["params"]["name"]
        args = message["params"].get("arguments", {})
        if name == "echo":
            reply(message["id"], {"content": [{"type": "text", "text": args.get("text", "")}]})
        elif name == "wreck":
            reply(message["id"], {"content": [{"type": "text", "text": "broke it"}],
                                  "isError": True})
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32602, "message": f"no tool {name}"}}) + "\n")
            sys.stdout.flush()
    else:
        reply(message["id"], {})
"""


@pytest.fixture
def server(tmp_path):
    """A stdio MCP server, connected, and shut down afterwards."""
    script = tmp_path / "fixture_server.py"
    script.write_text(SERVER, encoding="utf-8")
    client = McpClient(
        "fixture", StdioTransport(sys.executable, ["-u", str(script)]), timeout=20
    )
    client.connect()
    try:
        yield client
    finally:
        client.close()


@pytest.mark.slow
def test_the_handshake_names_the_server(server: McpClient) -> None:
    assert server.connected
    assert server.server_info["name"] == "fixture"
    assert "fixture" in server.describe()


@pytest.mark.slow
def test_the_tools_arrive_with_their_annotations(server: McpClient) -> None:
    tools = server.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {"echo", "wreck"}
    assert by_name["echo"].read_only is True
    assert by_name["wreck"].destructive is True
    # A notification was sent before the answer; it must not have been
    # mistaken for it.
    assert by_name["echo"].description == "Give back what it was handed."


@pytest.mark.slow
def test_a_call_goes_out_and_the_answer_comes_back(server: McpClient) -> None:
    server.list_tools()
    ok, text = server.call_tool("echo", {"text": "hello there"})
    assert ok is True
    assert text == "hello there"


@pytest.mark.slow
def test_a_tool_that_fails_says_so_without_raising(server: McpClient) -> None:
    """isError is a result the model can read, not a transport failure."""
    server.list_tools()
    ok, text = server.call_tool("wreck", {})
    assert ok is False
    assert "broke it" in text


@pytest.mark.slow
def test_an_unknown_tool_is_an_error_from_the_server(server: McpClient) -> None:
    with pytest.raises(McpError, match="no tool"):
        server.call_tool("nonexistent", {})


@pytest.mark.slow
def test_a_server_that_is_not_there_is_reported_not_raised(tmp_path) -> None:
    """One broken entry must not stop the others, and must not crash a turn."""
    registry = McpRegistry()
    report = registry.connect_all(
        {
            "mcp": {
                "enabled": True,
                "servers": {
                    "missing": {"command": "definitely-not-a-real-binary-9137"},
                },
            }
        }
    )
    assert len(report) == 1
    assert report[0].connected is False
    assert "PATH" in report[0].error
    assert registry.tools() == []


# ------------------------------------------------------------- without a server


def test_the_qualified_name_carries_the_server() -> None:
    """Two servers may both offer `search`, and the model has one namespace."""
    tool = Tool(server="github", name="search")
    assert tool.qualified == "mcp__github__search"


def test_the_schema_is_what_a_provider_expects() -> None:
    tool = Tool(
        server="db",
        name="query",
        description="Run SQL.",
        schema={"type": "object", "properties": {"sql": {"type": "string"}}},
    )
    schema = tool.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp__db__query"
    assert "[db]" in schema["function"]["description"]
    assert schema["function"]["parameters"]["properties"]["sql"]["type"] == "string"


def test_a_tool_with_no_schema_still_produces_a_valid_one() -> None:
    """Servers in the wild omit inputSchema; a provider will not."""
    schema = Tool(server="s", name="t").to_schema()
    assert schema["function"]["parameters"] == {"type": "object", "properties": {}}
    assert schema["function"]["description"]


def test_content_is_flattened_to_something_a_model_can_read() -> None:
    assert render_content({"content": [{"type": "text", "text": "a"}]}) == "a"
    # Not silence: "there was an image here" is information.
    assert "image" in render_content({"content": [{"type": "image", "data": "..."}]})
    assert (
        render_content(
            {"content": [{"type": "resource", "resource": {"text": "body"}}]}
        )
        == "body"
    )
    assert '"n": 1' in render_content({"structuredContent": {"n": 1}})


def test_a_server_needs_a_command_or_a_url_and_not_both() -> None:
    with pytest.raises(McpError, match="either a command or a url"):
        build_transport({})
    with pytest.raises(McpError, match="not both"):
        build_transport({"command": "x", "url": "https://y"})
    assert isinstance(build_transport({"command": "echo"}), StdioTransport)
    assert isinstance(build_transport({"url": "https://x/mcp"}), HttpTransport)


# --------------------------------------------------------------- confirmation


def a_registry(**annotations) -> McpRegistry:
    registry = McpRegistry()
    client = McpClient("s", HttpTransport("https://x/mcp"))
    client.tools = [Tool(server="s", name="t", **annotations)]
    registry.clients["s"] = client
    return registry


def test_a_read_only_tool_is_not_confirmed() -> None:
    assert a_registry(read_only=True).needs_confirmation("mcp__s__t") is False


def test_everything_else_is_confirmed_by_default() -> None:
    """A local tool is code in this repository with a test behind it. This is
    somebody else's program, described by its own author."""
    assert a_registry().needs_confirmation("mcp__s__t") is True


def test_a_destructive_tool_is_confirmed_whatever_the_setting_says() -> None:
    """That is the server's own warning; a setting that overrode it would be
    a setting for ignoring warnings."""
    registry = a_registry(destructive=True, read_only=True)
    registry._confirm_default = False
    assert registry.needs_confirmation("mcp__s__t") is True


def test_an_unknown_tool_is_confirmed() -> None:
    assert a_registry().needs_confirmation("mcp__s__gone") is True


def test_the_confirmation_says_which_server_and_what_it_will_be_sent() -> None:
    registry = a_registry(destructive=True)
    text = registry.describe_call("mcp__s__t", {"path": "/etc/passwd"})
    assert "RUN t ON s" in text
    assert "https://x/mcp" in text
    assert "destructive" in text
    assert "/etc/passwd" in text


# ---------------------------------------------------------------------- HTTP


class FakeReply(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        super().__init__(body)
        headers = EmailMessage()
        headers["Content-Type"] = content_type
        headers["Mcp-Session-Id"] = "session-42"
        self.headers = headers
        self.status = 200

    def geturl(self) -> str:
        return "https://x/mcp"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_the_http_transport_keeps_the_session_it_is_given(monkeypatch) -> None:
    """A sequence of POSTs is one conversation only because the session id
    goes back on each of them."""
    sent: list[dict] = []

    def urlopen(request, timeout=None):
        sent.append(dict(request.headers))
        return FakeReply(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    transport = HttpTransport("https://x/mcp")
    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, 5)
    assert transport.session_id == "session-42"
    transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 5)
    assert sent[1].get("Mcp-session-id") == "session-42"


def test_an_sse_answer_is_read_as_well_as_a_json_one(monkeypatch) -> None:
    """A server may narrate progress before the answer; the answer is the
    last thing it says."""
    body = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n'
        b"\n"
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        b"\n"
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeReply(body, "text/event-stream"),
    )
    transport = HttpTransport("https://x/mcp")
    reply = transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, 5)
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


def test_a_server_error_becomes_an_mcp_error(monkeypatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeReply(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "nope"}}
            ).encode()
        ),
    )
    client = McpClient("s", HttpTransport("https://x/mcp"))
    with pytest.raises(McpError, match="nope"):
        client.list_tools()


# ------------------------------------------------------------------ settings


def test_mcp_is_off_and_empty_until_asked_for() -> None:
    """Starting somebody else's program is not something a default does."""
    from vox_chat.config import default_config

    settings = mcp.settings_from_config(default_config())
    assert settings["enabled"] is False
    assert settings["servers"] == {}
    assert settings["confirm"] is True


def test_a_disabled_block_connects_to_nothing() -> None:
    registry = McpRegistry()
    assert registry.connect_all({"mcp": {"enabled": False, "servers": {"a": {}}}}) == []
