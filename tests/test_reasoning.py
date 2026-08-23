"""Reasoning: dedicated fields, inline <think> tags and how it is displayed."""

from __future__ import annotations

from pathlib import Path

from vox_chat.agent import run_turn
from vox_chat.llm_client import consume_stream
from vox_chat.models import Message, Session
from vox_chat.reasoning import ThinkSplitter
from vox_chat.tools import Workspace

from .test_streaming import AGENT_CONFIG, FakeChunk, FakeClient, FakeDelta, FakeChoice


class ReasoningChunk(FakeChunk):
    """A chunk carrying the dedicated reasoning field some providers use."""

    def __init__(self, reasoning: str) -> None:
        super().__init__()
        self.choices = [FakeChoice(FakeDelta(None))]
        self.choices[0].delta.reasoning_content = reasoning


def test_inline_think_tags_are_split_out() -> None:
    splitter = ThinkSplitter()
    segments = list(splitter.feed("<think>weighing options</think>the answer"))
    assert segments == [("reasoning", "weighing options"), ("text", "the answer")]


def test_a_tag_split_across_chunks_is_still_recognised() -> None:
    splitter = ThinkSplitter()
    segments: list[tuple[str, str]] = []
    for chunk in ("Hi <thi", "nk>step one", " step two</thi", "nk> done"):
        segments += list(splitter.feed(chunk))
    segments += list(splitter.flush())
    assert segments == [
        ("text", "Hi "),
        ("reasoning", "step one"),
        ("reasoning", " step two"),
        ("text", " done"),
    ]


def test_unclosed_thinking_is_flushed_as_reasoning() -> None:
    splitter = ThinkSplitter()
    segments = list(splitter.feed("<think>never closed"))
    segments += list(splitter.flush())
    assert segments == [("reasoning", "never closed")]
    assert ThinkSplitter().thinking is False


def test_text_without_tags_passes_through_untouched() -> None:
    splitter = ThinkSplitter()
    segments = list(splitter.feed("plain answer")) + list(splitter.flush())
    assert segments == [("text", "plain answer")]


def test_a_lone_angle_bracket_is_not_held_back_forever() -> None:
    splitter = ThinkSplitter()
    segments = list(splitter.feed("2 < 3 and 4 > 1")) + list(splitter.flush())
    assert "".join(text for _, text in segments) == "2 < 3 and 4 > 1"


def test_the_dedicated_reasoning_field_becomes_an_event() -> None:
    chunks = [ReasoningChunk("thinking hard"), FakeChunk(content="answer"),
              FakeChunk(finish_reason="stop")]
    events = list(consume_stream(chunks))
    kinds = [(event.type, event.text) for event in events if event.text]
    assert ("reasoning", "thinking hard") in kinds
    assert ("text", "answer") in kinds


def test_reasoning_is_kept_on_the_message_but_never_sent_upstream(
    workspace: Path,
) -> None:
    client = FakeClient(
        [[FakeChunk(content="<think>let me see</think>the answer"),
          FakeChunk(finish_reason="stop")]]
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
        )
    )
    assert [e.text for e in events if e.type == "reasoning"] == ["let me see"]
    assistant = events[-1].messages[0]
    assert assistant.content == "the answer"
    assert assistant.reasoning == "let me see"
    assert "reasoning" not in (assistant.to_api() or {})

    ui_only = Message(role="reasoning", content="shown, not sent")
    assert ui_only.to_api() is None


def test_reasoning_survives_a_session_round_trip() -> None:
    original = Message(role="assistant", content="answer", reasoning="because")
    restored = Message.from_dict(original.to_dict())
    assert restored.reasoning == "because"

    session = Session(messages=[original])
    assert Session.from_dict(session.to_dict()).messages[0].reasoning == "because"
