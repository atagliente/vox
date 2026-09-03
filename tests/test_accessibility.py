"""Quiet mode: what stops moving, and what the switch is.

The three things a screen reader trips over in VOX were visible from the code
before anybody ran one: the status bar repaints ten times a second, the
spinner in it is a braille glyph that changes every frame, and the modals had
a title drawn on screen but no name on the screen object. These tests hold
the answers to those three in place.

What they cannot do is prove VOX is usable with a screen reader. That takes
NVDA, JAWS, VoiceOver or Orca and someone listening, and it stays open in
cr.md for exactly that reason.
"""

from __future__ import annotations

import pytest

from vox_chat import accessibility
from vox_chat.ui.modals import ConfirmModal, KeysModal
from vox_chat.ui.widgets import ThinkingBox

# ------------------------------------------------------------- the switch


def test_quiet_mode_is_off_unless_it_is_asked_for():
    # No autodetection: no assistive technology announces itself to a
    # terminal program, and guessing wrong either way is worse than asking.
    assert accessibility.wanted({}, {}) is False
    assert accessibility.wanted({"ui": {}}, {}) is False


def test_the_configuration_turns_it_on():
    assert accessibility.wanted({"ui": {"screen_reader": True}}, {}) is True


def test_the_environment_wins_over_the_configuration():
    config = {"ui": {"screen_reader": True}}
    # Unlike NO_COLOR, where presence is the request, this one reads its
    # value: a single run has to be able to turn quiet mode off as well as on.
    assert accessibility.wanted(config, {"VOX_SCREEN_READER": "0"}) is False
    assert accessibility.wanted({}, {"VOX_SCREEN_READER": "1"}) is True
    assert accessibility.wanted({}, {"VOX_SCREEN_READER": ""}) is False


@pytest.mark.parametrize("value", ["false", "no", "off", "0", " OFF "])
def test_the_usual_ways_of_saying_no(value):
    assert accessibility.wanted({}, {"VOX_SCREEN_READER": value}) is False


# ------------------------------------------------------ what stops moving


def test_the_status_line_slows_from_ten_a_second_to_one():
    assert accessibility.tick_seconds(False) == 0.1
    assert accessibility.tick_seconds(True) == 1.0


def test_the_wait_is_spelled_out_instead_of_spun():
    loud = accessibility.waiting_text("WAITING FOR MODEL", 3.42, quiet=False)
    quiet = accessibility.waiting_text("WAITING FOR MODEL", 3.42, quiet=True)
    assert loud == "WAITING FOR MODEL… 3.4s"
    # Whole seconds and a word: a tenth of a second that changes ten times a
    # second is not information anyone can listen to.
    assert quiet == "WAITING FOR MODEL, 3 seconds"
    assert accessibility.waiting_text("X", 1.0, quiet=True) == "X, 1 second"


def test_the_thinking_box_drops_the_braille_frame():
    box = ThinkingBox("WAITING FOR MODEL", quiet=True)
    box._started -= 2.0
    box._advance = ThinkingBox._advance.__get__(box)
    rendered = []
    box.update = rendered.append  # type: ignore[method-assign]
    box._advance()
    text = str(rendered[-1])
    assert "WAITING FOR MODEL" in text
    assert not any(char in text for char in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_the_thinking_box_still_spins_for_everyone_else():
    box = ThinkingBox("WAITING FOR MODEL")
    rendered = []
    box.update = rendered.append  # type: ignore[method-assign]
    box._advance()
    assert any(char in str(rendered[-1]) for char in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


# ------------------------------------------------------------ modal names


def test_a_modal_names_itself_and_not_only_on_screen():
    """The Label was always there; the screen's own name was not."""
    modal = ConfirmModal("WRITE FILE", "a diff")
    label = modal.titled("WRITE FILE")
    assert modal.title == "WRITE FILE"
    assert "WRITE FILE" in str(label.content)
    assert label.id == "modal-title"


def test_every_modal_can_name_itself():
    # The mixin is what stops the drawn title and the screen name drifting
    # apart, so what matters is that every modal has it.
    for modal_type in (ConfirmModal, KeysModal):
        assert hasattr(modal_type, "titled")
