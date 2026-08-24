"""Headless smoke tests for the TUI.

The provider points at a closed port, so these tests prove the app starts and
stays usable with no server running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.storage import global_config_path, sessions_dir
from vox_chat.ui.widgets import MessageBox


def messages(app: VoxApp) -> list[MessageBox]:
    """Only the real messages: the spinner is a transcript child too."""
    return [box for box in app.transcript.children if isinstance(box, MessageBox)]

UNREACHABLE = "http://127.0.0.1:1/v1"


@pytest.fixture
def offline_app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = UNREACHABLE
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


async def test_the_app_starts_without_a_provider(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        header = str(offline_app.query_one("#header").render())
        assert "VOX" in header
        assert "LINK OFFLINE" in header
        assert "PROVIDER local-ollama" in header
        assert "ROLE python-developer" in header
        assert offline_app.connected is False
        status = str(offline_app.query_one("#status").render())
        assert "LINK OFFLINE" in status
        assert "MODE CHAT" in status


async def test_the_api_key_is_never_displayed(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        rendered = str(offline_app.query_one("#header").render())
        assert "LOGON ****ma" in rendered
        assert "LOGON ollama" not in rendered


async def test_help_command_answers(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.run_command("/help")
        await pilot.pause()
        text = "\n".join(
            box.message.content for box in messages(offline_app)
        )
        assert "/session-save" in text
        assert "alt+enter   new line" in text


async def test_unknown_command_suggests_alternatives(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.run_command("/sessio")
        await pilot.pause()
        last = messages(offline_app)[-1].message
        assert last.role == "error"
        assert "sessions" in last.content


async def test_agent_toggle_and_workspace_command(offline_app: VoxApp, tmp_path: Path) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.run_command("/agent on")
        await pilot.pause()
        assert offline_app.config["agent"]["enabled"] is True
        assert "MODE AGENT" in str(offline_app.query_one("#status").render())

        other = tmp_path / "other"
        other.mkdir()
        offline_app.run_command(f"/workspace {other}")
        await pilot.pause()
        assert offline_app.workspace_path == other.resolve()

        offline_app.run_command("/workspace /definitely/not/here")
        await pilot.pause()
        assert messages(offline_app)[-1].message.role == "error"


async def test_sending_a_message_without_a_provider_shows_an_error(
    offline_app: VoxApp,
) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.insert("hello")
        offline_app.action_send()
        await pilot.pause()
        roles = [box.message.role for box in messages(offline_app)]
        assert "user" in roles
        assert offline_app.session.messages[-1].content == "hello"


async def test_session_save_and_load_round_trip(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            __import__("vox_chat.models", fromlist=["Message"]).Message(
                role="user", content="remember me"
            )
        )
        offline_app.dirty = True
        offline_app.save_session("smoke")
        await pilot.pause()
        assert (sessions_dir() / "smoke.json").exists()
        assert offline_app.dirty is False

        offline_app.run_command("/new")
        await pilot.pause()
        offline_app.load_session("smoke")
        await pilot.pause()
        assert any(m.content == "remember me" for m in offline_app.session.messages)


async def test_prompt_is_loaded_into_the_input_not_sent(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.prompt_store.save_prompt("smoke", "work in {{workspace}}")
        offline_app.run_command("/prompt smoke")
        await pilot.pause()
        assert str(offline_app.workspace_path) in offline_app.input_area.text
        assert offline_app.generating is False


async def test_stats_command_and_status_readout(offline_app: VoxApp) -> None:
    from vox_chat.usage import TurnUsage

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.run_command("/stats")
        await pilot.pause()
        assert "no completed turn" in messages(offline_app)[-1].message.content

        offline_app.handle_agent_event(
            __import__("vox_chat.agent", fromlist=["AgentEvent"]).AgentEvent(
                "usage", usage=TurnUsage(1000, 200, 4.0)
            )
        )
        await pilot.pause()
        status = str(offline_app.query_one("#status").render())
        assert "CTX 1.2k/8.2k 15%" in status
        assert "tok/s" in status

        offline_app.run_command("/stats")
        await pilot.pause()
        report = messages(offline_app)[-1].message.content
        assert "SESSION USAGE" in report
        assert "50.0 tok/s" in report


async def test_a_new_session_resets_the_usage_counters(offline_app: VoxApp) -> None:
    from vox_chat.usage import TurnUsage

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.usage.add(TurnUsage(10, 10, 1.0))
        offline_app.run_command("/new")
        await pilot.pause()
        assert offline_app.usage.turns == 0
        assert offline_app.usage_summary() == ""


async def test_a_narrow_terminal_does_not_break_the_header(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(34, 20)) as pilot:
        await pilot.pause()
        rendered = str(offline_app.query_one("#header").render())
        assert "┌" not in rendered, "no box is drawn when it would not fit"
        assert all(len(line) <= 34 for line in rendered.splitlines())


async def test_enter_sends_and_alt_enter_inserts_a_newline(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("h", "i")
        await pilot.press("alt+enter")
        await pilot.press("t", "h", "e", "r", "e")
        assert offline_app.input_area.text == "hi\nthere"

        await pilot.press("enter")
        await pilot.pause()
        assert offline_app.input_area.text == "", "the input is cleared on send"
        assert offline_app.session.messages[-1].content == "hi\nthere"
        assert offline_app.session.messages[-1].role == "user"


async def test_ctrl_j_also_inserts_a_newline(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("a", "ctrl+j", "b")
        assert offline_app.input_area.text == "a\nb"


async def test_enter_on_a_slash_command_runs_it(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        offline_app.input_area.insert("/help")
        await pilot.press("enter")
        await pilot.pause()
        assert offline_app.input_area.text == ""
        assert "AVAILABLE COMMANDS" in messages(offline_app)[-1].message.content


async def test_ctrl_c_quits_when_there_is_nothing_to_copy_or_stop(
    offline_app: VoxApp,
) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert offline_app._exit is True, "the app was asked to quit"


async def test_ctrl_c_copies_a_selection_instead_of_quitting(
    offline_app: VoxApp,
) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        area = offline_app.input_area
        area.focus()
        area.insert("copy me")
        area.select_all()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert offline_app._exit is False, "a selection means copy, not quit"
        assert area.text == "copy me"


async def test_ctrl_c_stops_a_running_generation(offline_app: VoxApp) -> None:
    class AbortableClient:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_active_stream(self) -> bool:
            self.cancel_calls += 1
            return True

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        client = AbortableClient()
        offline_app.client = client  # type: ignore[assignment]
        offline_app.generating = True
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert offline_app.cancel_event.is_set()
        assert client.cancel_calls == 1, "the active HTTP stream is aborted once"
        assert sum(
            box.message.content == "STOP REQUESTED…" for box in messages(offline_app)
        ) == 1
        assert offline_app._exit is False
        offline_app.generating = False


async def test_confirmation_buttons_are_selected_with_arrow_keys(
    offline_app: VoxApp,
) -> None:
    from textual.widgets import Button

    from vox_chat.ui.modals import ConfirmModal

    result: list[bool] = []
    async with offline_app.run_test() as pilot:
        offline_app.push_screen(
            ConfirmModal("UNSAVED CHANGES", "Discard this session?", "DISCARD"),
            result.append,
        )
        await pilot.pause()
        modal = offline_app.screen
        assert isinstance(modal, ConfirmModal)
        assert modal.query_one("#cancel", Button).has_focus

        await pilot.press("left")
        assert modal.query_one("#ok", Button).has_focus
        await pilot.press("enter")
        await pilot.pause()
        assert result == [True]


async def test_arrow_keys_recall_previous_input(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        area = offline_app.input_area
        area.focus()
        area.insert("first message")
        await pilot.press("enter")
        await pilot.pause()
        area.insert("draft in progress")

        await pilot.press("up")
        await pilot.pause()
        assert area.text == "first message"

        await pilot.press("down")
        await pilot.pause()
        assert area.text == "draft in progress", "the draft comes back"


async def test_arrows_still_move_inside_a_multiline_draft(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.history.add("recallable")
        area = offline_app.input_area
        area.focus()
        area.insert("line one\nline two")
        await pilot.press("up")
        await pilot.pause()
        assert area.text == "line one\nline two", "the cursor moved, the text did not"
        assert area.cursor_location[0] == 0

        await pilot.press("up")
        await pilot.pause()
        assert area.text == "recallable", "at the top edge the history takes over"


async def test_the_key_bar_is_always_visible(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        bar = str(offline_app.query_one("#keybar").render())
        assert "enter send" in bar
        assert "^C quit" in bar
        assert "^G stop" in bar


async def test_the_key_bar_drops_entries_it_cannot_fit(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        bar = str(offline_app.query_one("#keybar").render())
        assert "enter send" in bar, "the most useful key survives"
        assert "^B panel" not in bar
        assert all(len(line) <= 30 for line in bar.splitlines())


async def test_key_bindings_open_the_modals(offline_app: VoxApp) -> None:
    """Regression: bindings run on the main task, where push_screen_wait fails.

    The modal flows have to run inside a worker or Textual raises
    NoActiveWorker, which used to crash the app on ctrl+p and ctrl+r.
    """
    from vox_chat.ui.modals import PickerModal

    async with offline_app.run_test() as pilot:
        for key, title in (("ctrl+p", "SAVED PROMPTS"), ("ctrl+r", "ROLES")):
            await pilot.press(key)
            await pilot.pause()
            await pilot.pause()
            screen = offline_app.screen
            assert isinstance(screen, PickerModal), f"{key} did not open a picker"
            assert screen.title_text == title
            await pilot.press("escape")
            await pilot.pause()


async def test_slash_commands_open_the_same_modals(offline_app: VoxApp) -> None:
    from vox_chat.ui.modals import PickerModal

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        for command, title in (
            ("/prompts", "SAVED PROMPTS"),
            ("/roles", "ROLES"),
            ("/sessions", "SESSIONS"),
            ("/provider", "PROVIDERS"),
        ):
            offline_app.run_command(command)
            await pilot.pause()
            await pilot.pause()
            screen = offline_app.screen
            assert isinstance(screen, PickerModal), f"{command} did not open a picker"
            assert screen.title_text == title
            await pilot.press("escape")
            await pilot.pause()


async def test_ctrl_comma_opens_the_settings_modal(offline_app: VoxApp) -> None:
    from vox_chat.ui.modals import SettingsModal

    async with offline_app.run_test() as pilot:
        await pilot.press("ctrl+comma")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(offline_app.screen, SettingsModal)
        await pilot.press("escape")
        await pilot.pause()


async def test_a_spinner_shows_while_waiting_for_the_model(offline_app: VoxApp) -> None:
    from vox_chat.agent import AgentEvent
    from vox_chat.ui.widgets import SPINNER_FRAMES, ThinkingBox

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.show_thinking("WAITING FOR MODEL")
        await pilot.pause()
        box = offline_app.query_one(ThinkingBox)
        rendered = str(box.render())
        assert any(frame in rendered for frame in SPINNER_FRAMES)
        assert "WAITING FOR MODEL" in rendered

        offline_app.generating = True
        offline_app.refresh_status()
        status = str(offline_app.query_one("#status").render())
        assert "GENERATING" in status
        assert any(frame in status for frame in SPINNER_FRAMES)

        offline_app.handle_agent_event(AgentEvent("text", text="first token"))
        await pilot.pause()
        assert not offline_app.query(ThinkingBox), "the spinner goes when text arrives"
        offline_app.generating = False


async def test_the_spinner_is_removed_when_generation_ends(offline_app: VoxApp) -> None:
    from vox_chat.ui.widgets import ThinkingBox

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.show_thinking("WAITING FOR MODEL")
        await pilot.pause()
        offline_app.finish_generation("LINK DOWN")
        await pilot.pause()
        assert not offline_app.query(ThinkingBox)
        assert "IDLE" in str(offline_app.query_one("#status").render())


async def test_reasoning_is_shown_in_its_own_block(offline_app: VoxApp) -> None:
    from vox_chat.agent import AgentEvent

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.handle_agent_event(AgentEvent("reasoning", text="weighing "))
        offline_app.handle_agent_event(AgentEvent("reasoning", text="options"))
        offline_app.handle_agent_event(AgentEvent("text", text="the answer"))
        await pilot.pause()

        blocks = messages(offline_app)
        roles = [box.message.role for box in blocks]
        assert roles[-2:] == ["reasoning", "assistant"]
        assert blocks[-2].message.content == "weighing options"
        assert "THINK" in str(blocks[-2].render())


async def test_reasoning_can_be_turned_off(offline_app: VoxApp) -> None:
    from vox_chat.agent import AgentEvent

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.config["ui"]["show_reasoning"] = False
        offline_app.handle_agent_event(AgentEvent("reasoning", text="hidden"))
        await pilot.pause()
        assert not [b for b in messages(offline_app) if b.message.role == "reasoning"]


async def test_a_saved_session_replays_its_reasoning(offline_app: VoxApp) -> None:
    from vox_chat.models import Message as ChatMessage

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            ChatMessage(role="assistant", content="answer", reasoning="because")
        )
        offline_app.save_session("with-thinking")
        offline_app.run_command("/clear")
        await pilot.pause()
        offline_app.load_session("with-thinking")
        await pilot.pause()
        roles = [box.message.role for box in messages(offline_app)]
        assert "reasoning" in roles


async def test_the_spinner_warns_when_the_first_token_is_slow(
    offline_app: VoxApp,
) -> None:
    from vox_chat.ui.widgets import ThinkingBox

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.show_thinking("WAITING FOR MODEL")
        await pilot.pause()
        box = offline_app.query_one(ThinkingBox)
        box._started -= ThinkingBox.SLOW_AFTER + 1
        box._advance()
        assert "loading the model" in str(box.render())


async def test_ctrl_shift_v_pastes_the_system_clipboard(
    offline_app: VoxApp, monkeypatch
) -> None:
    from vox_chat import clipboard

    monkeypatch.setattr(clipboard, "paste", lambda: ("pasted text", "fake"))
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("ctrl+shift+v")
        for _ in range(20):
            await pilot.pause()
            if offline_app.input_area.text:
                break
        assert offline_app.input_area.text == "pasted text"


async def test_ctrl_v_in_the_input_pastes_too(offline_app: VoxApp, monkeypatch) -> None:
    from vox_chat import clipboard

    monkeypatch.setattr(clipboard, "paste", lambda: ("from ctrl+v", "fake"))
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("ctrl+v")
        for _ in range(20):
            await pilot.pause()
            if offline_app.input_area.text:
                break
        assert offline_app.input_area.text == "from ctrl+v"


async def test_ctrl_shift_c_copies_the_selection(offline_app: VoxApp, monkeypatch) -> None:
    from vox_chat import clipboard

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: (copied.append(text), (True, "fake"))[1])
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        area = offline_app.input_area
        area.focus()
        area.insert("copy this")
        area.select_all()
        await pilot.press("ctrl+shift+c")
        for _ in range(20):
            await pilot.pause()
            if copied:
                break
        assert copied == ["copy this"]


async def test_ctrl_shift_c_falls_back_to_the_last_answer(
    offline_app: VoxApp, monkeypatch
) -> None:
    from vox_chat import clipboard
    from vox_chat.models import Message as ChatMessage

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: (copied.append(text), (True, "fake"))[1])
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            ChatMessage(role="assistant", content="the answer to copy")
        )
        await pilot.press("ctrl+shift+c")
        for _ in range(20):
            await pilot.pause()
            if copied:
                break
        assert copied == ["the answer to copy"]


async def test_a_failing_clipboard_is_reported(offline_app: VoxApp, monkeypatch) -> None:
    from vox_chat import clipboard

    monkeypatch.setattr(clipboard, "paste", lambda: (None, "no clipboard helper found"))
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.input_area.focus()
        await pilot.press("ctrl+shift+v")
        for _ in range(20):
            await pilot.pause()
            if messages(offline_app) and messages(offline_app)[-1].message.role == "error":
                break
        last = messages(offline_app)[-1].message
        assert last.role == "error"
        assert "PASTE FAILED" in last.content


async def test_copying_with_nothing_available_says_so(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+shift+c")
        await pilot.pause()
        assert messages(offline_app)[-1].message.content == "NOTHING TO COPY"


CODE_ANSWER = """Try this:

```python
x = 1
print(x)
```
"""


async def test_the_code_panel_opens_with_the_answer(offline_app: VoxApp) -> None:
    from vox_chat.models import Message as ChatMessage

    async with offline_app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert not offline_app.query_one("#side-panel").has_class("visible")

        offline_app.session.messages.append(
            ChatMessage(role="assistant", content=CODE_ANSWER)
        )
        offline_app.refresh_code_blocks()
        await pilot.pause()

        panel = offline_app.query_one("#side-panel")
        assert panel.has_class("visible"), "code opens the panel"
        assert panel.has_class("code"), "and widens it"
        content = str(offline_app.query_one("#side-content").render())
        assert "print(x)" in content
        assert "```" not in content


async def test_an_answer_without_code_leaves_the_panel_alone(
    offline_app: VoxApp,
) -> None:
    from vox_chat.models import Message as ChatMessage

    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            ChatMessage(role="assistant", content="no code here")
        )
        offline_app.refresh_code_blocks()
        await pilot.pause()
        assert offline_app.code_blocks == []
        assert not offline_app.query_one("#side-panel").has_class("visible")


async def test_ctrl_y_copies_the_last_code_block(offline_app: VoxApp, monkeypatch) -> None:
    from vox_chat import clipboard
    from vox_chat.models import Message as ChatMessage

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: (copied.append(text), (True, "fake"))[1])
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            ChatMessage(role="assistant", content=CODE_ANSWER)
        )
        offline_app.refresh_code_blocks()
        await pilot.press("ctrl+y")
        for _ in range(20):
            await pilot.pause()
            if copied:
                break
        assert copied == ["x = 1\nprint(x)"]


async def test_code_command_copies_a_numbered_block(offline_app: VoxApp, monkeypatch) -> None:
    from vox_chat import clipboard
    from vox_chat.models import Message as ChatMessage

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: (copied.append(text), (True, "fake"))[1])
    answer = CODE_ANSWER + "\nand:\n\n```bash\nls -la\n```\n"
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        offline_app.session.messages.append(
            ChatMessage(role="assistant", content=answer)
        )
        offline_app.refresh_code_blocks()
        offline_app.run_command("/code 2")
        for _ in range(20):
            await pilot.pause()
            if copied:
                break
        assert copied == ["ls -la"]

        offline_app.run_command("/code 9")
        await pilot.pause()
        assert "NO SUCH BLOCK" in messages(offline_app)[-1].message.content


async def test_ctrl_y_without_code_says_so(offline_app: VoxApp) -> None:
    async with offline_app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert "NO CODE BLOCK" in messages(offline_app)[-1].message.content


async def test_panel_command_switches_modes(offline_app: VoxApp) -> None:
    async with offline_app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        offline_app.run_command("/panel index")
        await pilot.pause()
        title = str(offline_app.query_one("#side-title").render())
        assert "INDEX" in title
        assert not offline_app.query_one("#side-panel").has_class("code")

        offline_app.run_command("/panel code")
        await pilot.pause()
        assert "CODE" in str(offline_app.query_one("#side-title").render())
