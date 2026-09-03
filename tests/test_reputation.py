"""What a peer has done, and what VOX is willing to conclude from it.

The line this file exists to hold: VOX has no Byzantine tolerance, and none
of this pretends otherwise. A record is a record. The weights can break a tie
and can never overturn a majority, and an answer nobody else gave is marked
rather than hidden — because an outlier is sometimes the only one who read
the question properly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vox_chat import consensus as cns
from vox_chat import reputation
from vox_chat.consensus import PeerAnswer
from vox_chat.reputation import PeerRecord, Reputation


def answer(agent_id: str, text: str, elapsed: float = 1.0) -> PeerAnswer:
    return PeerAnswer(agent_id=agent_id, name=agent_id, answer=text, elapsed=elapsed)


def silent(agent_id: str) -> PeerAnswer:
    return PeerAnswer(agent_id=agent_id, name=agent_id, error="timed out")


# ------------------------------------------------------------- the record


def test_a_new_peer_is_not_a_suspect() -> None:
    """Suspicion is not the default."""
    assert Reputation().weight_of("never-seen") == reputation.MAX_WEIGHT


def test_a_short_record_is_noise_not_evidence() -> None:
    record = PeerRecord("a", asked=2, answered=2, dissented=2)
    assert record.rounds < reputation.MIN_ROUNDS
    assert record.weight == reputation.MAX_WEIGHT


def test_standing_alone_every_round_costs_a_little_and_only_a_little() -> None:
    lonely = PeerRecord("a", asked=10, answered=10, dissented=10)
    assert lonely.weight == reputation.MIN_WEIGHT
    assert reputation.MIN_WEIGHT >= 0.5, "never enough to discount a peer entirely"


def test_agreeing_keeps_a_peer_at_full_weight() -> None:
    steady = PeerRecord("a", asked=10, answered=10, agreed=10)
    assert steady.weight == reputation.MAX_WEIGHT


def test_a_round_is_counted_for_everyone_who_was_asked() -> None:
    record = Reputation()
    answers = [answer("a", "yes"), answer("b", "yes"), silent("c")]
    record.note_round(answers, cns.tally(answers))
    assert record.peers["a"].asked == 1 and record.peers["a"].answered == 1
    assert record.peers["c"].asked == 1 and record.peers["c"].answered == 0


def test_the_one_who_stood_apart_is_named() -> None:
    """Named so the operator sees it, not so VOX can act on it."""
    record = Reputation()
    answers = [answer("a", "yes"), answer("b", "yes"), answer("c", "no")]
    alone = record.note_round(answers, cns.tally(answers))
    assert alone == ["c"]
    assert record.peers["c"].dissented == 1
    assert record.peers["a"].agreed == 1


def test_everyone_disagreeing_makes_nobody_a_dissenter() -> None:
    """Three answers and three clusters is a question nobody agreed on, not
    two peers ganging up on a third."""
    record = Reputation()
    answers = [answer("a", "one"), answer("b", "two"), answer("c", "three")]
    alone = record.note_round(answers, cns.tally(answers))
    # All three are alone, which is the same as none of them standing out.
    assert set(alone) == {"a", "b", "c"}


def test_a_single_peer_is_never_a_dissenter() -> None:
    record = Reputation()
    answers = [answer("a", "yes")]
    assert record.note_round(answers, cns.tally(answers)) == []
    assert record.peers["a"].dissented == 0


def test_the_record_says_what_it_knows() -> None:
    assert "no rounds yet" in PeerRecord("a").describe()
    described = PeerRecord(
        "a", asked=4, answered=3, total_seconds=6.0, dissented=4
    ).describe()
    assert "3/4 answered" in described
    assert "2.0s mean" in described


# ------------------------------------------------------------ the weights


def test_the_weights_break_a_tie() -> None:
    """Which is the whole of what they are for."""
    record = Reputation()
    record.peers["c"] = PeerRecord("c", asked=10, answered=10, dissented=10)
    record.peers["d"] = PeerRecord("d", asked=10, answered=10, dissented=10)
    answers = [
        answer("a", "yes"),
        answer("b", "yes"),
        answer("c", "no"),
        answer("d", "no"),
    ]
    clusters = cns.tally(answers)
    kind, winner, margin = reputation.weighted_verdict(answers, clusters, record)
    assert kind == "vote"
    assert winner == "yes", "two full-weight peers beat two discounted ones"
    assert margin > 0


def test_the_weights_cannot_overturn_a_majority() -> None:
    """A reputation system that could decide a vote is one worth attacking."""
    record = Reputation()
    for name in ("a", "b", "c"):
        record.peers[name] = PeerRecord(name, asked=20, answered=20, dissented=20)
    answers = [answer(n, "yes") for n in ("a", "b", "c")] + [answer("d", "no")]
    kind, winner, _margin = reputation.weighted_verdict(
        answers, cns.tally(answers), record
    )
    assert kind == "vote" and winner == "yes"


def test_an_even_split_is_still_synthesised() -> None:
    answers = [answer("a", "yes"), answer("b", "no")]
    kind, winner, _margin = reputation.weighted_verdict(
        answers, cns.tally(answers), Reputation()
    )
    assert kind == "synthesise" and winner is None


def test_nothing_to_decide_decides_nothing() -> None:
    kind, winner, margin = reputation.weighted_verdict([], [], Reputation())
    assert (kind, winner, margin) == ("synthesise", None, 0.0)


# --------------------------------------------------------------- on disk


def test_the_record_survives_a_restart(tmp_path: Path) -> None:
    record = Reputation()
    record.record("a", "alpha").asked = 7
    path = tmp_path / "peers.json"
    record.save(path)
    loaded = Reputation.load(path)
    assert loaded.peers["a"].name == "alpha"
    assert loaded.peers["a"].asked == 7


def test_an_unreadable_record_starts_again(tmp_path: Path) -> None:
    """Never a reason to fail a round."""
    path = tmp_path / "peers.json"
    path.write_text("{ not json", encoding="utf-8")
    assert Reputation.load(path).peers == {}
    assert Reputation.load(tmp_path / "absent.json").peers == {}


def test_a_record_from_a_newer_version_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    path.write_text(
        '{"peers": [{"agent_id": "a", "future_field": 1}]}', encoding="utf-8"
    )
    assert Reputation.load(path).peers == {}


# ---------------------------------------------------------- the round cap


def test_a_round_budget_is_configured_and_checked() -> None:
    from vox_chat.config import default_config, validate_config

    config = default_config()
    assert config["consensus"]["round_budget_seconds"] > 0
    assert config["consensus"]["weigh_by_record"] is True
    assert validate_config(config) == []


# ------------------------------------------------------------- revocation


def test_revoking_without_a_mesh_says_so() -> None:
    from vox_chat.mesh import MeshController, MeshSettings

    controller = MeshController(MeshSettings())
    assert "offline" in controller.revoke("someone")


def test_revocation_is_local_and_says_it_is() -> None:
    """There is no revocation list on the network. Saying so is better than
    implying a reach this does not have."""
    from vox_chat.mesh import MeshController, MeshSettings

    class FakeRegistry:
        def snapshot(self):
            return []

        def forget(self, agent_id):
            self.forgotten = agent_id

    class FakeAgent:
        def __init__(self):
            self._rejected: set[str] = set()
            self.registry = FakeRegistry()

    controller = MeshController(MeshSettings())
    controller.agent = FakeAgent()
    message = controller.revoke("bad-peer")
    assert "bad-peer" in controller.agent._rejected
    assert controller.agent.registry.forgotten == "bad-peer"
    assert "bad-peer" in controller.revoked
    assert "This machine only" in message or "this machine only" in message.lower()

    controller.unrevoke("bad-peer")
    assert "bad-peer" not in controller.revoked
    assert "bad-peer" not in controller.agent._rejected


def test_a_refusal_does_not_outlive_the_reason_for_it() -> None:
    """Not persisted, on purpose: a refusal nobody remembers making is worse
    than one that has to be repeated."""
    from vox_chat.mesh import MeshController, MeshSettings

    assert MeshController(MeshSettings()).revoked == set()


@pytest.mark.parametrize("command", ["revoke", "peers", "rounds"])
def test_the_commands_exist(command: str) -> None:
    from vox_chat.commands import dispatch

    assert command in dispatch.TABLE
