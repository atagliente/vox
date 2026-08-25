"""Measurements over the returned distribution: arithmetic and criteria."""

from __future__ import annotations

import math

import pytest

from vox_chat.inspection import (
    Alternative,
    DecisionCriteria,
    InspectionRun,
    conclusion_match,
    criteria_from_config,
    is_punctuation,
    split_segments,
)


def alts(*pairs: tuple[str, float]) -> list[Alternative]:
    """Alternatives from plain probabilities, the way a provider reports them."""
    return [Alternative(token, math.log(probability)) for token, probability in pairs]


def test_probability_entropy_and_margin_are_exact() -> None:
    record = InspectionRun().add(
        "a", math.log(0.5), alts(("a", 0.5), ("b", 0.25), ("c", 0.25))
    )
    assert record.probability == pytest.approx(0.5, abs=1e-12)
    # -(0.5*log2 0.5 + 0.25*log2 0.25 + 0.25*log2 0.25) = 1.5 bits exactly
    assert record.entropy == pytest.approx(1.5, abs=1e-12)
    assert record.margin == pytest.approx(0.25, abs=1e-12)


def test_a_certain_token_has_no_entropy_and_a_full_margin() -> None:
    record = InspectionRun().add("x", math.log(1.0), alts(("x", 1.0)))
    assert record.entropy == pytest.approx(0.0)
    assert record.margin == 1.0, "a single alternative leaves nothing to be close to"


def test_runners_up_exclude_the_chosen_token_and_are_ranked() -> None:
    record = InspectionRun().add(
        " Earth", math.log(0.43),
        alts((" Earth", 0.43), (" dominant", 0.28), (" selective", 0.11)),
    )
    assert [a.token for a in record.runners_up] == [" dominant", " selective"]


def test_a_flat_close_position_is_a_decision_point() -> None:
    run = InspectionRun(criteria=DecisionCriteria(min_distance=0))
    record = run.add(" of", math.log(0.4), alts((" of", 0.4), (" in", 0.35), (" to", 0.25)))
    assert record.is_decision
    assert run.decisions == [record]


def test_a_confident_position_is_not() -> None:
    run = InspectionRun(criteria=DecisionCriteria(min_distance=0))
    record = run.add(" sky", math.log(0.99), alts((" sky", 0.99), (" sun", 0.01)))
    assert not record.is_decision


def test_a_wide_margin_disqualifies_even_a_spread_distribution() -> None:
    run = InspectionRun(criteria=DecisionCriteria(min_distance=0, margin_threshold=0.35))
    spread = alts(("a", 0.6), ("b", 0.1), ("c", 0.1), ("d", 0.1), ("e", 0.1))
    record = run.add("a", math.log(0.6), spread)
    assert record.entropy > 1.0, "the distribution is spread"
    assert record.margin > 0.35
    assert not record.is_decision, "but the winner was well clear"


def test_punctuation_is_skipped_unless_asked_otherwise() -> None:
    assert is_punctuation(".") and is_punctuation("  ") and is_punctuation("")
    assert not is_punctuation(" word")

    flat = alts((".", 0.4), (",", 0.35), ("!", 0.25))
    skipping = InspectionRun(criteria=DecisionCriteria(min_distance=0))
    assert not skipping.add(".", math.log(0.4), flat).is_decision

    keeping = InspectionRun(
        criteria=DecisionCriteria(min_distance=0, skip_punctuation=False)
    )
    assert keeping.add(".", math.log(0.4), flat).is_decision


def test_decision_points_keep_their_distance() -> None:
    run = InspectionRun(criteria=DecisionCriteria(min_distance=3))
    flat = alts(("a", 0.4), ("b", 0.35), ("c", 0.25))
    marked = [run.add(f"w{i}", math.log(0.4), flat).is_decision for i in range(8)]
    assert marked == [True, False, False, True, False, False, True, False]


def test_statistics_of_an_empty_run_say_why() -> None:
    run = InspectionRun()
    run.note = "the provider rejected logprobs"
    stats = run.stats()
    assert stats["tokens"] == 0
    assert "rejected" in stats["note"]


def test_per_phase_statistics_split_thinking_from_answer() -> None:
    run = InspectionRun()
    run.add("t", math.log(0.5), alts(("t", 0.5), ("u", 0.5)), phase="thinking")
    run.add("a", math.log(0.9), alts(("a", 0.9), ("b", 0.1)), phase="answer")
    run.add("?", math.log(0.9), alts(("?", 0.9), ("!", 0.1)), phase="unattributed")

    stats = run.stats()
    assert stats["thinking"]["tokens"] == 1
    assert stats["answer"]["tokens"] == 1
    assert stats["unattributed_tokens"] == 1
    assert stats["answer_minus_thinking_probability"] == pytest.approx(0.4, abs=1e-9)
    assert "top-k" in stats["entropy_basis"], "the basis is always stated"


def test_an_answer_only_run_has_no_thinking_figures() -> None:
    run = InspectionRun()
    run.add("a", math.log(0.9), alts(("a", 0.9), ("b", 0.1)))
    stats = run.stats()
    assert stats["thinking"]["tokens"] == 0
    assert stats["thinking"]["mean_probability"] is None
    assert stats["answer_minus_thinking_probability"] is None


def test_thinking_is_split_at_rethink_markers() -> None:
    segments = split_segments(
        "17 divides by nothing. Wait, check 3. Actually 17 is prime."
    )
    assert [segment.marker for segment in segments] == [None, "Wait", "Actually"]
    assert segments[-1].text == "Actually 17 is prime."


def test_thinking_without_markers_is_one_segment() -> None:
    segments = split_segments("Straight through, no second thoughts.")
    assert len(segments) == 1 and segments[0].marker is None
    assert split_segments("   ") == []


@pytest.mark.parametrize(
    "thinking, answer, verdict",
    [
        ("So 17 is prime.", "17 is prime", "yes"),
        ("So 17 is prime.", "Yes, 17 is prime.", "yes"),
        ("So the answer is 21.", "Purple.", "no"),
        ("", "anything", "undetermined"),
        ("So it is 21 apples in a barrel somewhere", "21", "yes"),
        # The mistake worth avoiding: overlapping words, different number.
        ("So the answer is 21.", "The answer is 22.", "undetermined"),
    ],
)
def test_conclusion_match_never_guesses(thinking: str, answer: str, verdict: str) -> None:
    assert conclusion_match(thinking, answer) == verdict


def test_criteria_come_from_the_configuration() -> None:
    criteria = criteria_from_config(
        {"inspect": {"entropy_threshold": 2.0, "margin_threshold": 0.1,
                     "min_distance": 7, "skip_punctuation": False}}
    )
    assert criteria.entropy_threshold == 2.0
    assert criteria.min_distance == 7
    assert criteria.skip_punctuation is False
    assert criteria_from_config({}) == DecisionCriteria()
    assert criteria.to_dict()["min_distance_tokens"] == 7


def test_a_record_serialises_everything_it_measured() -> None:
    run = InspectionRun(model="m", provider="p", top_k=3)
    run.add("a", math.log(0.5), alts(("a", 0.5), ("b", 0.5)), phase="answer")
    data = run.to_dict()
    assert data["model"] == "m"
    assert data["tokens"][0]["token"] == "a"
    assert data["tokens"][0]["alternatives"][0]["probability"] == pytest.approx(0.5)
    assert "top_k_entropy_bits" in data["tokens"][0], "the name states the basis"
