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
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
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


# ------------------------------------------------------------------ live view


@dataclass
class Fragment:
    """One piece of what an agent was writing, as it arrived."""

    agent: str
    kind: str          # "text" or "reasoning"
    text: str
    ts: float


class RoundLog:
    """What every agent said during a round, in the order it arrived.

    Kept apart from the transcript on purpose: the transcript shows each peer's
    answer in one block, which loses the interleaving. Here the fragments stay
    in wall-clock order, so it is visible when two agents were writing at once
    and which one was still thinking while another had finished.
    """

    def __init__(self, limit: int = 4000) -> None:
        self.fragments: list[Fragment] = []
        self.question: str = ""
        self.conversation_id: str = ""
        # Set when this round is somebody else's: the agent that asked us.
        self.asked_by: str = ""
        self.started: float = 0.0
        self._limit = limit

    def begin(self, question: str, started: float, conversation_id: str = "",
              asked_by: str = "") -> None:
        self.fragments = []
        self.question = question
        self.conversation_id = conversation_id
        self.asked_by = asked_by
        self.started = started

    def add(self, agent: str, kind: str, text: str, ts: float) -> None:
        if not text:
            return
        last = self.fragments[-1] if self.fragments else None
        # A model arrives token by token; one line per token is unreadable, so
        # consecutive fragments from the same agent are joined.
        if last is not None and last.agent == agent and last.kind == kind:
            last.text += text
            return
        self.fragments.append(Fragment(agent, kind, text, ts or time.time()))
        if len(self.fragments) > self._limit:
            del self.fragments[: len(self.fragments) - self._limit]

    def agents(self) -> list[str]:
        seen: list[str] = []
        for fragment in self.fragments:
            if fragment.agent not in seen:
                seen.append(fragment.agent)
        return seen

    def lines(self, width: int = 76) -> list[tuple[str, str, str, str]]:
        """``(timestamp, agent, kind, body)`` rows, wrapped for a screen."""
        rows: list[tuple[str, str, str, str]] = []
        for fragment in self.fragments:
            stamp = datetime.fromtimestamp(fragment.ts).strftime("%H:%M:%S")
            body = " ".join(fragment.text.split())
            if not body:
                continue
            for index, chunk in enumerate(textwrap.wrap(body, width) or [body]):
                rows.append(
                    (stamp if index == 0 else "", fragment.agent, fragment.kind, chunk)
                )
        return rows

    def as_text(self, width: int = 76) -> str:
        """The plain rendering: ``14:32:07 node-b: …``."""
        out: list[str] = []
        for stamp, agent, kind, body in self.lines(width):
            head = f"{stamp} {agent}:" if stamp else " " * 9 + " " * (len(agent) + 1)
            mark = "· " if kind == "reasoning" else ""
            out.append(f"{head} {mark}{body}")
        return "\n".join(out)
