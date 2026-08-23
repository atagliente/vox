"""Separating a model's thinking from its answer.

Providers expose reasoning in two incompatible ways: a dedicated
``reasoning_content`` field on the delta, or ``<think>…</think>`` tags inlined
in the ordinary content. This module handles the second case, including tags
split across streaming chunks.
"""

from __future__ import annotations

from typing import Iterator, Literal

Kind = Literal["text", "reasoning"]

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"


def _partial_tail(buffer: str, marker: str) -> int:
    """Length of the buffer suffix that could still grow into ``marker``."""
    for size in range(min(len(marker) - 1, len(buffer)), 0, -1):
        if marker.startswith(buffer[-size:]):
            return size
    return 0


class ThinkSplitter:
    """Split a stream of content chunks into answer and reasoning segments.

    A tag broken across two chunks is held back until it can be resolved, so
    ``<thi`` + ``nk>`` is recognised as one opening tag and never leaks into
    the answer.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False

    @property
    def thinking(self) -> bool:
        return self._thinking

    def feed(self, chunk: str) -> Iterator[tuple[Kind, str]]:
        """Yield ``(kind, text)`` pairs for everything that can be decided."""
        self._buffer += chunk
        while self._buffer:
            marker = CLOSE_TAG if self._thinking else OPEN_TAG
            index = self._buffer.find(marker)
            if index >= 0:
                before = self._buffer[:index]
                if before:
                    yield ("reasoning" if self._thinking else "text", before)
                self._buffer = self._buffer[index + len(marker) :]
                self._thinking = not self._thinking
                continue
            keep = _partial_tail(self._buffer, marker)
            emit = self._buffer[: len(self._buffer) - keep] if keep else self._buffer
            self._buffer = self._buffer[len(self._buffer) - keep :] if keep else ""
            if emit:
                yield ("reasoning" if self._thinking else "text", emit)
            return

    def flush(self) -> Iterator[tuple[Kind, str]]:
        """Emit whatever is still buffered once the stream is over."""
        if self._buffer:
            yield ("reasoning" if self._thinking else "text", self._buffer)
            self._buffer = ""
