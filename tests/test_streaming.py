"""Stream assembly and the agent loop, driven by a fake client.

No Ollama server, and no network, is involved anywhere in this file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import openai
import pytest

from vox_chat.agent import needs_confirmation, parse_arguments, run_turn
from vox_chat.llm_client import (
    LLMError,
    _status_error,
    consume_stream,
    to_api_messages,
)
from vox_chat.models import Message
from vox_chat.tools import ToolError, Workspace


class FakeFunction:
    def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(
        self, index: int, id: str | None = None, function: FakeFunction | None = None
    ) -> None:
        self.index = index
        self.id = id
        self.function = function


class FakeDelta:
    def __init__(
        self, content: str | None = None, tool_calls: list[FakeToolCall] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta: FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason
        self.logprobs = None


class FakeAlternative:
    def __init__(self, token: str, logprob: float) -> None:
        self.token = token
        self.logprob = logprob


class FakeLogprob:
    def __init__(
        self,
        token: str,
        logprob: float,
        alternatives: list[FakeAlternative] | None = None,
    ) -> None:
        self.token = token
        self.logprob = logprob
        self.top_logprobs = alternatives or []


class FakeLogprobs:
    def __init__(self, entries: list[FakeLogprob]) -> None:
        self.content = entries


class FakeChunk:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[FakeToolCall] | None = None,
        finish_reason: str | None = None,
        logprobs: list[FakeLogprob] | None = None,
        reasoning: str | None = None,
    ) -> None:
        delta = FakeDelta(content, tool_calls)
        if reasoning is not None:
            delta.reasoning_content = reasoning
        self.choices = [FakeChoice(delta, finish_reason)]
        if logprobs is not None:
            self.choices[0].logprobs = FakeLogprobs(logprobs)


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
    chunks: list[Any] = [
        FakeChunk(content=None),
        FakeChunk(content=""),
        FakeChunk(content="x"),
    ]
    events = list(consume_stream(chunks))
    assert [e.text for e in events if e.type == "text"] == ["x"]


def test_tool_call_fragments_are_merged_by_index() -> None:
    chunks = [
        FakeChunk(
            tool_calls=[FakeToolCall(0, "call_1", FakeFunction("read_file", '{"pa'))]
        ),
        FakeChunk(
            tool_calls=[FakeToolCall(0, None, FakeFunction(None, 'th": "a.py"}'))]
        ),
        FakeChunk(
            tool_calls=[FakeToolCall(1, "call_2", FakeFunction("list_files", "{}"))]
        ),
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

    def stream_chat(
        self,
        messages,
        model,
        temperature=0.2,
        max_tokens=None,
        tools=None,
        cancel=None,
        include_usage=True,
        top_logprobs=None,
        # A stand-in for the client has to tolerate what the real one takes,
        # or every new parameter breaks every test that uses one.
        **kwargs,
    ):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": tools,
                "include_usage": include_usage,
                "top_logprobs": top_logprobs,
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
                        FakeToolCall(
                            0, "c1", FakeFunction("read_file", '{"path": "a.py"}')
                        )
                    ]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("the file prints 1"),
        ]
    )
    events = turn(
        client, Workspace(workspace), lambda name, body: asked.append(name) or True
    )
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
                            0,
                            "c1",
                            FakeFunction(
                                "write_file", '{"path": "x.txt", "content": "hi"}'
                            ),
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
                            0,
                            "c1",
                            FakeFunction(
                                "write_file", '{"path": "x.txt", "content": "hi"}'
                            ),
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
                tool_calls=[
                    FakeToolCall(0, "c", FakeFunction("list_files", '{"path": "."}'))
                ]
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
        def stream_chat(
            self,
            messages,
            model,
            temperature=0.2,
            max_tokens=None,
            tools=None,
            cancel=None,
            include_usage=True,
            top_logprobs=None,
            **kwargs,
        ):
            if tools:
                raise LLMError(
                    "http",
                    "provider returned HTTP 400",
                    "this model does not support tools",
                )
            return super().stream_chat(
                messages,
                model,
                temperature,
                max_tokens,
                None,
                cancel,
                include_usage,
                top_logprobs,
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
                    tool_calls=[
                        FakeToolCall(0, "c1", FakeFunction("read_file", "{not json"))
                    ]
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
    assert not needs_confirmation(
        "write_file", dict(AGENT_CONFIG, confirm_writes=False)
    )


def test_a_preview_that_refuses_the_path_denies_the_call(workspace: Path) -> None:
    """describe_call reads the workspace, so it can refuse before the tool does."""
    client = FakeClient(
        [
            [
                FakeChunk(
                    tool_calls=[
                        FakeToolCall(
                            0,
                            "c1",
                            FakeFunction(
                                "write_file",
                                '{"path": "../escape.txt", "content": "x"}',
                            ),
                        )
                    ]
                ),
                FakeChunk(finish_reason="tool_calls"),
            ],
            text_chunks("understood"),
        ]
    )
    events = turn(client, Workspace(workspace), lambda name, body: True)
    denied = [e for e in events if e.type in ("tool_result", "tool_denied")]
    assert "escapes the workspace" in denied[0].text
    assert not (workspace.parent / "escape.txt").exists()


# --------------------------------------------------------------- logprobs


def logprob_chunk(
    content: str, pairs: list[tuple[str, float]], reasoning: str | None = None
) -> FakeChunk:
    """A chunk carrying the distribution behind the token it delivers."""
    import math

    entries = [
        FakeLogprob(
            pairs[0][0],
            math.log(pairs[0][1]),
            [FakeAlternative(token, math.log(p)) for token, p in pairs],
        )
    ]
    return FakeChunk(content=content, reasoning=reasoning, logprobs=entries)


def test_logprobs_become_token_events_with_their_distribution() -> None:
    chunks = [
        logprob_chunk("The", [("The", 0.9), ("A", 0.1)]),
        logprob_chunk(" sky", [(" sky", 0.5), (" sun", 0.3), (" air", 0.2)]),
        FakeChunk(finish_reason="stop"),
    ]
    events = [event for event in consume_stream(chunks) if event.type == "token"]
    assert [event.token.text for event in events] == ["The", " sky"]
    assert events[0].phase == "answer"
    assert len(events[1].token.alternatives) == 3
    assert events[1].token.alternatives[0][0] == " sky"


def test_a_stream_without_logprobs_produces_no_token_events() -> None:
    events = list(consume_stream(text_chunks("plain")))
    assert not [event for event in events if event.type == "token"]


def test_tokens_are_attributed_to_the_phase_they_arrived_with() -> None:
    chunks = [
        logprob_chunk("", [("Hmm", 0.6), ("Wait", 0.4)], reasoning="Hmm"),
        logprob_chunk("Yes", [("Yes", 0.8), ("No", 0.2)]),
        FakeChunk(finish_reason="stop"),
    ]
    phases = [e.phase for e in consume_stream(chunks) if e.type == "token"]
    assert phases == ["thinking", "answer"]


def test_inline_think_tags_also_mark_the_phase() -> None:
    chunks = [
        logprob_chunk("<think>step", [("<think>step", 0.9), ("x", 0.1)]),
        logprob_chunk("</think>done", [("</think>done", 0.9), ("y", 0.1)]),
        FakeChunk(finish_reason="stop"),
    ]
    phases = [e.phase for e in consume_stream(chunks) if e.type == "token"]
    assert phases[0] == "thinking", "the tag opened before this token arrived"


def test_the_turn_relays_token_events_and_passes_top_k_through(workspace: Path) -> None:
    client = FakeClient(
        [
            [
                logprob_chunk("hi", [("hi", 0.7), ("yo", 0.3)]),
                FakeChunk(finish_reason="stop"),
            ]
        ]
    )
    events = list(
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
            top_logprobs=5,
        )
    )
    assert client.calls[0]["top_logprobs"] == 5
    tokens = [event for event in events if event.type == "token"]
    assert len(tokens) == 1 and tokens[0].token.text == "hi"


def test_by_default_no_logprobs_are_requested(workspace: Path) -> None:
    """Inspection off must leave the request exactly as it was."""
    client = FakeClient([text_chunks("plain")])
    turn(client, Workspace(workspace), lambda name, body: True)
    assert client.calls[0]["top_logprobs"] is None


def test_a_logprob_without_text_continues_the_phase() -> None:
    """Measured on qwen3.5:4b: about a quarter of chunks carry a logprob and
    an empty reasoning string. They are still thinking, not unattributed."""
    import math

    silent = FakeChunk(
        logprobs=[
            FakeLogprob("\n", math.log(0.9), [FakeAlternative("\n", math.log(0.9))])
        ]
    )
    chunks = [
        logprob_chunk("", [("Hmm", 0.6), ("Wait", 0.4)], reasoning="Hmm"),
        silent,
        logprob_chunk("", [("so", 0.7), ("then", 0.3)], reasoning="so"),
        logprob_chunk("No", [("No", 0.99), ("Yes", 0.01)]),
        silent,
        FakeChunk(finish_reason="stop"),
    ]
    phases = [event.phase for event in consume_stream(chunks) if event.type == "token"]
    assert phases == ["thinking", "thinking", "thinking", "answer", "answer"]


def test_tokens_before_any_delta_are_unattributed() -> None:
    """Only what arrives before the model has said which phase it is in."""
    import math

    orphan = FakeChunk(
        logprobs=[
            FakeLogprob(
                "?",
                math.log(0.5),
                [
                    FakeAlternative("?", math.log(0.5)),
                    FakeAlternative("!", math.log(0.5)),
                ],
            )
        ]
    )
    events = list(consume_stream([orphan, FakeChunk(finish_reason="stop")]))
    tokens = [event for event in events if event.type == "token"]
    assert [event.phase for event in tokens] == ["unattributed"]


def test_logprobs_are_not_asked_for_alongside_tools() -> None:
    """Ollama answers "logprobs is not supported with tools + stream" with a
    400, and a rejected request costs the whole turn."""
    from vox_chat.llm_client import LLMClient

    sent: list[dict] = []

    class FakeCompletions:
        def create(self, **payload):
            sent.append(payload)
            return iter(())

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

        def with_options(self, **_kwargs):
            return self

    client = LLMClient(base_url="http://localhost:11434/v1", api_key="x")
    # Each request builds its own disposable client, so that is what to replace.
    client._new_request_client = lambda: FakeClient()

    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    list(
        client.stream_chat(
            messages=[Message(role="user", content="hi")],
            model="m",
            temperature=0.0,
            max_tokens=10,
            tools=tools,
            top_logprobs=5,
        )
    )
    assert "logprobs" not in sent[-1], "not asked for when tools are in play"
    assert client.logprobs_refused is True, "and the operator is told once"

    # Without tools it is asked for as usual.
    client.logprobs_refused = False
    list(
        client.stream_chat(
            messages=[Message(role="user", content="hi")],
            model="m",
            temperature=0.0,
            max_tokens=10,
            top_logprobs=5,
        )
    )
    assert sent[-1]["logprobs"] is True
    assert sent[-1]["top_logprobs"] == 5


def test_context_overflow_is_named_rather_than_reported_as_http_400():
    """Ollama's refusal for lack of room has a remedy; "HTTP 400" has none."""
    inner = json.dumps(
        {
            "error": {
                "code": 400,
                "message": (
                    "request (4227 tokens) exceeds the available context size "
                    "(4096 tokens), try increasing it"
                ),
                "type": "exceed_context_size_error",
            }
        }
    )
    body = {"error": {"message": inner, "type": "invalid_request_error"}}

    class FakeStatusError(openai.APIStatusError):
        def __init__(self) -> None:
            self.status_code = 400
            self.body = body
            Exception.__init__(self, "Error code: 400")

    error = _status_error(FakeStatusError())
    assert error.kind == "context"
    assert "4227" in error.message and "4096" in error.message
    assert "shorten the turn" in error.message
    # The nested document is unwrapped, not shown as escaped JSON.
    assert error.detail.startswith("request (4227 tokens)")


def test_an_ordinary_400_still_reads_as_one():
    class FakeStatusError(openai.APIStatusError):
        def __init__(self) -> None:
            self.status_code = 400
            self.body = {"error": {"message": "unknown field 'foo'"}}
            Exception.__init__(self, "Error code: 400")

    error = _status_error(FakeStatusError())
    assert error.kind == "http"
    assert error.message == "provider returned HTTP 400"
    assert error.detail == "unknown field 'foo'"
