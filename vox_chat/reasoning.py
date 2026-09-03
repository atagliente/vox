"""Separating a model's thinking from its answer.

Providers expose reasoning in two incompatible ways: a dedicated
``reasoning_content`` field on the delta, or ``<think>…</think>`` tags inlined
in the ordinary content. This module handles the second case, including tags
split across streaming chunks.

One case is not symmetric with the others: a stream whose **first** tag is a
closing one. Several servers put the model already inside a thought and send
only the close, so the answer begins with a literal ``</think>``. Read as
ordinary text — which is what this did — that tag is printed to the user,
and a tag printed to the user is wrong under every reading. So a ``</think>``
with nothing but whitespace before it is swallowed and the stream carries on
in answer mode.

The rule is deliberately narrow. It applies only at the start, only when no
``<think>`` has been seen, and only across whitespace: a ``</think>`` further
in is still ordinary text, because by then the model is writing an answer and
a model writing about tags is likelier than a provider announcing the end of
a thought a paragraph late.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

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
        # True until the first tag is resolved or the first non-whitespace
        # answer text is emitted. Only while it holds can a bare close tag
        # mean "the thought you were not told about ends here".
        self._at_start = True

    @property
    def thinking(self) -> bool:
        return self._thinking

    def feed(self, chunk: str) -> Iterator[tuple[Kind, str]]:
        """Yield ``(kind, text)`` pairs for everything that can be decided."""
        self._buffer += chunk
        while self._buffer:
            if self._at_start and not self._thinking:
                consumed = self._swallow_leading_close()
                if consumed is None:
                    return  # it might still grow into </think>
                if consumed:
                    continue
            marker = CLOSE_TAG if self._thinking else OPEN_TAG
            index = self._buffer.find(marker)
            if index >= 0:
                before = self._buffer[:index]
                if before:
                    yield ("reasoning" if self._thinking else "text", before)
                self._buffer = self._buffer[index + len(marker) :]
                self._thinking = not self._thinking
                self._at_start = False
                continue
            keep = _partial_tail(self._buffer, marker)
            emit = self._buffer[: len(self._buffer) - keep] if keep else self._buffer
            self._buffer = self._buffer[len(self._buffer) - keep :] if keep else ""
            if emit:
                if emit.strip():
                    self._at_start = False
                yield ("reasoning" if self._thinking else "text", emit)
            return

    def _swallow_leading_close(self) -> bool | None:
        """Deal with a stream that opens on a closing tag.

        Returns True when one was swallowed, False when there is nothing to
        do, and None when the buffer is still only whitespace or a partial
        tag and the answer has to wait for more.
        """
        stripped = self._buffer.lstrip()
        if not stripped:
            return None  # whitespace only: it could still be led by the tag
        if stripped.startswith(CLOSE_TAG):
            gap = len(self._buffer) - len(stripped)
            self._buffer = self._buffer[gap + len(CLOSE_TAG) :]
            self._at_start = False
            return True
        if CLOSE_TAG.startswith(stripped):
            return None  # "</thi" so far, and it may yet be the whole tag
        self._at_start = False
        return False

    def flush(self) -> Iterator[tuple[Kind, str]]:
        """Emit whatever is still buffered once the stream is over."""
        if self._buffer:
            yield ("reasoning" if self._thinking else "text", self._buffer)
            self._buffer = ""
