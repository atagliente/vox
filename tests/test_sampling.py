"""Sampling parameters: which are sent, where they go, and who wins.

The rule the whole module turns on is that a parameter VOX has not been told
about is not sent at all. That is not tidiness: `top_k` and `repeat_penalty`
are not OpenAI parameters, and a gateway that validates its input rejects a
request carrying them. Sending a default for each would break every strict
provider on every turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat import sampling
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config, validate_config
from vox_chat.storage import global_config_path
from vox_chat.ui.widgets import MessageBox


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


# ------------------------------------------------------------------ resolving


def test_nothing_unset_is_sent() -> None:
    """The default configuration sets temperature and max_tokens, which go on
    the existing path; nothing else reaches the request."""
    resolved = sampling.resolve(default_config())
    assert resolved.request_fields() == {}
    assert resolved.native_fields() == {}


def test_the_standard_ones_go_at_the_top_level() -> None:
    config = default_config()
    config["generation"].update({"top_p": 0.9, "seed": 7, "stop": ["END"]})
    fields = sampling.resolve(config).request_fields()
    assert fields == {"top_p": 0.9, "seed": 7, "stop": ["END"]}


def test_the_native_ones_travel_separately() -> None:
    """top_k and repeat_penalty are llama.cpp and Ollama parameters. A strict
    gateway rejects them at the top level, so they go in extra_body."""
    config = default_config()
    config["generation"].update({"top_k": 40, "repeat_penalty": 1.1})
    resolved = sampling.resolve(config)
    assert resolved.request_fields() == {}
    assert resolved.native_fields() == {"top_k": 40, "repeat_penalty": 1.1}


def test_think_is_how_a_reasoning_model_is_asked_to_reason() -> None:
    """VOX could always read thinking. Asking for it is a different thing,
    and was the gap."""
    config = default_config()
    config["generation"]["think"] = True
    assert sampling.resolve(config).native_fields()["think"] is True


def test_reasoning_effort_is_a_request_field() -> None:
    config = default_config()
    config["generation"]["reasoning_effort"] = "high"
    assert sampling.resolve(config).request_fields()["reasoning_effort"] == "high"


def test_a_model_preset_beats_the_general_setting() -> None:
    """Different models want different settings, and storing that beside the
    model is the only arrangement that survives switching between them."""
    config = default_config()
    config["active_model"] = "a-model"
    config["generation"]["top_p"] = 0.9
    config["model_presets"] = {"a-model": {"top_p": 0.5}}
    assert sampling.resolve(config).get("top_p") == 0.5


def test_a_preset_for_another_model_is_ignored() -> None:
    config = default_config()
    config["active_model"] = "a-model"
    config["generation"]["top_p"] = 0.9
    config["model_presets"] = {"other-model": {"top_p": 0.5}}
    assert sampling.resolve(config).get("top_p") == 0.9


def test_a_bad_value_in_the_configuration_does_not_fail_the_turn() -> None:
    """validate_config reports it; the turn drops it and carries on."""
    config = default_config()
    config["generation"]["top_p"] = "nonsense"
    assert "top_p" not in sampling.resolve(config).values


# ------------------------------------------------------------------ coercion


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("top_p", "0.9", 0.9),
        ("top_k", "40", 40),
        ("seed", "-1", -1),
        ("think", "on", True),
        ("think", "off", False),
        ("stop", "END,STOP", ["END", "STOP"]),
        ("reasoning_effort", "HIGH", "high"),
    ],
)
def test_values_are_read_the_way_an_operator_types_them(name, raw, expected) -> None:
    assert sampling.coerce(name, raw) == expected


@pytest.mark.parametrize(
    ("name", "raw", "says"),
    [
        ("top_p", "9", "between 0 and 2"),
        ("top_k", "0", "positive"),
        ("seed", "x", "whole number"),
        ("think", "maybe", "on or off"),
        ("reasoning_effort", "enormous", "minimal, low, medium, high"),
        ("nonsense", "1", "unknown parameter"),
        ("stop", "a,b,c,d,e", "at most 4"),
    ],
)
def test_a_bad_value_says_what_it_should_have_been(name, raw, says) -> None:
    with pytest.raises(ValueError, match=says):
        sampling.coerce(name, raw)


def test_the_configuration_is_checked_against_the_same_rules() -> None:
    """One definition of a valid top_p, so /set and the config file cannot
    disagree about it."""
    config = default_config()
    config["generation"]["top_p"] = 3.0
    config["model_presets"] = {"m": {"top_k": -1}}
    errors = validate_config(config)
    assert any("generation.top_p" in e for e in errors)
    assert any("model_presets.m.top_k" in e for e in errors)


# ------------------------------------------------------------------ commands


async def test_set_reports_only_what_is_actually_sent(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/set")
        await pilot.pause()
        written = transcript(app)
    assert "SAMPLING" in written
    assert "top_k" in written, "the settable list names them"


async def test_set_stores_a_value_and_clears_it_again(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/set top_p 0.85")
        await pilot.pause()
        assert app.config["generation"]["top_p"] == 0.85
        app.run_command("/set top_p off")
        await pilot.pause()
        assert "top_p" not in app.config["generation"]
        assert "cleared" in transcript(app)


async def test_a_bad_value_changes_nothing(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/set top_p enormous")
        await pilot.pause()
        assert "top_p" not in app.config["generation"]
        assert "SET - top_p is a number" in transcript(app)


async def test_set_preset_stores_what_is_in_force_for_this_model(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/set top_k 40")
        await pilot.pause()
        app.run_command("/set preset")
        await pilot.pause()
    model = app.config["active_model"]
    assert app.config["model_presets"][model]["top_k"] == 40


async def test_format_json_asks_for_an_object(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/format json")
        await pilot.pause()
        assert app.response_format == {"type": "json_object"}


async def test_format_takes_a_schema_and_off_removes_it(app: VoxApp) -> None:
    schema = '{"type": "object", "properties": {"answer": {"type": "string"}}}'
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command(f"/format {schema}")
        await pilot.pause()
        assert app.response_format is not None
        assert app.response_format["type"] == "json_schema"
        assert app.response_format["json_schema"]["schema"]["type"] == "object"
        app.run_command("/format off")
        await pilot.pause()
        assert app.response_format is None


async def test_a_schema_that_is_not_json_is_refused_clearly(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/format {not json at all")
        await pilot.pause()
        assert app.response_format is None
        assert "not JSON" in transcript(app)


def test_the_format_is_not_saved_to_disk() -> None:
    """A schema belongs to the question being asked, not to the installation."""
    assert "response_format" not in default_config()
