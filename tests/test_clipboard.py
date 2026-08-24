"""System clipboard bridge: helper selection, failures and the UI wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vox_chat import clipboard


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_every_platform_offers_a_helper() -> None:
    assert clipboard.copy_helpers(), "no copy helper for this platform"
    assert clipboard.paste_helpers(), "no paste helper for this platform"
    for helper in clipboard.copy_helpers() + clipboard.paste_helpers():
        assert helper.argv and helper.label


def test_copy_sends_the_text_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        seen["shell"] = kwargs.get("shell")
        return FakeCompleted()

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    ok, detail = clipboard.copy("hello àè ✓")
    assert ok
    assert seen["input"] == "hello àè ✓"
    assert seen["shell"] is False, "never through a shell"
    assert detail


def test_copy_refuses_empty_text() -> None:
    ok, detail = clipboard.copy("")
    assert not ok
    assert "nothing to copy" in detail


def test_copy_falls_through_to_the_next_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        return FakeCompleted(returncode=1 if len(calls) == 1 else 0)

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        clipboard,
        "copy_helpers",
        lambda: [clipboard.Helper(["broken"], "broken"),
                 clipboard.Helper(["working"], "working")],
    )
    ok, detail = clipboard.copy("text")
    assert ok and detail == "working"
    assert calls == ["broken", "working"]


def test_missing_helpers_are_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    ok, detail = clipboard.copy("text")
    assert not ok and "no clipboard helper" in detail

    text, detail = clipboard.paste()
    assert text is None and "no clipboard helper" in detail
    assert clipboard.describe() == "none found"


def test_a_hanging_helper_does_not_hang_vox(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        assert kwargs.get("timeout") == clipboard.TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(argv, clipboard.TIMEOUT_SECONDS)

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    ok, detail = clipboard.copy("text")
    assert not ok and "failed" in detail


def test_paste_strips_the_trailing_newline_helpers_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        clipboard.subprocess, "run",
        lambda argv, **kwargs: FakeCompleted(stdout="pasted text\r\n"),
    )
    text, detail = clipboard.paste()
    assert text == "pasted text"
    assert detail
