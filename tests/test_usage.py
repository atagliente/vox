"""Token accounting: reported usage, estimates, throughput and averages."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from vox_chat.agent import run_turn
from vox_chat.llm_client import consume_stream
from vox_chat.models import Message
from vox_chat.tools import Workspace
from vox_chat.usage import (
    LiveMeter,
    TurnUsage,
    UsageTracker,
    estimate_tokens,
    format_tokens,
)

from .test_streaming import AGENT_CONFIG, FakeChunk, FakeClient, text_chunks


class FakeUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class UsageChunk(FakeChunk):
    """The final chunk providers send when include_usage is honoured."""

    def __init__(self, prompt: int, completion: int) -> None:
        super().__init__(finish_reason="stop")
        self.usage = FakeUsage(prompt, completion)


def test_estimates_and_formatting() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100
    assert format_tokens(999) == "999"
    assert format_tokens(1234) == "1.2k"
    assert format_tokens(34_000) == "34k"


def test_turn_usage_arithmetic() -> None:
    turn = TurnUsage(prompt_tokens=100, completion_tokens=50, elapsed=2.0)
    assert turn.total_tokens == 150
    assert turn.tokens_per_second == pytest.approx(25.0)
    assert TurnUsage(elapsed=0.0).tokens_per_second == 0.0


def test_tracker_totals_and_averages() -> None:
    tracker = UsageTracker()
    tracker.add(TurnUsage(100, 50, 2.0))
    tracker.add(TurnUsage(200, 150, 3.0))

    assert tracker.turns == 2
    assert tracker.prompt_tokens == 300
    assert tracker.completion_tokens == 200
    assert tracker.total_tokens == 500
    assert tracker.average_tokens_per_second == pytest.approx(200 / 5.0)
    assert tracker.average_tokens_per_turn == pytest.approx(250.0)
    assert tracker.peak_tokens_per_second == pytest.approx(50.0)
    assert tracker.context_tokens == 350, "context is the latest turn, not the sum"


def test_context_ratio_is_clamped() -> None:
    tracker = UsageTracker()
    tracker.add(TurnUsage(9000, 1000, 1.0))
    assert tracker.context_ratio(8192) == 1.0
    assert tracker.context_ratio(0) == 0.0
    assert UsageTracker().context_tokens == 0


def test_status_line_and_report() -> None:
    tracker = UsageTracker()
    assert tracker.status_line(8192) == ""
    assert "no completed turn" in tracker.report(8192)

    tracker.add(TurnUsage(1000, 200, 4.0))
    line = tracker.status_line(8192)
    assert "CTX 1.2k/8.2k 15%" in line
    assert "tok/s" in line

    report = tracker.report(8192)
    assert "SESSION USAGE" in report
    assert "1200" in report
    assert "50.0 tok/s" in report


def test_estimated_turns_are_flagged() -> None:
    tracker = UsageTracker()
    tracker.add(TurnUsage(10, 10, 1.0, estimated=True))
    assert "~est" in tracker.status_line(8192)
    assert "this provider reports no usage" in tracker.report(8192)


def test_live_meter_counts_while_streaming() -> None:
    meter = LiveMeter()
    assert meter.tokens == 0
    assert meter.tokens_per_second == 0.0
    meter.add_text("a" * 400)
    time.sleep(0.01)
    assert meter.tokens == 100
    assert meter.tokens_per_second > 0
    assert meter.first_token_latency is not None
    assert "tok/s" in meter.summary()


def test_reported_usage_reaches_the_stream_events() -> None:
    chunks = [FakeChunk(content="hello"), UsageChunk(prompt=42, completion=7)]
    events = list(consume_stream(chunks))
    usage = next(event for event in events if event.type == "usage")
    assert usage.usage is not None
    assert usage.usage.prompt_tokens == 42
    assert usage.usage.completion_tokens == 7
    assert events[-1].type == "done", "usage arrives before done"


def test_no_usage_event_when_the_provider_reports_nothing() -> None:
    events = list(consume_stream(text_chunks("hi")))
    assert not [event for event in events if event.type == "usage"]


def test_agent_turn_reports_provider_usage(workspace: Path) -> None:
    client = FakeClient([[FakeChunk(content="answer"), UsageChunk(120, 30)]])
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
    usage = next(event for event in events if event.type == "usage").usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens) == (120, 30)
    assert usage.estimated is False
    assert usage.elapsed >= 0
    assert events[-1].type == "assistant_done"


def test_agent_turn_estimates_when_the_provider_is_silent(workspace: Path) -> None:
    client = FakeClient([text_chunks("a" * 40)])
    events = list(
        run_turn(
            client=client,
            messages=[Message(role="user", content="b" * 80)],
            model="fake",
            temperature=0.1,
            max_tokens=None,
            workspace=Workspace(workspace),
            agent_config=AGENT_CONFIG,
            confirm=lambda name, body: True,
            agent_enabled=False,
        )
    )
    usage = next(event for event in events if event.type == "usage").usage
    assert usage is not None
    assert usage.estimated is True
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 10


def test_speed_ignores_the_wait_for_a_cold_model() -> None:
    """A model that took 39s to load still generated at its real rate."""
    turn = TurnUsage(
        prompt_tokens=67, completion_tokens=30, elapsed=42.0, first_token_latency=39.0
    )
    assert turn.generation_time == pytest.approx(3.0)
    assert turn.tokens_per_second == pytest.approx(10.0)

    tracker = UsageTracker()
    tracker.add(turn)
    assert tracker.average_tokens_per_second == pytest.approx(10.0)
    report = tracker.report(8192)
    assert "wall time          42.0s" in report
    assert "generating         3.0s" in report


def test_speed_without_a_latency_measurement_uses_the_whole_turn() -> None:
    turn = TurnUsage(prompt_tokens=10, completion_tokens=20, elapsed=2.0)
    assert turn.generation_time == pytest.approx(2.0)
    assert turn.tokens_per_second == pytest.approx(10.0)
