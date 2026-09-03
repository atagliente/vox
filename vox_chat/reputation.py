"""What each peer has done, and what to make of an answer nobody else gave.

The README says it plainly and it stays true: **VOX has no Byzantine
tolerance.** A peer that authenticates and then lies is believed. mTLS
answers "who is this", not "is this true", and nothing here changes that.

What can be done without promising what is not there is smaller and honest:

- **Count what happened.** How often a peer answered, how long it took, and
  how often it was alone against everybody else. That is a record, not a
  judgement.
- **Say when one answer stands apart.** Four peers agree and the fifth does
  not. That fifth is *marked*, in front of the operator, and it is still
  shown — an outlier is sometimes the only one who read the question
  properly, and hiding it would be a worse failure than showing it.
- **Weight a vote, a little, by that record.** A peer that has been the lone
  dissenter every round counts for slightly less than one that has not. The
  weight is bounded and small, because a reputation system that could
  actually decide a vote is a reputation system worth attacking.

What this deliberately does not do is decide anything on its own. A weighted
quorum that quietly discounted a peer would be a claim to know who is
trustworthy, which is the claim the README refuses to make.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("reputation")

# The most and least a peer's vote can be worth. Narrow on purpose: wide
# enough to break a tie, never wide enough to overturn a majority.
MIN_WEIGHT = 0.6
MAX_WEIGHT = 1.0

# Below this many rounds there is no record, only noise.
MIN_ROUNDS = 4


@dataclass
class PeerRecord:
    """What one peer has done across the rounds this installation has run."""

    agent_id: str
    name: str = ""
    asked: int = 0
    answered: int = 0
    total_seconds: float = 0.0
    agreed: int = 0
    """Rounds where this peer was in the largest cluster."""
    dissented: int = 0
    """Rounds where it was the only one saying what it said."""

    @property
    def response_rate(self) -> float:
        return self.answered / self.asked if self.asked else 0.0

    @property
    def mean_seconds(self) -> float:
        return self.total_seconds / self.answered if self.answered else 0.0

    @property
    def rounds(self) -> int:
        return self.agreed + self.dissented

    @property
    def weight(self) -> float:
        """How much this peer's vote counts. Between MIN_WEIGHT and 1.0.

        A peer with no record counts fully: suspicion is not the default, and
        a new peer is not a suspect.
        """
        if self.rounds < MIN_ROUNDS:
            return MAX_WEIGHT
        lone = self.dissented / self.rounds
        return round(max(MIN_WEIGHT, MAX_WEIGHT - lone * (MAX_WEIGHT - MIN_WEIGHT)), 3)

    def describe(self) -> str:
        if not self.asked:
            return "no rounds yet"
        parts = [
            f"{self.answered}/{self.asked} answered",
            f"{self.mean_seconds:.1f}s mean",
        ]
        if self.rounds:
            parts.append(f"{self.dissented}/{self.rounds} alone")
        if self.weight < MAX_WEIGHT:
            parts.append(f"weight {self.weight}")
        return "  ·  ".join(parts)


@dataclass
class Reputation:
    """Every peer this installation has asked."""

    peers: dict[str, PeerRecord] = field(default_factory=dict)

    def record(self, agent_id: str, name: str = "") -> PeerRecord:
        entry = self.peers.get(agent_id)
        if entry is None:
            entry = PeerRecord(agent_id=agent_id, name=name)
            self.peers[agent_id] = entry
        if name:
            entry.name = name
        return entry

    def note_round(self, answers: list[Any], clusters: list[Any]) -> list[str]:
        """Fold one round into the record, and name the peers that stood apart.

        Returns the agent ids that were alone in what they said, so the UI can
        mark them. Marked, not hidden: an outlier is sometimes the only one
        who read the question properly.
        """
        largest = {
            answer.agent_id for _text, members in clusters[:1] for answer in members
        }
        alone = {
            members[0].agent_id
            for _text, members in clusters
            if len(members) == 1 and len(clusters) > 1
        }
        for answer in answers:
            entry = self.record(answer.agent_id, getattr(answer, "name", ""))
            entry.asked += 1
            if not answer.ok:
                continue
            entry.answered += 1
            entry.total_seconds += float(getattr(answer, "elapsed", 0.0) or 0.0)
            if answer.agent_id in largest:
                entry.agreed += 1
            elif answer.agent_id in alone:
                entry.dissented += 1
        return sorted(alone)

    def weight_of(self, agent_id: str) -> float:
        entry = self.peers.get(agent_id)
        return entry.weight if entry is not None else MAX_WEIGHT

    # ------------------------------------------------------------ on disk

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "peers": [asdict(p) for p in self.peers.values()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reputation:
        peers = {}
        for item in data.get("peers", []):
            try:
                record = PeerRecord(**item)
            except TypeError:
                continue
            peers[record.agent_id] = record
        return cls(peers)

    @classmethod
    def load(cls, path: Path) -> Reputation:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            # A record that cannot be read is a record to start again, never
            # a reason to fail a round.
            return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - a record, not a promise
            log.debug("reputation not saved: %s", exc)


def weighted_verdict(
    answers: list[Any],
    clusters: list[Any],
    reputation: Reputation,
    quorum: int = 2,
) -> tuple[str, str | None, float]:
    """The same decision as ``consensus.verdict``, with the votes weighted.

    Returns ``(kind, winner, margin)``. The weights are bounded to a narrow
    range, so this can break a tie between two clusters of equal size and can
    never overturn a majority — which is the point. A reputation system that
    could decide a vote on its own would be one worth attacking.
    """
    if not clusters:
        return "synthesise", None, 0.0
    scored = [
        (sum(reputation.weight_of(a.agent_id) for a in members), text, members)
        for text, members in clusters
    ]
    scored.sort(key=lambda item: -item[0])
    best, _text, members = scored[0]
    total = sum(weight for weight, _t, _m in scored)
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if len(members) >= max(2, quorum) and best * 2 > total:
        return "vote", members[0].answer.strip(), round(best - runner_up, 3)
    return "synthesise", None, round(best - runner_up, 3)
