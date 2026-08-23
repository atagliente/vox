"""Stream assembly and the agent loop, driven by a fake client.

No Ollama server, and no network, is involved anywhere in this file.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from vox_chat.agent import needs_confirmation, parse_arguments, run_turn
from vox_chat.llm_client import LLMError, consume_stream, to_api_messages
from vox_chat.models import Message
from vox_chat.tools import ToolError, Workspace


class FakeFunction:
    def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, index: int, id: str | None = None,
                 function: FakeFunction | None = None) -> None:
        self.index = index
        self.id = id
        self.function = function


class FakeDelta:
    def __init__(self, content: str | None = None,
                 tool_calls: list[FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta: FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, content: str | None = None,
                 tool_calls: list[FakeToolCall] | None = None,
                 finish_reason: str | None = None) -> None:
        self.choices = [FakeChoice(FakeDelta(content, tool_calls), finish_reason)]


def text_chunks(*parts: str) -> list[FakeChunk]:
    chunks = [FakeChunk(content=part) for part in parts]
    chunks.append(FakeChunk(finish_reason="stop"))
    return chunks


def test_text_fragments_are_assembled_once() -> None:
    events = list(consume_stream(text_chunks("Hel", "lo ", "world")))
    assert [e.text for e in events if e.type == "text"] == ["Hel", "lo ", "world"]
    assert "".join(e.text for e in events if e.type == "text") == "Hello world"
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"


def test_empty_and_headerless_chunks_are_ignored() -> None:
    chunks: list[Any] = [FakeChunk(content=None), FakeChunk(content=""), FakeChunk(content="x")]
    events = list(consume_stream(chunks))
    assert [e.text for e in events if e.type == "text"] == ["x"]


def test_tool_call_fragments_are_merged_by_index() -> None:
    chunks = [
        FakeChunk(tool_calls=[FakeToolCall(0, "call_1", FakeFunction("read_file", '{"pa'))]),
        FakeChunk(tool_calls=[FakeToolCall(0, None, FakeFunction(None, 'th": "a.py"}'))]),
        FakeChunk(tool_calls=[FakeToolCall(1, "call_2", FakeFunction("list_files", "{}"))]),
        FakeChunk(finish_reason="tool_calls"),
    ]
    events = list(consume_stream(chunks))
    calls = next(e for e in events if e.type == "tool_calls").tool_calls
    assert [c.id for c in calls] == ["call_1", "call_2"]
    assert calls[0].name == "read_file"
    assert calls[0].arguments == '{"path": "a.py"}'


def test_cancellation_stops_the_stream() -> None:
    cancel = threading.Event()

    def chunks() -> Iterator[FakeChunk]:
        yield FakeChunk(content="one")
        cancel.set()
        yield FakeChunk(content="two")

    events = list(consume_stream(chunks(), cancel))
    assert [e.type for e in events] == ["text", "cancelled"]


def test_client_cancellation_closes_request_and_stream() -> None:
    class Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    from vox_chat.llm_client import LLMClient

    client = LLMClient("http://127.0.0.1:1/v1", "test", timeout=1)
    request = Closable()
    stream = Closable()
    client._active_request_client = request  # type: ignore[assignment]
    client._active_stream = stream

    assert client.cancel_active_stream() is True
    assert request.closed is True
    assert stream.closed is True


def test_error_messages_are_never_sent_upstream() -> None:
    payload = to_api_messages(
        [
            Message(role="system", content="be nice"),
            Message(role="user", content="hi"),
            Message(role="error", content="LINK DOWN"),
            Message(role="tool", content="result", tool_call_id="c1", name="read_file"),
        ]
    )
    assert [m["role"] for m in payload] == ["system", "user", "tool"]
    assert payload[-1]["tool_call_id"] == "c1"


class FakeClient:
    """Replays scripted streams and records the requests it received."""

    def __init__(self, scripts: list[list[FakeChunk]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    def stream_chat(self, messages, model, temperature=0.2, max_tokens=None,
                    tools=None, cancel=None, include_usage=True):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": tools,
                "include_usage": include_usage,
            }
        )
        script = self.scripts.pop(0) if self.scripts else text_chunks("")
        return consume_stream(script, cancel)


AGENT_CONFIG = {
    "enabled": True,
    "confirm_writes": True,
    "confirm_commands": True,
    "command_timeout_seconds": 5,
    "max_tool_cycles": 2,
    "max_output_bytes": 4096,
}


def turn(client: FakeClient, ws: Workspace | None, confirm, **kwargs):
    return list(
        run_turn(
            client=client,
            messages=[Message(role="user", content="go")],
            model="fake",
            temperature=0.1,
            max_tokens=None,
            workspace=ws,
            agent_config=dict(AGENT_CONFIG, **kwargs),
            confirm=confirm,
            agent_enabled=True,
        )
    )


def test_plain_answer_produces_one_assistant_message(workspace: Path) -> None:
    client = FakeClient([text_chunks("all ", "done")])
    events = turn(client, Workspace(workspace), lambda name, body: True)
    done = events[-1]
    assert done.type == "assistant_done"
    assert len(done.messages) == 1
    assert done.messages[0].content == "all done"


def test_a_read_tool_runs_without_confirmation(workspace: Path) -> None:
    (workspace / "a.py").write_text("print(1)\n", encoding="utf-8")
    asked: list[str] = []
    client = FakeClient(
        [
            [
                FakeChunk(
                    tool_calls=[
                        FakeToolCall(0, "c1", FakeFunction("read_file", '{"path": "a.py"}'))
                    ]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("the file prints 1"),
        ]
    )
    events = turn(client, Workspace(workspace), lambda name, body: asked.append(name) or True)
    assert asked == [], "reads must not ask for confirmation"
    results = [e for e in events if e.type == "tool_result"]
    assert "print(1)" in results[0].text
    assert events[-1].messages[-1].content == "the file prints 1"


def test_a_write_is_not_performed_when_the_user_denies(workspace: Path) -> None:
    client = FakeClient(
        [
            [
                FakeChunk(
                    tool_calls=[
                        FakeToolCall(
                            0, "c1",
                            FakeFunction("write_file", '{"path": "x.txt", "content": "hi"}'),
                        )
                    ]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("understood"),
        ]
    )
    events = turn(client, Workspace(workspace), lambda name, body: False)
    denied = [e for e in events if e.type == "tool_denied"]
    assert denied and "denied" in denied[0].text
    assert not (workspace / "x.txt").exists()


def test_a_write_is_performed_once_authorised(workspace: Path) -> None:
    seen: list[str] = []
    client = FakeClient(
        [
            [
                FakeChunk(
                    tool_calls=[
                        FakeToolCall(
                            0, "c1",
                            FakeFunction("write_file", '{"path": "x.txt", "content": "hi"}'),
                        )
                    ]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("written"),
        ]
    )
    turn(client, Workspace(workspace), lambda name, body: seen.append(body) or True)
    assert (workspace / "x.txt").read_text(encoding="utf-8") == "hi"
    assert "WRITE x.txt" in seen[0]


def test_the_tool_cycle_limit_stops_a_loop(workspace: Path) -> None:
    def looping() -> list[FakeChunk]:
        return [
            FakeChunk(
                tool_calls=[FakeToolCall(0, "c", FakeFunction("list_files", '{"path": "."}'))]
            ),
            FakeChunk(finish_reason="tool_calls"),
        ]

    client = FakeClient([looping() for _ in range(5)])
    events = turn(client, Workspace(workspace), lambda name, body: True)
    assert any(e.type == "limit" for e in events)
    assert len(client.calls) == AGENT_CONFIG["max_tool_cycles"] + 1


def test_tools_are_not_offered_when_agent_mode_is_off(workspace: Path) -> None:
    client = FakeClient([text_chunks("plain")])
    list(
        run_turn(
            client=client,
            messages=[Message(role="user", content="go")],
            model="fake",
            temperature=0.1,
            max_tokens=None,
            workspace=Workspace(workspace),
            agent_config=AGENT_CONFIG,
            confirm=lambda name, body: True,
            agent_enabled=False,
        )
    )
    assert client.calls[0]["tools"] is None


def test_a_provider_without_tool_support_degrades_gracefully(workspace: Path) -> None:
    class RefusingClient(FakeClient):
        def stream_chat(self, messages, model, temperature=0.2, max_tokens=None,
                        tools=None, cancel=None, include_usage=True):
            if tools:
                raise LLMError("http", "provider returned HTTP 400",
                               "this model does not support tools")
            return super().stream_chat(
                messages, model, temperature, max_tokens, None, cancel, include_usage
            )

    client = RefusingClient([text_chunks("plain answer")])
    events = turn(client, Workspace(workspace), lambda name, body: True)
    assert any(e.type == "notice" and "TOOL CALLING" in e.text for e in events)
    assert events[-1].messages[-1].content == "plain answer"


def test_invalid_tool_arguments_are_reported_not_raised(workspace: Path) -> None:
    client = FakeClient(
        [
            [
                FakeChunk(
                    tool_calls=[FakeToolCall(0, "c1", FakeFunction("read_file", "{not json"))]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("recovered"),
        ]
    )
    events = turn(client, Workspace(workspace), lambda name, body: True)
    results = [e for e in events if e.type in ("tool_result", "tool_denied")]
    assert "invalid tool arguments" in results[0].text


def test_parse_arguments_and_confirmation_rules() -> None:
    assert parse_arguments("") == {}
    assert parse_arguments('{"a": 1}') == {"a": 1}
    with pytest.raises(ToolError):
        parse_arguments("[1, 2]")

    assert needs_confirmation("write_file", AGENT_CONFIG)
    assert needs_confirmation("run_command", AGENT_CONFIG)
    assert not needs_confirmation("read_file", AGENT_CONFIG)
    assert not needs_confirmation("write_file", dict(AGENT_CONFIG, confirm_writes=False))
