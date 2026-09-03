"""Slash commands: what they are, and what runs them.

``spec`` knows the vocabulary — the command table, the parser, the help and
the key legend — and holds no application state at all. ``handlers`` holds
what each command does, as plain functions over a small view of the app
rather than methods on it. ``dispatch`` is the table that joins the two.

The point of the split is that ``VoxApp`` goes back to being the screen: it
draws, it owns the widgets, and it hands a parsed command to this package
instead of carrying forty methods that happen to live in the same class.
"""

from __future__ import annotations

from .spec import (
    COMMANDS,
    KEY_GROUPS,
    CommandSpec,
    KeyEntry,
    ParsedCommand,
    command_index,
    commands_text,
    complete,
    help_text,
    is_command,
    keys_text,
    legend_text,
    parse,
)

__all__ = [
    "COMMANDS",
    "KEY_GROUPS",
    "CommandSpec",
    "KeyEntry",
    "ParsedCommand",
    "command_index",
    "commands_text",
    "complete",
    "help_text",
    "is_command",
    "keys_text",
    "legend_text",
    "parse",
]
