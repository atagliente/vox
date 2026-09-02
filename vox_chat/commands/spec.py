"""Slash command parsing.

Pure functions only: the app decides what each command does, this module just
recognises them and produces help and completions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


class KeyEntry(NamedTuple):
    """One key combination and what it does."""

    keys: str
    what: str


# The one place the keys are written down. /help renders it, the legend modal
# renders it, and the bindings in app.py are checked against it by a test, so
# a key cannot be added in one place and go missing in the other two.
KEY_GROUPS: tuple[tuple[str, tuple[KeyEntry, ...]], ...] = (
    (
        "WRITING",
        (
            KeyEntry("enter", "send"),
            KeyEntry("alt+enter", "new line"),
            KeyEntry("ctrl+j", "new line"),
            KeyEntry("shift+enter", "new line"),
            KeyEntry("up / down", "walk the input history, at the edges of the text"),
            KeyEntry("ctrl+c", "copy"),
            KeyEntry("ctrl+v", "paste"),
            KeyEntry("ctrl+y", "copy the last code block"),
        ),
    ),
    (
        "SESSION",
        (
            KeyEntry("ctrl+n", "new session"),
            KeyEntry("ctrl+w", "save session"),
            KeyEntry("ctrl+s", "settings"),
            KeyEntry("ctrl+p", "prompts"),
            KeyEntry("ctrl+r", "roles"),
            KeyEntry("ctrl+e", "export"),
            KeyEntry("ctrl+q", "quit"),
        ),
    ),
    (
        "GENERATION",
        (
            KeyEntry("ctrl+g", "stop generation"),
            KeyEntry("f2", "agent mode on/off"),
            KeyEntry("f12", "models, arrows to choose"),
            KeyEntry("ctrl+t", "inspect"),
            KeyEntry("ctrl+b", "side panel"),
        ),
    ),
    (
        "MESH AND WEB",
        (
            KeyEntry("f3", "mesh online/off"),
            KeyEntry("f4", "universe"),
            KeyEntry("f5", "the round: agents thinking, live"),
            KeyEntry("f6", "web mode: answers researched first"),
        ),
    ),
    (
        "THIS LEGEND",
        (
            # ctrl+shift+<letter> is not delivered by every terminal, so the
            # legend is on a function key as well. Neither is worth much if the
            # only way to find them is to already know them, hence /keys too.
            KeyEntry("ctrl+shift+l", "open this legend"),
            KeyEntry("f1", "open it where the terminal swallows ctrl+shift+l"),
            KeyEntry("/keys", "the same legend, written into the transcript"),
            KeyEntry("esc", "close it"),
        ),
    ),
)


def keys_text(indent: str = "  ") -> str:
    """The whole key legend, one group per block, keys in a fixed column."""
    width = max(len(entry.keys) for _title, group in KEY_GROUPS for entry in group)
    lines: list[str] = []
    for title, group in KEY_GROUPS:
        if lines:
            lines.append("")
        lines.append(title)
        lines.extend(f"{indent}{e.keys.ljust(width)}   {e.what}" for e in group)
    return "\n".join(lines)


@dataclass(frozen=True)
class CommandSpec:
    """One slash command, its usage line and its help text."""

    name: str
    usage: str
    help: str
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "/help", "Show this help", ("h", "?")),
    CommandSpec("keys", "/keys", "Every key combination, in one place"),
    CommandSpec("new", "/new", "Start a new session", ("n",)),
    CommandSpec("clear", "/clear", "Clear the transcript, keep the session"),
    CommandSpec("settings", "/settings", "Open the settings modal"),
    CommandSpec("config", "/config", "Edit config.json in $VISUAL / $EDITOR"),
    CommandSpec("provider", "/provider [name]", "Show or switch provider"),
    CommandSpec("model", "/model [name]", "Show or switch model"),
    CommandSpec(
        "model-ctx", "/model ctx [N|off]", "Context window of the active model (Ollama)"
    ),
    CommandSpec(
        "model-gpu",
        "/model gpu [max|N|off]",
        "How many layers of it live on the GPU (Ollama)",
    ),
    CommandSpec("role", "/role [name]", "Show or switch the active role"),
    CommandSpec("roles", "/roles", "Open the role picker"),
    CommandSpec("prompts", "/prompts", "Open the saved prompt picker"),
    CommandSpec("prompt", "/prompt <name>", "Load a prompt into the input"),
    CommandSpec("prompt-save", "/prompt-save <name>", "Save the input as a prompt"),
    CommandSpec("prompt-delete", "/prompt-delete <name>", "Delete a saved prompt"),
    CommandSpec("sessions", "/sessions", "Open the session picker"),
    CommandSpec("session-save", "/session-save [name]", "Save the current session"),
    CommandSpec("session-load", "/session-load <name>", "Load a saved session"),
    CommandSpec("session-delete", "/session-delete <name>", "Delete a saved session"),
    CommandSpec("agent", "/agent on|off", "Toggle coding-agent mode"),
    CommandSpec("workspace", "/workspace <path>", "Set the agent workspace"),
    CommandSpec(
        "set", "/set [name value|off]", "Sampling parameters, or a model preset"
    ),
    CommandSpec(
        "format", "/format [json|<schema>|off]", "Make the answer match a JSON Schema"
    ),
    CommandSpec("stats", "/stats", "Token usage, context and speed", ("usage",)),
    CommandSpec("inspect", "/inspect [on|off]", "Per-token measurements, live"),
    CommandSpec(
        "export", "/export [html|json|md|toon]", "Save the session and its figures"
    ),
    CommandSpec("code", "/code [n]", "Show code blocks, copy one by number"),
    CommandSpec("panel", "/panel code|index|consensus", "Switch the side panel"),
    CommandSpec("connect", "/connect", "Re-check the provider connection"),
    CommandSpec("warm", "/warm", "Preload the active model on the server"),
    CommandSpec(
        "mesh",
        "/mesh [on|off|new-ca|sample-ca]",
        "Join or leave the mesh; swap the certificate authority",
    ),
    CommandSpec(
        "mcp",
        "/mcp [on|off|list|reload]",
        "Tools from Model Context Protocol servers",
    ),
    CommandSpec("universe", "/universe", "Agents on the mesh, with their category"),
    CommandSpec(
        "web",
        "/web [on|off|start|stop|status]",
        "Internet search on or off; start or stop the local engine",
    ),
    CommandSpec("search", "/search <query>", "Search the internet"),
    CommandSpec("fetch", "/fetch <url>", "Read one web page into the conversation"),
    CommandSpec("round", "/round", "The live log of the current consensus round"),
    CommandSpec(
        "consensus",
        "/consensus [on|off]",
        "Answer [CNS] … [/CNS] with the other agents on the mesh",
    ),
    CommandSpec("stop", "/stop", "Stop the current generation"),
    CommandSpec("exit", "/exit", "Quit VOX", ("quit", "q")),
)

_BY_NAME: dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    _BY_NAME[_spec.name] = _spec
    for _alias in _spec.aliases:
        _BY_NAME[_alias] = _spec


@dataclass
class ParsedCommand:
    """The result of parsing one input line."""

    name: str = ""
    argument: str = ""
    args: list[str] = field(default_factory=list)
    spec: CommandSpec | None = None
    is_command: bool = False
    error: str | None = None
    suggestions: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.spec is not None


def is_command(text: str) -> bool:
    """True when ``text`` looks like a slash command rather than a message."""
    stripped = text.strip()
    return stripped.startswith("/") and not stripped.startswith("//")


def parse(text: str) -> ParsedCommand:
    """Parse an input line into a :class:`ParsedCommand`.

    Non-command text yields ``is_command=False``; a leading ``//`` is the
    escape for a message that really does start with a slash.
    """
    stripped = text.strip()
    if not is_command(stripped):
        return ParsedCommand(is_command=False)
    body = stripped[1:]
    head, _, rest = body.partition(" ")
    name = head.strip().lower()
    argument = rest.strip()
    spec = _BY_NAME.get(name)
    if spec is None:
        return ParsedCommand(
            name=name,
            argument=argument,
            args=argument.split() if argument else [],
            is_command=True,
            error=f"unknown command: /{name}",
            suggestions=complete(name),
        )
    return ParsedCommand(
        name=spec.name,
        argument=argument,
        args=argument.split() if argument else [],
        spec=spec,
        is_command=True,
    )


def complete(prefix: str) -> list[str]:
    """Command names starting with ``prefix`` (leading slash optional)."""
    needle = prefix.lstrip("/").lower()
    if not needle:
        return [spec.name for spec in COMMANDS]
    exact = [spec.name for spec in COMMANDS if spec.name.startswith(needle)]
    if exact:
        return exact
    return [spec.name for spec in COMMANDS if needle in spec.name]


def help_text() -> str:
    """The full command reference, rendered for the transcript."""
    width = max(len(spec.usage) for spec in COMMANDS)
    lines = ["AVAILABLE COMMANDS", ""]
    lines.extend(f"  {spec.usage.ljust(width)}   {spec.help}" for spec in COMMANDS)
    lines.extend(["", "KEYS", ""])
    lines.extend(keys_text().splitlines())
    lines.extend(
        [
            "",
            "CONSENSUS",
            "  Mark part of a message with [CNS] … [/CNS] and it is answered by",
            "  the other agents on the mesh. Only the marked text leaves this",
            "  machine. /consensus shows who would be asked.",
            "",
            "Start a message with // to send text that begins with a slash.",
        ]
    )
    return "\n".join(lines)
