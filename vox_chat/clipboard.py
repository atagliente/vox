"""System clipboard access, without adding a dependency.

Textual can only reach the clipboard through OSC 52, which many terminals
ignore, and its own paste buffer is internal. This module shells out to
whatever clipboard helper the platform actually provides, and reports
honestly when it finds none.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Helper:
    """One clipboard command, with the arguments it needs."""

    argv: list[str]
    label: str

    @property
    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None


def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")


def copy_helpers() -> list[Helper]:
    """Candidate copy commands, best first, for this platform."""
    if sys.platform == "win32":
        return [
            Helper(
                ["powershell", "-NoProfile", "-Command",
                 "$input | Set-Clipboard"],
                "powershell Set-Clipboard",
            ),
            Helper(["clip"], "clip"),
        ]
    if sys.platform == "darwin":
        return [Helper(["pbcopy"], "pbcopy")]
    if _is_termux():
        return [Helper(["termux-clipboard-set"], "termux-clipboard-set")]
    return [
        Helper(["wl-copy"], "wl-copy"),
        Helper(["xclip", "-selection", "clipboard"], "xclip"),
        Helper(["xsel", "--clipboard", "--input"], "xsel"),
    ]


def paste_helpers() -> list[Helper]:
    """Candidate paste commands, best first, for this platform."""
    if sys.platform == "win32":
        return [
            Helper(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                "powershell Get-Clipboard",
            )
        ]
    if sys.platform == "darwin":
        return [Helper(["pbpaste"], "pbpaste")]
    if _is_termux():
        return [Helper(["termux-clipboard-get"], "termux-clipboard-get")]
    return [
        Helper(["wl-paste", "--no-newline"], "wl-paste"),
        Helper(["xclip", "-selection", "clipboard", "-o"], "xclip"),
        Helper(["xsel", "--clipboard", "--output"], "xsel"),
    ]


def copy(text: str) -> tuple[bool, str]:
    """Put ``text`` on the system clipboard. Returns ``(ok, detail)``."""
    if not text:
        return False, "nothing to copy"
    tried: list[str] = []
    for helper in copy_helpers():
        if not helper.available:
            continue
        tried.append(helper.label)
        try:
            completed = subprocess.run(
                helper.argv,
                input=text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return True, helper.label
    if tried:
        return False, "clipboard helper failed: " + ", ".join(tried)
    return False, "no clipboard helper found"


def paste() -> tuple[str | None, str]:
    """Read the system clipboard. Returns ``(text, detail)``."""
    tried: list[str] = []
    for helper in paste_helpers():
        if not helper.available:
            continue
        tried.append(helper.label)
        try:
            completed = subprocess.run(
                helper.argv,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            # PowerShell appends a newline of its own to Get-Clipboard -Raw.
            return completed.stdout.rstrip("\r\n"), helper.label
    if tried:
        return None, "clipboard helper failed: " + ", ".join(tried)
    return None, "no clipboard helper found"


def describe() -> str:
    """What is available here, for the doctor report."""
    copiers = [helper.label for helper in copy_helpers() if helper.available]
    pasters = [helper.label for helper in paste_helpers() if helper.available]
    if not copiers and not pasters:
        return "none found"
    return f"copy: {', '.join(copiers) or 'none'} | paste: {', '.join(pasters) or 'none'}"
