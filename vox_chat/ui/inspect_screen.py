"""The full-screen inspection view.

It shows what the provider reported about each token: the probability it gave
the token it emitted, how spread the returned distribution was, and what it
passed over. Nothing here says what any of that means.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..inspection import InspectionRun, TokenRecord

FILTERS = ("all", "decisions", "thinking", "answer")

_HEADER = (
    f"{'#':>4}  {'TOKEN':<18}{'P':>6}{'H':>7}{'MARGIN':>8}   ALTERNATIVES"
)


class InspectScreen(Screen[None]):
    """Live table of the current turn, one row per token."""

    BINDINGS = [
        Binding("escape", "close", "Back", priority=True),
        Binding("q", "close", "Back", show=False),
        Binding("d", "filter('decisions')", "Decisions"),
        Binding("t", "filter('thinking')", "Thinking"),
        Binding("a", "filter('answer')", "Answer"),
        Binding("f", "filter('all')", "All"),
    ]

    def __init__(self, run: InspectionRun, enabled: bool = True) -> None:
        super().__init__()
        self.run = run
        self.enabled = enabled
        self.filter = "all"
        self._follow = True

    def compose(self) -> ComposeResult:
        yield Static("", id="inspect-title")
        yield Static("", id="inspect-summary")
        yield Static(Text(_HEADER, style="bold"), id="inspect-header")
        with VerticalScroll(id="inspect-body"):
            yield Static("", id="inspect-rows")
        yield Static("", id="inspect-keys")

    def on_mount(self) -> None:
        self.refresh_view()

    # ----------------------------------------------------------------- view

    def visible_records(self) -> list[TokenRecord]:
        if self.filter == "decisions":
            return self.run.decisions
        if self.filter in ("thinking", "answer"):
            return self.run.phase_records(self.filter)  # type: ignore[arg-type]
        return self.run.records

    def refresh_view(self) -> None:
        """Redraw from the run; cheap enough to call on every token."""
        self.query_one("#inspect-title", Static).update(self._title())
        self.query_one("#inspect-summary", Static).update(self._summary())
        self.query_one("#inspect-rows", Static).update(self._rows())
        self.query_one("#inspect-keys", Static).update(
            "esc back   ·   f all   ·   d decisions   ·   t thinking   ·   a answer"
        )
        if self._follow:
            self.query_one("#inspect-body", VerticalScroll).scroll_end(animate=False)

    def _title(self) -> Text:
        run = self.run
        parts = [f"INSPECT · {run.model or 'no model'}", f"top-k {run.top_k}"]
        parts.append(f"{len(run)} tokens")
        parts.append(f"{len(run.decisions)} decision points")
        if self.filter != "all":
            parts.append(f"showing {self.filter}")
        return Text("  ·  ".join(parts), style="bold")

    def _summary(self) -> Text:
        if not len(self.run):
            if not self.enabled:
                return Text(
                    "Inspection is off. Turn it on with /inspect on, then ask "
                    "something: the table fills while the answer streams.",
                )
            note = self.run.note or (
                "No token data arrived yet. If the provider does not report "
                "logprobs, none will."
            )
            return Text(note)
        stats = self.run.stats()
        thinking = self.run.phase_stats("thinking")
        answer = self.run.phase_stats("answer")
        line = (
            f"mean p {stats['mean_probability']:.2f}   "
            f"mean top-k entropy {stats['mean_top_k_entropy_bits']:.2f} bit"
        )
        if thinking.tokens and answer.tokens:
            line += (
                f"   ·   thinking {thinking.mean_probability:.2f} / "
                f"answer {answer.mean_probability:.2f}"
            )
        return Text(line + "\n" + "entropy is over the returned top-k, not the "
                    "full vocabulary")

    def _rows(self) -> Text:
        records = self.visible_records()
        if not records:
            return Text("")
        rendered = Text()
        threshold = self.run.criteria.entropy_threshold
        for record in records:
            runners = "  ".join(
                f"{alternative.token!r} {alternative.probability:.2f}"
                for alternative in record.runners_up[:3]
            )
            token = repr(record.text)
            if len(token) > 18:
                token = token[:17] + "…"
            rendered.append(f"{record.index:>4}  ")
            rendered.append(f"{token:<18}")
            rendered.append(f"{record.probability:>6.2f}")
            rendered.append(
                f"{record.entropy:>7.2f}",
                style="#c9a15a" if record.entropy >= threshold else "#8da287",
            )
            rendered.append(f"{record.margin:>8.2f}   ")
            rendered.append(runners, style="dim")
            if record.is_decision:
                rendered.append("  ◄ DECISION", style="#c9a15a")
            rendered.append("\n")
        return rendered

    # -------------------------------------------------------------- actions

    def action_filter(self, name: str) -> None:
        if name in FILTERS:
            self.filter = name
            self.refresh_view()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_mouse_scroll_up(self) -> None:  # pragma: no cover - interactive
        self._follow = False

    def on_mouse_scroll_down(self) -> None:  # pragma: no cover - interactive
        body = self.query_one("#inspect-body", VerticalScroll)
        self._follow = body.scroll_offset.y >= body.max_scroll_y - 1
