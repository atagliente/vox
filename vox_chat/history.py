"""Input history: what was typed before, recalled with the arrow keys.

Persisted in ``~/.vox/history.json`` so it survives a restart, capped so it
cannot grow without bound.
"""

from __future__ import annotations

from pathlib import Path

from .storage import read_json_safe, vox_home, write_json_atomic

MAX_ENTRIES = 200


def history_path() -> Path:
    return vox_home() / "history.json"


class InputHistory:
    """A shell-style history with a cursor and a preserved draft.

    ``previous`` walks towards older entries, ``next`` back towards the draft
    the operator was writing when they started browsing.
    """

    def __init__(
        self, path: Path | None = None, max_entries: int = MAX_ENTRIES
    ) -> None:
        self.path = Path(path) if path is not None else history_path()
        self.max_entries = max_entries
        self.warnings: list[str] = []
        self.entries: list[str] = []
        self._cursor: int | None = None
        self._draft = ""
        self.load()

    def load(self) -> None:
        result = read_json_safe(self.path, [])
        if result.error is not None:
            self.warnings.append(result.error)
        raw = result.data if isinstance(result.data, list) else []
        self.entries = [
            str(item) for item in raw if isinstance(item, str) and item.strip()
        ]
        self.entries = self.entries[-self.max_entries :]

    def save(self) -> None:
        try:
            write_json_atomic(self.path, self.entries)
        except OSError as exc:
            self.warnings.append(f"cannot save history: {exc}")

    def add(self, text: str) -> None:
        """Record a submitted line, skipping blanks and immediate repeats."""
        entry = text.strip()
        self.reset()
        if not entry:
            return
        if self.entries and self.entries[-1] == entry:
            return
        self.entries.append(entry)
        del self.entries[: max(0, len(self.entries) - self.max_entries)]
        self.save()

    def reset(self) -> None:
        """Stop browsing and forget the stored draft."""
        self._cursor = None
        self._draft = ""

    @property
    def browsing(self) -> bool:
        return self._cursor is not None

    def previous(self, current: str) -> str | None:
        """Older entry, or ``None`` when there is nothing further back."""
        if not self.entries:
            return None
        if self._cursor is None:
            self._draft = current
            self._cursor = len(self.entries)
        if self._cursor == 0:
            return None
        self._cursor -= 1
        return self.entries[self._cursor]

    def next(self) -> str | None:
        """Newer entry, then the draft, then ``None`` once back at the draft."""
        if self._cursor is None:
            return None
        if self._cursor >= len(self.entries) - 1:
            draft = self._draft
            self.reset()
            return draft
        self._cursor += 1
        return self.entries[self._cursor]

    def __len__(self) -> int:
        return len(self.entries)
