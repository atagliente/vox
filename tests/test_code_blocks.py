"""Pulling fenced code out of an answer."""

from __future__ import annotations

from vox_chat.code_blocks import CodeBlock, extract, latest, render_panel

ANSWER = """Here is how to read a file:

```python
with open(path, encoding="utf-8") as handle:
    for line in handle:
        print(line.rstrip())
```

and a shell one-liner:

```bash
wc -l file.txt
```
"""


def test_every_block_is_found_with_its_language() -> None:
    blocks = extract(ANSWER)
    assert [block.language for block in blocks] == ["python", "bash"]
    assert blocks[0].code.startswith("with open(path")
    assert blocks[1].code == "wc -l file.txt"
    assert all(block.closed for block in blocks)


def test_prose_without_code_yields_nothing() -> None:
    assert extract("just an explanation, no code at all") == []
    assert extract("") == []
    assert latest("nothing here") is None


def test_a_fence_still_open_is_kept_and_flagged() -> None:
    blocks = extract("streaming…\n\n```python\nx = 1\n")
    assert len(blocks) == 1
    assert blocks[0].closed is False
    assert "incomplete" in blocks[0].label(1)


def test_a_block_without_a_language_tag() -> None:
    blocks = extract("```\nplain text\n```")
    assert blocks[0].language == ""
    assert "text" in blocks[0].label(1)


def test_tilde_fences_work_too() -> None:
    blocks = extract("~~~js\nconst a = 1;\n~~~")
    assert blocks[0].language == "js"
    assert blocks[0].code == "const a = 1;"


def test_indented_fences_are_recognised() -> None:
    blocks = extract("- example:\n\n  ```python\n  x = 1\n  ```\n")
    assert len(blocks) == 1
    assert "x = 1" in blocks[0].code


def test_empty_fences_are_dropped() -> None:
    assert extract("```python\n\n```") == []


def test_latest_returns_the_last_block() -> None:
    block = latest(ANSWER)
    assert block is not None and block.language == "bash"


def test_line_counts_and_labels() -> None:
    block = CodeBlock("python", "a = 1\nb = 2")
    assert block.line_count == 2
    assert block.label(3) == "[3] python · 2 lines"


def test_the_panel_lists_blocks_and_their_code() -> None:
    rendered = render_panel(extract(ANSWER))
    assert "[1] python" in rendered
    assert "[2] bash" in rendered
    assert "wc -l file.txt" in rendered
    assert "```" not in rendered, "fences are not part of the copyable code"


def test_the_empty_panel_explains_itself() -> None:
    assert "No code" in render_panel([])


def test_the_panel_truncates_a_huge_block() -> None:
    rendered = render_panel([CodeBlock("python", "\n".join(f"line {i}" for i in range(500)))], max_lines=10)
    assert "truncated" in rendered
    assert rendered.count("\n") < 30
