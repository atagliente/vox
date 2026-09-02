"""A floor under the speed of the streaming path, against a fake provider.

No network and no model: the chunks are already in memory, so what is timed
is only what VOX does with them — reassembly, the reasoning split, the
inspection bookkeeping. That is the part a refactor can quietly make
quadratic, and the symptom on a real answer is a transcript that falls behind
the model.

The floors are deliberately far below what the machine does, because a CI
runner is shared and a laptop throttles. They are there to catch a change
that costs an order of magnitude, not to measure the hardware.
"""

from __future__ import annotations

import sys
import time

import pytest

from vox_chat.llm_client import consume_stream
from vox_chat.reasoning import ThinkSplitter

from .test_streaming import FakeChunk, text_chunks


def _measuring_coverage() -> bool:
    """Is something counting every line as it runs?

    Under coverage the figure below is the tracer's speed, not the code's, and
    the run in CI that has to clear the coverage floor is exactly the one that
    would report it. Better to say so than to set a floor low enough to be
    meaningless.
    """
    if sys.gettrace() is not None:  # coverage on 3.11, and any debugger
        return True
    try:
        import coverage

        return coverage.Coverage.current() is not None
    except Exception:  # pragma: no cover - coverage not installed
        return False


pytestmark = pytest.mark.skipif(
    _measuring_coverage(),
    reason="a speed floor measured under coverage measures the tracer",
)

# A long answer as a provider sends it: many small deltas.
TOKENS = 20_000
FLOOR_TOKENS_PER_SECOND = 20_000


def measure(chunks: list[FakeChunk]) -> float:
    """Tokens per second through consume_stream, chunks already in memory."""
    started = time.perf_counter()
    events = list(consume_stream(iter(chunks)))
    elapsed = time.perf_counter() - started
    assert events, "the stream produced nothing to time"
    return len(chunks) / max(elapsed, 1e-9)


def test_plain_text_streams_faster_than_any_provider_sends_it() -> None:
    rate = measure(text_chunks(*[f"tok{index} " for index in range(TOKENS)]))
    assert rate > FLOOR_TOKENS_PER_SECOND, (
        f"{rate:,.0f} tokens/s through the assembly path; the floor is "
        f"{FLOOR_TOKENS_PER_SECOND:,} and a local model peaks near 30"
    )


def test_reasoning_tags_do_not_make_it_quadratic() -> None:
    """The splitter holds a partial tag back across chunks. Holding the whole
    buffer instead of the tail would look identical on short answers and turn
    a long one into O(n²)."""
    parts = ["<think>"]
    parts += [f"step{index} " for index in range(TOKENS // 2)]
    parts += ["</think>"]
    parts += [f"word{index} " for index in range(TOKENS // 2)]
    rate = measure(text_chunks(*parts))
    assert rate > FLOOR_TOKENS_PER_SECOND, (
        f"{rate:,.0f} tokens/s with reasoning tags in the stream"
    )


@pytest.mark.parametrize("chunk_size", [1, 3, 7])
def test_the_splitter_is_linear_in_the_answer_it_has_seen(chunk_size: int) -> None:
    """Fed a token at a time — the worst case a provider can produce — the
    cost per character must not depend on how much came before."""
    text = "a" * 40_000
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    splitter = ThinkSplitter()
    started = time.perf_counter()
    for chunk in chunks:
        list(splitter.feed(chunk))
    list(splitter.flush())
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, (
        f"{elapsed:.2f}s to split 40k characters in {chunk_size}-character "
        "chunks; that is the shape of a buffer that is never drained"
    )
