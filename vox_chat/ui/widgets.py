"""Transcript, header and status widgets."""

from __future__ import annotations

from datetime import datetime
from time import monotonic

from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Static, TextArea

from ..models import Message
from . import branding

_ROLE_LABEL = {
    "user": "YOU",
    "assistant": "VOX",
    "system": "SYS",
    "tool": "TOOL",
    "error": "ERR",
    "reasoning": "THINK",
    "peer": "PEER",
    "web": "WEB",
}


def _stamp(timestamp: str) -> str:
    """Render an ISO timestamp as HH:MM:SS, tolerating anything else."""
    try:
        return datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
    except ValueError:
        return timestamp[:8]


class MessageBox(Static):
    """One message in the transcript; assistant boxes grow while streaming."""

    def __init__(self, message: Message, show_timestamps: bool = True) -> None:
        super().__init__(classes=f"msg msg-{message.role}")
        self.message = message
        self.show_timestamps = show_timestamps

    def on_mount(self) -> None:
        self.refresh_content()

    def append_text(self, chunk: str) -> None:
        self.message.content += chunk
        self.refresh_content()

    def set_text(self, text: str) -> None:
        self.message.content = text
        self.refresh_content()

    def refresh_content(self) -> None:
        label = _ROLE_LABEL.get(self.message.role, self.message.role.upper())
        prefix = f"{label}"
        if self.show_timestamps and self.message.timestamp:
            prefix = f"{_stamp(self.message.timestamp)} {label}"
        body = self.message.content
        if self.message.role == "tool" and self.message.name:
            prefix = f"{prefix} {self.message.name}"
        rendered = Text()
        rendered.append(f"{prefix} ▸ ", style="bold")
        rendered.append(body or "…")
        self.update(rendered)


class Transcript(VerticalScroll):
    """The scrollable conversation area."""

    def __init__(self, show_timestamps: bool = True) -> None:
        super().__init__(id="transcript")
        self.show_timestamps = show_timestamps

    def add(self, message: Message) -> MessageBox:
        box = MessageBox(message, self.show_timestamps)
        self.mount(box)
        self.scroll_end(animate=False)
        return box

    def clear_messages(self) -> None:
        for child in list(self.children):
            child.remove()


class PromptArea(TextArea):
    """The multiline input box.

    Enter sends the message, because ``ctrl+enter`` is not delivered by most
    terminals. A new line is inserted with alt+enter, ctrl+j or shift+enter,
    whichever the terminal happens to support.
    """

    NEWLINE_KEYS = ("alt+enter", "ctrl+j", "shift+enter")

    class Submitted(TextualMessage):
        """Posted when the operator asks for the message to be sent."""

    class Recall(TextualMessage):
        """Posted when an arrow key should walk the input history."""

        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class ClipboardPaste(TextualMessage):
        """Posted when the operator asks for the system clipboard."""

    PASTE_KEYS = ("ctrl+v", "ctrl+shift+v")

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    async def _on_key(self, event: events.Key) -> None:
        if self.read_only:
            await super()._on_key(event)
            return
        # Arrows only reach the history at the edges of the text, so they keep
        # working normally inside a multiline draft.
        if event.key == "up" and self._at_first_line():
            event.prevent_default()
            event.stop()
            self.post_message(self.Recall(-1))
            return
        if event.key == "down" and self._at_last_line():
            event.prevent_default()
            event.stop()
            self.post_message(self.Recall(1))
            return
        if event.key in self.PASTE_KEYS:
            event.prevent_default()
            event.stop()
            self.post_message(self.ClipboardPaste())
            return
        if event.key in self.NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted())
            return
        await super()._on_key(event)


class HeaderBar(Static):
    """The mission-control header: link state, provider, model and role."""

    def __init__(self) -> None:
        super().__init__(id="header")
        self._state: dict[str, str] = {
            "link": "OFFLINE",
            "mesh": branding.OFF_MESH,
            "provider": "",
            "model": "",
            "role": "",
            "style": "frame",
        }

    def update_state(self, link: str, mesh: str, provider: str = "",
                     model: str = "", role: str = "", style: str = "frame") -> None:
        self._state.update(
            link=link,
            mesh=mesh,
            provider=provider,
            model=model,
            role=role,
            style=branding.logo_style(style),
        )
        self._paint()

    def on_resize(self) -> None:
        self._paint()

    def _paint(self) -> None:
        # The CSS adds one column of padding on each side; stay inside it.
        available = max(0, self.size.width - 2)
        self.update(branding.logo(width=available or None, **self._state))


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner_frame(tick: int) -> str:
    """The braille frame for ``tick``, used by the status bar too."""
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]


class ThinkingBox(Static):
    """Placeholder shown between sending and the first token.

    It animates on its own timer, so it keeps moving even while the worker
    thread is blocked waiting on the provider.
    """

    def __init__(self, label: str = "WAITING FOR MODEL") -> None:
        super().__init__(classes="msg msg-system")
        self.label = label
        self._tick = 0
        self._started = monotonic()
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._advance)
        self._advance()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def set_label(self, label: str) -> None:
        self.label = label
        self._advance()

    SLOW_AFTER = 10.0

    def _advance(self) -> None:
        self._tick += 1
        elapsed = monotonic() - self._started
        rendered = Text()
        rendered.append(f"{spinner_frame(self._tick)} ", style="bold")
        rendered.append(f"{self.label}… {elapsed:.1f}s")
        if elapsed >= self.SLOW_AFTER:
            rendered.append("  (the server may still be loading the model)",
                            style="dim")
        self.update(rendered)


class KeyBar(Static):
    """The always-visible key legend at the very bottom of the screen.

    Entries are dropped from the right when the terminal is too narrow, so
    the most useful keys stay on screen.
    """

    KEYS: tuple[tuple[str, str], ...] = (
        # Five entries, deliberately: a legend nobody can read at a glance is
        # decoration. Everything else is in /help, and on the function keys.
        ("enter", "send"),
        ("^C/^V", "copy/paste"),
        ("^Q", "quit"),
        ("^G", "stop"),
        ("F2", "mode"),
    )

    def __init__(self) -> None:
        super().__init__(id="keybar")

    def on_mount(self) -> None:
        self._paint()

    def on_resize(self) -> None:
        self._paint()

    def _paint(self) -> None:
        width = max(0, self.size.width - 2)
        rendered = Text(no_wrap=True)
        for key, label in self.KEYS:
            entry_width = len(key) + len(label) + 1
            if width and len(rendered) + entry_width + 2 > width:
                break
            if rendered:
                rendered.append("  ")
            rendered.append(key, style="bold")
            rendered.append(f" {label}", style="dim")
        self.update(rendered)


class StatusBar(Static):
    """The bottom status line."""

    def __init__(self) -> None:
        super().__init__(id="status")

    def update_state(self, connected: bool, generating: bool, agent: bool,
                     workspace: str, extra: str = "", spinner: str = "",
                     busy: str = "") -> None:
        text = branding.status_line(
            connected, generating, agent, workspace, spinner, busy
        )
        if extra:
            text = f"{text}  ·  {extra}"
        self.update(text)
