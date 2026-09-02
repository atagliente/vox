"""Measurements over the output distribution the provider returns.

Everything here is arithmetic on logprobs: no model internals are involved,
and nothing in this module interprets what a number means. A flat
distribution is reported as a flat distribution, never as the model
"hesitating" or "thinking".

Entropy is computed over the returned top-k, because the API does not return
the tail of the vocabulary. It is called top-k entropy everywhere it is shown.
"""

from __future__ import annotations

import math
import re
import statistics
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Phase = Literal["thinking", "answer", "unattributed"]

MAX_TOP_K = 20
"""Measured limit of the Ollama endpoint: 25 is refused with HTTP 400."""

RETHINK_MARKERS = (
    "wait",
    "but",
    "actually",
    "hold on",
    "alternatively",
    "let me reconsider",
)
"""Words that open a reconsidering stretch, matched at the start of a line or
sentence. Listed here once so the report can show exactly what was matched."""

_MARKER_RE = re.compile(
    r"(?:^|(?<=[.!?\n]))\s*("
    + "|".join(re.escape(m) for m in RETHINK_MARKERS)
    + r")\b",
    re.IGNORECASE,
)

_PUNCTUATION = set(string.punctuation) | {"…", "—", "–", "“", "”", "‘", "’", "·"}

_FILLER_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "answer",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "conclusion",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "no",
        "of",
        "on",
        "or",
        "result",
        "so",
        "that",
        "the",
        "then",
        "therefore",
        "this",
        "to",
        "was",
        "we",
        "yes",
    ]
)
"""Words that carry no substance when comparing a conclusion to an answer."""


def is_punctuation(text: str) -> bool:
    """True for a token that is only whitespace, punctuation, or empty."""
    stripped = text.strip()
    if not stripped:
        return True
    return all(character in _PUNCTUATION for character in stripped)


@dataclass(frozen=True)
class Alternative:
    """One entry of the provider's top_logprobs for a position."""

    token: str
    logprob: float

    @property
    def probability(self) -> float:
        return math.exp(self.logprob)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "logprob": self.logprob,
            "probability": self.probability,
        }


@dataclass
class TokenRecord:
    """One emitted token with the distribution it was drawn from."""

    index: int
    text: str
    logprob: float
    alternatives: list[Alternative] = field(default_factory=list)
    phase: Phase = "answer"
    is_decision: bool = False

    @property
    def probability(self) -> float:
        return math.exp(self.logprob)

    @property
    def entropy(self) -> float:
        """Shannon entropy in bits over the returned top-k, not the vocabulary."""
        total = 0.0
        for alternative in self.alternatives:
            probability = alternative.probability
            if probability > 0:
                total -= probability * math.log2(probability)
        return total

    @property
    def margin(self) -> float:
        """Gap between the most likely alternative and the runner-up."""
        if len(self.alternatives) < 2:
            return 1.0
        ranked = sorted((a.probability for a in self.alternatives), reverse=True)
        return ranked[0] - ranked[1]

    @property
    def runners_up(self) -> list[Alternative]:
        """The alternatives that were not chosen, most likely first."""
        others = [a for a in self.alternatives if a.token != self.text]
        return sorted(others, key=lambda a: a.logprob, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "token": self.text,
            "logprob": self.logprob,
            "probability": self.probability,
            "top_k_entropy_bits": self.entropy,
            "margin": self.margin,
            "phase": self.phase,
            "is_decision_point": self.is_decision,
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


@dataclass(frozen=True)
class DecisionCriteria:
    """When a position counts as a decision point.

    The defaults are the ones the specification names; they are configuration,
    not discoveries, and the report prints the values that were used.
    """

    entropy_threshold: float = 1.0
    margin_threshold: float = 0.35
    min_distance: int = 3
    skip_punctuation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "entropy_threshold_bits": self.entropy_threshold,
            "margin_threshold": self.margin_threshold,
            "min_distance_tokens": self.min_distance,
            "skip_punctuation": self.skip_punctuation,
        }


@dataclass(frozen=True)
class PhaseStats:
    """Aggregates for one phase of a turn."""

    tokens: int = 0
    mean_probability: float | None = None
    mean_entropy: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "mean_probability": self.mean_probability,
            "mean_top_k_entropy_bits": self.mean_entropy,
        }


@dataclass(frozen=True)
class Segment:
    """A stretch of thinking, starting at the marker that opened it."""

    marker: str | None
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"marker": self.marker, "text": self.text}


def split_segments(thinking: str) -> list[Segment]:
    """Split thinking at rethink markers, keeping the marker that opened each."""
    if not thinking.strip():
        return []
    segments: list[Segment] = []
    cursor = 0
    marker: str | None = None
    for match in _MARKER_RE.finditer(thinking):
        if match.start() == 0 and not thinking[: match.start()].strip():
            marker = match.group(1)
            cursor = match.start()
            continue
        text = thinking[cursor : match.start()].strip()
        if text:
            segments.append(Segment(marker, text))
        marker = match.group(1)
        cursor = match.start()
    tail = thinking[cursor:].strip()
    if tail:
        segments.append(Segment(marker, tail))
    return segments


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def conclusion_match(
    thinking: str, answer: str
) -> Literal["yes", "no", "undetermined"]:
    """Whether the end of the thinking and the answer say the same thing.

    Decided by normalised containment, which only recognises agreement it can
    actually see; anything else is ``undetermined`` rather than a guess.
    """
    segments = split_segments(thinking)
    if not segments or not answer.strip():
        return "undetermined"
    last = _normalise(segments[-1].text)
    target = _normalise(answer)
    if not last or not target:
        return "undetermined"
    if target in last or last in target:
        return "yes"
    last_words = set(last.split())
    target_words = set(target.split())
    # The substance of the answer, with the words that carry none removed.
    # A bare word-overlap ratio would call "the answer is 21" and "the answer
    # is 22" agreement, which is exactly the mistake worth avoiding.
    substance = target_words - _FILLER_WORDS
    if substance and substance <= last_words:
        return "yes"
    if not last_words & target_words:
        return "no"
    return "undetermined"


@dataclass
class InspectionRun:
    """Everything measured for one turn, filled in as the tokens arrive."""

    model: str = ""
    provider: str = ""
    top_k: int = 5
    criteria: DecisionCriteria = field(default_factory=DecisionCriteria)
    records: list[TokenRecord] = field(default_factory=list)
    note: str | None = None
    """Why there is no data, when there is none."""

    _last_decision: int | None = field(default=None, repr=False)

    def add(
        self,
        text: str,
        logprob: float,
        alternatives: Iterable[Alternative],
        phase: Phase = "answer",
    ) -> TokenRecord:
        """Append one token, deciding there and then whether it is a decision point."""
        record = TokenRecord(
            index=len(self.records),
            text=text,
            logprob=logprob,
            alternatives=list(alternatives),
            phase=phase,
        )
        record.is_decision = self._qualifies(record)
        if record.is_decision:
            self._last_decision = record.index
        self.records.append(record)
        return record

    def _qualifies(self, record: TokenRecord) -> bool:
        criteria = self.criteria
        if criteria.skip_punctuation and is_punctuation(record.text):
            return False
        if record.entropy < criteria.entropy_threshold:
            return False
        if record.margin > criteria.margin_threshold:
            return False
        return not (
            self._last_decision is not None
            and record.index - self._last_decision < criteria.min_distance
        )

    # ------------------------------------------------------------ selections

    def __len__(self) -> int:
        return len(self.records)

    @property
    def decisions(self) -> list[TokenRecord]:
        return [record for record in self.records if record.is_decision]

    def phase_records(self, phase: Phase) -> list[TokenRecord]:
        return [record for record in self.records if record.phase == phase]

    # ------------------------------------------------------------ statistics

    def phase_stats(self, phase: Phase) -> PhaseStats:
        records = self.phase_records(phase)
        if not records:
            return PhaseStats()
        return PhaseStats(
            tokens=len(records),
            mean_probability=statistics.fmean(r.probability for r in records),
            mean_entropy=statistics.fmean(r.entropy for r in records),
        )

    def stats(self) -> dict[str, Any]:
        """The figures the view and the report show, and nothing more."""
        if not self.records:
            return {
                "tokens": 0,
                "note": self.note or "no token data was collected",
                "top_k": self.top_k,
                "criteria": self.criteria.to_dict(),
            }
        probabilities = [record.probability for record in self.records]
        entropies = [record.entropy for record in self.records]
        thinking = self.phase_stats("thinking")
        answer = self.phase_stats("answer")
        difference = None
        if (
            thinking.mean_probability is not None
            and answer.mean_probability is not None
        ):
            difference = answer.mean_probability - thinking.mean_probability
        return {
            "tokens": len(self.records),
            "top_k": self.top_k,
            "entropy_basis": "top-k only; the API does not return the tail",
            "mean_probability": statistics.fmean(probabilities),
            "median_probability": statistics.median(probabilities),
            "mean_top_k_entropy_bits": statistics.fmean(entropies),
            "median_top_k_entropy_bits": statistics.median(entropies),
            "decision_points": len(self.decisions),
            "unattributed_tokens": len(self.phase_records("unattributed")),
            "thinking": thinking.to_dict(),
            "answer": answer.to_dict(),
            "answer_minus_thinking_probability": difference,
            "criteria": self.criteria.to_dict(),
        }

    def to_dict(self, include_tokens: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "model": self.model,
            "provider": self.provider,
            "statistics": self.stats(),
        }
        if self.note:
            data["note"] = self.note
        if include_tokens:
            data["tokens"] = [record.to_dict() for record in self.records]
        else:
            data["decision_points"] = [r.to_dict() for r in self.decisions]
        return data


def criteria_from_config(config: dict[str, Any]) -> DecisionCriteria:
    """Read the criteria out of the ``inspect`` block of the configuration."""
    section = config.get("inspect", {}) if isinstance(config, dict) else {}
    defaults = DecisionCriteria()
    return DecisionCriteria(
        entropy_threshold=float(
            section.get("entropy_threshold", defaults.entropy_threshold)
        ),
        margin_threshold=float(
            section.get("margin_threshold", defaults.margin_threshold)
        ),
        min_distance=int(section.get("min_distance", defaults.min_distance)),
        skip_punctuation=bool(
            section.get("skip_punctuation", defaults.skip_punctuation)
        ),
    )
