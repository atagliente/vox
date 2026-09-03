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


def rate(chunks: list) -> float:
    started = time.perf_counter()
    events = list(consume_stream(iter(chunks)))
    assert events
    return len(chunks) / max(time.perf_counter() - started, 1e-9)


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
    Measured, that description is wrong about *where*.

    Assembling a stream carrying logprobs costs about the same as one that
    does not — the two rates land within noise of each other, run to run,
    and which one comes out ahead varies. So the weight is not in this code.
    It is in the two places this cannot time: the provider sending a
    distribution per token over the wire, and the inspect screen redrawing a
    table that grows by a row per token.

    Written down here because "several times heavier" would send anyone
    optimising it to the wrong file.
    """
    plain = rate(text_chunks(*[f"tok{n} " for n in range(TOKENS)]))
    measured = rate(with_logprobs(TOKENS))
    ratio = plain / measured
    print(f"\n  plain    {plain:>10,.0f} tokens/s")
    print(f"  inspect  {measured:>10,.0f} tokens/s   ({ratio:.2f}x)")
    assert measured > 0
    # Within a factor of three either way is "the same, plus noise" at this
    # scale. A real regression in the assembly path would be an order of
    # magnitude, and would trip this.
    assert 0.33 < ratio < 3, (
        f"the assembly path is {ratio:.1f}x different with logprobs, which is "
        "outside noise: something in consume_stream changed"
    )


def test_recording_a_token_is_constant_work() -> None:
    """The inspection run holds every token of an answer. If adding the
    thousandth cost more than adding the tenth, a long answer would slow
    down as it went."""
    run = InspectionRun(
        model="m", provider="p", top_k=5, criteria=criteria_from_config({})
    )

    def add(count: int) -> float:
        started = time.perf_counter()
        for index in range(count):
            run.add(f"t{index}", -0.5, [], "answer")
        return time.perf_counter() - started

    first = add(2000)
    later = add(2000)
    assert later < first * 4, "adding to a long run costs the same as to a short one"


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
