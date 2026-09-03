"""Not asking the same question twice.

Web mode searches, then fetches two or three of what it found, on every turn.
Ask a follow-up about the same subject and it does the whole thing again —
the same query to the same index, the same pages down the same wire. That is
slow, it is rude to the people running those servers, and it is the fastest
way to meet DuckDuckGo's captcha.

So: a small cache on disk, keyed by what was asked, with a time to live.

Two decisions worth stating:

- **On disk, not in memory.** A session is where the repetition is, but the
  next session an hour later asking the same thing is repetition too, and
  the whole point of a local client is that closing it costs nothing.
- **Short by default.** Fifteen minutes for a search, an hour for a page. The
  web changes, and an answer built on yesterday's cache while claiming to be
  current is worse than a slow one — so the entry records when it was taken
  and the transcript says when an answer came from it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .storage import vox_home

log = get_logger("webcache")

SEARCH_TTL = 15 * 60
FETCH_TTL = 60 * 60

# Enough for a long session's worth of pages, small enough to stay a cache.
MAX_ENTRIES = 400


@dataclass(frozen=True)
class Entry:
    """One cached answer, and when it was taken."""

    value: Any
    taken_at: float

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.taken_at)

    def describe_age(self) -> str:
        seconds = int(self.age)
        if seconds < 60:
            return f"{seconds}s old"
        if seconds < 3600:
            return f"{seconds // 60}m old"
        return f"{seconds // 3600}h old"


def _key(kind: str, subject: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{subject}".encode()).hexdigest()[:24]
    return f"{kind}-{digest}"


class WebCache:
    """Searches and pages, kept for a while.

    Never raises: a cache that can fail a search is worse than no cache, so
    everything here degrades to a miss.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else vox_home() / "webcache"
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, kind: str, subject: str, ttl: float) -> Entry | None:
        """The cached value, if there is one and it is young enough."""
        path = self._path(_key(kind, subject))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = Entry(payload["value"], float(payload["taken_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            self.misses += 1
            return None
        if entry.age > ttl:
            self.misses += 1
            with contextlib.suppress(OSError):  # it will be overwritten anyway
                path.unlink(missing_ok=True)
            return None
        self.hits += 1
        return entry

    def put(self, kind: str, subject: str, value: Any) -> None:
        """Keep a value. A cache that cannot be written is simply not used."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(_key(kind, subject)).write_text(
                json.dumps({"value": value, "taken_at": time.time()}),
                encoding="utf-8",
            )
        except (OSError, TypeError) as exc:
            log.debug("not cached: %s", exc)
            return
        self._prune()

    def _prune(self) -> None:
        """Drop the oldest when there are too many. Cheap, and rarely needed."""
        try:
            files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:  # pragma: no cover
            return
        for path in files[: max(0, len(files) - MAX_ENTRIES)]:
            with contextlib.suppress(OSError):  # a cache entry, not a promise
                path.unlink(missing_ok=True)

    def clear(self) -> int:
        """Empty it, and say how many entries went."""
        removed = 0
        try:
            for path in self.root.glob("*.json"):
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:  # pragma: no cover
            pass
        self.hits = self.misses = 0
        return removed

    def describe(self) -> str:
        try:
            count = len(list(self.root.glob("*.json")))
        except OSError:  # pragma: no cover
            count = 0
        asked = self.hits + self.misses
        share = f", {self.hits * 100 // asked}% reused" if asked else ""
        return f"{count} entr{'y' if count == 1 else 'ies'} in {self.root}{share}"
