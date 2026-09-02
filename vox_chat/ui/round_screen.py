"""The round: what every agent is writing, while it is writing it.

The transcript shows each peer's answer as one block, which is the right shape
for reading afterwards and the wrong one for watching. Here the fragments stay
in the order they arrived, so it is visible that one agent was still thinking
while another had already finished — and how long each of them took.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..consensus import RoundLog

REFRESH_SECONDS = 0.5

# One colour per agent, reused in order. Deliberately the muted palette: this
# is a lot of text and it should not shout.
_AGENT_COLOURS = ["#8ea7bb", "#8da287", "#c9a15a", "#c47a5d", "#9b8bb4"]
_REASONING = "#6b6349"


class RoundScreen(Screen[None]):
    """A live, timestamped log of one consensus round."""

    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("q", "close", "Back", show=False),
        Binding("f5", "close", "Back", show=False),
    ]

    def __init__(self, round_log: RoundLog) -> None:
        super().__init__()
        self.round_log = round_log
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("", id="round-title")
        yield Static("", id="round-question")
        with VerticalScroll(id="round-body"):
            yield Static("", id="round-lines")
        yield Static("", id="round-keys")

    def on_mount(self) -> None:
        self.refresh_view()
        self._timer = self.set_interval(REFRESH_SECONDS, self.refresh_view)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # ------------------------------------------------------------------ view

    def colour_for(self, agent: str) -> str:
        agents = self.round_log.agents()
        if agent not in agents:
            return _AGENT_COLOURS[0]
        return _AGENT_COLOURS[agents.index(agent) % len(_AGENT_COLOURS)]

    def refresh_view(self) -> None:
        agents = self.round_log.agents()
        title = Text(
            "ANSWERING" if self.round_log.asked_by else "THE ROUND", style="bold"
        )
        if agents:
            title.append("  ·  ")
            for index, agent in enumerate(agents):
                if index:
                    title.append("  ")
                title.append(agent, style=self.colour_for(agent))
        else:
            title.append("  ·  nobody is talking yet", style="dim")
        self.query_one("#round-title", Static).update(title)

        question = self.round_log.question or "no question yet"
        if self.round_log.asked_by:
            line = Text(f"asked by {self.round_log.asked_by}: {question}", style="dim")
        else:
            line = Text(f"asked: {question}", style="dim")
        if self.round_log.conversation_id:
            line.append(
                f"\nconversation {self.round_log.conversation_id}", style="dim"
            )
        self.query_one("#round-question", Static).update(line)

        width = max(30, self.size.width - 26)
        body = Text()
        for stamp, agent, kind, text in self.round_log.lines(width):
            if stamp:
                body.append(f"{stamp} ", style="dim")
                body.append(f"{agent}: ", style=f"bold {self.colour_for(agent)}")
            else:
                body.append(" " * (9 + len(agent) + 2))
            body.append(
                text + "\n",
                style=f"italic {_REASONING}" if kind == "reasoning" else "",
            )
        if not self.round_log.fragments:
            body.append(
                "Nothing yet. Agents that stream their answer appear here as "
                "they write; a peer that answers in one go appears when it is "
                "done.",
                style="dim",
            )
        self.query_one("#round-lines", Static).update(body)
        self.query_one("#round-body", VerticalScroll).scroll_end(animate=False)
        self.query_one("#round-keys", Static).update(
            "esc back   ·   italics are the agent thinking, upright is its answer"
        )

    def action_close(self) -> None:
        self.dismiss(None)
