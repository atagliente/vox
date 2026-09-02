"""Token accounting: context usage, throughput and per-session averages.

Providers that honour ``stream_options.include_usage`` report exact counts;
for the ones that do not, everything here falls back to a character-based
estimate, and every number derived from it is flagged as approximate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 4
"""Rough ratio used when the provider reports no usage at all."""


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text``."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def format_tokens(count: int) -> str:
    """Compact token count: 987, 1.2k, 34k."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


@dataclass(frozen=True)
class TokenUsage:
    """Counts as reported by the provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    """Prompt tokens the provider served from its cache, where it says so.

    Last, and only ever last: these are built positionally in a dozen places,
    and a field inserted in the middle silently reassigns every one of them.
    """

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class TurnUsage:
    """What one completed turn cost, in tokens and in time."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed: float = 0.0
    estimated: bool = False
    first_token_latency: float | None = None
    # Appended rather than inserted: TurnUsage is built positionally in the
    # tests and in agent.py, and a field in the middle reassigns them all.
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def generation_time(self) -> float:
        """Streaming time only, with the model load and queue wait removed."""
        if self.first_token_latency is None:
            return self.elapsed
        return max(0.0, self.elapsed - self.first_token_latency)

    @property
    def tokens_per_second(self) -> float:
        """Generation speed, measured from the first token onwards.

        Counting the wait for a cold model would report a speed the model
        never had.
        """
        span = self.generation_time
        if span <= 0:
            return 0.0
        return self.completion_tokens / span


class LiveMeter:
    """Counts tokens while they stream, so the status bar can move."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.first_token_at: float | None = None
        self.characters = 0

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.first_token_at = None
        self.characters = 0

    def add_text(self, chunk: str) -> None:
        if self.first_token_at is None:
            self.first_token_at = time.monotonic()
        self.characters += len(chunk)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    @property
    def tokens(self) -> int:
        if not self.characters:
            return 0
        return max(1, round(self.characters / CHARS_PER_TOKEN))

    @property
    def tokens_per_second(self) -> float:
        if self.first_token_at is None:
            return 0.0
        span = max(1e-6, time.monotonic() - self.first_token_at)
        return self.tokens / span

    @property
    def first_token_latency(self) -> float | None:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.started_at

    def summary(self) -> str:
        """Live line shown while generating."""
        return f"{format_tokens(self.tokens)} tok  {self.tokens_per_second:.1f} tok/s"


@dataclass
class UsageTracker:
    """Running totals for one session."""

    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed: float = 0.0
    generation_elapsed: float = 0.0
    cached_tokens: int = 0
    estimated_turns: int = 0
    last: TurnUsage | None = None
    history: list[TurnUsage] = field(default_factory=list)

    def add(self, turn: TurnUsage) -> None:
        self.turns += 1
        self.prompt_tokens += turn.prompt_tokens
        self.completion_tokens += turn.completion_tokens
        self.cached_tokens += turn.cached_tokens
        self.elapsed += turn.elapsed
        self.generation_elapsed += turn.generation_time
        if turn.estimated:
            self.estimated_turns += 1
        self.last = turn
        self.history.append(turn)

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def average_tokens_per_second(self) -> float:
        """Throughput over the whole session, not a mean of the means."""
        if self.generation_elapsed <= 0:
            return 0.0
        return self.completion_tokens / self.generation_elapsed

    @property
    def average_tokens_per_turn(self) -> float:
        if not self.turns:
            return 0.0
        return self.total_tokens / self.turns

    @property
    def peak_tokens_per_second(self) -> float:
        return max((turn.tokens_per_second for turn in self.history), default=0.0)

    @property
    def context_tokens(self) -> int:
        """Tokens the last request actually carried: prompt plus its answer."""
        if self.last is None:
            return 0
        return self.last.total_tokens

    def context_ratio(self, window: int) -> float:
        if window <= 0:
            return 0.0
        return min(1.0, self.context_tokens / window)

    def status_line(self, window: int, live: LiveMeter | None = None) -> str:
        """One-line summary for the status bar."""
        parts: list[str] = []
        if window > 0 and self.context_tokens:
            percent = round(self.context_ratio(window) * 100)
            parts.append(
                f"CTX {format_tokens(self.context_tokens)}/{format_tokens(window)}"
                f" {percent}%"
            )
        if live is not None:
            parts.append(live.summary())
        elif self.last is not None:
            parts.append(
                f"{format_tokens(self.last.total_tokens)} tok"
                f"  {self.last.tokens_per_second:.1f} tok/s"
            )
        if self.cached_tokens:
            # Only when there is something to report: a provider that does
            # not cache says nothing, and a zero on the bar would look like a
            # feature that is failing rather than one that is absent.
            parts.append(f"cached {format_tokens(self.cached_tokens)}")
        if self.turns:
            parts.append(f"avg {self.average_tokens_per_second:.1f} tok/s")
        if self.estimated_turns:
            parts.append("~est")
        return "  ·  ".join(parts)

    def report(self, window: int) -> str:
        """The multi-line breakdown printed by ``/stats``."""
        if not self.turns:
            return "USAGE: no completed turn in this session yet."
        percent = round(self.context_ratio(window) * 100) if window else 0
        last = self.last
        lines = [
            "SESSION USAGE",
            "",
            f"  turns              {self.turns}",
            f"  prompt tokens      {self.prompt_tokens}",
            *(
                [
                    f"  served from cache  {self.cached_tokens}"
                    f"  ({self.cached_tokens * 100 // max(1, self.prompt_tokens)}% of the prompt)"
                ]
                if self.cached_tokens
                else []
            ),
            f"  completion tokens  {self.completion_tokens}",
            f"  total tokens       {self.total_tokens}",
            f"  tokens / turn      {self.average_tokens_per_turn:.0f}",
            "",
            f"  wall time          {self.elapsed:.1f}s",
            f"  generating         {self.generation_elapsed:.1f}s",
            f"  average speed      {self.average_tokens_per_second:.1f} tok/s",
            f"  peak speed         {self.peak_tokens_per_second:.1f} tok/s",
        ]
        if last is not None:
            lines.extend(
                [
                    "",
                    "LAST TURN",
                    "",
                    f"  prompt / completion  {last.prompt_tokens} / {last.completion_tokens}",
                    f"  wall time            {last.elapsed:.1f}s",
                    f"  generating           {last.generation_time:.1f}s",
                    f"  speed                {last.tokens_per_second:.1f} tok/s",
                ]
            )
            if last.first_token_latency is not None:
                lines.append(f"  first token after    {last.first_token_latency:.2f}s")
        if window:
            lines.extend(
                [
                    "",
                    "CONTEXT",
                    "",
                    f"  in use  {self.context_tokens} / {window} tokens ({percent}%)",
                ]
            )
        if self.estimated_turns:
            lines.extend(
                [
                    "",
                    f"  {self.estimated_turns} of {self.turns} turns were estimated: "
                    "this provider reports no usage.",
                ]
            )
        return "\n".join(lines)
