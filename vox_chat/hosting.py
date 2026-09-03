"""The surface a command is allowed to touch.

`handlers.py` was written against `VoxApp`, and the type annotation on every
one of its forty-nine functions says so. That looked like forty-nine functions
tied to Textual. Counting says otherwise: of two hundred and twenty-four uses
of `app.` in that file, fifty-six are `write_system`, thirty-three are
`write_error`, twenty-two are `config` and twelve are `persist_config` — and
**fifteen call sites in the whole file are actually about a screen**, all of
them of one shape, "open this view".

So the commands are not tied to a terminal. They are tied to a *host*: a thing
that holds the configuration and the conversation, can say something to the
person, and can be asked to show them a view. This module writes that surface
down, so a second host — a browser, in `vox_chat/webui/` — can satisfy it
without any of the commands being rewritten or duplicated.

Two things live here:

- :class:`Host`, a ``Protocol``. It is documentation with a type checker behind
  it: the honest answer to "what can a command do?", which nobody could give
  before because the answer was "anything on VoxApp".
- :class:`HostBase`, defaults that **refuse politely**. A host under
  construction inherits it, implements what it has, and everything else tells
  the person it is not available here instead of raising ``AttributeError`` at
  them. That is what lets the web host be built in stages that each work.

`VoxApp` implements the protocol without inheriting from it: it had almost all
of it already, and a Textual ``App`` has one base class too many as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# The views a command may ask for. Named rather than free-form so a typo is
# caught by the host that does not know the name, and so this list is the
# answer to "what screens are there" for whoever writes the next host.
VIEWS = (
    "inspect",
    "keys",
    "settings",
    "sessions",
    "save-session",
    "roles",
    "prompts",
    "universe",
    "round",
    "quit",
    # Takes an argument: "code", "index" or "consensus".
    "panel",
)


class HostError(Exception):
    """A host was asked for something it does not have."""


@runtime_checkable
class Host(Protocol):
    """What a slash command, and a turn, can ask of the thing running it.

    Deliberately wide. It is not an interface designed from first principles;
    it is a census of what the commands already do, and shrinking it would
    mean changing them, which is a separate job with its own reasons.
    """

    # ------------------------------------------------------------ the data

    config: dict[str, Any]
    session: Any
    workspace_path: Path
    code_blocks: list[Any]
    pending_images: list[Any]
    todos: Any
    undo: Any
    usage: Any
    mesh: Any
    mcp: Any
    reputation: Any
    role_store: Any
    prompt_store: Any
    search_server: Any
    search_index: Any
    response_format: Any
    connected: bool
    dirty: bool
    panel_mode: str

    # --------------------------------------------------------- saying things

    def write_system(self, text: str) -> None: ...

    def write_error(self, text: str) -> None: ...

    # ------------------------------------------------------------- the views

    def open_view(self, name: str, argument: str = "") -> None:
        """Show one of :data:`VIEWS`.

        The one call in this protocol that a terminal and a browser answer
        differently, and the reason the other seventy-odd do not have to be.
        """
        ...

    # -------------------------------------------------------- doing things

    def persist_config(self) -> None: ...

    def new_session(self) -> None: ...

    def save_session(self, name: str) -> None: ...

    def load_session(self, name: str) -> None: ...

    def clear_transcript(self) -> None: ...

    def stop_generation(self) -> None: ...

    def toggle_mesh(self) -> None: ...

    def set_role(self, name: str) -> None: ...

    def set_workspace(self, path: str) -> None: ...

    @property
    def draft_text(self) -> str:
        """What the person has typed but not sent."""
        ...


class HostBase:
    """Defaults that say no in words.

    Every method here is something a host may not have yet. Refusing with a
    sentence the person can read is the difference between a feature that is
    missing and a crash — and it is what makes it possible to ship a host
    that does half of this and grow it, rather than all of it or none.
    """

    def write_system(self, text: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError("a host has to be able to say something")

    def write_error(self, text: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError("a host has to be able to say something")

    def unavailable(self, what: str) -> None:
        """Say that this host cannot do ``what``, and carry on."""
        self.write_error(f"{what.upper()} IS NOT AVAILABLE HERE")

    def open_view(self, name: str, argument: str = "") -> None:
        self.unavailable(name)

    def new_session(self) -> None:
        self.unavailable("new session")

    def save_session(self, name: str) -> None:
        self.unavailable("save session")

    def load_session(self, name: str) -> None:
        self.unavailable("load session")

    def clear_transcript(self) -> None:
        self.unavailable("clear")

    def stop_generation(self) -> None:
        self.unavailable("stop")

    def toggle_mesh(self) -> None:
        self.unavailable("mesh")

    @property
    def draft_text(self) -> str:
        return ""
