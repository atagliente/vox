"""Pulling fenced code out of an answer, so it can be shown and copied.

The parser is deliberately forgiving: while a reply is still streaming the
last fence is usually still open, and that half-written block is worth showing
too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>\S*)")


@dataclass(frozen=True)
class CodeBlock:
    """One fenced block: its language tag, if any, and its lines."""

    language: str
    code: str
    closed: bool = True

    @property
    def line_count(self) -> int:
        return len(self.code.splitlines()) or (1 if self.code else 0)

    def label(self, index: int) -> str:
        language = self.language or "text"
        suffix = "" if self.closed else " (incomplete)"
        return f"[{index}] {language} · {self.line_count} lines{suffix}"


def extract(text: str) -> list[CodeBlock]:
    """Return every fenced block in ``text``, in order.

    A fence left open at the end of the text still produces a block, marked
    ``closed=False``.
    """
    if not text or ("```" not in text and "~~~" not in text):
        return []
    blocks: list[CodeBlock] = []
    fence: str | None = None
    language = ""
    body: list[str] = []
    for line in text.splitlines():
        if fence is None:
            match = FENCE_RE.match(line)
            if match:
                fence = match.group("fence")[0] * 3
                language = match.group("info")
                body = []
            continue
        stripped = line.strip()
        if stripped.startswith(fence) and set(stripped) <= {fence[0]}:
            blocks.append(CodeBlock(language, "\n".join(body), closed=True))
            fence = None
            continue
        body.append(line)
    if fence is not None:
        blocks.append(CodeBlock(language, "\n".join(body), closed=False))
    return [block for block in blocks if block.code.strip()]


def latest(text: str) -> CodeBlock | None:
    """The last block in ``text``, or ``None`` when there is none."""
    blocks = extract(text)
    return blocks[-1] if blocks else None


def render_panel(blocks: list[CodeBlock], max_lines: int = 400) -> str:
    """Lay the blocks out for the side panel, newest last.

    The code is written flush left with no prefix, so dragging over it in the
    terminal copies exactly the code and nothing else.
    """
    if not blocks:
        return "No code in this answer yet.\n\nCode blocks from the assistant\nappear here, ready to copy."
    lines: list[str] = []
    budget = max_lines
    for index, block in enumerate(blocks, start=1):
        lines.append(block.label(index))
        lines.append("")
        code_lines = block.code.splitlines() or [""]
        if len(code_lines) > budget:
            code_lines = [*code_lines[:budget], "… (truncated)"]
        lines.extend(code_lines)
        lines.append("")
        budget -= len(code_lines)
        if budget <= 0:
            lines.append("… more blocks not shown")
            break
    return "\n".join(lines).rstrip()
