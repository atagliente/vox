"""Input history: recall, draft preservation and persistence."""

from __future__ import annotations

from pathlib import Path

from vox_chat.history import InputHistory, history_path


def test_entries_are_recorded_and_persisted() -> None:
    history = InputHistory()
    history.add("first")
    history.add("/help")
    assert history_path().exists()
    assert InputHistory().entries == ["first", "/help"]


def test_blanks_and_immediate_repeats_are_skipped() -> None:
    history = InputHistory()
    history.add("same")
    history.add("same")
    history.add("   ")
    history.add("")
    assert history.entries == ["same"]


def test_walking_back_and_forward_restores_the_draft() -> None:
    history = InputHistory()
    for entry in ("one", "two", "three"):
        history.add(entry)

    assert history.previous("draft") == "three"
    assert history.previous("draft") == "two"
    assert history.previous("draft") == "one"
    assert history.previous("draft") is None, "nothing older than the first entry"

    assert history.next() == "two"
    assert history.next() == "three"
    assert history.next() == "draft", "the unsent draft comes back last"
    assert history.next() is None
    assert history.browsing is False


def test_next_without_browsing_does_nothing() -> None:
    history = InputHistory()
    history.add("one")
    assert history.next() is None


def test_an_empty_history_has_nothing_to_recall() -> None:
    assert InputHistory().previous("draft") is None


def test_submitting_ends_the_browsing_session() -> None:
    history = InputHistory()
    history.add("one")
    history.previous("draft")
    assert history.browsing is True
    history.add("two")
    assert history.browsing is False


def test_the_history_is_capped() -> None:
    history = InputHistory(max_entries=3)
    for index in range(6):
        history.add(f"entry-{index}")
    assert history.entries == ["entry-3", "entry-4", "entry-5"]
    assert InputHistory(max_entries=3).entries == ["entry-3", "entry-4", "entry-5"]


def test_a_corrupt_history_file_is_reported_not_fatal(vox_home: Path) -> None:
    history_path().write_text("{ not a list", encoding="utf-8")
    history = InputHistory()
    assert history.warnings
    assert history.entries == []
