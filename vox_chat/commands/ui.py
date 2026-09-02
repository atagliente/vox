"""What a command handler is allowed to ask of the application.

A handler needs to say something in the transcript, read and write the
configuration, and reach a handful of stores. It does not need — and must not
reach for — the widget tree, the compose order, or the Textual event loop.
Writing that down as a Protocol is what lets a handler be tested against a
dozen lines of stand-in instead of a mounted application, and what stops the
boundary eroding one attribute at a time.

It is a Protocol, not a base class: ``VoxApp`` satisfies it by having the
methods, and nothing has to inherit anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CommandUI(Protocol):
    """The application, as a command handler sees it."""

    # --- state a handler reads and writes -------------------------------
    config: dict[str, Any]
    workspace_path: Path

    # --- saying something ------------------------------------------------
    def write_system(self, text: str) -> None:
        """A line of VOX's own voice in the transcript."""

    def write_error(self, text: str) -> None:
        """Something did not happen, and why."""

    def set_input(self, text: str) -> None:
        """Put text in the prompt box, ready to be edited or sent."""
