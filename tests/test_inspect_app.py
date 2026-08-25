"""Inspection and export as the operator meets them, inside the app."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from vox_chat.agent import AgentEvent
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.llm_client import TokenSample
from vox_chat.models import Message as ChatMessage
from vox_chat.report import reports_dir
from vox_chat.storage import global_config_path
from vox_chat.ui.inspect_screen import InspectScreen
from vox_chat.ui.widgets import MessageBox


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def messages(app: VoxApp) -> list[MessageBox]:
    return [box for box in app.transcript.children if isinstance(box, MessageBox)]


def token_event(pairs: list[tuple[str, float]], phase: str = "answer") -> AgentEvent:
    """A token event shaped the way the client produces one."""
    sample = TokenSample(
        pairs[0][0],
        math.log(pairs[0][1]),
        tuple((name, math.log(probability)) for name, probability in pairs),
    )
    return AgentEvent("token", token=sample, phase=phase)


CONFIDENT = [("The", 0.99), ("A", 0.01)]
FLAT = [(" of", 0.4), (" in", 0.35), (" to", 0.25)]


async def test_inspection_is_off_until_asked(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.inspect_enabled is False
        assert app.inspect_top_k() is None, "no logprobs are requested"

        app.run_command("/inspect on")
        await pilot.pause()
        assert app.inspect_enabled is True
        assert app.inspect_top_k() == 5
        assert "INSPECTION ON" in messages(app)[-1].message.content

        app.run_command("/inspect off")
        await pilot.pause()
        assert app.inspect_top_k() is None
        assert "INSPECTION OFF" in messages(app)[-1].message.content


async def test_a_bad_inspect_argument_is_refused(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/inspect maybe")
        await pilot.pause()
        last = messages(app)[-1].message
        assert last.role == "error" and "USAGE" in last.content


async def test_tokens_are_measured_as_they_arrive(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_agent_event(token_event(CONFIDENT))
        app.handle_agent_event(token_event(FLAT))
        await pilot.pause()

        run = app.inspection
        assert len(run) == 2
        assert run.records[0].probability == pytest.approx(0.99, abs=1e-9)
        assert [record.text for record in run.decisions] == [" of"]


async def test_the_screen_shows_the_measurements(app: VoxApp) -> None:
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.handle_agent_event(token_event(FLAT))
        await pilot.press("ctrl+i")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, InspectScreen)
        rows = str(app.screen.query_one("#inspect-rows").render())
        assert "' of'" in rows
        assert "DECISION" in rows
        assert "1 decision points" in str(app.screen.query_one("#inspect-title").render())
        summary = str(app.screen.query_one("#inspect-summary").render())
        assert "top-k" in summary, "the entropy basis is stated on screen"

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InspectScreen)


async def test_the_screen_keeps_filling_while_the_answer_streams(app: VoxApp) -> None:
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, InspectScreen)

        app.handle_agent_event(token_event([("live", 0.8), ("x", 0.2)]))
        await pilot.pause()
        assert "'live'" in str(screen.query_one("#inspect-rows").render())

        await pilot.press("escape")
        await pilot.pause()


async def test_the_screen_explains_an_empty_table(app: VoxApp) -> None:
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        summary = str(app.screen.query_one("#inspect-summary").render())
        assert "Inspection is off" in summary, "it says which of the two it is"
        await pilot.press("escape")
        await pilot.pause()


async def test_filters_narrow_the_table(app: VoxApp) -> None:
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.handle_agent_event(token_event([("calm", 0.99), ("x", 0.01)], "thinking"))
        app.handle_agent_event(token_event(FLAT))
        await pilot.press("ctrl+i")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, InspectScreen)

        await pilot.press("d")
        await pilot.pause()
        rows = str(screen.query_one("#inspect-rows").render())
        assert "' of'" in rows and "'calm'" not in rows

        await pilot.press("t")
        await pilot.pause()
        rows = str(screen.query_one("#inspect-rows").render())
        assert "'calm'" in rows and "' of'" not in rows

        await pilot.press("escape")
        await pilot.pause()


async def test_a_new_turn_starts_a_new_run(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_agent_event(token_event(CONFIDENT))
        assert len(app.inspection) == 1

        app.input_area.insert("again")
        app.action_send()
        await pilot.pause()
        assert len(app.inspection) == 0, "each turn is measured on its own"


async def test_export_writes_all_three_formats(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session.messages.append(
            ChatMessage(role="user", content="why is the sky blue?")
        )
        app.session.messages.append(
            ChatMessage(role="assistant", content="Scattering.")
        )
        app.handle_agent_event(token_event([("Sc", 0.9), ("x", 0.1)]))

        await pilot.press("ctrl+e")
        await pilot.pause()

        assert sorted(path.suffix for path in reports_dir().glob("*")) == [
            ".html", ".json", ".md"
        ]
        assert "SAVED" in messages(app)[-1].message.content

        html = next(reports_dir().glob("*.html")).read_text(encoding="utf-8")
        assert "why is the sky blue?" in html
        assert "Scattering." in html
        assert "<script" not in html.lower()


async def test_the_export_records_the_parameters_actually_used(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/inspect on")
        app.session.messages.append(ChatMessage(role="user", content="q"))
        await pilot.pause()

        report = app.build_report()
        assert report.model == app.config["active_model"]
        assert report.endpoint == "http://127.0.0.1:1/v1"
        assert report.parameters["agent_mode"] is False
        assert report.parameters["inspect"]["top_k"] == 5
        assert report.question == "q"


async def test_export_accepts_one_format_and_refuses_nonsense(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/export md")
        await pilot.pause()
        assert [path.suffix for path in reports_dir().glob("*")] == [".md"]

        app.run_command("/export pdf")
        await pilot.pause()
        last = messages(app)[-1].message
        assert last.role == "error" and "UNKNOWN FORMAT" in last.content


async def test_a_provider_that_refuses_logprobs_is_reported_once(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.client is not None
        app.client.logprobs_refused = True

        app.finish_generation(None)
        await pilot.pause()

        def errors() -> list[str]:
            return [
                box.message.content for box in messages(app)
                if box.message.role == "error"
            ]

        assert any("INSPECTION UNAVAILABLE" in text for text in errors())
        assert "rejected logprobs" in (app.inspection.note or "")

        before = len(errors())
        app.finish_generation(None)
        await pilot.pause()
        assert len(errors()) == before, "it is said once, not on every turn"


async def test_exporting_an_unmeasured_session_still_works(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.session.messages.append(ChatMessage(role="user", content="no inspection"))
        app.run_command("/export html")
        await pilot.pause()

        html = next(reports_dir().glob("*.html")).read_text(encoding="utf-8")
        assert "Inspection was off" in html
        assert "no inspection" in html
