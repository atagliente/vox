"""The VOX Textual application.

The UI thread never performs blocking HTTP: generation runs in a worker
thread and posts updates back through ``call_from_thread``.
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Static

from . import __version__, clipboard, code_blocks, commands
from .agent import AgentEvent, run_turn
from .config import (
    ConfigError,
    LoadedConfig,
    active_provider,
    load_config,
    open_in_editor,
    redacted_copy,
    reload_from_disk,
    save_global_config,
    validate_config,
)
from .llm_client import LLMClient, LLMError
from .logging_setup import get_logger, setup_logging
from .mesh import MeshController, MeshError, MeshSettings
from .models import Message, Session
from .prompts import PromptStore, find_variables, render
from .history import InputHistory
from .inspection import (
    Alternative,
    InspectionRun,
    criteria_from_config,
)
from .roles import RoleStore
from .sessions import SessionStore
from .storage import global_config_path
from . import report as reporting
from .tools import Workspace
from .usage import LiveMeter, UsageTracker
from .ui import branding
from .ui.inspect_screen import InspectScreen
from .ui.universe_screen import UniverseScreen
from .ui.modals import (
    ConfirmModal,
    PickerItem,
    PickerModal,
    SettingsModal,
    TextPromptModal,
)
from .ui.widgets import (
    HeaderBar,
    KeyBar,
    MessageBox,
    PromptArea,
    StatusBar,
    ThinkingBox,
    Transcript,
    spinner_frame,
)

log = get_logger("app")


class VoxApp(App[None]):
    """Terminal chat client for OpenAI-compatible providers."""

    TITLE = f"VOX {__version__}"
    AUTO_FOCUS = "#input"

    BINDINGS = [
        Binding("ctrl+enter", "send", "Send", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+s", "open_settings", "Settings", priority=True),
        Binding("ctrl+w", "save_session", "Save session", priority=True),
        Binding("ctrl+p", "open_prompts", "Prompts", priority=True),
        Binding("ctrl+r", "open_roles", "Roles", priority=True),
        Binding("ctrl+comma", "open_settings", "Settings", show=False, priority=True),
        Binding("ctrl+g", "stop", "Stop", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True),
        Binding("ctrl+y", "copy_code", "Copy code", priority=True),
        # Not ctrl+i: terminals send the same byte for it as for tab, so
        # Textual reports it as "tab" and the binding never fires.
        Binding("ctrl+t", "open_inspect", "Inspect", priority=True),
        Binding("f2", "open_inspect", "Inspect", show=False, priority=True),
        Binding("ctrl+e", "export_report", "Export", priority=True),
        # Function keys only. ctrl+shift+<letter> was not delivered here, and
        # neither was ctrl+o; a function key travels through every terminal.
        Binding("f3", "toggle_mesh", "Online", priority=True),
        Binding("f4", "open_universe", "Universe", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+c", "copy_selection", "Copy", priority=True),
        Binding("ctrl+v", "paste_clipboard", "Paste", priority=True),
        Binding("ctrl+shift+c", "copy_selection", "Copy", show=False, priority=True),
        Binding("ctrl+shift+v", "paste_clipboard", "Paste", show=False, priority=True),
    ]

    def __init__(
        self,
        loaded: LoadedConfig | None = None,
        workspace: Path | None = None,
        show_splash: bool = True,
    ) -> None:
        self.workspace_path = Path(workspace or Path.cwd()).expanduser().resolve()
        self.loaded = loaded if loaded is not None else load_config(self.workspace_path)
        self.config: dict[str, Any] = self.loaded.data
        theme = str(self.config.get("ui", {}).get("theme", "nasa"))
        type(self).CSS = branding.theme_css(theme)
        super().__init__()

        self.show_splash = show_splash and bool(self.config.get("ui", {}).get("splash", True))
        self.role_store = RoleStore()
        self.prompt_store = PromptStore()
        # Sessions and reports belong to the work, so they follow the
        # directory VOX was started in rather than the home.
        self.session_store = SessionStore(self.workspace_path)
        self.history = InputHistory()
        self.workspace = Workspace(self.workspace_path)

        self.client: LLMClient | None = None
        self.connected = False
        self.generating = False
        self.dirty = False
        self.session_name: str | None = None
        self.cancel_event = threading.Event()
        self.usage = UsageTracker()
        self.live_meter: LiveMeter | None = None
        self._usage_timer = None
        self._shutting_down = False
        self._preloading: str | None = None
        self._thinking: ThinkingBox | None = None
        self._reasoning_box: MessageBox | None = None
        self._spinner_tick = 0
        self.session = self._new_session()
        self._assistant_box: MessageBox | None = None
        self.code_blocks: list[code_blocks.CodeBlock] = []
        self.mesh = MeshController(MeshSettings.from_config(self.config))
        self._universe_screen: UniverseScreen | None = None
        self.inspection = self._new_inspection()
        self._inspect_screen: InspectScreen | None = None
        self._panel_mode = "code"

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="body"):
            yield Transcript(
                show_timestamps=bool(self.config.get("ui", {}).get("show_timestamps", True))
            )
            with VerticalScroll(id="side-panel"):
                yield Static("", id="side-title")
                yield Static("", id="side-content")
        with Vertical(id="input-area"):
            yield PromptArea(id="input")
        yield StatusBar()
        yield KeyBar()

    def on_mount(self) -> None:
        setup_logging()
        self.rebuild_client()
        self.refresh_header()
        self.refresh_status()
        if self.show_splash:
            self.write_system(
                branding.splash(
                    model=str(self.config.get("active_model", "")),
                    role=str(self.config.get("active_role", "")),
                    workspace=str(self.workspace_path),
                )
            )
        for warning in self.loaded.warnings:
            self.write_error(f"CONFIG: {warning}")
        for warning in [
            *self.role_store.warnings,
            *self.prompt_store.warnings,
            *self.history.warnings,
        ]:
            self.write_error(f"STORE: {warning}")
        self.check_connection()

    # ------------------------------------------------------------ transcript

    @property
    def transcript(self) -> Transcript:
        return self.query_one(Transcript)

    def write_message(self, message: Message) -> MessageBox:
        return self.transcript.add(message)

    def write_system(self, text: str) -> None:
        self.write_message(Message(role="system", content=text))

    def write_error(self, text: str) -> None:
        self.write_message(Message(role="error", content=text))

    # -------------------------------------------------------------- provider

    def provider_block(self) -> dict[str, Any] | None:
        try:
            return active_provider(self.config)
        except ConfigError as exc:
            self.write_error(str(exc))
            return None

    def link_label(self) -> str:
        provider = self.provider_block()
        base = str(provider.get("base_url", "?")) if provider else "?"
        return f"{'ONLINE' if self.connected else 'OFFLINE'}  {base}"

    def rebuild_client(self) -> None:
        """Recreate the client after a provider change. The only build site."""
        provider = self.provider_block()
        if provider is None:
            self.client = None
            return
        try:
            self.client = LLMClient.from_provider(provider)
        except (ValueError, TypeError, OSError) as exc:
            self.client = None
            self.write_error(f"cannot create client: {exc}")

    def refresh_header(self) -> None:
        try:
            header = self.query_one(HeaderBar)
        except NoMatches:  # the screen is already being torn down
            return
        header.update_state(
            link=self.link_label(),
            mesh=branding.ON_MESH if self.mesh.online else branding.OFF_MESH,
            provider=str(self.config.get("active_provider", "")),
            model=str(self.config.get("active_model", "")),
            role=str(self.config.get("active_role", "")),
            style=str(self.config.get("ui", {}).get("logo", "frame")),
        )

    def context_window(self) -> int:
        return int(self.config.get("generation", {}).get("context_window", 8192))

    def usage_summary(self) -> str:
        """Context, token and throughput figures for the status bar."""
        if not self.config.get("ui", {}).get("show_usage", True):
            return ""
        return self.usage.status_line(self.context_window(), self.live_meter)

    def refresh_status(self, extra: str = "") -> None:
        agent = bool(self.config.get("agent", {}).get("enabled", False))
        detail = (
            f"{self.config.get('active_provider', '')}/"
            f"{self.config.get('active_model', '')} "
            f"[{self.config.get('active_role', '')}]"
        )
        mesh = self.mesh.status_line()
        if mesh:
            extra = f"{extra}  ·  {mesh}" if extra else mesh
        usage = self.usage_summary()
        if usage:
            extra = f"{extra}  ·  {usage}" if extra else usage
        try:
            status = self.query_one(StatusBar)
        except NoMatches:  # a timer can outlive the widgets for one tick
            return
        status.update_state(
            connected=self.connected,
            generating=self.generating,
            agent=agent,
            workspace=str(self.workspace_path),
            extra=f"{detail}  {extra}".strip(),
            spinner=(
                spinner_frame(self._spinner_tick)
                if self.generating or self._preloading
                else ""
            ),
            busy="" if self._preloading is None else f"PRELOADING {self._preloading}",
        )

    @work(thread=True, group="connection")
    def check_connection(self) -> None:
        """Probe /v1/models in the background; never blocks the UI."""
        if self.client is None:
            return
        ok, detail = self.client.ping()
        self.call_from_thread(self._connection_result, ok, detail)

    def _connection_result(self, ok: bool, detail: str) -> None:
        self.connected = ok
        self.refresh_header()
        self.refresh_status()
        if ok:
            self.write_system(f"LINK ESTABLISHED - {detail}")
            if self.config.get("generation", {}).get("preload", True):
                self.preload_model()
        else:
            self.write_error(f"LINK DOWN - {detail}".replace(": http", " at http"))

    @work(thread=True, group="warm")
    def preload_model(self) -> None:
        """Load the model on the server before the first message needs it.

        A cold model can take minutes, so this shows a live spinner and gives
        up after ``generation.preload_timeout_seconds`` rather than sitting on
        the provider timeout with nothing on screen.
        """
        if self.client is None:
            return
        model = str(self.config.get("active_model", ""))
        if not model:
            return
        generation = self.config.get("generation", {})
        timeout = float(generation.get("preload_timeout_seconds", 180))
        self.call_from_thread(self.begin_preload, model, self.client.base_url)
        try:
            elapsed = self.client.warm(model, timeout=timeout)
        except LLMError as exc:
            self.call_from_thread(self.end_preload, model, None, exc.message)
            return
        self.call_from_thread(self.end_preload, model, elapsed, None)

    def begin_preload(self, model: str, endpoint: str = "") -> None:
        self._preloading = model
        # Naming the endpoint makes a config pointing at the wrong server
        # obvious, instead of looking like the model is slow.
        self.show_thinking(f"PRELOADING {model} ON {endpoint}" if endpoint
                           else f"PRELOADING {model}")
        if self._usage_timer is None:
            self._usage_timer = self.set_interval(0.1, self._tick_status)
        self.refresh_status()

    def end_preload(self, model: str, elapsed: float | None,
                    error: str | None) -> None:
        if self._preloading is None:
            return  # the operator stopped waiting for it
        self._preloading = None
        self.clear_thinking()
        if not self.generating:
            self._stop_status_timer()
        if error is not None:
            endpoint = self.client.base_url if self.client is not None else "?"
            self.write_error(
                f"PRELOAD FAILED - {error} ({model} on {endpoint}). "
                "The model will load on your first message instead; check "
                "/settings if that endpoint is not the one you meant."
            )
        else:
            self.write_system(
                f"MODEL RESIDENT - {model} ready in {elapsed:.1f}s"
            )
        self.refresh_status()

    def cancel_preload(self) -> bool:
        """Stop waiting on a preload. The request itself cannot be recalled."""
        if self._preloading is None:
            return False
        model, self._preloading = self._preloading, None
        self.clear_thinking()
        if not self.generating:
            self._stop_status_timer()
        self.write_system(
            f"STOPPED WAITING FOR {model} - it may still be loading on the server"
        )
        self.refresh_status()
        return True

    def cmd_warm(self, argument: str) -> None:
        self.preload_model()

    # ----------------------------------------------------------------- input

    @property
    def input_area(self) -> PromptArea:
        return self.query_one("#input", PromptArea)

    def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        """Enter in the input box sends the message."""
        event.stop()
        self.action_send()

    def on_prompt_area_clipboard_paste(self, event: PromptArea.ClipboardPaste) -> None:
        """Ctrl+V and Ctrl+Shift+V in the input box."""
        event.stop()
        self.action_paste_clipboard()

    def focused_text_widget(self) -> Any:
        """The widget a copy or paste should act on, modal screens included."""
        focused = self.focused
        if hasattr(focused, "selected_text") or hasattr(focused, "insert"):
            return focused
        try:
            return self.input_area
        except NoMatches:
            return None

    def action_copy_selection(self) -> None:
        """Copy the selection, or the latest answer when there is none."""
        widget = self.focused_text_widget()
        text = getattr(widget, "selected_text", "") or ""
        source = "selection"
        on_main_screen = self.screen is self.screen_stack[0]
        if not text and not on_main_screen:
            # Inside a modal there is no transcript to fall back on; stay quiet
            # rather than writing an error behind the dialog.
            return
        if not text:
            answer = next(
                (
                    message.content
                    for message in reversed(self.session.messages)
                    if message.role == "assistant" and message.content
                ),
                "",
            )
            text, source = answer, "last answer"
        if not text:
            self.write_error("NOTHING TO COPY")
            return
        self.copy_to_system(text, source)

    def action_paste_clipboard(self) -> None:
        self.paste_from_system()

    def insert_into_focused(self, text: str) -> None:
        """Insert pasted text where the cursor actually is."""
        widget = self.focused_text_widget()
        inserter = getattr(widget, "insert", None)
        if callable(inserter):
            inserter(text)
            return
        self.write_error("NOWHERE TO PASTE")

    @work(thread=True, group="clipboard")
    def copy_to_system(self, text: str, source: str) -> None:
        """Reaching the real clipboard means shelling out; do it off the UI."""
        ok, detail = clipboard.copy(text)
        if ok:
            self.call_from_thread(
                self.write_system,
                f"COPIED {source.upper()} - {len(text)} characters ({detail})",
            )
        else:
            self.call_from_thread(self.write_error, f"COPY FAILED - {detail}")

    @work(thread=True, group="clipboard")
    def paste_from_system(self) -> None:
        text, detail = clipboard.paste()
        if text is None:
            self.call_from_thread(self.write_error, f"PASTE FAILED - {detail}")
            return
        if not text:
            self.call_from_thread(self.write_system, "CLIPBOARD IS EMPTY")
            return
        self.call_from_thread(self.insert_into_focused, text)

    def on_prompt_area_recall(self, event: PromptArea.Recall) -> None:
        """Up and down at the edges of the input walk the history."""
        event.stop()
        area = self.input_area
        if event.direction < 0:
            recalled = self.history.previous(area.text)
        else:
            recalled = self.history.next()
        if recalled is None:
            return
        self.set_input(recalled)

    def set_input(self, text: str) -> None:
        area = self.input_area
        area.clear()
        area.insert(text)
        area.focus()

    def action_send(self) -> None:
        text = self.input_area.text.strip()
        if not text:
            return
        if commands.is_command(text):
            self.input_area.clear()
            self.history.add(text)
            self.run_command(text)
            return
        if self.generating:
            self.write_error("ALREADY GENERATING - CTRL+G TO STOP")
            return
        if text.startswith("//"):
            text = text[1:]
        self.input_area.clear()
        self.history.add(text)
        message = Message(role="user", content=text)
        self.session.messages.append(message)
        self.write_message(message)
        self.dirty = True
        self.start_generation()

    # ------------------------------------------------------------ generation

    def build_request_messages(self) -> list[Message]:
        """Prepend the active role system prompt to the conversation."""
        role = self.role_store.get(str(self.config.get("active_role", "")))
        messages: list[Message] = []
        if role is not None and role.system_prompt:
            messages.append(Message(role="system", content=role.system_prompt))
        messages.extend(m for m in self.session.messages if m.role != "error")
        return messages

    def start_generation(self) -> None:
        if self.client is None:
            self.write_error("NO PROVIDER CONFIGURED")
            return
        role = self.role_store.get(str(self.config.get("active_role", "")))
        generation = self.config.get("generation", {})
        temperature = float(
            role.temperature if role is not None else generation.get("temperature", 0.2)
        )
        self.cancel_event = threading.Event()
        self.inspection = self._new_inspection()
        if self._inspect_screen is not None:
            self._inspect_screen.run = self.inspection
            self._inspect_screen.refresh_view()
        self.generating = True
        self._assistant_box = None
        self.live_meter = LiveMeter()
        self.show_thinking("WAITING FOR MODEL")
        self._usage_timer = self.set_interval(0.1, self._tick_status)
        self.refresh_status()
        self.generate(
            temperature=temperature,
            max_tokens=generation.get("max_tokens"),
            model=str(self.config.get("active_model", "")),
        )

    @work(thread=True, group="generation", exclusive=True)
    def generate(self, temperature: float, max_tokens: int | None, model: str) -> None:
        assert self.client is not None
        agent_config = dict(self.config.get("agent", {}))
        agent_enabled = bool(agent_config.get("enabled", False))
        try:
            for event in run_turn(
                client=self.client,
                messages=self.build_request_messages(),
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                workspace=self.workspace,
                agent_config=agent_config,
                confirm=self._confirm_from_thread,
                cancel=self.cancel_event,
                agent_enabled=agent_enabled,
                include_usage=bool(
                    self.config.get("generation", {}).get("include_usage", True)
                ),
                top_logprobs=self.inspect_top_k(),
            ):
                self.call_from_thread(self.handle_agent_event, event)
        except LLMError as exc:
            if self._shutting_down:
                return
            log.warning("generation failed: %s", exc.kind)
            self.call_from_thread(self.finish_generation, f"{exc.message}")
        else:
            self.call_from_thread(self.finish_generation, None)

    def _confirm_from_thread(self, name: str, description: str) -> bool:
        """Blocking confirmation request from the worker thread."""
        result = self.call_from_thread(
            self.push_screen_wait,
            ConfirmModal(f"AUTHORIZE {name.upper()}?", description),
        )
        return bool(result)

    def handle_agent_event(self, event: AgentEvent) -> None:
        if event.type == "text":
            self.clear_thinking()
            if self.live_meter is not None:
                self.live_meter.add_text(event.text)
            if self._assistant_box is None:
                self._assistant_box = self.write_message(
                    Message(role="assistant", content="")
                )
            self._assistant_box.append_text(event.text)
            self.transcript.scroll_end(animate=False)
            # Re-parsing on every token would be wasteful; a fence or a line
            # break is the only thing that can change the blocks.
            if any(marker in event.text for marker in ("`", "~", "\n")):
                self.refresh_code_blocks(self._assistant_box.message.content)
        elif event.type == "token":
            self.record_token(event.token, event.phase)
        elif event.type == "reasoning":
            self.show_reasoning(event.text)
        elif event.type == "tool_start" and event.tool_call is not None:
            self.show_thinking(f"RUNNING {event.tool_call.name}")
            self.write_message(
                Message(
                    role="tool",
                    name=event.tool_call.name,
                    content=f"REQUESTED {event.tool_call.name}",
                )
            )
        elif event.type in ("tool_result", "tool_denied") and event.tool_call is not None:
            self.show_thinking("WAITING FOR MODEL")
            self.write_message(
                Message(
                    role="tool" if event.type == "tool_result" else "error",
                    name=event.tool_call.name,
                    content=event.text,
                )
            )
        elif event.type in ("notice", "limit"):
            self.write_system(event.text)
            if event.type == "notice":
                self.config.setdefault("agent", {})["enabled"] = False
                self.refresh_status()
        elif event.type == "usage" and event.usage is not None:
            self.usage.add(event.usage)
            self.refresh_status()
        elif event.type == "cancelled":
            self.write_system("GENERATION STOPPED BY OPERATOR")
            self._commit(event.messages)
        elif event.type == "assistant_done":
            self._commit(event.messages)

    def show_reasoning(self, chunk: str) -> None:
        """Stream the model's thinking into its own block above the answer."""
        if not self.config.get("ui", {}).get("show_reasoning", True):
            return
        self.clear_thinking()
        if self._reasoning_box is None:
            self._reasoning_box = self.write_message(
                Message(role="reasoning", content="")
            )
        self._reasoning_box.append_text(chunk)
        self.transcript.scroll_end(animate=False)

    # --------------------------------------------------------------- inspect

    def inspect_config(self) -> dict[str, Any]:
        section = self.config.get("inspect", {})
        return section if isinstance(section, dict) else {}

    @property
    def inspect_enabled(self) -> bool:
        return bool(self.inspect_config().get("enabled", False))

    def inspect_top_k(self) -> int | None:
        """The top-k to ask the provider for, or None when inspection is off."""
        if not self.inspect_enabled:
            return None
        return int(self.inspect_config().get("top_k", 5))

    def _new_inspection(self) -> InspectionRun:
        return InspectionRun(
            model=str(self.config.get("active_model", "")),
            provider=str(self.config.get("active_provider", "")),
            top_k=int(self.inspect_config().get("top_k", 5)),
            criteria=criteria_from_config(self.config),
        )

    def record_token(self, sample: Any, phase: str) -> None:
        """Add one reported token to the current run and refresh the view."""
        if sample is None:
            return
        alternatives = [
            Alternative(token, logprob) for token, logprob in sample.alternatives
        ]
        self.inspection.add(
            sample.text,
            sample.logprob,
            alternatives,
            phase if phase in ("thinking", "answer") else "unattributed",
        )
        if self._inspect_screen is not None:
            self._inspect_screen.refresh_view()

    def action_open_inspect(self) -> None:
        """Toggle the view, turning collection on if it was off.

        Asking to inspect is asking to measure: opening the view used to
        show nothing but an instruction to type /inspect on, which is a
        step the key itself can take.
        """
        if isinstance(self.screen, InspectScreen):
            self.screen.dismiss(None)
            return
        just_enabled = False
        if not self.inspect_enabled:
            self.config.setdefault("inspect", {})["enabled"] = True
            self.persist_config()
            if not len(self.inspection):
                # Pick up the settings just written, without discarding
                # anything already measured.
                self.inspection = self._new_inspection()
            self.write_system(
                "INSPECTION ON - the next answer is measured. "
                "/inspect off stops it."
            )
            self.refresh_status()
            just_enabled = True
        screen = InspectScreen(
            self.inspection, enabled=True, just_enabled=just_enabled
        )
        self._inspect_screen = screen
        self.push_screen(screen, lambda _result: self._forget_inspect_screen())

    def _forget_inspect_screen(self) -> None:
        self._inspect_screen = None

    def cmd_inspect(self, argument: str) -> None:
        value = argument.strip().lower()
        if not value:
            self.action_open_inspect()
            return
        if value not in ("on", "off"):
            self.write_error("USAGE: /inspect [on|off]")
            return
        self.config.setdefault("inspect", {})["enabled"] = value == "on"
        self.persist_config()
        if value == "on":
            top_k = self.inspect_config().get("top_k", 5)
            self.write_system(
                f"INSPECTION ON - the next answer is measured with top-k {top_k}. "
                "Ctrl+T shows the table."
            )
        else:
            self.write_system("INSPECTION OFF - requests carry no logprobs again")
        self.refresh_status()

    def refresh_code_blocks(self, answer: str | None = None) -> None:
        """Update the code panel from ``answer``, or from the latest one.

        Called while the answer is still streaming as well as when it lands,
        so a long reply does not leave the previous answer's code on screen.
        """
        if answer is None:
            answer = next(
                (
                    message.content
                    for message in reversed(self.session.messages)
                    if message.role == "assistant" and message.content
                ),
                "",
            )
        blocks = code_blocks.extract(answer)
        if blocks == self.code_blocks:
            return
        self.code_blocks = blocks
        if blocks and self.config.get("ui", {}).get("code_panel", True):
            self._panel_mode = "code"
            self.query_one("#side-panel").set_class(True, "visible")
        if self.query_one("#side-panel").has_class("visible"):
            self.refresh_panel()

    def action_copy_code(self) -> None:
        """Ctrl+Y: copy the most recent code block."""
        self.copy_code_block(len(self.code_blocks))

    def copy_code_block(self, number: int) -> None:
        if not self.code_blocks:
            self.write_error("NO CODE BLOCK IN THE LAST ANSWER")
            return
        if not 1 <= number <= len(self.code_blocks):
            self.write_error(
                f"NO SUCH BLOCK: {number} (1-{len(self.code_blocks)} available)"
            )
            return
        block = self.code_blocks[number - 1]
        self.copy_to_system(block.code, f"code block {number}")

    def _commit(self, messages: list[Message]) -> None:
        """Append the messages produced by a turn to the session."""
        for message in messages:
            self.session.messages.append(message)
        if messages:
            self.dirty = True
        self._assistant_box = None
        self._reasoning_box = None
        self.refresh_code_blocks()

    def note_logprob_refusal(self) -> None:
        """Say once that the provider would not measure, and keep chatting."""
        if self.client is None or not self.client.logprobs_refused:
            return
        if self.inspection.note:
            return
        self.inspection.note = (
            "the provider rejected logprobs, so this answer was not measured"
        )
        self.write_error(
            "INSPECTION UNAVAILABLE - this provider rejects logprobs. "
            "The answer is unaffected."
        )

    def finish_generation(self, error: str | None) -> None:
        self.note_logprob_refusal()
        self.generating = False
        self._assistant_box = None
        self.live_meter = None
        self._reasoning_box = None
        self.clear_thinking()
        self._stop_status_timer()
        if error:
            self.write_error(error)
            self.connected = False
            self.refresh_header()
        self.refresh_status()

    def show_thinking(self, label: str) -> None:
        """Mount, or relabel, the spinner that says the model is working."""
        if self._thinking is None:
            self._thinking = ThinkingBox(label)
            self.transcript.mount(self._thinking)
            self.transcript.scroll_end(animate=False)
        else:
            self._thinking.set_label(label)

    def clear_thinking(self) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None

    def _tick_status(self) -> None:
        if not self.is_running:
            self._stop_status_timer()
            return
        self._spinner_tick += 1
        self.refresh_status()

    def _stop_status_timer(self) -> None:
        if self._usage_timer is not None:
            self._usage_timer.stop()
            self._usage_timer = None

    def on_unmount(self) -> None:
        self.shutdown()

    def action_stop(self) -> None:
        if self.cancel_preload():
            return
        if not self.generating or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        if self.client is not None:
            self.client.cancel_active_stream()
        self.show_thinking("STOPPING GENERATION")
        self.write_system("STOP REQUESTED…")

    # -------------------------------------------------------------- sessions

    def _new_session(self) -> Session:
        return Session(
            title="untitled",
            provider=str(self.config.get("active_provider", "")),
            model=str(self.config.get("active_model", "")),
            role=str(self.config.get("active_role", "")),
            workspace=str(self.workspace_path),
            agent_enabled=bool(self.config.get("agent", {}).get("enabled", False)),
        )

    async def _flow_new_session(self) -> None:
        if self.dirty and not await self._confirm_discard("START A NEW SESSION?"):
            return
        self.session = self._new_session()
        self.session_name = None
        self.dirty = False
        self.usage = UsageTracker()
        self.transcript.clear_messages()
        self.write_system("NEW SESSION INITIALISED")
        self.refresh_status()

    async def _flow_save_session(self) -> None:
        name = self.session_name
        if not name:
            name = await self.push_screen_wait(
                TextPromptModal("SAVE SESSION AS", placeholder="name", value=self.session.title)
            )
        if not name:
            return
        self.save_session(name)

    def save_session(self, name: str) -> None:
        self.session.title = name
        self.session.provider = str(self.config.get("active_provider", ""))
        self.session.model = str(self.config.get("active_model", ""))
        self.session.role = str(self.config.get("active_role", ""))
        self.session.workspace = str(self.workspace_path)
        self.session.agent_enabled = bool(self.config.get("agent", {}).get("enabled", False))
        try:
            path = self.session_store.save(self.session, name)
        except OSError as exc:
            self.write_error(f"cannot save session: {exc}")
            return
        self.session_name = path.stem
        self.dirty = False
        self.write_system(f"SESSION SAVED - {path.name}")
        self.refresh_status()

    def load_session(self, name: str) -> None:
        try:
            session = self.session_store.load(name)
        except (FileNotFoundError, ValueError) as exc:
            self.write_error(str(exc))
            return
        self.session = session
        self.session_name = name
        self.dirty = False
        self.transcript.clear_messages()
        self.write_system(f"SESSION LOADED - {session.title} ({len(session.messages)} messages)")
        for message in session.messages:
            if message.reasoning and self.config.get("ui", {}).get(
                "show_reasoning", True
            ):
                self.write_message(
                    Message(role="reasoning", content=message.reasoning,
                            timestamp=message.timestamp)
                )
            self.write_message(message)
        if session.workspace:
            self.set_workspace(session.workspace, announce=False)
        self.refresh_status()

    async def _flow_sessions(self) -> None:
        entries = self.session_store.list()
        items = [
            PickerItem(
                entry.name,
                entry.title,
                entry.error or f"{entry.updated_at}  {entry.message_count} msg  {entry.model}",
            )
            for entry in entries
        ]
        choice = await self.push_screen_wait(
            PickerModal(
                "SESSIONS",
                items,
                f"no sessions in {self.workspace_path}\n"
                "they are saved here, next to the work",
            )
        )
        if choice:
            self.load_session(choice)

    # ----------------------------------------------------------------- roles

    async def _flow_roles(self) -> None:
        items = [
            PickerItem(role.name, role.name, role.description)
            for role in self.role_store.all()
        ]
        choice = await self.push_screen_wait(PickerModal("ROLES", items, "no roles"))
        if choice:
            self.set_role(choice)

    def set_role(self, name: str) -> None:
        role = self.role_store.get(name)
        if role is None:
            self.write_error(f"unknown role: {name}")
            return
        self.config["active_role"] = name
        self.persist_config()
        self.write_system(f"ROLE SET - {name}")
        self.refresh_status()

    # --------------------------------------------------------------- prompts

    async def _flow_prompts(self) -> None:
        items = [
            PickerItem(
                prompt.name,
                prompt.name,
                f"{prompt.description}  [{', '.join(prompt.tags)}]" if prompt.tags
                else prompt.description,
            )
            for prompt in self.prompt_store.all()
        ]
        choice = await self.push_screen_wait(
            PickerModal("SAVED PROMPTS", items, "no saved prompts")
        )
        if choice:
            self.load_prompt(choice)

    def load_prompt(self, name: str) -> None:
        """Load a prompt into the editor without sending it."""
        prompt = self.prompt_store.get(name)
        if prompt is None:
            self.write_error(f"unknown prompt: {name}")
            return
        values = {
            "workspace": str(self.workspace_path),
            "file": "",
            "selection": self.input_area.selected_text or "",
        }
        text = render(prompt.content, values, strict=False)
        missing = find_variables(text)
        self.set_input(text)
        if missing:
            self.write_system(
                "PROMPT LOADED - UNRESOLVED VARIABLES: " + ", ".join(missing)
            )
        else:
            self.write_system(f"PROMPT LOADED - {prompt.name}")

    # -------------------------------------------------------------- settings

    async def _flow_settings(self) -> None:
        result = await self.push_screen_wait(
            SettingsModal(self.config, redacted_copy(self.config), validate_config)
        )
        if result is None:
            return
        self.apply_config(result)
        self.write_system("SETTINGS APPLIED")

    def apply_config(self, data: dict[str, Any]) -> None:
        """Adopt a validated configuration and refresh everything it affects."""
        self.config = data
        self.loaded.data = data
        try:
            save_global_config(data)
        except ConfigError as exc:
            self.write_error(f"settings not saved: {exc}")
        type(self).CSS = branding.theme_css(str(data.get("ui", {}).get("theme", "nasa")))
        self.rebuild_client()
        self.connected = False
        self.refresh_header()
        self.refresh_status()
        self.check_connection()

    def persist_config(self) -> None:
        try:
            save_global_config(self.config)
        except ConfigError as exc:
            self.write_error(f"config not saved: {exc}")

    def edit_config_file(self) -> None:
        """Open config.json in the user editor, then validate and reload."""
        path = global_config_path()
        with self.suspend():
            ok, detail = open_in_editor(path)
        if not ok:
            self.write_error(detail)
            return
        data, error = reload_from_disk(self.config, self.workspace_path)
        if error is not None:
            self.write_error(f"CONFIG NOT RELOADED - {error}")
            return
        self.config = data
        self.loaded.data = data
        self.rebuild_client()
        self.connected = False
        self.refresh_header()
        self.refresh_status()
        self.write_system("CONFIG RELOADED")
        self.check_connection()

    # ------------------------------------------------------------- workspace

    def set_workspace(self, path: str, announce: bool = True) -> None:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (self.workspace_path / candidate).resolve()
        if not candidate.is_dir():
            self.write_error(f"not a directory: {candidate}")
            return
        self.workspace_path = candidate.resolve()
        self.workspace = Workspace(self.workspace_path)
        self.session_store = SessionStore(self.workspace_path)
        if announce:
            self.write_system(f"WORKSPACE SET - {self.workspace_path}")
        self.refresh_status()

    # ----------------------------------------------------------- side panel

    def action_toggle_panel(self) -> None:
        panel = self.query_one("#side-panel")
        panel.set_class(not panel.has_class("visible"), "visible")
        if panel.has_class("visible"):
            self.refresh_panel()

    def refresh_panel(self) -> None:
        panel = self.query_one("#side-panel")
        panel.set_class(self._panel_mode == "code", "code")
        if self._panel_mode == "code":
            self.query_one("#side-title", Static).update("VOX · CODE")
            self.query_one("#side-content", Static).update(
                code_blocks.render_panel(self.code_blocks)
            )
            return
        sessions = self.session_store.list()
        lines = ["SESSIONS"]
        lines.extend(f"  {entry.name}  ({entry.message_count})" for entry in sessions[:10])
        lines.append("")
        lines.append("PROMPTS")
        lines.extend(f"  {prompt.name}" for prompt in self.prompt_store.all()[:10])
        lines.append("")
        lines.append("ROLES")
        lines.extend(
            f"  {'*' if role.name == self.config.get('active_role') else ' '}{role.name}"
            for role in self.role_store.all()
        )
        self.query_one("#side-title", Static).update("VOX · INDEX")
        self.query_one("#side-content", Static).update("\n".join(lines))

    # -------------------------------------------------------------- commands

    def run_command(self, text: str) -> None:
        parsed = commands.parse(text)
        if not parsed.is_command:
            return
        if parsed.error is not None:
            hint = f" DID YOU MEAN: {', '.join(parsed.suggestions[:5])}" if parsed.suggestions else ""
            self.write_error(parsed.error.upper() + hint)
            return
        handler = getattr(self, f"cmd_{parsed.name.replace('-', '_')}", None)
        if handler is None:
            self.write_error(f"command not wired: /{parsed.name}")
            return
        result = handler(parsed.argument)
        if inspect.iscoroutine(result):
            self.run_worker(result, exclusive=False)

    def cmd_help(self, argument: str) -> None:
        self.write_system(commands.help_text())

    def cmd_new(self, argument: str) -> None:
        self.action_new_session()

    def cmd_clear(self, argument: str) -> None:
        self.transcript.clear_messages()
        self.session.messages.clear()
        self.dirty = False
        self.write_system("TRANSCRIPT CLEARED")

    def cmd_settings(self, argument: str) -> None:
        self.action_open_settings()

    def cmd_config(self, argument: str) -> None:
        self.edit_config_file()

    def cmd_provider(self, argument: str) -> Any:
        providers = list(self.config.get("providers", {}))
        if not argument:
            return self._pick_provider(providers)
        if argument not in providers:
            self.write_error(f"UNKNOWN PROVIDER: {argument} - HAVE: {', '.join(providers)}")
            return None
        self._set_provider(argument)
        return None

    async def _pick_provider(self, providers: list[str]) -> None:
        items = [
            PickerItem(name, name, str(self.config["providers"][name].get("base_url", "")))
            for name in providers
        ]
        choice = await self.push_screen_wait(PickerModal("PROVIDERS", items))
        if choice:
            self._set_provider(choice)

    def _set_provider(self, name: str) -> None:
        self.config["active_provider"] = name
        models = self.config.get("providers", {}).get(name, {}).get("models", [])
        if models and self.config.get("active_model") not in models:
            self.config["active_model"] = models[0]
        self.persist_config()
        self.rebuild_client()
        self.connected = False
        self.refresh_header()
        self.refresh_status()
        self.write_system(f"PROVIDER SET - {name}")
        self.check_connection()

    def cmd_model(self, argument: str) -> Any:
        if argument:
            self.config["active_model"] = argument
            self.persist_config()
            self.write_system(f"MODEL SET - {argument}")
            self.refresh_status()
            if self.connected and self.config.get("generation", {}).get("preload", True):
                self.preload_model()
            return None
        return self._pick_model()

    async def _pick_model(self) -> None:
        provider = self.provider_block() or {}
        listed = list(provider.get("models", []))
        if self.client is not None and self.connected:
            try:
                listed = sorted(set(listed) | set(self.client.list_models(timeout=5)))
            except LLMError as exc:
                self.write_error(f"cannot list models: {exc.message}")
        items = [PickerItem(name, name) for name in listed]
        choice = await self.push_screen_wait(PickerModal("MODELS", items, "no models listed"))
        if choice:
            self.config["active_model"] = choice
            self.persist_config()
            self.write_system(f"MODEL SET - {choice}")
            self.refresh_status()
            if self.connected and self.config.get("generation", {}).get("preload", True):
                self.preload_model()

    def cmd_role(self, argument: str) -> Any:
        if argument:
            self.set_role(argument)
            return None
        self.action_open_roles()
        return None

    def cmd_roles(self, argument: str) -> None:
        self.action_open_roles()

    def cmd_prompts(self, argument: str) -> None:
        self.action_open_prompts()

    def cmd_prompt(self, argument: str) -> None:
        if not argument:
            self.write_error("USAGE: /prompt <name>")
            return
        self.load_prompt(argument)

    def cmd_prompt_save(self, argument: str) -> Any:
        content = self.input_area.text.strip()
        if not content:
            self.write_error("NOTHING TO SAVE - THE INPUT IS EMPTY")
            return None
        if argument:
            self.prompt_store.save_prompt(argument, content)
            self.write_system(f"PROMPT SAVED - {argument}")
            return None
        return self._ask_prompt_name(content)

    async def _ask_prompt_name(self, content: str) -> None:
        name = await self.push_screen_wait(TextPromptModal("SAVE PROMPT AS", "name"))
        if name:
            self.prompt_store.save_prompt(name, content)
            self.write_system(f"PROMPT SAVED - {name}")

    def cmd_prompt_delete(self, argument: str) -> Any:
        if not argument:
            self.write_error("USAGE: /prompt-delete <name>")
            return None
        return self._delete_prompt(argument)

    async def _delete_prompt(self, name: str) -> None:
        if self.prompt_store.get(name) is None:
            self.write_error(f"UNKNOWN PROMPT: {name}")
            return
        if not await self.push_screen_wait(
            ConfirmModal(
                "DELETE PROMPT?",
                f"{name} will be removed permanently.",
                authorize_label="DELETE",
                deny_label="KEEP",
            )
        ):
            return
        self.prompt_store.delete(name)
        self.write_system(f"PROMPT DELETED - {name}")

    def cmd_sessions(self, argument: str) -> None:
        self.action_open_sessions()

    def cmd_session_save(self, argument: str) -> Any:
        if argument:
            self.save_session(argument)
            return None
        self.action_save_session()
        return None

    def cmd_session_load(self, argument: str) -> Any:
        if not argument:
            self.action_open_sessions()
            return None
        self.load_session(argument)
        return None

    def cmd_session_delete(self, argument: str) -> Any:
        if not argument:
            self.write_error("USAGE: /session-delete <name>")
            return None
        return self._delete_session(argument)

    async def _delete_session(self, name: str) -> None:
        if not self.session_store.exists(name):
            self.write_error(f"UNKNOWN SESSION: {name}")
            return
        if not await self.push_screen_wait(
            ConfirmModal(
                "DELETE SESSION?",
                f"{name} will be removed permanently.",
                authorize_label="DELETE",
                deny_label="KEEP",
            )
        ):
            return
        try:
            self.session_store.delete(name)
        except (FileNotFoundError, OSError) as exc:
            self.write_error(str(exc))
            return
        if self.session_name == name:
            self.session_name = None
        self.write_system(f"SESSION DELETED - {name}")

    def cmd_agent(self, argument: str) -> None:
        value = argument.strip().lower()
        if value not in ("on", "off"):
            state = "ON" if self.config.get("agent", {}).get("enabled") else "OFF"
            self.write_system(f"AGENT MODE IS {state} - USAGE: /agent on|off")
            return
        self.config.setdefault("agent", {})["enabled"] = value == "on"
        self.persist_config()
        self.write_system(
            f"AGENT MODE {value.upper()} - WORKSPACE {self.workspace_path}"
            if value == "on"
            else "AGENT MODE OFF"
        )
        self.refresh_status()

    def cmd_workspace(self, argument: str) -> None:
        if not argument:
            self.write_system(f"WORKSPACE: {self.workspace_path}")
            return
        self.set_workspace(argument)

    # ---------------------------------------------------------------- export

    def build_report(self) -> reporting.Report:
        """Gather the session, its settings and its measurements in one place."""
        from datetime import datetime

        generation = self.config.get("generation", {})
        provider = self.provider_block() or {}
        role = self.role_store.get(str(self.config.get("active_role", "")))
        parameters: dict[str, Any] = {
            "temperature": (
                role.temperature if role is not None
                else generation.get("temperature", 0.2)
            ),
            "max_tokens": generation.get("max_tokens"),
            "context_window": generation.get("context_window"),
            "include_usage": generation.get("include_usage", True),
            "agent_mode": bool(self.config.get("agent", {}).get("enabled", False)),
            "workspace": str(self.workspace_path),
            "timeout_seconds": provider.get("timeout_seconds"),
        }
        extra = provider.get("extra_body")
        if isinstance(extra, dict) and extra:
            parameters["extra_body"] = dict(extra)
        if self.inspect_enabled:
            parameters["inspect"] = self.inspect_config()

        usage: dict[str, Any] = {
            "turns": self.usage.turns,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "wall_time_seconds": round(self.usage.elapsed, 2),
            "generating_seconds": round(self.usage.generation_elapsed, 2),
            "average_tokens_per_second": round(self.usage.average_tokens_per_second, 2),
            "peak_tokens_per_second": round(self.usage.peak_tokens_per_second, 2),
            "context_tokens_last_turn": self.usage.context_tokens,
            "estimated_turns": self.usage.estimated_turns,
        }
        if self.usage.last is not None and self.usage.last.first_token_latency:
            usage["last_first_token_latency_seconds"] = round(
                self.usage.last.first_token_latency, 2
            )

        return reporting.Report(
            title=f"VOX session · {self.config.get('active_model', '')}",
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            provider=str(self.config.get("active_provider", "")),
            endpoint=str(provider.get("base_url", "")),
            model=str(self.config.get("active_model", "")),
            role=str(self.config.get("active_role", "")),
            parameters=parameters,
            messages=list(self.session.messages),
            usage=usage,
            inspection=self.inspection if len(self.inspection) else None,
            inspect_enabled=self.inspect_enabled,
        )

    def action_export_report(self) -> None:
        self.export_report()

    def export_report(self, formats: tuple[str, ...] = reporting.FORMATS) -> None:
        """Write the session out. Never blocks on anything but the disk."""
        try:
            written = reporting.write(
                self.build_report(), formats=formats, directory=self.workspace_path
            )
        except (OSError, ValueError) as exc:
            self.write_error(f"EXPORT FAILED - {exc}")
            return
        names = ", ".join(path.name for path in written)
        self.write_system(f"SAVED {names} in {written[0].parent}")

    def cmd_export(self, argument: str) -> None:
        wanted = argument.strip().lower().split()
        if not wanted:
            self.export_report()
            return
        unknown = [fmt for fmt in wanted if fmt not in reporting.FORMATS]
        if unknown:
            self.write_error(
                f"UNKNOWN FORMAT: {', '.join(unknown)} - "
                f"use {', '.join(reporting.FORMATS)}"
            )
            return
        self.export_report(tuple(wanted))

    # ------------------------------------------------------------------ mesh

    def action_toggle_mesh(self) -> None:
        """Ctrl+Shift+O: join the mesh, or leave it."""
        if self.mesh.online:
            self.mesh.stop()
            self.config.setdefault("mesh", {})["enabled"] = False
            self.persist_config()
            self.set_mesh_border(False)
            self.write_system("MESH OFFLINE - no longer announcing")
            self.refresh_header()
            self.refresh_status()
            return
        self.write_system("JOINING THE MESH…")
        self.start_mesh()

    @work(thread=True, group="mesh")
    def start_mesh(self) -> None:
        """Certificates and sockets both block, so this is off the UI thread."""
        try:
            detail = self.mesh.start()
        except MeshError as exc:
            self.mesh.last_error = str(exc)
            self.call_from_thread(self.write_error, f"MESH FAILED - {exc}")
            return
        except OSError as exc:
            self.mesh.last_error = str(exc)
            self.call_from_thread(self.write_error, f"MESH FAILED - {exc}")
            return
        self.call_from_thread(self.mesh_started, detail)

    def mesh_started(self, detail: str) -> None:
        self.config.setdefault("mesh", {})["enabled"] = True
        self.persist_config()
        self.set_mesh_border(True)
        # Going online is an announcement on the local network, so it says so.
        self.write_system(f"MESH ONLINE - {detail}")
        self.write_system(self.mesh.sharing_note())
        self.refresh_header()
        self.refresh_status()
        if self._universe_screen is not None:
            self._universe_screen.refresh_view()

    def set_mesh_border(self, online: bool) -> None:
        """The red border is the standing reminder that we are announcing."""
        try:
            self.screen_stack[0].set_class(online, "mesh-online")
        except IndexError:  # pragma: no cover - before the screen exists
            pass

    def action_open_universe(self) -> None:
        """Ctrl+Shift+U: who else is out there."""
        if isinstance(self.screen, UniverseScreen):
            self.screen.dismiss(None)
            return
        screen = UniverseScreen(self.mesh)
        self._universe_screen = screen
        self.push_screen(screen, lambda _result: self._forget_universe_screen())

    def _forget_universe_screen(self) -> None:
        self._universe_screen = None

    def cmd_mesh(self, argument: str) -> None:
        value = argument.strip().lower()
        if not value:
            state = "ONLINE" if self.mesh.online else "OFFLINE"
            self.write_system(
                f"MESH IS {state} - {self.mesh.agent_id} · {self.mesh.category} "
                f"- /mesh on|off"
            )
            return
        if value not in ("on", "off"):
            self.write_error("USAGE: /mesh [on|off]")
            return
        if (value == "on") == self.mesh.online:
            self.write_system(f"MESH IS ALREADY {value.upper()}")
            return
        self.action_toggle_mesh()

    def cmd_universe(self, argument: str) -> None:
        self.action_open_universe()

    def cmd_stats(self, argument: str) -> None:
        self.write_system(self.usage.report(self.context_window()))

    def cmd_code(self, argument: str) -> None:
        """Show the code panel, or copy one block by number."""
        self._panel_mode = "code"
        self.query_one("#side-panel").set_class(True, "visible")
        self.refresh_panel()
        if not argument:
            if self.code_blocks:
                summary = ", ".join(
                    block.label(index)
                    for index, block in enumerate(self.code_blocks, start=1)
                )
                self.write_system(f"CODE BLOCKS: {summary}  ·  /code <n> to copy")
            else:
                self.write_system("NO CODE BLOCK IN THE LAST ANSWER")
            return
        try:
            number = int(argument.split()[0])
        except ValueError:
            self.write_error("USAGE: /code [number]")
            return
        self.copy_code_block(number)

    def cmd_panel(self, argument: str) -> None:
        mode = argument.strip().lower() or ("index" if self._panel_mode == "code" else "code")
        if mode not in ("code", "index"):
            self.write_error("USAGE: /panel code|index")
            return
        self._panel_mode = mode
        self.query_one("#side-panel").set_class(True, "visible")
        self.refresh_panel()

    def cmd_connect(self, argument: str) -> None:
        self.write_system("PROBING /v1/models…")
        self.check_connection()

    def cmd_stop(self, argument: str) -> None:
        self.action_stop()

    def cmd_exit(self, argument: str) -> None:
        self.action_request_quit()

    # ------------------------------------------------------------------ quit

    async def _confirm_discard(self, question: str) -> bool:
        return bool(
            await self.push_screen_wait(
                ConfirmModal(
                    "UNSAVED CHANGES",
                    f"{question}\nThe current session has unsaved messages.",
                    authorize_label="DISCARD",
                    deny_label="CANCEL",
                )
            )
        )

    async def _flow_quit(self) -> None:
        if self.generating:
            self.cancel_event.set()
        if self.dirty and not await self._confirm_discard("QUIT ANYWAY?"):
            return
        self.shutdown()
        self.exit()

    def shutdown(self) -> None:
        """Stop everything we own before the screen goes away."""
        self._shutting_down = True
        self._preloading = None
        self.mesh.stop()
        self.cancel_event.set()
        self._stop_status_timer()
        if self.client is not None:
            self.client.close()


    # Modal flows must run inside a worker: push_screen_wait suspends until the
    # screen is dismissed, and Textual refuses that on the main task.

    @work
    async def action_new_session(self) -> None:
        await self._flow_new_session()

    @work
    async def action_save_session(self) -> None:
        await self._flow_save_session()

    @work
    async def action_open_sessions(self) -> None:
        await self._flow_sessions()

    @work
    async def action_open_roles(self) -> None:
        await self._flow_roles()

    @work
    async def action_open_prompts(self) -> None:
        await self._flow_prompts()

    @work
    async def action_open_settings(self) -> None:
        await self._flow_settings()

    @work
    async def action_request_quit(self) -> None:
        await self._flow_quit()


def run(workspace: Path | None = None, show_splash: bool = True) -> int:
    """Convenience entry point; ``__main__`` drives the real one."""
    from .__main__ import leave

    target = Path(workspace or Path.cwd())
    VoxApp(loaded=load_config(target), workspace=target, show_splash=show_splash).run()
    return leave(0)
