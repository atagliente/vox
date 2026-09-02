"""Modal screens: confirmation, settings, pickers and single-field prompts."""

from __future__ import annotations

import json
from typing import Any, Iterable, NamedTuple

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static, TextArea


_DIFF_STYLES = (
    ("+++ ", "bold"),
    ("--- ", "bold"),
    ("@@", "#c9a15a"),
    ("+", "#8da287"),
    ("-", "#c47a5d"),
)


def diff_text(body: str) -> Text:
    """Colour a confirmation body, tinting diff lines by their prefix."""
    rendered = Text()
    for index, line in enumerate(body.splitlines()):
        if index:
            rendered.append("\n")
        style = next(
            (style for prefix, style in _DIFF_STYLES if line.startswith(prefix)), ""
        )
        rendered.append(line, style=style)
    return rendered


class PickerItem(NamedTuple):
    """One row in a picker: the value returned and what the user sees."""

    value: str
    title: str
    subtitle: str = ""


class ConfirmModal(ModalScreen[bool]):
    """Two-button dialog: authorise a write or a command, or confirm a quit.

    Left and right move between the buttons, Enter activates the focused one,
    and the legend under them spells out every key that does something here.
    """

    BINDINGS = [
        Binding("escape", "deny", "Deny", priority=True),
        Binding("y", "authorize", "Authorize", priority=True),
        Binding("n", "deny", "Deny", show=False, priority=True),
        Binding("left", "move(-1)", "Previous", show=False, priority=True),
        Binding("right", "move(1)", "Next", show=False, priority=True),
        Binding("tab", "move(1)", "Next", show=False, priority=True),
        Binding("shift+tab", "move(-1)", "Previous", show=False, priority=True),
    ]

    def __init__(self, title: str, body: str, authorize_label: str = "AUTHORIZE",
                 deny_label: str = "DENY") -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.authorize_label = authorize_label
        self.deny_label = deny_label

    def hint(self) -> str:
        """The legend under the buttons, built from the real bindings."""
        parts = [
            "←/→ choose",
            "enter select",
            f"y {self.authorize_label.lower()}",
            f"esc {self.deny_label.lower()}",
        ]
        return "   ·   ".join(parts)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.title_text, id="modal-title")
            with VerticalScroll():
                yield Static(diff_text(self.body_text))
            with Horizontal(id="modal-buttons"):
                yield Button(self.authorize_label, id="ok", variant="success")
                yield Button(self.deny_label, id="cancel", variant="error")
            yield Static(self.hint(), id="modal-hint")

    def on_mount(self) -> None:
        # Start on the safe choice, so a stray Enter never authorises anything.
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_move(self, direction: int) -> None:
        buttons = list(self.query(Button))
        if not buttons:
            return
        focused = self.focused
        index = buttons.index(focused) if focused in buttons else 0
        buttons[(index + direction) % len(buttons)].focus()

    def action_authorize(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class TextPromptModal(ModalScreen[str | None]):
    """A one-line question, e.g. a session or prompt name."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.title_text, id="modal-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="value")
            with Horizontal(id="modal-buttons"):
                yield Button("OK", id="ok", variant="success")
                yield Button("CANCEL", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#value", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickerModal(ModalScreen[str | None]):
    """A searchable list; returns the selected value or ``None``.

    The filter keeps the focus and the arrows move the highlight, so a choice
    can be made without typing anything: down, down, enter. Filtering is for
    long lists, not a step you are made to go through.
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("down", "move(1)", "Next", show=False, priority=True),
        Binding("up", "move(-1)", "Previous", show=False, priority=True),
        Binding("pagedown", "move(10)", "Down", show=False, priority=True),
        Binding("pageup", "move(-10)", "Up", show=False, priority=True),
    ]

    def __init__(self, title: str, items: Iterable[PickerItem],
                 empty_message: str = "nothing here yet") -> None:
        super().__init__()
        self.title_text = title
        self.items = list(items)
        self.empty_message = empty_message

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.title_text, id="modal-title")
            yield Input(placeholder="filter…", id="filter")
            if self.items:
                yield ListView(*self._rows(self.items), id="items")
            else:
                yield Static(self.empty_message)
                yield ListView(id="items")
            with Horizontal(id="modal-buttons"):
                yield Button("SELECT", id="ok", variant="success")
                yield Button("CANCEL", id="cancel")

    def _rows(self, items: list[PickerItem]) -> list[ListItem]:
        rows: list[ListItem] = []
        for item in items:
            label = item.title if not item.subtitle else f"{item.title}\n  {item.subtitle}"
            row = ListItem(Static(label))
            row.value = item.value
            rows.append(row)
        return rows

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()
        self._highlight(0)

    def _highlight(self, index: int) -> None:
        listing = self.query_one("#items", ListView)
        if listing.children:
            listing.index = max(0, min(index, len(listing.children) - 1))

    def action_move(self, delta: int) -> None:
        listing = self.query_one("#items", ListView)
        if not listing.children:
            return
        current = listing.index if listing.index is not None else 0
        self._highlight(current + delta)

    def _selected(self) -> str | None:
        highlighted = self.query_one("#items", ListView).highlighted_child
        return getattr(highlighted, "value", None) if highlighted else None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the filter takes the highlighted row, the way a launcher does.
        self.dismiss(self._selected())

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        matching = [
            item
            for item in self.items
            if needle in item.title.lower() or needle in item.subtitle.lower()
        ]
        listing = self.query_one("#items", ListView)
        listing.clear()
        for row in self._rows(matching):
            listing.append(row)
        self._highlight(0)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "value", None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        self.dismiss(self._selected())

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsModal(ModalScreen[dict[str, Any] | None]):
    """Edit the configuration as JSON, with validation before it is accepted.

    The API key of every provider is masked while displayed; a masked value
    left untouched is restored from the live configuration on save.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, config: dict[str, Any], masked: dict[str, Any],
                 validate: Any) -> None:
        super().__init__()
        self.config = config
        self.masked = masked
        self.validate = validate

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("SETTINGS - config.json", id="modal-title")
            yield TextArea(
                json.dumps(self.masked, indent=2, ensure_ascii=False),
                language="json",
                id="editor",
            )
            yield Static("", id="settings-error", classes="msg-error")
            with Horizontal(id="modal-buttons"):
                yield Button("SAVE", id="ok", variant="success")
                yield Button("CANCEL", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#editor", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        text = self.query_one("#editor", TextArea).text
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError as exc:
            self._show_error(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
            return
        if not isinstance(candidate, dict):
            self._show_error("configuration root must be a JSON object")
            return
        self._restore_secrets(candidate)
        errors = self.validate(candidate)
        if errors:
            self._show_error("; ".join(errors))
            return
        self.dismiss(candidate)

    def _restore_secrets(self, candidate: dict[str, Any]) -> None:
        """Put back real API keys wherever the masked placeholder survived."""
        live = self.config.get("providers", {})
        shown = self.masked.get("providers", {})
        for name, provider in candidate.get("providers", {}).items():
            if not isinstance(provider, dict):
                continue
            masked_value = shown.get(name, {}).get("api_key") if isinstance(
                shown.get(name), dict
            ) else None
            if masked_value is not None and provider.get("api_key") == masked_value:
                original = live.get(name, {})
                if isinstance(original, dict) and "api_key" in original:
                    provider["api_key"] = original["api_key"]

    def _show_error(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)
