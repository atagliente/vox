"""VOX on the other end of the protocol, driven by VOX's own client.

The interesting tests here are the round trips: `vox serve` is started as a
real subprocess and `McpClient` — the same class that talks to everybody
else's servers — does the handshake, lists the tools and calls them. If the
framing, the handshake or the content shape were wrong on either side, these
would not pass, and neither side is allowed to be wrong on its own.

The rest are about the one decision this server had to make. There is no
operator behind it, so a confirmation prompt is not available; what is
offered is decided once, by the person who typed the command line. That has
to hold both in what `tools/list` advertises and in what `tools/call`
actually does, because a client is entitled to trust the first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vox_chat.mcp.client import McpClient
from vox_chat.mcp.server import Offer, Server, build_offer
from vox_chat.mcp.transport import StdioTransport


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "hello.txt").write_text("a line\nanother\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("needle in here\n", encoding="utf-8")
    return tmp_path


def client_for(workspace, *flags: str) -> McpClient:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "vox_chat", "mcp-serve", "-w", str(workspace), *flags],
        cwd=str(workspace),
        # The server runs with the workspace as its working directory, which
        # is the whole point, so the checkout has to reach it some other way.
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    return McpClient("vox", transport, timeout=30.0)


# ------------------------------------------------------- the offer, decided


def test_nothing_that_writes_is_offered_by_default():
    offer = build_offer(".")
    assert offer.names() == ["list_files", "read_file", "search_text"]
    assert "read-only" in offer.describe()


def test_writes_arrive_only_when_they_were_asked_for():
    assert "write_file" in build_offer(".", allow_write=True).names()
    assert "run_command" not in build_offer(".", allow_write=True).names()
    assert "run_command" in build_offer(".", allow_run=True).names()
    # The two switches are separate on purpose: editing a file in a directory
    # and running anything at all are not the same risk.
    assert "write_file" not in build_offer(".", allow_run=True).names()


def test_every_advertised_tool_says_whether_it_only_looks():
    schemas = build_offer(".", allow_write=True, allow_run=True).schemas()
    hints = {s["name"]: s["annotations"] for s in schemas}
    assert hints["read_file"]["readOnlyHint"] is True
    assert hints["read_file"]["destructiveHint"] is False
    assert hints["run_command"]["readOnlyHint"] is False
    assert hints["run_command"]["destructiveHint"] is True
    assert all(s["inputSchema"].get("type") == "object" for s in schemas)


def test_a_refusal_explains_which_switch_was_missing():
    offer = build_offer(".")
    assert "--allow-write" in (offer.refuse("write_file") or "")
    assert "--allow-run" in (offer.refuse("run_command") or "")
    assert offer.refuse("read_file") is None
    assert "no tool named" in (offer.refuse("nonsense") or "")


# ------------------------------------------------------ answering in place


def answer(offer: Offer, method: str, **params):
    """One request through the handler, without a process in the way."""
    server = Server(offer)
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert reply is not None
    return reply


def test_a_notification_is_not_answered():
    server = Server(build_offer("."))
    assert (
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    )


def test_an_unknown_method_is_a_protocol_error(tmp_path):
    reply = answer(build_offer(tmp_path), "tools/nonsense")
    assert reply["error"]["code"] == -32601


def test_a_refused_tool_is_a_result_the_model_can_read(workspace):
    # Not a JSON-RPC error: the caller asked something reasonable and the
    # answer is no, which is a thing to say rather than a malformed request.
    reply = answer(
        build_offer(workspace),
        "tools/call",
        name="write_file",
        arguments={"path": "x.txt", "content": "no"},
    )
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "--allow-write" in reply["result"]["content"][0]["text"]
    assert not (workspace / "x.txt").exists()


def test_a_path_outside_the_workspace_is_refused(workspace):
    reply = answer(
        build_offer(workspace),
        "tools/call",
        name="read_file",
        arguments={"path": "../../../etc/passwd"},
    )
    assert reply["result"]["isError"] is True


def test_a_missing_argument_is_reported_rather_than_raised(workspace):
    reply = answer(build_offer(workspace), "tools/call", name="read_file", arguments={})
    assert reply["result"]["isError"] is True
    assert "missing argument" in reply["result"]["content"][0]["text"]


def test_a_denied_command_stays_denied_here_too(workspace):
    offer = build_offer(
        workspace,
        allow_run=True,
        agent_config={"commands": {"deny": ["rm"], "default": "allow"}},
    )
    reply = answer(
        offer, "tools/call", name="run_command", arguments={"command": "rm -rf ."}
    )
    assert reply["result"]["isError"] is True
    assert "deny list" in reply["result"]["content"][0]["text"]


# ------------------------------------------------------------- round trips


@pytest.mark.slow
def test_a_client_can_hand_shake_and_list_what_is_offered(workspace):
    client = client_for(workspace)
    try:
        client.connect()
        assert client.server_info.get("name") == "vox"
        names = sorted(tool.name for tool in client.list_tools())
        assert names == ["list_files", "read_file", "search_text"]
    finally:
        client.close()


@pytest.mark.slow
def test_a_client_can_read_a_file_through_the_protocol(workspace):
    client = client_for(workspace)
    try:
        client.connect()
        ok, text = client.call_tool("read_file", {"path": "hello.txt"})
        assert ok and "another" in text
        ok, found = client.call_tool("search_text", {"query": "needle"})
        assert ok and "deep.txt" in found
    finally:
        client.close()


@pytest.mark.slow
def test_a_write_goes_through_only_with_the_switch(workspace):
    client = client_for(workspace, "--allow-write")
    try:
        client.connect()
        assert "write_file" in {tool.name for tool in client.list_tools()}
        ok, said = client.call_tool(
            "write_file", {"path": "made.txt", "content": "by mcp\n"}
        )
        assert ok, said
        assert (workspace / "made.txt").read_text(encoding="utf-8") == "by mcp\n"
    finally:
        client.close()
