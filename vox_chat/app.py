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
from textual.widgets import Static

from . import __version__, commands
from .agent import AgentEvent, run_turn
from .config import (
    ConfigError,
    LoadedConfig,
    active_provider,
    load_config,
    mask_secret,
    open_in_editor,
    redacted_copy,
    reload_from_disk,
    save_global_config,
    validate_config,
)
from .llm_client import LLMClient, LLMError
from .logging_setup import get_logger, setup_logging
from .models import Message, Session
from .prompts import PromptStore, find_variables, render
from .history import InputHistory
from .roles import RoleStore
from .sessions import SessionStore
from .storage import global_config_path
from .tools import Workspace
from .usage import LiveMeter, UsageTracker
from .ui import branding
from .ui.modals import ConfirmModal, PickerItem, PickerModal, SettingsModal, TextPromptModal
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
        Binding("ctrl+s", "save_session", "Save", priority=True),
        Binding("ctrl+p", "open_prompts", "Prompts", priority=True),
        Binding("ctrl+r", "open_roles", "Roles", priority=True),
        Binding("ctrl+comma", "open_settings", "Settings", priority=True),
        Binding("ctrl+g", "stop", "Stop", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+c", "interrupt", "Copy / stop / quit", priority=True),
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
        self.session_store = SessionStore()
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
        self._thinking: ThinkingBox | None = None
        self._reasoning_box: MessageBox | None = None
        self._spinner_tick = 0
        self.session = self._new_session()
        self._assistant_box: MessageBox | None = None
        self._panel_mode = "sessions"

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

    def masked_key(self) -> str:
        provider = self.provider_block()
        if provider is None:
            return "********"
        return mask_secret(str(provider.get("api_key", "")))

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
        header = self.query_one(HeaderBar)
        header.update_state(
            link=self.link_label(),
            logon=self.masked_key(),
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
        usage = self.usage_summary()
        if usage:
            extra = f"{extra}  ·  {usage}" if extra else usage
        self.query_one(StatusBar).update_state(
            connected=self.connected,
            generating=self.generating,
            agent=agent,
            workspace=str(self.workspace_path),
            extra=f"{detail}  {extra}".strip(),
            spinner=spinner_frame(self._spinner_tick) if self.generating else "",
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
        """Load the model on the server before the first message needs it."""
        if self.client is None:
            return
        model = str(self.config.get("active_model", ""))
        if not model:
            return
        self.call_from_thread(self.write_system, f"PRELOADING {model}…")
        try:
            elapsed = self.client.warm(model)
        except LLMError as exc:
            self.call_from_thread(self.write_error, f"PRELOAD FAILED - {exc.message}")
            return
        self.call_from_thread(
            self.write_system, f"MODEL RESIDENT - {model} ready in {elapsed:.1f}s"
        )

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
            ):
                self.call_from_thread(self.handle_agent_event, event)
        except LLMError as exc:
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

    def _commit(self, messages: list[Message]) -> None:
        """Append the messages produced by a turn to the session."""
        for message in messages:
            self.session.messages.append(message)
        if messages:
            self.dirty = True
        self._assistant_box = None
        self._reasoning_box = None

    def finish_generation(self, error: str | None) -> None:
        self.generating = False
        self._assistant_box = None
        self.live_meter = None
        self._reasoning_box = None
        self.clear_thinking()
        if self._usage_timer is not None:
            self._usage_timer.stop()
            self._usage_timer = None
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
        self._spinner_tick += 1
        self.refresh_status()

    def action_stop(self) -> None:
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
            PickerModal("SESSIONS", items, "no saved sessions")
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

    def cmd_stats(self, argument: str) -> None:
        self.write_system(self.usage.report(self.context_window()))

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
                    ctrl_c_confirms=True,
                )
            )
        )

    async def _flow_interrupt(self) -> None:
        """Ctrl+C: copy a selection, else stop generating, else quit.

        Textual leaves ctrl+c to the focused widget, where the input box
        claims it for copy; this priority binding gives the key its usual
        terminal meaning back without losing copy.
        """
        screen = self.screen
        if isinstance(screen, ConfirmModal):
            # On the quit dialog a second ctrl+c means "yes"; on an authorise
            # dialog it means "no" — it must never approve a write.
            screen.dismiss(screen.ctrl_c_confirms)
            return
        if screen is not self.screen_stack[0]:
            screen.dismiss(None)
            return
        area = self.input_area
        if self.focused is area and area.selected_text:
            area.action_copy()
            return
        if self.generating:
            self.action_stop()
            return
        await self._flow_quit()

    async def _flow_quit(self) -> None:
        if self.generating:
            self.cancel_event.set()
        if self.dirty and not await self._confirm_discard("QUIT ANYWAY?"):
            return
        self.exit()


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

    @work
    async def action_interrupt(self) -> None:
        await self._flow_interrupt()

def run(workspace: Path | None = None, show_splash: bool = True) -> None:
    """Entry point used by ``__main__``."""
    target = Path(workspace or Path.cwd())
    VoxApp(loaded=load_config(target), workspace=target, show_splash=show_splash).run()
