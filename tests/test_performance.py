"""The two numbers §11 asked for, measured rather than asserted.

`test_streaming_speed.py` puts a floor under the assembly path. This measures
the two things the roadmap said were described without a figure: what
inspection costs, and what batching the UI hops saved. Both run against a fake
provider, so what is timed is VOX and nothing else.

The assertions are loose on purpose. A CI runner is shared and a laptop
throttles; what these hold is the shape of the answer — inspection costs
something bounded, batching costs nothing — not a number from one machine.
"""

from __future__ import annotations

import sys
import time

import pytest

from vox_chat import coalesce
from vox_chat.inspection import InspectionRun, criteria_from_config
from vox_chat.llm_client import consume_stream

from .test_streaming import FakeChunk, FakeLogprob, text_chunks


def _measuring_coverage() -> bool:
    if sys.gettrace() is not None:
        return True
    try:
        import coverage

        return coverage.Coverage.current() is not None
    except Exception:  # pragma: no cover - coverage not installed
        return False


pytestmark = pytest.mark.skipif(
    _measuring_coverage(),
    reason="a speed measurement under coverage measures the tracer",
)

TOKENS = 6000


def rate(chunks: list, passes: int = 5) -> float:
    """Tokens per second, best of several passes.

    Best-of rather than mean, because the suite runs across every core and a
    single pass measures whatever else the scheduler was doing. The fastest
    pass is the one that got a whole core; the slow ones are the machine, not
    the code. Comparing two means under contention is how this test claimed a
    3.4x difference on one run and 0.8x on the next.
    """
    best = 0.0
    for _ in range(passes):
        started = time.perf_counter()
        events = list(consume_stream(iter(chunks)))
        elapsed = time.perf_counter() - started
        assert events
        best = max(best, len(chunks) / max(elapsed, 1e-9))
    return best


def with_logprobs(count: int) -> list:
    """The same stream, with the distribution behind each token attached."""
    chunks = [
        FakeChunk(
            content=f"tok{index} ",
            logprobs=[FakeLogprob(f"tok{index}", -0.5)],
        )
        for index in range(count)
    ]
    chunks.append(FakeChunk(finish_reason="stop"))
    return chunks


def test_what_inspection_costs_on_the_assembly_path() -> None:
    """Inspection has been called "several times heavier" without a figure.

    Here is one: assembling a stream that carries logprobs runs at about
    **1.6x the cost** of one that does not on Python 3.12, and about 1.8x on
    3.13 — steady across runs on a quiet machine, and steadily in the same
    direction. One `TokenSample` and its alternatives per token, which is
    real and is bounded.

    Getting this number took two attempts and the first was wrong in both
    directions. It compared two single passes while fifteen other test
    processes ran, so it reported the scheduler: 3.4x on one run, 0.8x on the
    next, and a conclusion of "within noise" that was itself noise. Best-of-N
    is what a microbenchmark needs when the suite owns every core.

    1.6x is not "several times", so the original description still overstates
    *this* path. The rest of the weight is where this cannot time it: the
    provider sending a distribution per token over the wire, and the inspect
    screen redrawing a table that grows by a row per token.
    """
    plain = rate(text_chunks(*[f"tok{n} " for n in range(TOKENS)]))
    measured = rate(with_logprobs(TOKENS))
    ratio = plain / measured
    print(f"\n  plain    {plain:>10,.0f} tokens/s")
    print(f"  inspect  {measured:>10,.0f} tokens/s   ({ratio:.2f}x)")
    assert measured > 0
    # The bound is far wider than the figure, on purpose. Quiet, this reads
    # 1.5-1.7x on 3.12 and 1.74-1.82x on 3.13; under a suite that owns every
    # core it has read past 3x, because best-of-N narrows contention without
    # removing it. A bound tight enough to be interesting is a bound that
    # fails on a shared runner, and a test that only passes on an idle
    # machine gets deleted rather than believed. What this catches is the
    # regression worth catching: per-token bookkeeping turning quadratic,
    # which is an order of magnitude and not a factor.
    assert 0.5 < ratio < 6, (
        f"the assembly path is {ratio:.2f}x with logprobs; it has measured "
        "1.5-1.8x quiet, so something in consume_stream changed shape"
    )


def test_recording_a_token_is_constant_work() -> None:
    """The inspection run holds every token of an answer. If adding the
    thousandth cost more than adding the tenth, a long answer would slow down
    as it went — and the slowing would arrive exactly when an answer is long
    enough to be worth inspecting.

    Best of three, for the same reason as `rate` above: two single passes
    under a suite that owns every core measure the scheduler. This test
    caught that on Python 3.13 and nowhere else, which is what a flaky
    threshold looks like from the outside.
    """
    best_first = best_later = float("inf")
    for _ in range(3):
        run = InspectionRun(
            model="m", provider="p", top_k=5, criteria=criteria_from_config({})
        )

        def add(count: int, run: InspectionRun = run) -> float:
            started = time.perf_counter()
            for index in range(count):
                run.add(f"t{index}", -0.5, [], "answer")
            return time.perf_counter() - started

        best_first = min(best_first, add(2000))
        best_later = min(best_later, add(2000))

    assert best_later < best_first * 4, (
        f"adding to a long run took {best_later / best_first:.1f}x adding to a "
        "short one; that is the shape of quadratic bookkeeping"
    )


def test_batching_the_ui_hops_costs_nothing() -> None:
    """The point of coalesce is to remove work at the far end. It must not
    add any at this one."""
    events = list(consume_stream(iter(text_chunks(*[f"t{n} " for n in range(TOKENS)]))))

    started = time.perf_counter()
    batches = list(coalesce.coalesce(events))
    elapsed = time.perf_counter() - started

    text_events = sum(1 for event in events if event.type == "text")
    text_batches = sum(1 for batch in batches if batch.is_text)
    print(f"\n  {text_events} text events -> {text_batches} handovers")
    assert text_batches < text_events, "that is the whole point"
    assert elapsed < 1.0


def test_the_batches_carry_every_character() -> None:
    """A performance change that loses text is not a performance change."""
    events = list(consume_stream(iter(text_chunks("one ", "two ", "three"))))
    joined = "".join(b.text for b in coalesce.coalesce(events) if b.is_text)
    assert joined == "one two three"
