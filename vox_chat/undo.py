"""Taking back the last thing the agent wrote.

Every write is confirmed before it happens, which is the real protection. But
a confirmation is a judgement made from a diff in a modal, and a diff can look
right and still be wrong — the wrong file, the wrong branch, a change that
made sense until the next one contradicted it.

The bytes are already in hand: the confirmation reads the file to build the
diff, so keeping what it read costs nothing. Refusing an undo because nobody
thought to hold on to them would be a poor reason.

Deliberately shallow. This is one step, not a version-control system: it
undoes the last authorised write, and says plainly that it is not a
substitute for committing. Anything deeper is what git is for, and pretending
otherwise would be the sort of half-promise that gets trusted once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .logging_setup import get_logger
from .tools import Snapshot, ToolError

log = get_logger("undo")


@dataclass
class Step:
    """One authorised write, and what the files looked like before it."""

    tool: str
    snapshots: list[Snapshot]

    def describe(self) -> str:
        names = ", ".join(item.path.name for item in self.snapshots) or "nothing"
        return f"{self.tool} ({names})"


@dataclass
class UndoStack:
    """The last write, kept so it can be taken back."""

    last: Step | None = None
    history: list[str] = field(default_factory=list)

    def remember(self, tool: str, snapshots: list[Snapshot]) -> None:
        if not snapshots:
            return
        self.last = Step(tool, snapshots)
        log.debug("undo available for %s", self.last.describe())

    def clear(self) -> None:
        self.last = None

    @property
    def available(self) -> bool:
        return self.last is not None

    def describe(self) -> str:
        if self.last is None:
            return "nothing to undo: no write has been authorised yet"
        return f"undo would take back: {self.last.describe()}"

    def undo(self) -> str:
        """Put the files back, and forget the step. Raises ToolError if it cannot."""
        if self.last is None:
            raise ToolError("nothing to undo")
        step, self.last = self.last, None
        done = [item.restore() for item in step.snapshots]
        line = f"UNDONE {step.tool} - " + "; ".join(done)
        self.history.append(line)
        return (
            line + "\nOne step only. This is not version control: commit before "
            "the next change if it matters."
        )
