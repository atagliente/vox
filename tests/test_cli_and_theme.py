"""Using VOX without the screen: --ask, --resume, /theme and NO_COLOR.

None of these talks to a model. What is tested is what reaches stdout, what
reaches stderr, and what the exit code says — because the whole value of a
one-shot mode is that a script can rely on those three.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.llm_client import LLMError, StreamEvent
from vox_chat.storage import global_config_path
from vox_chat.ui import branding
from vox_chat.ui.widgets import MessageBox


@pytest.fixture
def configured(workspace: Path):
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return load_config(workspace)


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


class FakeClient:
    """A provider that answers from a script, or refuses."""

    def __init__(self, parts: list[str] | None = None, error: LLMError | None = None):
        self.parts = parts or []
        self.error = error
        self.closed = False
        self.kwargs: dict = {}

    def stream_chat(self, messages, **kwargs):
        self.kwargs = kwargs
        self.messages = list(messages)
        if self.error is not None:
            raise self.error
        return iter(
            [StreamEvent("text", text=part) for part in self.parts]
            + [StreamEvent("reasoning", text="thinking out loud")]
            + [StreamEvent("done")]
        )

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------- --ask


def one_shot(monkeypatch, loaded, workspace, client) -> tuple[int, str, str]:
    from vox_chat import oneshot

    monkeypatch.setattr(
        oneshot.LLMClient, "from_provider", staticmethod(lambda p: client)
    )
    out, err = io.StringIO(), io.StringIO()
    code = oneshot.ask("what is 2+2", loaded, workspace, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_the_answer_and_nothing_else_goes_to_stdout(
    monkeypatch, configured, workspace
) -> None:
    """Anything else on stdout makes the output unusable to whatever asked."""
    client = FakeClient(["4", " exactly"])
    code, out, err = one_shot(monkeypatch, configured, workspace, client)
    assert code == 0
    assert out == "4 exactly\n"
    assert err == ""


def test_the_models_thinking_is_not_the_answer(
    monkeypatch, configured, workspace
) -> None:
    """A script asked a question; reasoning on stdout would be the caller's
    problem to strip."""
    _code, out, _err = one_shot(
        monkeypatch, configured, workspace, FakeClient(["the answer"])
    )
    assert "thinking out loud" not in out


def test_a_refusal_is_an_exit_code_and_stderr(
    monkeypatch, configured, workspace
) -> None:
    """So `vox --ask ... || echo failed` behaves the way anyone expects."""
    client = FakeClient(error=LLMError("connection", "nothing is listening", ""))
    code, out, err = one_shot(monkeypatch, configured, workspace, client)
    assert code == 1
    assert out == ""
    assert "nothing is listening" in err


def test_the_client_is_closed_either_way(monkeypatch, configured, workspace) -> None:
    client = FakeClient(error=LLMError("connection", "no", ""))
    one_shot(monkeypatch, configured, workspace, client)
    assert client.closed is True


def test_a_misconfiguration_is_a_line_not_a_traceback(configured, workspace) -> None:
    """The caller is a shell script. A traceback is the wrong answer to a
    provider that is not defined."""
    from vox_chat import oneshot

    configured.data["active_provider"] = "not-configured"
    err = io.StringIO()
    code = oneshot.ask("x", configured, workspace, out=io.StringIO(), err=err)
    assert code == 2
    assert "vox doctor" in err.getvalue()
    assert "Traceback" not in err.getvalue()


def test_the_project_notes_are_carried(monkeypatch, configured, workspace) -> None:
    (workspace / "AGENTS.md").write_text("this project uses tabs", encoding="utf-8")
    client = FakeClient(["ok"])
    one_shot(monkeypatch, configured, workspace, client)
    assert any("uses tabs" in m.content for m in client.messages)


def test_no_agent_tools_are_offered(monkeypatch, configured, workspace) -> None:
    """Confirming a write needs somebody to confirm it, and there is nobody
    here. Running unattended is not something to arrive at by accident."""
    client = FakeClient(["ok"])
    one_shot(monkeypatch, configured, workspace, client)
    assert not client.kwargs.get("tools")


def test_the_flag_exists_and_is_not_the_provider_flag() -> None:
    """-p was already --provider, and changing it would break every existing
    invocation."""
    from vox_chat.__main__ import build_parser

    args = build_parser().parse_args(["--ask", "hello"])
    assert args.ask == "hello"
    args = build_parser().parse_args(["-a", "hello"])
    assert args.ask == "hello"
    assert build_parser().parse_args(["-p", "openai"]).provider == "openai"


# ----------------------------------------------------------------- --resume


def test_resume_is_a_flag() -> None:
    from vox_chat.__main__ import build_parser

    assert build_parser().parse_args(["--resume"]).resume is True
    assert build_parser().parse_args([]).resume is False


async def test_resume_reopens_the_last_saved_session(configured, workspace) -> None:
    from vox_chat.models import Message

    app = VoxApp(loaded=configured, workspace=workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session.messages.append(Message(role="user", content="remember this"))
        app.save_session("earlier")
        await pilot.pause()

    resumed = VoxApp(loaded=load_config(workspace), workspace=workspace)
    resumed.resume_on_start = "earlier"
    async with resumed.run_test() as pilot:
        await pilot.pause()
        written = transcript(resumed)
    assert "remember this" in written


async def test_nothing_to_resume_is_not_a_crash(configured, workspace) -> None:
    app = VoxApp(loaded=configured, workspace=workspace)
    app.resume_on_start = "never-saved"
    async with app.run_test() as pilot:
        await pilot.pause()
        written = transcript(app)
    assert app.resume_on_start == ""
    assert written, "the app started and said something"


# ------------------------------------------------------------------ NO_COLOR


def test_the_variable_being_present_is_the_request() -> None:
    """https://no-color.org: whatever it is set to, including empty. Reading
    it as a boolean is the common way to get this wrong."""
    assert branding.no_color({"NO_COLOR": ""}) is True
    assert branding.no_color({"NO_COLOR": "0"}) is True
    assert branding.no_color({"NO_COLOR": "false"}) is True
    assert branding.no_color({}) is False


def test_no_color_overrides_the_saved_theme() -> None:
    """A theme that ignored it would make the setting useless in exactly the
    case it exists for."""
    plain = branding.theme_css("nasa", {"NO_COLOR": "1"})
    assert "background:" not in plain
    assert "color:" not in plain


def test_the_layout_survives_having_no_colour() -> None:
    plain = branding.theme_css("nasa", {"NO_COLOR": "1"})
    for part in ("#side-panel", "#transcript", "#header"):
        assert part in plain


def test_a_theme_still_has_colour_without_the_variable() -> None:
    assert "background:" in branding.theme_css("nasa", {})


# -------------------------------------------------------------------- /theme


async def test_theme_lists_what_there_is(configured, workspace) -> None:
    app = VoxApp(loaded=configured, workspace=workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/theme")
        await pilot.pause()
        written = transcript(app)
    for name in branding.theme_names():
        assert name in written


async def test_theme_saves_and_says_when_it_applies(configured, workspace) -> None:
    """Textual reads the stylesheet when the app is built, so saying it
    applies now would be a theme that half-applies."""
    app = VoxApp(loaded=configured, workspace=workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/theme light")
        await pilot.pause()
        written = transcript(app)
    assert app.config["ui"]["theme"] == "light"
    assert "next start" in written


async def test_an_unknown_theme_changes_nothing(configured, workspace) -> None:
    app = VoxApp(loaded=configured, workspace=workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/theme chartreuse")
        await pilot.pause()
        written = transcript(app)
    assert app.config["ui"]["theme"] == "nasa"
    assert "unknown" in written.lower()


async def test_theme_says_when_no_color_is_in_charge(
    configured, workspace, monkeypatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    app = VoxApp(loaded=configured, workspace=workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/theme")
        await pilot.pause()
        written = transcript(app)
    assert "NO_COLOR" in written
