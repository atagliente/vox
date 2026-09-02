"""CONSENSUS: the tag, and what to do with the answers it brings back.

``[CNS] … [/CNS]`` marks the part of a message that may leave the machine.
Everything outside the tag stays here. That boundary is the whole point of the
feature, so it lives in one place, is pure, and is tested on its own.

What comes back is reconciled, not agreed: several models answering the same
question are not a quorum. Identical answers are reported as a vote with its
tally; anything else is handed to the local model to synthesise, and the raw
replies stay on screen either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

OPEN = "[CNS]"
CLOSE = "[/CNS]"

# Case-insensitive, non-greedy, and newlines are part of a span.
_SPAN_RE = re.compile(
    re.escape(OPEN) + r"(.*?)" + re.escape(CLOSE), re.IGNORECASE | re.DOTALL
)
_OPEN_RE = re.compile(re.escape(OPEN), re.IGNORECASE)

DEFAULT_QUORUM = 2


@dataclass
class PeerAnswer:
    """One agent's reply, whatever became of it."""

    agent_id: str
    name: str = ""
    model: str = ""
    answer: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.answer.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "model": self.model,
            "answer": self.answer,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed, 2),
        }


@dataclass
class Marked:
    """The result of reading a message for consensus tags."""

    spans: list[str] = field(default_factory=list)
    text: str = ""          # what the local model sees: the tags removed
    unclosed: bool = False  # a stray [CNS] with no [/CNS] after it

    @property
    def wanted(self) -> bool:
        return bool(self.spans)

    @property
    def question(self) -> str:
        """The spans as one question, when there is more than one."""
        return "\n\n".join(self.spans)


def extract(text: str) -> Marked:
    """Read a message for ``[CNS]`` spans.

    An unclosed tag distributes nothing and says so: a stray tag must never
    silently ship the rest of the message.
    """
    spans = [span.strip() for span in _SPAN_RE.findall(text)]
    spans = [span for span in spans if span]
    stripped = _SPAN_RE.sub(lambda m: m.group(1).strip(), text).strip()
    remaining_opens = len(_OPEN_RE.findall(_SPAN_RE.sub("", text)))
    return Marked(spans=spans, text=stripped or text.strip(),
                  unclosed=remaining_opens > 0)


# ------------------------------------------------------------- reconciliation


_PUNCTUATION = ".,;:!?…"


def normalise(answer: str) -> str:
    """A form two answers can be compared in. For clustering only."""
    collapsed = " ".join(answer.split()).strip().lower()
    return collapsed.strip(_PUNCTUATION + " \"'`*").strip()


def tally(answers: list[PeerAnswer]) -> list[tuple[str, list[PeerAnswer]]]:
    """Cluster the usable answers by their normalised text, largest first."""
    clusters: dict[str, list[PeerAnswer]] = {}
    for answer in answers:
        if not answer.ok:
            continue
        clusters.setdefault(normalise(answer.answer), []).append(answer)
    return sorted(clusters.items(), key=lambda item: -len(item[1]))


def verdict(answers: list[PeerAnswer], quorum: int = DEFAULT_QUORUM):
    """``("vote", winner, clusters)`` or ``("synthesise", None, clusters)``.

    A vote needs both a quorum and a strict majority of the usable answers:
    two out of five agreeing is a coincidence, not a decision.
    """
    clusters = tally(answers)
    usable = sum(len(members) for _, members in clusters)
    if clusters:
        winner, members = clusters[0]
        if len(members) >= max(2, quorum) and len(members) * 2 > usable:
            return "vote", members[0].answer.strip(), clusters
    return "synthesise", None, clusters


def describe(answers: list[PeerAnswer]) -> str:
    """One line for the transcript: who answered, who did not."""
    ok = [answer for answer in answers if answer.ok]
    failed = [answer for answer in answers if not answer.ok]
    parts = [f"{len(ok)}/{len(answers)} answered"]
    if ok:
        slowest = max(answer.elapsed for answer in ok)
        parts.append(f"slowest {slowest:.1f}s")
    for answer in failed:
        parts.append(f"{answer.name or answer.agent_id}: {answer.error or 'no answer'}")
    return "  ·  ".join(parts)


ANSWER_SYSTEM_PROMPT = (
    "You are answering a single question sent to you by another agent on a "
    "local network. Answer it directly and concisely, on its own terms. You "
    "have no other context and must not ask for any."
)


def synthesis_prompt(question: str, answers: list[PeerAnswer]) -> str:
    """The system message that turns several replies into one answer."""
    lines = [
        "Several agents were asked the same question and answered "
        "independently. Their replies are below.",
        "",
        f"QUESTION: {question}",
        "",
    ]
    for index, answer in enumerate(answers, start=1):
        if not answer.ok:
            continue
        label = answer.name or answer.agent_id
        model = f", {answer.model}" if answer.model else ""
        lines.append(f"--- AGENT {index} ({label}{model}) ---")
        lines.append(answer.answer.strip())
        lines.append("")
    lines.extend([
        "Write the answer the user should act on. Say plainly where the agents "
        "agreed and where they did not, and if they disagreed on something "
        "that matters, say which reading you think is right and why. Do not "
        "invent agreement that is not there, and do not simply list the "
        "replies back."
    ])
    return "\n".join(lines)


def render_panel(answers: list[PeerAnswer]) -> str:
    """The replies, side by side, so a verdict can be checked not trusted."""
    if not answers:
        return f"No agent has answered yet.\n\nMark text with {OPEN} … {CLOSE}."
    lines: list[str] = []
    for answer in answers:
        label = answer.name or answer.agent_id
        if answer.ok:
            lines.append(f"{label}  ·  {answer.model or '?'}  ·  {answer.elapsed:.1f}s")
            lines.append(answer.answer.strip())
        else:
            lines.append(f"{label}  ·  no answer")
            lines.append(answer.error or "silent")
        lines.append("")
    return "\n".join(lines).rstrip()
