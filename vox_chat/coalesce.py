"""Crossing to the UI thread once per frame instead of once per token.

Every event the provider produced went over `call_from_thread`, which means a
context switch, a queue, and a wait for the Textual loop to pick it up — once
per token. On a fast local model that is several hundred hops a second, each
one carrying three characters, and the cost is not the hop itself but what it
makes the UI do at the far end: a transcript refresh and a re-layout per
token, whether or not anything changed visibly.

So text is gathered on the worker thread and handed over in batches. A batch
closes when enough time has passed or enough characters have accumulated,
whichever comes first.

Two things this is careful not to break:

- **Order.** Anything that is not text — a tool call, a usage report, the
  end of the turn — flushes whatever text is waiting before it goes. A tool
  result that arrived before the text preceding it would be a transcript
  nobody could read.
- **The feel of streaming.** The window is 50ms, which is under the ~100ms
  where a person starts to perceive delay and well over the per-token
  interval of any local model. Text still appears as it is written; it
  appears in words rather than in characters.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

# Under what a person perceives as delay, over what a model produces in.
WINDOW_SECONDS = 0.05

# A batch this large is worth sending whatever the clock says: a model that
# emits a whole paragraph in one delta should not wait on a timer.
MAX_CHARS = 400


@dataclass
class Batched:
    """One handover: some text, or one event that is not text."""

    text: str = ""
    event: Any = None

    @property
    def is_text(self) -> bool:
        return self.event is None


def coalesce(
    events: Iterable[Any],
    window: float = WINDOW_SECONDS,
    max_chars: int = MAX_CHARS,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[Batched]:
    """Group consecutive text events, passing everything else straight through.

    ``now`` is injectable so the timing can be tested without waiting for a
    real clock, which is the only way to test a time window honestly.
    """
    buffer: list[str] = []
    opened = 0.0

    def flush() -> Iterator[Batched]:
        if buffer:
            yield Batched(text="".join(buffer))
            buffer.clear()

    for event in events:
        if getattr(event, "type", None) == "text" and getattr(event, "text", ""):
            if not buffer:
                opened = now()
            buffer.append(event.text)
            size = sum(len(part) for part in buffer)
            if size >= max_chars or (now() - opened) >= window:
                yield from flush()
            continue
        # Not text: whatever is waiting goes first, or the transcript would
        # show a tool result before the sentence that led to it.
        yield from flush()
        yield Batched(event=event)
    yield from flush()
