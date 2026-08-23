"""Workspace confinement, tool behaviour and command execution limits."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vox_chat.tools import (
    ToolError,
    ToolSecurityError,
    Workspace,
    apply_patch,
    execute,
    list_files,
    read_file,
    run_command,
    search_text,
    truncate,
    write_file,
)


@pytest.fixture
def ws(workspace: Path) -> Workspace:
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text(
        "def main():\n    print('hello')\n\n\nmain()\n", encoding="utf-8"
    )
    (workspace / "notes.txt").write_text("alpha\nbeta\nGAMMA\n", encoding="utf-8")
    return Workspace(workspace)


def test_paths_are_confined_to_the_workspace(ws: Workspace) -> None:
    assert ws.resolve("src/main.py").is_file()
    for candidate in ("../outside.txt", "../../etc/passwd", "src/../../escape"):
        with pytest.raises(ToolSecurityError):
            ws.resolve(candidate)


def test_absolute_paths_outside_the_workspace_are_refused(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    with pytest.raises(ToolSecurityError):
        ws.resolve(str(outside))
    with pytest.raises(ToolSecurityError):
        read_file(ws, str(outside))


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("VOX_TEST_SYMLINKS"),
    reason="creating symlinks on Windows needs developer mode or admin rights",
)
def test_symlinks_leaving_the_workspace_are_refused(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    link = ws.root / "escape.txt"
    link.symlink_to(outside)
    with pytest.raises(ToolSecurityError):
        read_file(ws, "escape.txt")


def test_read_file_returns_a_numbered_range(ws: Workspace) -> None:
    result = read_file(ws, "src/main.py", 1, 2)
    assert result.ok
    assert "1  def main():" in result.output
    assert "print" in result.output
    assert "main()" not in result.output.split("\n")[-1]


def test_read_file_refuses_directories_and_missing_files(ws: Workspace) -> None:
    with pytest.raises(ToolError):
        read_file(ws, "src")
    with pytest.raises(ToolError):
        read_file(ws, "nope.py")


def test_list_files_and_search_text(ws: Workspace) -> None:
    listing = list_files(ws, ".")
    assert "src/" in listing.output
    assert "notes.txt" in listing.output

    found = search_text(ws, "gamma", ".")
    assert "notes.txt:3" in found.output
    assert "no match" in search_text(ws, "zzz", ".").output


def test_write_file_stays_inside_the_workspace(ws: Workspace) -> None:
    result = write_file(ws, "out/new.txt", "content\n")
    assert result.ok
    assert (ws.root / "out" / "new.txt").read_text(encoding="utf-8") == "content\n"
    with pytest.raises(ToolSecurityError):
        write_file(ws, "../evil.txt", "x")


def test_apply_patch_edits_an_existing_file(ws: Workspace) -> None:
    patch = (
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " GAMMA\n"
    )
    result = apply_patch(ws, patch)
    assert result.ok
    assert (ws.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\nGAMMA\n"


def test_apply_patch_refuses_a_context_mismatch(ws: Workspace) -> None:
    patch = (
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " WRONG\n"
        "+added\n"
    )
    with pytest.raises(ToolError):
        apply_patch(ws, patch)
    assert (ws.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\nGAMMA\n"


def test_apply_patch_cannot_escape_the_workspace(ws: Workspace) -> None:
    patch = "--- a/../evil.txt\n+++ b/../evil.txt\n@@ -0,0 +1 @@\n+pwned\n"
    with pytest.raises(ToolSecurityError):
        apply_patch(ws, patch)


def test_run_command_reports_stdout_and_exit_code(ws: Workspace) -> None:
    result = run_command(ws, f'"{sys.executable}" -c "print(41+1)"', ".", 30)
    assert result.ok
    assert "42" in result.output
    assert "exit code: 0" in result.output


def test_run_command_reports_a_failure_without_raising(ws: Workspace) -> None:
    result = run_command(ws, f'"{sys.executable}" -c "raise SystemExit(3)"', ".", 30)
    assert not result.ok
    assert "exit code: 3" in result.output


def test_run_command_times_out(ws: Workspace) -> None:
    result = run_command(
        ws, f'"{sys.executable}" -c "import time; time.sleep(5)"', ".", 1
    )
    assert not result.ok
    assert "timed out after 1s" in result.output


def test_run_command_has_no_shell(ws: Workspace) -> None:
    """Chaining and redirection must not be interpreted."""
    result = run_command(ws, f'"{sys.executable}" -c "print(1)" && echo pwned', ".", 30)
    stdout = result.output.split("--- stdout ---")[-1]
    assert "pwned" not in stdout, "&& must be an argument, never a shell operator"
    assert stdout.strip() == "1"


def test_run_command_refuses_a_cwd_outside_the_workspace(ws: Workspace) -> None:
    with pytest.raises(ToolSecurityError):
        run_command(ws, "echo hi", "..", 5)


def test_truncate_limits_the_payload_sent_back_to_the_model() -> None:
    text, cut = truncate("a" * 100, 10)
    assert cut and len(text) == 10
    text, cut = truncate("short", 10)
    assert not cut and text == "short"


def test_execute_dispatches_and_rejects_unknown_tools(ws: Workspace) -> None:
    assert "notes.txt" in execute("list_files", {"path": "."}, ws).output
    with pytest.raises(ToolError):
        execute("rm_rf", {}, ws)


def test_execute_truncates_long_output(ws: Workspace) -> None:
    (ws.root / "big.txt").write_text("x" * 5000, encoding="utf-8")
    result = execute("read_file", {"path": "big.txt"}, ws, max_output=200)
    assert result.truncated
    assert "[output truncated]" in result.as_text()
