"""The universe: every agent this node has seen, and what it is.

The category is not guessed here — it is the one the peer's own declared verbs
produce through the taxonomy, so the same agent is classified the same way on
every node that sees it.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..mesh import MeshController, PeerView

_HEADER = (
    f"{'AGENT':<20}{'CATEGORY':<14}{'STATE':<11}{'ADDRESS':<22}"
    f"{'SEEN':>8}   VERBS"
)

_STATE_STYLE = {
    "ACTIVE": "#8da287",     # sage: answering, classified, routable
    "PROBATION": "#c9a15a",  # amber: seen, not yet interrogated
    "SUSPECT": "#c47a5d",    # terracotta: heartbeats missing
    "DEAD": "#6b6349",       # dim: out of routing
}

REFRESH_SECONDS = 1.0


class UniverseScreen(Screen[None]):
    """A live list of the mesh, refreshed while it is open."""

    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("q", "close", "Back", show=False),
        Binding("ctrl+l", "toggle_legend", "Legend"),
        Binding("question_mark", "toggle_legend", "Legend", show=False),
    ]

    def __init__(self, controller: MeshController) -> None:
        super().__init__()
        self.controller = controller
        self.legend_open = False
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("", id="universe-title")
        yield Static("", id="universe-summary")
        yield Static("", id="universe-legend")
        yield Static(Text(_HEADER, style="bold"), id="universe-header")
        with VerticalScroll(id="universe-body"):
            yield Static("", id="universe-rows")
        yield Static("", id="universe-keys")

    def on_mount(self) -> None:
        self.refresh_view()
        self._timer = self.set_interval(REFRESH_SECONDS, self.refresh_view)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # ------------------------------------------------------------------ view

    def refresh_view(self) -> None:
        peers = self.controller.peers()
        self.query_one("#universe-title", Static).update(self._title(peers))
        self.query_one("#universe-summary", Static).update(self._summary(peers))
        self.query_one("#universe-rows", Static).update(self._rows(peers))
        self.query_one("#universe-keys", Static).update(
            "esc back   ·   ctrl+l legend   ·   the list refreshes on its own"
        )
        legend = self.query_one("#universe-legend", Static)
        legend.set_class(self.legend_open, "visible")
        if self.legend_open:
            legend.update(self._legend())

    def _title(self, peers: list[PeerView]) -> Text:
        controller = self.controller
        parts = [f"UNIVERSE · {controller.agent_id}", controller.category]
        if controller.online:
            active = len([peer for peer in peers if peer.state == "ACTIVE"])
            parts.append(f"{len(peers)} agents")
            parts.append(f"{active} active")
        else:
            parts.append("OFFLINE")
        return Text("  ·  ".join(parts), style="bold")

    def _summary(self, peers: list[PeerView]) -> Text:
        controller = self.controller
        if not controller.online:
            return Text(
                "Not on the mesh. Ctrl+Shift+O goes online — it announces this "
                "machine on the local network segment — and this list fills as "
                "agents answer."
            )
        settings = controller.settings
        line = (
            f"announcing on {settings.group}:{settings.port} every "
            f"{settings.announce_interval:g}s   ·   "
            f"verbs {', '.join(settings.verbs)}"
        )
        if not peers:
            line += "\nno other agent has announced yet; multicast does not leave this segment"
        return Text(line)

    def _rows(self, peers: list[PeerView]) -> Text:
        if not peers:
            return Text("")
        rendered = Text()
        for peer in peers:
            name = peer.name if len(peer.name) <= 19 else peer.name[:18] + "…"
            address = peer.address if len(peer.address) <= 21 else peer.address[:20] + "…"
            rendered.append(f"{name:<20}")
            rendered.append(f"{peer.category:<14}")
            rendered.append(
                f"{peer.state:<11}", style=_STATE_STYLE.get(peer.state, "")
            )
            rendered.append(f"{address:<22}")
            rendered.append(f"{peer.age:>7.1f}s   ", style="dim")
            rendered.append(", ".join(peer.verbs) or "—", style="dim")
            rendered.append("\n")
        return rendered

    def _legend(self) -> Text:
        rendered = Text()

        def row(name: str, meaning: str, style: str = "") -> None:
            rendered.append(f"  {name:<14}", style=style or "bold")
            rendered.append(meaning + "\n")

        rendered.append("STATES\n", style="bold")
        row("PROBATION", "announced itself, not yet interrogated: no work goes to it",
            _STATE_STYLE["PROBATION"])
        row("ACTIVE", "WHOIS completed over mTLS, classified, routable",
            _STATE_STYLE["ACTIVE"])
        row("SUSPECT", "heartbeats missing for three intervals",
            _STATE_STYLE["SUSPECT"])
        row("DEAD", "silent for five intervals: out of routing",
            _STATE_STYLE["DEAD"])

        rendered.append("\nCATEGORIES\n", style="bold")
        row("SOURCE", "ingest, publish")
        row("PROCESSOR", "transform, enrich, infer")
        row("SINK", "store, index, notify")
        row("ORCHESTRATOR", "schedule, dispatch")
        row("OBSERVER", "observe, audit — kept out of routing by design")

        rendered.append(
            "\nThe category is derived from the verbs a peer declares, the same "
            "way\non every node. A dash means the WHOIS has not happened yet.\n",
            style="dim",
        )
        return rendered

    # --------------------------------------------------------------- actions

    def action_toggle_legend(self) -> None:
        self.legend_open = not self.legend_open
        self.refresh_view()

    def action_close(self) -> None:
        self.dismiss(None)
