"""Making an over-long request fit the window the model was loaded with.

A provider that refuses for lack of room has told us something useful: the
size of the prompt and the size of the window. That is enough to cut the
request down and try once more, which is better than losing the turn — and it
works on any provider, not only the one that happened to report it.

What gets cut is the largest system message, which in practice is the research
block: the sources are ordered best-first, so the tail is the least valuable
text in the request.
"""

from __future__ import annotations

from .models import Message
from .usage import CHARS_PER_TOKEN, estimate_tokens

TRIM_NOTE = "\n\n[trimmed to fit the model's context window]"

# Room left for the answer and for the tokeniser disagreeing with our estimate.
HEADROOM_TOKENS = 256

MIN_KEEP_CHARS = 400
"""Below this a block carries nothing useful, so it is dropped whole."""


def largest_system(messages: list[Message]) -> int | None:
    """Index of the longest system message, or None when there is none."""
    best: int | None = None
    for index, message in enumerate(messages):
        if message.role != "system" or not message.content:
            continue
        if best is None or len(message.content) > len(messages[best].content):
            best = index
    return best


def shrink(
    messages: list[Message], used: int, window: int,
    headroom: int = HEADROOM_TOKENS,
) -> list[Message] | None:
    """Cut the request to fit ``window``, or None when it cannot be done.

    ``used`` and ``window`` are the provider's own numbers, so the ratio
    between them is trustworthy even where our character estimate is not.
    """
    overflow = used - window + headroom
    if overflow <= 0:
        return None
    index = largest_system(messages)
    if index is None:
        return None

    body = messages[index].content
    # 15% over the arithmetic, because the estimate is an estimate.
    cut = int(overflow * CHARS_PER_TOKEN * 1.15)
    keep = len(body) - cut
    trimmed = list(messages)
    if keep < MIN_KEEP_CHARS:
        # Nothing worth keeping: dropping the block outright is the only cut
        # left, and it is still better than failing the turn.
        del trimmed[index]
        return trimmed if len(trimmed) < len(messages) else None
    trimmed[index] = Message(
        role="system",
        content=body[:keep].rstrip() + TRIM_NOTE,
        timestamp=messages[index].timestamp,
    )
    return trimmed


def describe(before: list[Message], after: list[Message]) -> str:
    """One line for the transcript: what was given up, in tokens."""
    lost = estimate_tokens("".join(m.content for m in before)) - estimate_tokens(
        "".join(m.content for m in after)
    )
    return f"PROMPT TRIMMED - about {max(lost, 0)} tokens of sources dropped to fit"
