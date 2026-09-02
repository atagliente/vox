"""The VOX Textual application.

The UI thread never performs blocking HTTP: generation runs in a worker
thread and posts updates back through ``call_from_thread``.
"""

from __future__ import annotations

import contextlib
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

from . import (
    __version__,
    clipboard,
    code_blocks,
    commands,
    images,
    indexing,
    ollama,
    project,
    searchd,
)
from . import consensus as cns
from . import report as reporting
from . import web as web_module
from .agent import AgentEvent
from .commands import dispatch
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
from .consensus_flow import ConsensusController
from .generation import GenerationController
from .history import InputHistory
from .inspection import (
    Alternative,
    InspectionRun,
    criteria_from_config,
)
from .llm_client import LLMClient, LLMError
from .logging_setup import get_logger, setup_logging
from .mcp import McpRegistry
from .mesh import MeshController, MeshError, MeshSettings
from .models import Message, Session
from .prompts import PromptStore, find_variables, render
from .roles import RoleStore
from .sessions import SessionStore
from .storage import global_config_path, vox_home
from .tools import Workspace
from .ui import branding
from .ui.inspect_screen import InspectScreen
from .ui.modals import (
    ConfirmModal,
    KeysModal,
    PickerItem,
    PickerModal,
    SettingsModal,
    TextPromptModal,
)
from .ui.round_screen import RoundScreen
from .ui.universe_screen import UniverseScreen
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
from .usage import LiveMeter, UsageTracker

log = get_logger("app")


def describe_model(row: dict[str, Any]) -> str:
    """The line under a model name in the picker: size, parameters, precision."""
    parts: list[str] = []
    size = int(row.get("size") or 0)
    if size:
        parts.append(
            f"{size / 1_000_000_000:.1f} GB"
            if size >= 1_000_000_000
            else f"{size / 1_000_000:.0f} MB"
        )
    for key in ("parameters", "quantization"):
        if row.get(key):
            parts.append(str(row[key]))
    return "  ".join(parts)


def describe_residency(resident: ollama.Residency) -> str:
    """One line saying where a loaded model lives, and whether it overflowed."""
    parts = [f"{resident.size_vram / 1e9:.2f} GB on the GPU"]
    if resident.on_cpu:
        parts.append(f"{resident.on_cpu / 1e9:.2f} GB on the CPU")
    if resident.gpu_total_mb:
        parts.append(f"card {resident.gpu_used_mb}/{resident.gpu_total_mb} MB")
    if resident.context:
        parts.append(f"{resident.context} tokens")
    line = "  -  ".join(parts)
    if resident.spilling():
        line += (
            "\n  the card is full, so the rest is in shared memory - "
            "slower than leaving those layers on the CPU"
        )
    return line


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
        Binding("f2", "toggle_agent", "Mode", priority=True),
        Binding("ctrl+shift+m", "toggle_agent", "Mode", show=False, priority=True),
        Binding("ctrl+g", "stop", "Stop", priority=True),
        Binding("ctrl+b", "toggle_panel", "Panel", priority=True),
        Binding("ctrl+y", "copy_code", "Copy code", priority=True),
        # Not ctrl+i: terminals send the same byte for it as for tab, so
        # Textual reports it as "tab" and the binding never fires.
        Binding("ctrl+t", "open_inspect", "Inspect", priority=True),
        Binding("ctrl+e", "export_report", "Export", priority=True),
        # Function keys only. ctrl+shift+<letter> was not delivered here, and
        # neither was ctrl+o; a function key travels through every terminal.
        Binding("f3", "toggle_mesh", "Online", priority=True),
        Binding("f4", "open_universe", "Universe", priority=True),
        Binding("f5", "open_round", "Round", priority=True),
        Binding("f6", "toggle_web_mode", "Web", priority=True),
        Binding("f12", "pick_model", "Models", priority=True),
        # The legend asked for on ctrl+shift+l, and on f1 as well because
        # ctrl+shift+<letter> is not delivered by every terminal (see the
        # note above); /keys reaches it when neither key arrives.
        Binding("ctrl+shift+l", "open_keys", "Keys", priority=True),
        Binding("f1", "open_keys", "Keys", show=False, priority=True),
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

        self.show_splash = show_splash and bool(
            self.config.get("ui", {}).get("splash", True)
        )
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
        # Set while a consensus turn is being generated, cleared afterwards.
        self._consensus_prompt: str = ""
        # Set for one turn when web mode has gathered sources for it.
        self._web_prompt: str = ""
        self._web_running = False
        # The little search server, started when web mode needs one.
        self.search_server = searchd.LocalSearch()
        # provider/model pairs that have refused to return logprobs.
        self._logprob_refusals: set[str] = set()
        self._consensus_answers: list[cns.PeerAnswer] = []
        self._consensus_running = False
        self._consensus_round = 0
        self.consensus_log = cns.RoundLog()
        self._round_screen: RoundScreen | None = None
        self.inspection = self._new_inspection()
        self.generation = GenerationController(self)
        self.consensus = ConsensusController(self)
        self.mcp = McpRegistry()
        # Set by /format: a JSON Schema the answer has to match. Held
        # for the session rather than saved, because a schema belongs
        # to the question being asked, not to the installation.
        self.response_format: dict[str, Any] | None = None
        # Images attached with /image, waiting for the message they belong to.
        self.pending_images: list[images.Attachment] = []
        self._vision_checked: str = ""
        # The project's AGENTS.md / VOX.md, read once when the workspace is
        # set rather than on every turn.
        self.project_notes = project.find(self.workspace_path)
        self._index: indexing.Index | None = None
        self._inspect_screen: InspectScreen | None = None
        self._panel_mode = "code"

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="body"):
            yield Transcript(
                show_timestamps=bool(
                    self.config.get("ui", {}).get("show_timestamps", True)
                )
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

    def mesh_label(self) -> str:
        """LOCAL, ON-LINE, and whether that is on the sample certificate."""
        if not self.mesh.online:
            label = branding.OFF_MESH
        elif self.mesh.demo_ca:
            label = f"{branding.ON_MESH} ({branding.DEMO_CERT})"
        else:
            label = branding.ON_MESH
        if self.web_mode_active():
            label = f"{label}  ·  {branding.WEB_MODE}"
        return label

    def refresh_header(self) -> None:
        try:
            header = self.query_one(HeaderBar)
        except NoMatches:  # the screen is already being torn down
            return
        header.update_state(
            link=self.link_label(),
            mesh=self.mesh_label(),
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
            busy=(
                f"PRELOADING {self._preloading}"
                if self._preloading is not None
                else (
                    "ASKING THE MESH"
                    if self._consensus_running
                    else ("LOOKING IT UP" if self._web_running else "")
                )
            ),
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
        self.show_thinking(
            f"PRELOADING {model} ON {endpoint}" if endpoint else f"PRELOADING {model}"
        )
        if self._usage_timer is None:
            self._usage_timer = self.set_interval(0.1, self._tick_status)
        self.refresh_status()

    def end_preload(self, model: str, elapsed: float | None, error: str | None) -> None:
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
            self.write_system(f"MODEL RESIDENT - {model} ready in {elapsed:.1f}s")
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
        if self._consensus_running:
            self.write_error("STILL ASKING THE MESH - CTRL+G TO STOP")
            return
        if self._web_running:
            self.write_error("STILL LOOKING THINGS UP - CTRL+G TO STOP")
            return
        if text.startswith("//"):
            text = text[1:]
        marked = cns.extract(text)
        if marked.unclosed:
            # A stray tag must never quietly ship the rest of the message.
            self.write_error(
                f"UNCLOSED {cns.OPEN} - nothing was distributed; close it with "
                f"{cns.CLOSE}"
            )
            return
        refusal = self.consensus_refusal() if marked.wanted else None
        if refusal is not None:
            self.write_error(refusal)
            return

        self.input_area.clear()
        self.history.add(text)
        message = Message(
            role="user",
            content=marked.text if marked.wanted else text,
            # Whatever /image attached goes with the question it belongs to,
            # and the box is empty again afterwards.
            images=[item.data_uri for item in self.pending_images],
        )
        attached = [item.describe() for item in self.pending_images]
        self.pending_images.clear()
        self.session.messages.append(message)
        self.write_message(message)
        if attached:
            self.write_system("SENT WITH: " + "  ·  ".join(attached))
        self.dirty = True
        if marked.wanted:
            self.start_consensus(marked.question)
            return
        if self.web_mode_active():
            settings = self.web_settings()
            if not self.wants_local_server(settings) or self.ensure_search_server(
                settings
            ):
                self.start_web_turn(message.content)
                return
        self.start_generation()

    # ------------------------------------------------------------ generation

    # ------------------------------------------------------------ generation
    #
    # The turn itself lives in generation.py. What stays here is the @work
    # decorator, which is Textual's way of putting something on a thread, and
    # the handful of names the rest of the application and its tests already
    # call by.

    def build_request_messages(self) -> list[Message]:
        return self.generation.build_request_messages()

    def start_generation(self) -> None:
        self.generation.start()

    @work(thread=True, group="generation", exclusive=True)
    def generate(self, temperature: float, max_tokens: int | None, model: str) -> None:
        self.generation.run(temperature, max_tokens, model)

    def handle_agent_event(self, event: AgentEvent) -> None:
        self.generation.handle_event(event)

    def finish_generation(self, error: str | None) -> None:
        self.generation.finish(error)

    def show_reasoning(self, chunk: str) -> None:
        self.generation.show_reasoning(chunk)

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
        key = (
            f"{self.config.get('active_provider', '')}/"
            f"{self.config.get('active_model', '')}"
        )
        if key in self._logprob_refusals:
            # It said no once. Asking again costs a rejected request and a
            # retry on every turn, for the same answer.
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
                "INSPECTION ON - the next answer is measured. /inspect off stops it."
            )
            self.refresh_status()
            just_enabled = True
        screen = InspectScreen(self.inspection, enabled=True, just_enabled=just_enabled)
        self._inspect_screen = screen
        self.push_screen(screen, lambda _result: self._forget_inspect_screen())

    def _forget_inspect_screen(self) -> None:
        self._inspect_screen = None

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
        """Say once per model that it will not be measured, and carry on."""
        if self.client is None or not self.client.logprobs_refused:
            return
        self.inspection.note = (
            "the provider rejected logprobs, so this answer was not measured"
        )
        key = (
            f"{self.config.get('active_provider', '')}/"
            f"{self.config.get('active_model', '')}"
        )
        if key in self._logprob_refusals:
            return
        # Once, and as a note rather than an error: nothing failed, and the
        # answer is exactly what it would have been.
        self._logprob_refusals.add(key)
        self.write_system(
            f"NOT MEASURED - {self.config.get('active_model', '')} does not "
            "return logprobs, so Ctrl+T has nothing to show for it. The answers "
            "are unaffected, and this is said once per model."
        )

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
        if self._web_running:
            self._web_running = False
            self.clear_thinking()
            self.write_system("LOOKUP ABANDONED")
            self.refresh_status()
            return
        if self._consensus_running:
            self._consensus_running = False
            self._consensus_round += 1  # anything still in flight is stale now
            self.clear_thinking()
            self.write_system("CONSENSUS ABANDONED - the peers may still answer")
            self.refresh_status()
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
                TextPromptModal(
                    "SAVE SESSION AS", placeholder="name", value=self.session.title
                )
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
        self.session.agent_enabled = bool(
            self.config.get("agent", {}).get("enabled", False)
        )
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
        self.write_system(
            f"SESSION LOADED - {session.title} ({len(session.messages)} messages)"
        )
        for message in session.messages:
            if message.reasoning and self.config.get("ui", {}).get(
                "show_reasoning", True
            ):
                self.write_message(
                    Message(
                        role="reasoning",
                        content=message.reasoning,
                        timestamp=message.timestamp,
                    )
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
                entry.error
                or f"{entry.updated_at}  {entry.message_count} msg  {entry.model}",
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
                f"{prompt.description}  [{', '.join(prompt.tags)}]"
                if prompt.tags
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
        type(self).CSS = branding.theme_css(
            str(data.get("ui", {}).get("theme", "nasa"))
        )
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
        self.project_notes = project.find(self.workspace_path)
        self._index = None
        if announce:
            self.write_system(f"WORKSPACE SET - {self.workspace_path}")
            if self.project_notes is not None:
                self.write_system(
                    f"PROJECT NOTES - {self.project_notes.describe()}, read as "
                    "context for this codebase"
                )
        self.refresh_status()

    # ----------------------------------------------------------- side panel

    def action_toggle_panel(self) -> None:
        panel = self.query_one("#side-panel")
        panel.set_class(not panel.has_class("visible"), "visible")
        if panel.has_class("visible"):
            self.refresh_panel()

    def refresh_panel(self) -> None:
        panel = self.query_one("#side-panel")
        panel.set_class(self._panel_mode in ("code", "consensus"), "code")
        if self._panel_mode == "consensus":
            self.query_one("#side-title", Static).update("VOX · CONSENSUS")
            self.query_one("#side-content", Static).update(
                cns.render_panel(self._consensus_answers)
            )
            return
        if self._panel_mode == "code":
            self.query_one("#side-title", Static).update("VOX · CODE")
            self.query_one("#side-content", Static).update(
                code_blocks.render_panel(self.code_blocks)
            )
            return
        sessions = self.session_store.list()
        lines = ["SESSIONS"]
        lines.extend(
            f"  {entry.name}  ({entry.message_count})" for entry in sessions[:10]
        )
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
            hint = (
                f" DID YOU MEAN: {', '.join(parsed.suggestions[:5])}"
                if parsed.suggestions
                else ""
            )
            self.write_error(parsed.error.upper() + hint)
            return
        result = dispatch.run(self, parsed)
        if inspect.iscoroutine(result):
            self.run_worker(result, exclusive=False)

    async def _pick_provider(self, providers: list[str]) -> None:
        items = [
            PickerItem(
                name, name, str(self.config["providers"][name].get("base_url", ""))
            )
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

    def model_items(self) -> list[PickerItem]:
        """The models on offer, the active one first, with what they are.

        On Ollama this is what ``ollama list`` shows - size, parameters,
        quantisation - which is what tells one local build from another.
        """
        provider = self.provider_block() or {}
        base_url = str(provider.get("base_url", ""))
        listed = list(provider.get("models", []))
        details: dict[str, str] = {}
        if ollama.looks_like_ollama(base_url):
            try:
                for row in ollama.list_models(base_url, timeout=5):
                    listed.append(row["name"])
                    details[row["name"]] = describe_model(row)
            except ollama.OllamaError as exc:
                self.write_error(f"cannot list models: {exc}")
        elif self.client is not None and self.connected:
            try:
                listed += self.client.list_models(timeout=5)
            except LLMError as exc:
                self.write_error(f"cannot list models: {exc.message}")
        active = str(self.config.get("active_model", ""))
        names = sorted(set(name for name in listed if name))
        # The active model at the top: the picker opens on the first row, so
        # a stray Enter changes nothing.
        names.sort(key=lambda name: (name != active, name))
        return [
            PickerItem(
                name, f"{'>' if name == active else ' '} {name}", details.get(name, "")
            )
            for name in names
        ]

    async def _pick_model(self) -> None:
        choice = await self.push_screen_wait(
            PickerModal("MODELS", self.model_items(), "no models listed")
        )
        if choice:
            self.set_model(choice)

    @work(thread=True, group="model-ctx")
    def revert_model_ctx(self, base_url: str, model: str) -> None:
        """Go back to the model this one was built from."""
        try:
            parent = ollama.parent_model(base_url, model)
        except ollama.OllamaError as exc:
            self.call_from_thread(self.write_error, f"MODEL CTX - {exc}")
            return
        if not parent:
            self.call_from_thread(
                self.write_error,
                f"MODEL CTX - {model} does not say what it was built from",
            )
            return
        self.call_from_thread(self.set_model, parent, "MODEL CTX OFF")

    @work(thread=True, group="model-ctx")
    def report_model_ctx(self, base_url: str, model: str) -> None:
        """What window this model has now, and the most it could have."""
        try:
            loaded = ollama.loaded_context(base_url, model)
            configured = ollama.configured_context(base_url, model)
            trained = ollama.trained_context(base_url, model)
        except ollama.OllamaError as exc:
            self.call_from_thread(self.write_error, f"MODEL CTX - {exc}")
            return
        parts = [f"MODEL CTX - {model}"]
        if loaded is not None:
            parts.append(f"loaded with {loaded}")
        if configured is not None:
            parts.append(f"set to {configured}")
        elif loaded is None:
            parts.append("not loaded, so the server default applies")
        if trained is not None:
            parts.append(f"trained for up to {trained}")
        line = "  ·  ".join(parts)
        if configured is None:
            line += "\n  /model ctx <N> writes a build with a larger window"
        self.call_from_thread(self.write_system, line)

    def build_config(self) -> dict[str, Any]:
        """What every model VOX writes for itself carries."""
        block = self.config.get("model_build")
        return dict(block) if isinstance(block, dict) else {}

    @work(thread=True, group="model-ctx")
    def build_model_ctx(self, base_url: str, model: str, num_ctx: int) -> None:
        """Write the derived model, then switch to it."""
        try:
            trained = ollama.trained_context(base_url, model)
            if trained is not None and num_ctx > trained:
                self.call_from_thread(
                    self.write_error,
                    f"MODEL CTX - {model} was trained for {trained} tokens, "
                    f"asking for {num_ctx} would only waste memory",
                )
                return
            parameters = self._build_parameters(base_url, model, num_ctx)
            name = ollama.create_with_context(base_url, model, num_ctx, parameters)
        except ollama.OllamaError as exc:
            self.call_from_thread(self.write_error, f"MODEL CTX - {exc}")
            return
        layers = parameters.get("num_gpu")
        detail = f", {layers} layers on the GPU" if layers is not None else ""
        self.call_from_thread(
            self.set_model,
            name,
            f"MODEL CTX - {num_ctx} tokens{detail}, same weights",
        )

    def _build_parameters(
        self, base_url: str, model: str, num_ctx: int
    ) -> dict[str, Any]:
        """The Modelfile parameters for a build. Runs on the worker thread.

        num_gpu "max" is measured, not assumed: the largest number of layers
        that stays on the card. Spilling into shared memory is slower than
        leaving the same layers on the CPU, so that is what gets avoided.
        """
        build = self.build_config()
        parameters: dict[str, Any] = {}
        extra = build.get("extra")
        if isinstance(extra, dict):
            parameters.update(extra)
        if build.get("num_batch"):
            parameters["num_batch"] = int(build["num_batch"])
        wanted = build.get("num_gpu", "max")
        if wanted is None:
            return parameters
        if isinstance(wanted, int) and not isinstance(wanted, bool):
            parameters["num_gpu"] = wanted
            return parameters
        reserve = int(build.get("vram_reserve_mb", 384) or 0)
        self.call_from_thread(
            self.write_system, "MODEL BUILD - measuring what fits on the GPU..."
        )
        layers, resident = ollama.fit_layers(base_url, model, num_ctx, reserve)
        if layers > 0:
            parameters["num_gpu"] = layers
        if resident is not None:
            self.call_from_thread(self.write_system, describe_residency(resident))
        return parameters

    @work(thread=True, group="model-ctx")
    def report_model_gpu(self, base_url: str, model: str) -> None:
        """Where the model is right now: card, layers, and any spill."""
        try:
            resident = ollama.residency(base_url, model)
            layers = ollama.layer_count(base_url, model)
        except ollama.OllamaError as exc:
            self.call_from_thread(self.write_error, f"MODEL GPU - {exc}")
            return
        if resident is None:
            card = ollama.vram_mb()
            where = (
                f"card has {card[1]} MB, {card[0]} MB in use"
                if card
                else "no NVIDIA card reported"
            )
            tail = f"  -  {layers} layers" if layers else ""
            self.call_from_thread(
                self.write_system,
                f"MODEL GPU - {model} is not loaded  -  {where}{tail}",
            )
            return
        self.call_from_thread(
            self.write_system, f"MODEL GPU - {model}\n  {describe_residency(resident)}"
        )

    @work(thread=True, group="model-ctx")
    def build_model_gpu(self, base_url: str, model: str, layers: int | None) -> None:
        """Write a build with this many layers on the GPU, and switch to it."""
        try:
            window = ollama.configured_context(base_url, model) or int(
                self.config.get("generation", {}).get("context_window", 8192)
            )
            if layers is None:
                reserve = int(self.build_config().get("vram_reserve_mb", 384) or 0)
                self.call_from_thread(
                    self.write_system,
                    "MODEL GPU - measuring what fits, one load per try...",
                )
                layers, resident = ollama.fit_layers(base_url, model, window, reserve)
                if resident is not None:
                    self.call_from_thread(
                        self.write_system, describe_residency(resident)
                    )
                if layers <= 0:
                    self.call_from_thread(
                        self.write_error,
                        "MODEL GPU - nothing fits on this card at this window",
                    )
                    return
            name = ollama.create_with_context(
                base_url, model, window, {"num_gpu": layers}
            )
        except ollama.OllamaError as exc:
            self.call_from_thread(self.write_error, f"MODEL GPU - {exc}")
            return
        self.call_from_thread(
            self.set_model,
            name,
            f"MODEL GPU - {layers} layers on the card at {window} tokens",
        )

    def set_model(self, name: str, reason: str = "MODEL SET") -> None:
        """Make ``name`` the active model and tell the operator."""
        self.config["active_model"] = name
        self.persist_config()
        self.write_system(f"{reason} - {name}")
        self.refresh_status()
        self.refresh_header()
        if self.connected and self.config.get("generation", {}).get("preload", True):
            self.preload_model()

    async def _ask_prompt_name(self, content: str) -> None:
        name = await self.push_screen_wait(TextPromptModal("SAVE PROMPT AS", "name"))
        if name:
            self.prompt_store.save_prompt(name, content)
            self.write_system(f"PROMPT SAVED - {name}")

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

    def action_toggle_agent(self) -> None:
        """Flip agent mode in place; bound to ``Ctrl+Shift+M``."""
        value = not bool(self.config.get("agent", {}).get("enabled", False))
        self.config.setdefault("agent", {})["enabled"] = value
        self.persist_config()
        self.write_system(
            f"AGENT MODE ON - WORKSPACE {self.workspace_path}"
            if value
            else "AGENT MODE OFF"
        )
        self.refresh_status()

    # ---------------------------------------------------------------- export

    def build_report(self) -> reporting.Report:
        """Gather the session, its settings and its measurements in one place."""
        from datetime import datetime

        generation = self.config.get("generation", {})
        provider = self.provider_block() or {}
        role = self.role_store.get(str(self.config.get("active_role", "")))
        parameters: dict[str, Any] = {
            "temperature": (
                role.temperature
                if role is not None
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
            conversation_id=self.session.id,
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

    # --------------------------------------------------------------- the web

    def web_settings(self) -> web_module.WebSettings:
        return web_module.WebSettings.from_config(self.config)

    def web_mode_active(self) -> bool:
        """Is every message answered with sources fetched first?"""
        settings = self.web_settings()
        return bool(settings.enabled and settings.auto and not settings.unusable())

    def action_toggle_web_mode(self) -> None:
        """F6: chat as usual, but let the answer be researched first."""
        section = self.config.setdefault("web", {})
        settings = self.web_settings()
        turning_on = not (settings.enabled and settings.auto)
        if turning_on:
            section["enabled"] = True
            section["auto"] = True
            self.persist_config()
            settings = self.web_settings()
            reason = settings.unusable()
            if reason:
                section["auto"] = False
                self.persist_config()
                self.write_error(f"WEB MODE NEEDS A BACKEND - {reason}")
                return
            if self.wants_local_server(settings) and not self.ensure_search_server(
                settings
            ):
                section["auto"] = False
                self.persist_config()
                self.refresh_header()
                self.refresh_status()
                return
            self.announce_web_mode(settings)
        else:
            section["auto"] = False
            self.persist_config()
            self.write_system("WEB MODE OFF - answers come from the model alone")
        self.refresh_header()
        self.refresh_status()

    def announce_web_mode(self, settings) -> None:
        self.write_system(
            f"WEB MODE ON - every message is answered with sources from "
            f"{settings.describe()}: {settings.auto_results} results, "
            f"{settings.auto_fetch} of them read in full"
        )
        self.refresh_header()
        self.refresh_status()

    def wants_local_server(self, settings) -> bool:
        """Is this endpoint one VOX should be serving itself?"""
        return settings.provider in (
            "local",
            "searxng",
        ) and web_module.is_local_endpoint(settings.endpoint)

    def ensure_search_server(self, settings) -> bool:
        """Start the local search server if it is not already up.

        In this process, in a thread: no container to install, no daemon to
        run, and it goes away when VOX does.
        """
        if self.search_server.running:
            return True
        if searchd.answering(settings.endpoint):
            # Another VOX, or a SearXNG of your own, is already there.
            return True
        self.search_server.port = searchd.port_of(settings.endpoint)
        try:
            detail = self.search_server.start()
        except searchd.SearchdError as exc:
            self.write_error(f"SEARCH SERVER - {exc}")
            return False
        self.write_system(f"SEARCH SERVER - {detail}")
        return True

    def start_web_turn(self, question: str) -> None:
        self._web_running = True
        self.show_thinking("LOOKING IT UP")
        self.refresh_status()
        self.research(question, self.web_settings())

    @work(thread=True, group="web", exclusive=True)
    def research(self, question: str, settings: web_module.WebSettings) -> None:
        """Search and read before the model is asked anything."""
        try:
            sources = web_module.gather(question, settings)
        except web_module.WebError as exc:
            self.call_from_thread(self.research_failed, str(exc))
            return
        self.call_from_thread(self.research_finished, sources, settings)

    def research_failed(self, detail: str) -> None:
        self._web_running = False
        self.clear_thinking()
        # The question still deserves an answer, from what the model knows.
        self.write_error(f"SEARCH FAILED - {detail}; answering without sources")
        self.start_generation()

    def research_finished(self, sources, settings) -> None:
        self._web_running = False
        self.clear_thinking()
        if not sources.found:
            self.write_system(f"NOTHING FOUND FOR: {sources.query}")
            self.start_generation()
            return

        read = len(sources.pages)
        self.write_system(
            f"WEB - {len(sources.results)} sources for {sources.query!r}"
            + (f", {read} read in full" if read else "")
            + (f"  ·  {'  ·  '.join(sources.notes)}" if sources.notes else "")
        )
        # The citations stay in the conversation; the page text is only for
        # this turn, or every later message would carry the whole internet.
        citation = Message(role="web", name="web", content=sources.citations())
        self.session.messages.append(citation)
        self.write_message(citation)
        self.dirty = True
        self._web_prompt = web_module.research_prompt(
            sources, limit=settings.auto_max_chars
        )
        self.start_generation()

    @work(thread=True, group="web", exclusive=True)
    def run_search(self, query: str, settings: web_module.WebSettings) -> None:
        notes: list[str] = []
        try:
            results = web_module.search(query, settings, notes)
        except web_module.WebError as exc:
            self.call_from_thread(self.write_error, f"SEARCH FAILED - {exc}")
            return
        self.call_from_thread(self.search_finished, query, results, notes)

    def search_finished(
        self, query: str, results: list, notes: list | None = None
    ) -> None:
        if notes:
            self.write_system("WEB - " + "  ·  ".join(notes))
        text = web_module.render_results(query, results)
        # Into the conversation, so the model can use what was found, and
        # labelled so it treats the text as information rather than orders.
        message = Message(
            role="tool",
            name="web_search",
            content=f"{web_module.UNTRUSTED_NOTE}\n\n{text}",
        )
        self.session.messages.append(message)
        self.write_message(message)
        self.dirty = True

    @work(thread=True, group="web", exclusive=True)
    def run_fetch(self, url: str, settings: web_module.WebSettings) -> None:
        try:
            page = web_module.fetch(url, settings)
        except web_module.WebError as exc:
            self.call_from_thread(self.write_error, f"FETCH FAILED - {exc}")
            return
        self.call_from_thread(self.fetch_finished, page)

    def fetch_finished(self, page: web_module.Page) -> None:
        message = Message(
            role="web",
            name="fetch_url",
            content=web_module.render_page(page),
        )
        self.session.messages.append(message)
        self.write_message(message)
        self.dirty = True
        self.write_system(
            f"READ {page.url} - {len(page.text)} characters"
            + (" (truncated)" if page.truncated else "")
        )

    # ------------------------------------------------------------- consensus
    #
    # The round itself lives in consensus_flow.py. These are the names the
    # rest of the application and the tests call it by.

    def consensus_settings(self) -> dict:
        return self.consensus.consensus_settings()

    def consensus_refusal(self) -> str | None:
        return self.consensus.consensus_refusal()

    def start_consensus(self, question: str) -> None:
        self.consensus.start_consensus(question)

    def consensus_event(self, agent: str, kind: str, text: str, ts: float) -> None:
        self.consensus.consensus_event(agent, kind, text, ts)

    def consensus_answered(
        self, question: str, answers: list, settings: dict, round_number: int = 0
    ) -> None:
        self.consensus.consensus_answered(question, answers, settings, round_number)

    def install_answer_hook(self) -> None:
        self.consensus.install_answer_hook()

    def answer_for_peer(
        self, caller: str, question: str, emit=None, conversation: str = ""
    ) -> dict:
        return self.consensus.answer_for_peer(caller, question, emit, conversation)

    def answering_started(self, caller: str, question: str, conversation: str) -> None:
        self.consensus.answering_started(caller, question, conversation)

    def answering_event(self, kind: str, text: str) -> None:
        self.consensus.answering_event(kind, text)

    def answering_finished(self, caller: str, answer: str, elapsed: float) -> None:
        self.consensus.answering_finished(caller, answer, elapsed)

    @work(thread=True, group="consensus", exclusive=True)
    def ask_the_mesh(self, question: str, settings: dict) -> None:
        """Every peer at once; the round is as slow as the slowest of them."""

        def relay(agent: str, kind: str, text: str, ts: float) -> None:
            # One hop to the UI thread per fragment: the peers are on a network,
            # so these arrive far slower than local tokens.
            self.call_from_thread(self.consensus_event, agent, kind, text, ts)

        round_number = self._consensus_round
        answers = self.mesh.ask_peers(
            question,
            verb=str(settings.get("verb", "infer")),
            limit=int(settings.get("max_peers", 5)),
            timeout=float(settings.get("ask_timeout_seconds", 90.0)),
            on_event=relay,
            conversation=self.session.id,
        )
        self.call_from_thread(
            self.consensus_answered, question, answers, settings, round_number
        )

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
        self.install_answer_hook()
        # Going online is an announcement on the local network, so it says so.
        self.write_system(f"MESH ONLINE - {detail}")
        self.write_system(self.mesh.sharing_note())
        self.refresh_header()
        self.refresh_status()
        if self._universe_screen is not None:
            self._universe_screen.refresh_view()

    def set_mesh_border(self, online: bool) -> None:
        """The red border is the standing reminder that we are announcing."""
        with contextlib.suppress(IndexError):  # pragma: no cover - no screen yet
            self.screen_stack[0].set_class(online, "mesh-online")

    # ----------------------------------------------------------------- index

    def index_settings(self) -> dict[str, Any]:
        block = self.config.get("index")
        return block if isinstance(block, dict) else {}

    def index_file(self) -> Path:
        return indexing.index_path(self.workspace_path, vox_home())

    def load_index(self) -> indexing.Index | None:
        if self._index is None:
            self._index = indexing.load(self.index_file())
        return self._index

    def index_report(self) -> str:
        """What is indexed, how stale it is, and how to change that."""
        settings = self.index_settings()
        state = "on" if settings.get("enabled") else "off"
        index = self.load_index()
        if index is None:
            return (
                f"INDEX {state.upper()} - nothing built for "
                f"{self.workspace_path}\n"
                "  /index build   ·   needs Ollama and an embedding model\n"
                f"  ollama pull {settings.get('model', indexing.DEFAULT_MODEL)}"
            )
        changed, gone = index.stale(self.workspace_path)
        lines = [
            f"INDEX {state.upper()} - {self.workspace_path}",
            f"  {len(index.chunks)} chunk(s) from {len(index.stamps)} file(s)",
            f"  model {index.model}",
        ]
        if changed or gone:
            lines.append(
                f"  {len(changed)} file(s) changed, {len(gone)} removed - /index build"
            )
        else:
            lines.append("  up to date")
        return "\n".join(lines)

    def drop_index(self) -> None:
        self._index = None
        try:
            self.index_file().unlink(missing_ok=True)
        except OSError as exc:
            self.write_error(f"INDEX - could not remove it: {exc}")
            return
        self.config.setdefault("index", {})["enabled"] = False
        self.persist_config()
        self.write_system("INDEX OFF - dropped; questions go out on their own again")

    @work(thread=True, group="index", exclusive=True)
    def build_index(self) -> None:
        """Embed what has changed, on a thread, saying how far it has got."""
        settings = self.index_settings()
        model = str(settings.get("model", indexing.DEFAULT_MODEL))
        base_url = str((self.provider_block() or {}).get("base_url", ""))
        root = self.workspace_path
        index = self.load_index() or indexing.Index(root=str(root), model=model)
        if index.model != model:
            # A different embedding model produces vectors that cannot be
            # compared with the old ones, so the index starts again.
            index = indexing.Index(root=str(root), model=model)
        changed, gone = index.stale(root)
        index.drop(gone | {p.relative_to(root).as_posix() for p in changed})
        if not changed:
            self.call_from_thread(
                self.write_system, f"INDEX - already up to date ({len(gone)} removed)"
            )
            self._index = index
            indexing.save(index, self.index_file())
            return
        self.call_from_thread(
            self.write_system,
            f"INDEX - embedding {len(changed)} file(s) with {model}...",
        )
        done = 0
        for path in changed:
            name = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                chunks = indexing.split(text, name)
                if chunks:
                    vectors = indexing.embed(
                        [chunk.text for chunk in chunks], base_url, model
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True):
                        chunk.vector = vector
                    index.chunks.extend(chunks)
                index.stamps[name] = indexing.FileStamp.of(path)
            except indexing.IndexError_ as exc:
                self.call_from_thread(self.write_error, f"INDEX - {exc}")
                return
            except OSError:
                continue  # a file that vanished mid-build is not a failure
            done += 1
        indexing.save(index, self.index_file())
        self._index = index
        self.config.setdefault("index", {})["enabled"] = True
        self.call_from_thread(self.persist_config)
        self.call_from_thread(
            self.write_system,
            f"INDEX BUILT - {done} file(s), {len(index.chunks)} chunk(s). "
            "Questions now carry the files that look relevant.",
        )

    @work(thread=True, group="index")
    def search_index(self, query: str) -> None:
        """Show what the index thinks this question is about."""
        hits = self.relevant_chunks(query)
        if not hits:
            self.call_from_thread(
                self.write_system, "INDEX - nothing close enough; /index build?"
            )
            return
        lines = [f"INDEX - closest to {query!r}", ""]
        lines += [
            f"  {score:.2f}  {chunk.path}:{chunk.start_line}" for score, chunk in hits
        ]
        self.call_from_thread(self.write_system, "\n".join(lines))

    def relevant_chunks(self, query: str) -> list[tuple[float, indexing.Chunk]]:
        """The workspace chunks closest to this question. Never raises."""
        index = self.load_index()
        if index is None or not index.chunks or not query.strip():
            return []
        settings = self.index_settings()
        base_url = str((self.provider_block() or {}).get("base_url", ""))
        try:
            vector = indexing.embed([query], base_url, index.model)[0]
        except (indexing.IndexError_, IndexError):
            # Searching is a convenience. A question still deserves an answer
            # when the embedding server is not there.
            return []
        return index.search(vector, int(settings.get("results", 4)))

    # ---------------------------------------------------------------- images

    def show_image(self, attachment: images.Attachment) -> None:
        """Draw the image in the scrollback, where the terminal can do that.

        Detected rather than attempted: writing a Kitty escape to a terminal
        that does not know it prints the payload across the screen instead of
        failing quietly.
        """
        protocol = images.preview_protocol()
        if protocol == "none":
            return
        sequence = images.inline_preview(attachment, protocol)
        if sequence:
            self.write_system(sequence)

    @work(thread=True, group="vision", exclusive=True)
    def check_vision_support(self) -> None:
        """Say plainly when the active model cannot read a picture.

        Asked once per model rather than per image: the answer does not
        change, and a /api/show for every attachment is a request nobody
        wanted.
        """
        model = str(self.config.get("active_model", ""))
        if not model or self._vision_checked == model:
            return
        base_url = str((self.provider_block() or {}).get("base_url", ""))
        if not ollama.looks_like_ollama(base_url):
            # Only Ollama says; elsewhere the provider's own refusal is the
            # honest answer, and it arrives on the next turn.
            return
        self._vision_checked = model
        try:
            if ollama.reads_images(base_url, model):
                return
        except ollama.OllamaError:
            return  # the connection has its own error path
        self.call_from_thread(
            self.write_error,
            f"{model} does not read images. The attachment will be sent and "
            "refused; /model to pick one that does - qwen2.5-vl, gemma3, "
            "llava and moondream all can.",
        )

    # ------------------------------------------------------------------- mcp

    @work(thread=True, group="mcp", exclusive=True)
    def connect_mcp(self) -> None:
        """Bring up the configured MCP servers, and say what happened.

        On a thread because it spawns processes and opens sockets: a server
        that takes ten seconds to start must not take the screen with it.
        """
        self.call_from_thread(self.write_system, "MCP - connecting...")
        report = self.mcp.connect_all(self.config)
        if not report:
            self.call_from_thread(
                self.write_error, "MCP - nothing configured under mcp.servers"
            )
            return
        working = sum(1 for status in report if status.connected)
        tools = sum(status.tools for status in report)
        self.call_from_thread(
            self.write_system,
            f"MCP - {working}/{len(report)} server(s), {tools} tool(s). /mcp list",
        )
        for status in report:
            if not status.connected:
                self.call_from_thread(
                    self.write_error, f"MCP {status.name}: {status.error}"
                )

    def action_open_keys(self) -> None:
        """Ctrl+Shift+L: every key combination, without leaving the screen."""
        if isinstance(self.screen, KeysModal):
            self.screen.dismiss(None)
            return
        self.push_screen(KeysModal())

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

    @work(thread=True, group="mesh")
    def replace_mesh_ca(self, demo: bool = False) -> None:
        """Generating a key and restarting the agent both block."""
        try:
            detail = self.mesh.replace_ca(demo=demo)
        except (MeshError, OSError) as exc:
            self.call_from_thread(self.write_error, f"NEW CA FAILED - {exc}")
            return
        self.call_from_thread(self.mesh_ca_replaced, detail, demo)

    def mesh_ca_replaced(self, detail: str, demo: bool) -> None:
        # Persisted, or the next start would move the authority straight back.
        self.config.setdefault("mesh", {})["demo_ca"] = demo
        self.mesh.settings.demo_ca = demo
        self.persist_config()
        self.write_system(f"CERTIFICATE AUTHORITY - {detail}")
        self.refresh_header()
        self.refresh_status()

    def action_open_round(self) -> None:
        """F5: what every agent is writing, as it writes it."""
        if isinstance(self.screen, RoundScreen):
            self.screen.dismiss(None)
            return
        screen = RoundScreen(self.consensus_log)
        self._round_screen = screen
        self.push_screen(screen, lambda _result: self._forget_round_screen())

    def _forget_round_screen(self) -> None:
        self._round_screen = None

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
        self.search_server.stop()
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
    async def action_pick_model(self) -> None:
        await self._pick_model()

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
