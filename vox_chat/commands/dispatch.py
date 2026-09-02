"""The table that joins a command name to the function that runs it.

Built once, from the command table itself, so the two cannot drift: a command
declared in ``spec`` with no handler is caught at import time rather than by
someone typing it a month later.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import handlers
from .spec import COMMANDS, ParsedCommand

if TYPE_CHECKING:
    from ..app import VoxApp

Handler = Callable[["VoxApp", str], Any]


def _build_table() -> dict[str, Handler]:
    table: dict[str, Handler] = {}
    missing: list[str] = []
    for command in COMMANDS:
        function = getattr(handlers, f"cmd_{command.name.replace('-', '_')}", None)
        if function is None:
            missing.append(command.name)
        else:
            table[command.name] = function
    if missing:
        raise RuntimeError(
            "commands declared with nothing to run them: " + ", ".join(missing)
        )
    return table


TABLE: dict[str, Handler] = _build_table()


def run(app: VoxApp, parsed: ParsedCommand) -> Any:
    """Run a parsed command, or say why it cannot be run.

    Returns whatever the handler returned, coroutine included: the caller
    decides how to schedule it, because that is a Textual question and this
    is not the place for one.
    """
    handler = TABLE.get(parsed.name)
    if handler is None:  # pragma: no cover - _build_table refuses to let this happen
        app.write_error(f"command not wired: /{parsed.name}")
        return None
    return handler(app, parsed.argument)


def is_async(result: Any) -> bool:
    """Did the handler hand back something that still has to be awaited?"""
    return inspect.iscoroutine(result)
