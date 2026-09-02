"""Attaching an image, and knowing when the terminal or the model cannot help.

No real image files are needed: a PNG is recognised by its first eight bytes,
which is the point — the extension is not evidence and a renamed screenshot is
common enough to matter.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from vox_chat import images
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.models import Message
from vox_chat.storage import global_config_path
from vox_chat.ui.widgets import MessageBox

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


# ------------------------------------------------------------------- reading


def test_what_it_is_comes_from_the_bytes_not_the_name(tmp_path: Path) -> None:
    """A .png that is really a JPEG is an ordinary thing to have, and a
    provider rejects the mismatch rather than the file."""
    path = tmp_path / "screenshot.png"
    path.write_bytes(JPEG)
    assert images.load(path).media_type == "image/jpeg"


def test_every_format_worth_sending_is_recognised() -> None:
    assert images.media_type(PNG) == "image/png"
    assert images.media_type(b"GIF89a...") == "image/gif"
    assert images.media_type(b"BM..") == "image/bmp"
    assert images.media_type(b"RIFF____WEBPVP8 ") == "image/webp"


def test_something_that_is_not_an_image_says_so(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(images.ImageError, match="not a PNG"):
        images.load(path)


def test_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(images.ImageError, match="cannot read"):
        images.load(tmp_path / "nothing.png")


def test_an_image_too_large_to_be_a_question_is_refused(tmp_path: Path) -> None:
    """A base64 data URI is a third larger than the file and is counted as
    prompt tokens; past the cap it is a refused request, not a question."""
    path = tmp_path / "huge.png"
    path.write_bytes(PNG + b"\x00" * images.MAX_BYTES)
    with pytest.raises(images.ImageError, match="prompt text"):
        images.load(path)


def test_the_data_uri_is_what_a_provider_expects(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    attachment = images.load(path)
    assert attachment.data_uri.startswith("data:image/png;base64,")
    encoded = attachment.data_uri.split(",", 1)[1]
    assert base64.b64decode(encoded) == PNG
    part = attachment.content_part()
    assert part["type"] == "image_url"


# ------------------------------------------------------------- in a message


def test_a_message_with_an_image_becomes_a_list_of_parts() -> None:
    """Only this shape carries both the words and the picture."""
    message = Message(
        role="user", content="what is this?", images=["data:image/png;base64,AA"]
    )
    payload = message.to_api()
    assert isinstance(payload["content"], list)
    assert payload["content"][0] == {"type": "text", "text": "what is this?"}
    assert payload["content"][1]["image_url"]["url"] == "data:image/png;base64,AA"


def test_a_message_without_one_is_still_a_plain_string() -> None:
    """Sending a list where a string used to go would change every request."""
    assert Message(role="user", content="hello").to_api()["content"] == "hello"


def test_an_image_with_no_words_still_travels() -> None:
    payload = Message(role="user", images=["data:image/png;base64,AA"]).to_api()
    assert len(payload["content"]) == 1
    assert payload["content"][0]["type"] == "image_url"


def test_an_attached_image_survives_a_saved_session() -> None:
    message = Message(role="user", content="x", images=["data:image/png;base64,AA"])
    assert Message.from_dict(message.to_dict()).images == message.images


# ------------------------------------------------------------- the terminal


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"TERM": "xterm-kitty"}, "kitty"),
        ({"KITTY_WINDOW_ID": "1"}, "kitty"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iterm"),
        ({"LC_TERMINAL": "iTerm2"}, "iterm"),
        ({"TERM_PROGRAM": "WezTerm"}, "iterm"),
        ({"TERM": "xterm-256color"}, "none"),
        ({}, "none"),
    ],
)
def test_the_protocol_is_detected_not_attempted(env, expected) -> None:
    """Writing a Kitty escape to a terminal that does not know it prints the
    payload across the screen; there is no polite degradation to rely on."""
    assert images.preview_protocol(env) == expected


def test_no_protocol_means_no_escape_sequence(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    assert images.inline_preview(images.load(path), "none") == ""


def test_each_protocol_produces_its_own_sequence(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    attachment = images.load(path)
    assert images.inline_preview(attachment, "iterm").startswith("\033]1337;File=")
    kitty = images.inline_preview(attachment, "kitty")
    assert kitty.startswith("\033_Ga=T,f=100,")
    assert kitty.endswith("\033\\")


def test_a_large_image_is_chunked_for_kitty(tmp_path: Path) -> None:
    """4096 is the protocol's limit per escape, not a suggestion."""
    path = tmp_path / "big.png"
    path.write_bytes(PNG + b"\x01" * 20_000)
    kitty = images.inline_preview(images.load(path), "kitty")
    assert kitty.count("\033_G") > 1
    assert "m=0;" in kitty, "the last chunk says it is the last"


# ------------------------------------------------------------------ the app


async def test_image_attaches_and_the_next_message_carries_it(
    app: VoxApp, tmp_path: Path
) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(PNG)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command(f"/image {path}")
        await pilot.pause()
        assert len(app.pending_images) == 1
        assert "IMAGE ATTACHED" in transcript(app)

        app.input_area.text = "what is this?"
        app.action_send()
        await pilot.pause()

    assert app.pending_images == [], "the box is empty again afterwards"
    sent = [m for m in app.session.messages if m.role == "user"][-1]
    assert sent.images and sent.images[0].startswith("data:image/png")


async def test_a_file_that_cannot_be_read_attaches_nothing(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/image /nowhere/at/all.png")
        await pilot.pause()
        assert app.pending_images == []
        assert "IMAGE - cannot read" in transcript(app)


async def test_image_off_drops_what_was_attached(app: VoxApp, tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command(f"/image {path}")
        await pilot.pause()
        app.run_command("/image off")
        await pilot.pause()
        assert app.pending_images == []
        assert "dropped" in transcript(app)


# ------------------------------------------------------- does the model read?


def test_a_server_that_says_so_is_believed(monkeypatch) -> None:
    from vox_chat import ollama

    monkeypatch.setattr(
        ollama, "_post", lambda *a, **k: {"capabilities": ["completion", "vision"]}
    )
    assert ollama.reads_images("http://x:11434", "any") is True


def test_a_server_that_says_no_is_also_believed(monkeypatch) -> None:
    from vox_chat import ollama

    monkeypatch.setattr(
        ollama, "_post", lambda *a, **k: {"capabilities": ["completion"]}
    )
    assert ollama.reads_images("http://x:11434", "llava") is False


def test_an_older_server_is_read_from_the_family_name(monkeypatch) -> None:
    """Ollama did not always report capabilities, and the name is what is
    left. Guessing yes costs a rejected request, so the fallback errs to no."""
    from vox_chat import ollama

    monkeypatch.setattr(ollama, "_post", lambda *a, **k: {"details": {"families": []}})
    assert ollama.reads_images("http://x:11434", "qwen2.5-vl:7b") is True
    assert ollama.reads_images("http://x:11434", "qwen2.5-coder:3b") is False
