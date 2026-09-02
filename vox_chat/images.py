"""Attaching an image to a message, and showing it if the terminal can.

Local vision models are good enough now that "look at this screenshot" is an
ordinary thing to want from a terminal client. What it takes is small: read
the file, work out what it is, and send it as a ``image_url`` content part
with a ``data:`` URI. No dependency — the format is decided by the first few
bytes, which is more reliable than the extension anyway.

Two things this is careful about:

- **Size.** A base64 data URI is a third larger than the file, and it is
  counted as prompt tokens. A 4 MB photograph is not a question, it is a
  refused request, so there is a cap and it says so.
- **The terminal.** Kitty and iTerm2 can draw an image in the scrollback;
  everything else cannot, and guessing wrong writes escape codes across the
  screen. So the protocol is detected from the environment, and where there
  is none the fallback is a line of text saying what was attached.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

# What the first bytes of a file say it is. Checked in this order.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

# 4 MB of file is 5.5 MB of base64 and a very large number of prompt tokens.
# Past this it is not a question any local model will answer.
MAX_BYTES = 4 * 1024 * 1024


class ImageError(Exception):
    """The image could not be attached, and why."""


@dataclass(frozen=True)
class Attachment:
    """One image, ready to go into a request."""

    path: Path
    media_type: str
    data: bytes

    @property
    def data_uri(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def content_part(self) -> dict[str, object]:
        """The ``image_url`` part of a multimodal message."""
        return {"type": "image_url", "image_url": {"url": self.data_uri}}

    def describe(self) -> str:
        kind = self.media_type.split("/")[-1].upper()
        return f"{self.path.name} · {kind} · {len(self.data) / 1024:.0f} kB"


def media_type(data: bytes) -> str:
    """What this actually is, from its first bytes rather than its name.

    A ``.png`` that is really a JPEG is common enough — screenshots renamed by
    hand — and a provider rejects the mismatch, not the file.
    """
    for signature, kind in SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageError("that file is not a PNG, JPEG, GIF, BMP or WebP image")


def load(path: str | Path) -> Attachment:
    """Read an image from disk, or say why it cannot be sent."""
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        raise ImageError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if not resolved.is_file():
        raise ImageError(f"{resolved} is not a file")
    size = resolved.stat().st_size
    if size > MAX_BYTES:
        raise ImageError(
            f"{resolved.name} is {size / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_BYTES // 1024 // 1024} MB because it is sent as prompt text"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read {resolved}: {exc.strerror or exc}") from exc
    return Attachment(resolved, media_type(data), data)


# ---------------------------------------------------------------- the terminal


def preview_protocol(environ: dict[str, str] | None = None) -> str:
    """Which inline-image protocol this terminal speaks: kitty, iterm or none.

    Detected rather than attempted. Writing a Kitty escape sequence to a
    terminal that does not know it does not degrade politely — it prints the
    payload across the screen.
    """
    env = dict(environ if environ is not None else os.environ)
    if env.get("TERM", "").startswith("xterm-kitty") or env.get("KITTY_WINDOW_ID"):
        return "kitty"
    program = env.get("TERM_PROGRAM", "")
    if program in ("iTerm.app", "WezTerm") or env.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    return "none"


def inline_preview(attachment: Attachment, protocol: str) -> str:
    """The escape sequence that draws this image, or "" where none will."""
    encoded = base64.b64encode(attachment.data).decode("ascii")
    if protocol == "iterm":
        # OSC 1337: name and size are advisory, inline=1 is the part that
        # makes it appear rather than download.
        name = base64.b64encode(attachment.path.name.encode()).decode("ascii")
        return (
            f"\033]1337;File=name={name};size={len(attachment.data)};"
            f"inline=1;width=40;preserveAspectRatio=1:{encoded}\a"
        )
    if protocol == "kitty":
        # Chunked at 4096, which is the protocol's limit per escape.
        chunks = [encoded[i : i + 4096] for i in range(0, len(encoded), 4096)]
        out = []
        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            head = "a=T,f=100," if index == 0 else ""
            out.append(f"\033_G{head}m={more};{chunk}\033\\")
        return "".join(out)
    return ""
