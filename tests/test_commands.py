"""Slash command parsing and the help reference."""

from __future__ import annotations

import pytest

from vox_chat import commands


@pytest.mark.parametrize(
    "text, name, argument",
    [
        ("/help", "help", ""),
        ("  /new  ", "new", ""),
        ("/model qwen2.5-coder:3b", "model", "qwen2.5-coder:3b"),
        ("/prompt-save my prompt name", "prompt-save", "my prompt name"),
        ("/session-load 2026-08-23", "session-load", "2026-08-23"),
        ("/agent on", "agent", "on"),
        ("/workspace C:/Users/me/project", "workspace", "C:/Users/me/project"),
        ("/HELP", "help", ""),
    ],
)
def test_parsing_known_commands(text: str, name: str, argument: str) -> None:
    parsed = commands.parse(text)
    assert parsed.is_command and parsed.known
    assert parsed.name == name
    assert parsed.argument == argument
    assert parsed.error is None


def test_aliases_resolve_to_the_canonical_name() -> None:
    assert commands.parse("/q").name == "exit"
    assert commands.parse("/quit").name == "exit"
    assert commands.parse("/?").name == "help"


def test_plain_text_is_not_a_command() -> None:
    parsed = commands.parse("how do I open a file in Python?")
    assert not parsed.is_command
    assert parsed.name == ""


def test_double_slash_escapes_a_message_starting_with_a_slash() -> None:
    assert not commands.is_command("//this is /not a command")
    assert not commands.parse("//help").is_command


def test_unknown_commands_produce_suggestions() -> None:
    parsed = commands.parse("/sess")
    assert parsed.is_command and not parsed.known
    assert parsed.error is not None and "unknown command" in parsed.error
    assert "sessions" in parsed.suggestions


def test_completion_of_a_prefix() -> None:
    assert set(commands.complete("prompt")) == {
        "prompts", "prompt", "prompt-save", "prompt-delete"
    }
    assert commands.complete("/mod") == ["model"]
    assert len(commands.complete("")) == len(commands.COMMANDS)


def test_every_spec_command_exists() -> None:
    required = {
        "help", "new", "clear", "settings", "config", "provider", "model",
        "role", "roles", "prompts", "prompt", "prompt-save", "prompt-delete",
        "sessions", "session-save", "session-load", "session-delete",
        "agent", "workspace", "stop", "exit",
    }
    assert required <= {spec.name for spec in commands.COMMANDS}


def test_help_text_lists_every_command() -> None:
    text = commands.help_text()
    for spec in commands.COMMANDS:
        assert spec.usage in text
    assert "enter       send" in text
    assert "alt+enter   new line" in text


def test_arguments_are_also_exposed_as_a_list() -> None:
    parsed = commands.parse("/prompt-save alpha beta")
    assert parsed.args == ["alpha", "beta"]
