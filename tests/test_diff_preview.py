"""The diff shown before a write, a patch, or a refusal, is authorised."""

from __future__ import annotations

from pathlib import Path

import pytest

from vox_chat.tools import (
    ToolSecurityError,
    Workspace,
    describe_call,
    preview_patch,
    preview_write,
    render_diff,
)
from vox_chat.ui.modals import diff_text


@pytest.fixture
def ws(workspace: Path) -> Workspace:
    (workspace / "main.py").write_text(
        "def main():\n    print('hello')\n\n\nmain()\n", encoding="utf-8"
    )
    return Workspace(workspace)


def test_a_rewrite_shows_the_lines_that_change(ws: Workspace) -> None:
    preview = preview_write(
        ws, "main.py", "def main():\n    print('hello, world')\n\n\nmain()\n"
    )
    assert "-    print('hello')" in preview
    assert "+    print('hello, world')" in preview
    assert " def main():" in preview, "context is shown around the change"


def test_a_new_file_is_announced_as_such(ws: Workspace) -> None:
    preview = preview_write(ws, "new/module.py", "x = 1\ny = 2\n")
    assert "new file: new/module.py (2 lines)" in preview
    assert "+x = 1" in preview
    assert not (ws.root / "new" / "module.py").exists(), "nothing is written yet"


def test_writing_identical_content_says_there_is_nothing_to_do(ws: Workspace) -> None:
    same = (ws.root / "main.py").read_text(encoding="utf-8")
    assert "no changes" in preview_write(ws, "main.py", same)


def test_a_binary_file_is_reported_not_diffed(ws: Workspace) -> None:
    (ws.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    preview = preview_write(ws, "blob.bin", "text\n")
    assert "not UTF-8 text" in preview
    assert "1 lines would be written" in preview


def test_a_huge_diff_is_clipped(ws: Workspace) -> None:
    (ws.root / "big.py").write_text("\n".join(f"a{i}" for i in range(500)), encoding="utf-8")
    preview = preview_write(
        ws, "big.py", "\n".join(f"b{i}" for i in range(500)), max_lines=20
    )
    assert "more diff lines" in preview
    assert len(preview.splitlines()) <= 21


def test_a_patch_is_previewed_by_applying_it_in_memory(ws: Workspace) -> None:
    patch = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def main():\n"
        "-    print('hello')\n"
        "+    print('ciao')\n"
    )
    preview = preview_patch(ws, patch)
    assert "-    print('hello')" in preview
    assert "+    print('ciao')" in preview
    assert (ws.root / "main.py").read_text(encoding="utf-8").count("hello") == 1, (
        "previewing must not touch the file"
    )


def test_a_patch_that_does_not_apply_says_so_before_asking(ws: Workspace) -> None:
    preview = preview_patch(
        ws, "--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,2 @@\n WRONG\n+x\n"
    )
    assert "does not apply" in preview


def test_the_preview_still_refuses_paths_outside_the_workspace(ws: Workspace) -> None:
    with pytest.raises(ToolSecurityError):
        preview_write(ws, "../escape.txt", "x")
    assert "does not apply" in preview_patch(
        ws, "--- a/../escape.txt\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+x\n"
    )


def test_describe_call_puts_the_diff_in_the_dialog(ws: Workspace) -> None:
    body = describe_call(
        "write_file",
        {"path": "main.py", "content": "def main():\n    print('bye')\n\n\nmain()\n"},
        ws,
    )
    assert body.startswith("WRITE main.py")
    assert str(ws.root) in body
    assert "+    print('bye')" in body

    command = describe_call("run_command", {"command": "ls", "cwd": "."}, ws)
    assert command.startswith("RUN ls")


def test_render_diff_on_two_empty_versions() -> None:
    assert "no changes" in render_diff([], [], "a.py")


def test_the_dialog_colours_the_diff_by_prefix() -> None:
    rendered = diff_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n same")
    styles = [str(span.style) for span in rendered.spans]
    assert styles[:5] == ["bold", "bold", "#c9a15a", "#c47a5d", "#8da287"]
    assert str(rendered).endswith(" same"), "context keeps the default colour"
