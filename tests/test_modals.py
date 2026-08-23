"""The confirmation dialog: arrow navigation, legend and ctrl+c handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Button

from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.storage import global_config_path
from vox_chat.ui.modals import ConfirmModal


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def focused_button(app: VoxApp) -> str:
    focused = app.focused
    assert isinstance(focused, Button)
    return str(focused.id)


async def test_arrows_move_between_the_buttons(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfirmModal("AUTHORIZE WRITE?", "a.txt"))
        await pilot.pause()
        assert focused_button(app) == "cancel", "the safe choice is focused first"

        await pilot.press("left")
        assert focused_button(app) == "ok"
        await pilot.press("right")
        assert focused_button(app) == "cancel"
        await pilot.press("tab")
        assert focused_button(app) == "ok"


async def test_enter_activates_the_focused_button(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool] = []
        app.push_screen(ConfirmModal("AUTHORIZE WRITE?", "a.txt"), result.append)
        await pilot.pause()
        await pilot.press("left")
        await pilot.press("enter")
        await pilot.pause()
        assert result == [True]


async def test_enter_on_the_default_button_denies(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool] = []
        app.push_screen(ConfirmModal("AUTHORIZE WRITE?", "a.txt"), result.append)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert result == [False], "a stray enter never authorises"


async def test_the_legend_names_every_key(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfirmModal("DELETE PROMPT?", "x", "DELETE", "KEEP"))
        await pilot.pause()
        hint = str(app.screen.query_one("#modal-hint").render())
        assert "←/→ choose" in hint
        assert "enter select" in hint
        assert "y delete" in hint
        assert "esc keep" in hint
        assert "ctrl+c" not in hint, "ctrl+c must not authorise a deletion"


async def test_the_quit_legend_mentions_ctrl_c(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            ConfirmModal("UNSAVED CHANGES", "quit?", "DISCARD", "CANCEL",
                         ctrl_c_confirms=True)
        )
        await pilot.pause()
        hint = str(app.screen.query_one("#modal-hint").render())
        assert "ctrl+c discard" in hint
        assert "esc cancel" in hint


async def test_ctrl_c_confirms_a_quit_dialog(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dirty = True
        await pilot.press("ctrl+c")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal), "the quit dialog is up"
        assert app._exit is False

        await pilot.press("ctrl+c")
        await pilot.pause()
        await pilot.pause()
        assert app._exit is True, "pressing it again confirms the quit"


async def test_ctrl_c_never_authorises_a_write(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool] = []
        app.push_screen(
            ConfirmModal("AUTHORIZE WRITE_FILE?", "a.txt"), result.append
        )
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert result == [False]
        assert app._exit is False


async def test_ctrl_c_closes_a_picker_without_quitting(app: VoxApp) -> None:
    from vox_chat.ui.modals import PickerModal

    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/roles")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, PickerModal)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, PickerModal)
        assert app._exit is False


async def test_y_and_escape_still_work(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        first: list[bool] = []
        app.push_screen(ConfirmModal("AUTHORIZE WRITE?", "a.txt"), first.append)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert first == [True]

        second: list[bool] = []
        app.push_screen(ConfirmModal("AUTHORIZE WRITE?", "a.txt"), second.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert second == [False]
