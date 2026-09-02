"""Summarising the old part of a conversation instead of losing it.

`fitting.py` is the emergency: a provider has refused for lack of room, and
the largest system block gets cut so the turn survives. This is the thing that
should happen before that — when the conversation is approaching the window
rather than past it, the oldest turns are replaced by a summary of themselves.

Why summarise rather than truncate. Dropping the first turns of a session
loses exactly the part that says what is being worked on: the file that was
being edited, the decision that was taken, the constraint that was agreed.
A model asked twenty turns later why something is the way it is then has no
answer, and the operator has no idea why.

Three things this is careful about:

- **The recent turns are never touched.** Whatever is summarised, the last
  few exchanges stay verbatim, because that is what the current question is
  actually about.
- **A summary is marked as one.** It arrives as a system message that says
  what it is, so a model does not read it as something the operator wrote.
- **It costs a request.** Compaction asks the model to summarise, which is a
  real call. It happens when the threshold is crossed, and says so, rather
  than quietly on every turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Message
from .usage import estimate_tokens

SUMMARY_MARKER = "[EARLIER IN THIS CONVERSATION]"

INSTRUCTION = (
    "Summarise the conversation below for your own future reference. Keep: "
    "what is being worked on, decisions taken and why, file paths, names, "
    "numbers and constraints that were agreed. Drop: pleasantries, and "
    "anything already superseded. Write it as notes, not prose, and do not "
    "address anyone. Be brief but lose no fact that a later question could "
    "depend on."
)

# Turns kept verbatim however full the window gets: the current question is
# about these, and summarising them would be summarising the subject.
KEEP_RECENT = 6

# Below this there is nothing worth the request it would cost.
MIN_MESSAGES = 10


@dataclass(frozen=True)
class Plan:
    """What compaction would do, worked out before anything is asked of it."""

    older: list[Message]
    recent: list[Message]
    tokens_before: int

    @property
    def worth_doing(self) -> bool:
        return len(self.older) >= MIN_MESSAGES - KEEP_RECENT


def usage_ratio(messages: list[Message], window: int) -> float:
    """How full the window is, on our own estimate. 0 when there is no window."""
    if window <= 0:
        return 0.0
    used = estimate_tokens("".join(message.content for message in messages))
    return used / window


def should_compact(
    messages: list[Message], window: int, threshold: float, enabled: bool
) -> bool:
    """Has the conversation grown enough to be worth summarising?"""
    if not enabled or window <= 0 or len(messages) < MIN_MESSAGES:
        return False
    return usage_ratio(messages, window) >= threshold


def plan(messages: list[Message]) -> Plan:
    """Split the conversation into what to summarise and what to keep.

    The split falls on a user message, so a summary never ends halfway
    through an exchange with the answer to a question the summary dropped.
    """
    cut = max(0, len(messages) - KEEP_RECENT)
    while cut > 0 and messages[cut].role != "user":
        cut -= 1
    older, recent = messages[:cut], messages[cut:]
    return Plan(older, recent, estimate_tokens("".join(m.content for m in messages)))


def request_for(older: list[Message]) -> list[Message]:
    """The messages that ask the model to summarise those turns."""
    body = "\n\n".join(
        f"{message.role.upper()}: {message.content}"
        for message in older
        if message.content and message.role in ("user", "assistant", "system")
    )
    return [
        Message(role="system", content=INSTRUCTION),
        Message(role="user", content=body),
    ]


def apply(summary: str, plan: Plan) -> list[Message]:
    """The conversation with its older half replaced by the summary."""
    note = Message(
        role="system",
        content=f"{SUMMARY_MARKER}\n{summary.strip()}",
    )
    return [note, *plan.recent]


def describe(plan: Plan, after: list[Message]) -> str:
    """What the operator is told, in tokens they can compare."""
    now = estimate_tokens("".join(message.content for message in after))
    saved = max(0, plan.tokens_before - now)
    return (
        f"CONVERSATION COMPACTED - {len(plan.older)} earlier message(s) "
        f"summarised, about {saved} tokens freed. The last "
        f"{len(plan.recent)} are untouched."
    )
