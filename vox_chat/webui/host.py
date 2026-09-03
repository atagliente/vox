"""The browser's half of VOX: state, a turn, and a stream of events.

This is the second implementation of :class:`vox_chat.hosting.Host`. It holds
what `VoxApp` holds — the configuration, the session, the stores, the mesh —
and answers the same calls, except that "say something to the person" means
"push an event to every open tab" rather than "add a widget".

Three things are worth reading for, because they are decisions rather than
plumbing.

**One conversation, not one per tab.** A browser can have the page open twice,
and the obvious design — a session per connection — would mean two half
conversations that each think they are the whole one. Every tab watches the
same session and sees the same events, which is what a second window on the
same work should do.

**A confirmation blocks the turn, and a timeout denies.** `run_turn` asks by
calling `confirm(name, description)` and waiting for a bool; here that waits
on an Event that a `POST /confirm` sets. If the tab is closed with a
confirmation outstanding, nothing will ever set it — so it expires, and it
expires as **no**. A timeout that authorised would be a confirmation nobody
read, which SECURITY.md already says is not a confirmation.

**The stream is coalesced, for the same reason the TUI's is.** A 6,000-token
answer would otherwise be 6,000 SSE frames and 6,000 DOM writes. `coalesce`
gathers text for 50ms or 400 characters, which took the TUI from 6,000 hops
to 87 and does the same here.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .. import accessibility, coalesce, project, sampling, searchd, threads, turn
from .. import consensus as cns
from .. import images as images_module
from .. import reputation as reputation_module
from .. import web as web_module
from ..agent import run_turn
from ..commands import dispatch, parse
from ..config import ConfigError, LoadedConfig, active_provider, save_global_config
from ..hosting import HostBase
from ..inspection import InspectionRun, criteria_from_config
from ..llm_client import LLMClient, LLMError
from ..logging_setup import get_logger
from ..mcp.registry import McpRegistry
from ..mesh import MeshController, MeshSettings
from ..models import Message, Session
from ..prompts import PromptStore
from ..roles import RoleStore
from ..sessions import SessionStore
from ..tools import TodoList, Workspace
from ..undo import UndoStack
from ..usage import UsageTracker

log = get_logger("webui.host")

# How long a confirmation waits for an answer before it becomes a refusal.
# Long enough that thinking about a diff is not a race; short enough that a
# closed tab does not leave a thread parked for the life of the process.
CONFIRM_TIMEOUT = 300.0


class EventBus:
    """Fan-out to every open tab.

    A queue per connection, and a publish that never blocks: a browser that
    has stopped reading must not be able to stall the turn producing the
    events. Its queue fills, the oldest frame is dropped, and it catches up
    from the conversation when it reconnects.
    """

    LIMIT = 2000

    def __init__(self) -> None:
        self._queues: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        listener: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.LIMIT)
        with self._lock:
            self._queues.append(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if listener in self._queues:
                self._queues.remove(listener)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._queues)

    def publish(self, kind: str, **data: Any) -> None:
        frame = {"kind": kind, **data}
        with self._lock:
            listeners = list(self._queues)
        for listener in listeners:
            try:
                listener.put_nowait(frame)
            except queue.Full:  # pragma: no cover - a tab that stopped reading
                try:
                    listener.get_nowait()
                    listener.put_nowait(frame)
                except queue.Empty:
                    pass


class WebHost(HostBase):
    """Everything `vox ui` needs, with the page kept at arm's length."""

    def __init__(self, loaded: LoadedConfig, workspace: Path) -> None:
        self.loaded = loaded
        self.config: dict[str, Any] = loaded.data
        self.workspace_path = Path(workspace).expanduser().resolve()
        self.workspace = Workspace(self.workspace_path)

        self.role_store = RoleStore()
        self.prompt_store = PromptStore()
        self.session_store = SessionStore(self.workspace_path)
        self.session = Session()
        self.session_name: str | None = None

        self.client: LLMClient | None = None
        self.connected = False
        self.generating = False
        self.dirty = False
        self.panel_mode = "code"
        self.quiet = accessibility.wanted(self.config)

        self.usage = UsageTracker()
        self.undo = UndoStack()
        self.todos = TodoList()
        self.mcp = McpRegistry()
        self.mesh = MeshController(MeshSettings.from_config(self.config))
        self.search_server = searchd.LocalSearch()
        self.reputation = reputation_module.Reputation.load(self._reputation_file())
        self.consensus_log = cns.RoundLog()
        self.round_history: list[dict[str, Any]] = []

        self.code_blocks: list[Any] = []
        self.pending_images: list[images_module.Attachment] = []
        self.response_format: dict[str, Any] | None = None
        self.consensus_prompt = ""
        self.web_prompt = ""
        self.project_notes = project.find(self.workspace_path)
        self.inspection = self._new_inspection()
        self._index: Any = None
        self._logprob_refusals: set[str] = set()

        self.events = EventBus()
        # Set when the server is going away, so a stream thread parked on its
        # queue finishes instead of holding the shutdown open.
        self.closing = threading.Event()
        self.cancel_event = threading.Event()
        self._pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._draft = ""
        self.rebuild_client()

    # -------------------------------------------------------------- talking

    def write_system(self, text: str) -> None:
        self._append(Message(role="system", content=text))

    def write_error(self, text: str) -> None:
        self._append(Message(role="error", content=text))

    def write_message(self, message: Message) -> None:
        self._append(message)

    def _append(self, message: Message) -> None:
        self.session.messages.append(message)
        self.dirty = True
        self.events.publish("message", message=message.to_dict())

    def open_view(self, name: str, argument: str = "") -> None:
        """Ask the page to show a view.

        The one call a terminal and a browser answer differently, and the
        reason the other seventy-odd did not have to change.
        """
        if name == "panel":
            self.panel_mode = argument or "code"
        self.events.publish("view", name=name, argument=argument)

    # ---------------------------------------------------------------- state

    def provider_block(self) -> dict[str, Any] | None:
        try:
            return active_provider(self.config)
        except ConfigError as exc:
            self.write_error(str(exc))
            return None

    def rebuild_client(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        provider = self.provider_block()
        if provider is not None:
            self.client = LLMClient.from_provider(provider)

    def persist_config(self) -> None:
        try:
            save_global_config(self.config)
        except ConfigError as exc:
            self.write_error(f"config not saved: {exc}")

    def context_window(self) -> int:
        return int(self.config.get("generation", {}).get("context_window", 8192))

    def web_settings(self) -> web_module.WebSettings:
        return web_module.WebSettings.from_config(self.config)

    def web_mode_active(self) -> bool:
        settings = self.web_settings()
        return bool(settings.enabled and settings.auto and not settings.unusable())

    def index_settings(self) -> dict[str, Any]:
        block = self.config.get("index")
        return block if isinstance(block, dict) else {}

    def relevant_chunks(self, query: str) -> list[Any]:
        # The index is a Phase-4 concern here; a turn that asks for context
        # and gets none behaves exactly as it did before the index existed.
        return []

    def inspect_config(self) -> dict[str, Any]:
        block = self.config.get("inspect")
        return block if isinstance(block, dict) else {}

    def inspect_top_k(self) -> int | None:
        if not self.inspect_config().get("enabled", False):
            return None
        return int(self.inspect_config().get("top_k", 5))

    def _new_inspection(self) -> InspectionRun:
        return InspectionRun(
            model=str(self.config.get("active_model", "")),
            provider=str(self.config.get("active_provider", "")),
            top_k=int(self.inspect_config().get("top_k", 5)),
            criteria=criteria_from_config(self.config),
        )

    def _reputation_file(self) -> Path:
        from ..storage import vox_home

        return vox_home() / "reputation.json"

    def state(self) -> dict[str, Any]:
        """What the page needs to draw its chrome."""
        provider = None
        try:
            provider = active_provider(self.config)
        except ConfigError:
            provider = None
        return {
            "model": str(self.config.get("active_model", "")),
            "provider": str(self.config.get("active_provider", "")),
            "endpoint": str((provider or {}).get("base_url", "")),
            "role": str(self.config.get("active_role", "")),
            "workspace": str(self.workspace_path),
            "session": self.session_name or "",
            "agent": bool(self.config.get("agent", {}).get("enabled", False)),
            "web": bool(self.config.get("web", {}).get("enabled", False)),
            "mesh": self.mesh.online,
            "generating": self.generating,
            "models": [],
            "roles": self.role_store.names(),
            "messages": [m.to_dict() for m in self.session.messages],
        }

    # ------------------------------------------------------------- sessions

    def new_session(self) -> None:
        self.session = Session()
        self.session_name = None
        self.dirty = False
        self.usage = UsageTracker()
        self.events.publish("reset")
        self.write_system("NEW SESSION")

    def clear_transcript(self) -> None:
        self.session.messages.clear()
        self.dirty = False
        self.events.publish("reset")
        self.write_system("TRANSCRIPT CLEARED")

    def save_session(self, name: str) -> None:
        try:
            self.session_store.save(self.session, name or None)
        except OSError as exc:
            self.write_error(f"SESSION NOT SAVED - {exc}")
            return
        self.session_name = name or self.session_name
        self.dirty = False
        self.write_system(f"SESSION SAVED - {self.session_name or 'untitled'}")

    def load_session(self, name: str) -> None:
        try:
            self.session = self.session_store.load(name)
        except (OSError, ValueError) as exc:
            self.write_error(f"SESSION NOT LOADED - {exc}")
            return
        self.session_name = name
        self.dirty = False
        self.events.publish("reset")
        for message in self.session.messages:
            self.events.publish("message", message=message.to_dict())
        self.write_system(f"SESSION LOADED - {name}")

    def set_role(self, name: str) -> None:
        if self.role_store.get(name) is None:
            self.write_error(f"NO SUCH ROLE: {name}")
            return
        self.config["active_role"] = name
        self.write_system(f"ROLE - {name}")
        self.events.publish("state", state=self.state())

    def set_workspace(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.is_dir():
            self.write_error(f"NOT A DIRECTORY: {target}")
            return
        self.workspace_path = target.resolve()
        self.workspace = Workspace(self.workspace_path)
        self.session_store = SessionStore(self.workspace_path)
        self.project_notes = project.find(self.workspace_path)
        self.write_system(f"WORKSPACE - {self.workspace_path}")
        self.events.publish("state", state=self.state())

    @property
    def draft_text(self) -> str:
        return self._draft

    def set_draft(self, text: str) -> None:
        """What the page has in its textarea, so /prompt-save can read it."""
        self._draft = text

    # ------------------------------------------------- what a browser can do

    # Everything a command asks for that is not simply data. The ones that are
    # the same work in either host do it; the ones that are a screen say so.
    # HostBase and the guard in run_command catch whatever is left, so a
    # command growing a new call is a sentence in the transcript rather than a
    # dead request.

    def refresh_status(self, extra: str = "") -> None:
        self.events.publish("state", state=self.state())

    def refresh_panel(self) -> None:
        self.events.publish("state", state=self.state())

    def check_connection(self) -> None:
        """Probe the provider on a thread, and say what came back."""

        def probe() -> None:
            if self.client is None:
                self.write_error("NO PROVIDER CONFIGURED")
                return
            ok, detail = self.client.ping()
            self.connected = ok
            if ok:
                self.write_system(f"LINK ESTABLISHED - {detail}")
            else:
                self.write_error(f"LINK DOWN - {detail}".replace(": http", " at http"))
            self.refresh_status()

        threads.start(probe, "vox-ui-connect")

    def preload_model(self) -> None:
        # Warming a model matters when a person is watching a spinner. Here
        # the first message pays the same cost with the answer arriving after
        # it, which is the honest place for the wait.
        self.write_system(
            "PRELOAD - not done in the browser; the first message warms it"
        )

    def wants_local_server(self, settings: Any) -> bool:
        return settings.provider in (
            "local",
            "searxng",
        ) and web_module.is_local_endpoint(settings.endpoint)

    def ensure_search_server(self, settings: Any) -> bool:
        if self.search_server.running:
            return True
        if searchd.answering(settings.endpoint):
            return True
        self.search_server.port = searchd.port_of(settings.endpoint)
        try:
            detail = self.search_server.start()
        except searchd.SearchdError as exc:
            self.write_error(f"SEARCH SERVER - {exc}")
            return False
        self.write_system(f"SEARCH SERVER - {detail}")
        return True

    def run_search(self, query: str, settings: Any) -> None:
        def search() -> None:
            try:
                hits = web_module.search(query, settings)
            except web_module.WebError as exc:
                self.write_error(f"SEARCH - {exc}")
                return
            self.write_system(web_module.render_results(query, hits))

        threads.start(search, "vox-ui-search")

    def run_fetch(self, url: str, settings: Any) -> None:
        def fetch() -> None:
            try:
                page = web_module.fetch(url, settings)
            except web_module.WebError as exc:
                self.write_error(f"FETCH - {exc}")
                return
            body = f"{page.title}\n{page.url}\n\n{page.text}"
            self.write_message(Message(role="web", content=body))

        threads.start(fetch, "vox-ui-fetch")

    def connect_mcp(self) -> None:
        def connect() -> None:
            self.write_system("MCP - connecting...")
            report = self.mcp.connect_all(self.config)
            if not report:
                self.write_error("MCP - nothing configured under mcp.servers")
                return
            working = sum(1 for status in report if status.connected)
            tools = sum(status.tools for status in report)
            self.write_system(f"MCP - {working}/{len(report)} servers, {tools} tools")
            for status in report:
                if not status.connected:
                    self.write_error(f"MCP - {status.name}: {status.detail}")

        threads.start(connect, "vox-ui-mcp")

    def load_prompt(self, name: str) -> None:
        prompt = self.prompt_store.get(name)
        if prompt is None:
            self.write_error(f"NO SUCH PROMPT: {name}")
            return
        self._draft = prompt.body
        # The draft lives in the browser, so loading one has to travel there.
        self.events.publish("draft", text=prompt.body)
        self.write_system(f"PROMPT LOADED - {name}")

    def copy_code_block(self, number: int) -> None:
        # The clipboard belongs to the browser and a server cannot reach it.
        # Saying so beats a command that looks like it worked.
        self.write_error("COPY - select the block in the page instead")

    def show_image(self, attachment: Any) -> None:
        self.write_system(f"IMAGE - {attachment.describe()}")

    def export_report(self, formats: tuple[str, ...] = ()) -> None:
        self.write_error("REPORT IS NOT AVAILABLE IN THE BROWSER YET")

    def edit_config_file(self) -> None:
        # Suspending the screen and opening $EDITOR is a terminal's trick.
        from ..storage import global_config_path

        self.write_error(f"EDIT IT YOURSELF AND RESTART: {global_config_path()}")

    def report_model_ctx(self, base_url: str, model: str) -> None:
        self.write_error("MODEL CTX IS NOT AVAILABLE IN THE BROWSER YET")

    def report_model_gpu(self, base_url: str, model: str) -> None:
        self.write_error("MODEL GPU IS NOT AVAILABLE IN THE BROWSER YET")

    def rounds_report(self) -> None:
        self.write_error("ROUNDS IS NOT AVAILABLE IN THE BROWSER YET")

    def _pick_provider(self, providers: list[str]) -> None:
        self.write_system(
            "PROVIDERS: " + ", ".join(providers) + "  ·  /provider <name>"
        )

    def _pick_model(self) -> None:
        self.write_system("/model <name> to choose one")

    def _set_provider(self, name: str) -> None:
        self.config["active_provider"] = name
        self.persist_config()
        self.rebuild_client()
        self.write_system(f"PROVIDER - {name}")
        self.refresh_status()

    def consensus_settings(self) -> dict[str, Any]:
        block = self.config.get("consensus")
        return block if isinstance(block, dict) else {}

    def consensus_refusal(self) -> str | None:
        return "CONSENSUS IS NOT AVAILABLE IN THE BROWSER YET"

    def install_answer_hook(self) -> None:
        # Answering for a peer needs a turn this host can run, and it can —
        # but a round belongs to a screen this page does not have yet.
        return None

    # ------------------------------------------------------------ the turn

    def run_command(self, text: str) -> None:
        parsed = parse(text)
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
        try:
            result = dispatch.run(self, parsed)  # type: ignore[arg-type]
        except AttributeError as exc:
            # A command reaching for something this host has not grown yet.
            # The protocol documents the surface; it does not enforce it, and
            # a missing member must not take the request down with it.
            log.info("/%s is not available here: %s", parsed.name, exc)
            self.write_error(
                f"/{parsed.name.upper()} IS NOT AVAILABLE IN THE BROWSER YET"
            )
            return
        except Exception as exc:
            # Wide on purpose. Forty-nine commands run through here and any
            # of them failing should be a line in the transcript rather than
            # a dead connection and a page that looks frozen.
            log.exception("/%s failed", parsed.name)
            self.write_error(f"/{parsed.name.upper()} FAILED - {exc}")
            return
        if dispatch.is_async(result):
            # A handler that wants a modal flow returns a coroutine for the
            # TUI's worker to await. There is no such worker here, and the
            # handler has already asked for a view through open_view, so
            # closing it is the whole of what is left to do.
            result.close()

    def send(self, text: str) -> None:
        """Take one message from the person and answer it."""
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            self.run_command(text)
            return
        if self.generating:
            self.write_error("ALREADY GENERATING - STOP FIRST")
            return
        self._append(Message(role="user", content=text))
        self.start_turn()

    def start_turn(self) -> None:
        if self.client is None:
            self.write_error("NO PROVIDER CONFIGURED")
            return
        self.generating = True
        self.cancel_event = threading.Event()
        self.inspection = self._new_inspection()
        self.events.publish("state", state=self.state())
        threads.start(self._turn, "vox-ui-turn")

    def stop_generation(self) -> None:
        if not self.generating:
            return
        self.cancel_event.set()
        self.write_system("STOPPING GENERATION")

    def _turn(self) -> None:
        assert self.client is not None
        role = self.role_store.get(str(self.config.get("active_role", "")))
        generation = self.config.get("generation", {})
        agent_config = dict(self.config.get("agent", {}))
        if self.web_mode_active():
            agent_config["confirm_web"] = False
        parameters = sampling.resolve(self.config, role)
        messages = turn.build_request_messages(self)
        produced: list[Message] = []
        text = ""
        reasoning = ""
        try:
            events = run_turn(
                client=self.client,
                messages=messages,
                model=str(self.config.get("active_model", "")),
                temperature=float(
                    role.temperature
                    if role is not None
                    else generation.get("temperature", 0.2)
                ),
                max_tokens=generation.get("max_tokens"),
                workspace=self.workspace,
                agent_config=agent_config,
                confirm=self.confirm,
                cancel=self.cancel_event,
                agent_enabled=bool(agent_config.get("enabled", False)),
                include_usage=bool(generation.get("include_usage", True)),
                top_logprobs=self.inspect_top_k(),
                web_settings=self.web_settings(),
                mcp_registry=self.mcp if self.mcp.clients else None,
                sampling=parameters.request_fields(),
                native=parameters.native_fields(),
                response_format=self.response_format,
                undo=self.undo,
                todos=self.todos,
            )
            for batch in coalesce.coalesce(events):
                if batch.is_text:
                    text += batch.text
                    self.events.publish("delta", text=batch.text)
                    continue
                event = batch.event
                if event.type == "reasoning":
                    reasoning += event.text
                    self.events.publish("reasoning", text=event.text)
                elif event.type == "tool_call" and event.tool_call is not None:
                    self.events.publish(
                        "tool",
                        name=event.tool_call.name,
                        arguments=event.tool_call.arguments,
                        result=event.tool_call.result or "",
                    )
                elif event.type == "usage" and event.usage is not None:
                    self.usage.add(event.usage)
                    self.events.publish(
                        "usage",
                        line=self.usage.status_line(self.context_window(), None),
                    )
                elif event.type == "assistant_done":
                    produced = list(event.messages)
        except LLMError as exc:
            log.warning("ui turn failed: %s", exc.kind)
            self._finish(exc.message, text, reasoning, produced)
            return
        except Exception as exc:
            # Wide on purpose, and the reason is specific: this runs on its
            # own thread, so anything escaping here kills the thread quietly,
            # leaves `generating` set, and the page waits for a token that is
            # never coming. A visible error beats a frozen page.
            log.exception("ui turn crashed")
            self._finish(f"the turn failed: {exc}", text, reasoning, produced)
            return
        self._finish(None, text, reasoning, produced)

    def _finish(
        self,
        error: str | None,
        text: str,
        reasoning: str,
        produced: list[Message],
    ) -> None:
        self.generating = False
        self._expire_all("the turn ended")
        if reasoning:
            self.session.messages.append(Message(role="reasoning", content=reasoning))
        if produced:
            self.session.messages.extend(produced)
        elif text:
            self.session.messages.append(Message(role="assistant", content=text))
        self.dirty = True
        if error:
            self.write_error(error)
        self.events.publish("done", error=error or "")
        self.events.publish("state", state=self.state())

    # ------------------------------------------------------ confirmations

    def confirm(self, name: str, description: str) -> bool:
        """Ask the page, and wait. A tab that never answers means no.

        Called on the turn's thread by `run_turn`, which is why it blocks:
        the tool must not run until somebody has said it may.
        """
        if self.events.listeners == 0:
            # Nobody is watching, so nobody can authorise. Saying so beats
            # waiting five minutes to reach the same answer.
            log.info("refusing %s: no browser connected", name)
            return False
        pending = _Pending(name)
        with self._pending_lock:
            self._pending[pending.id] = pending
        self.events.publish(
            "confirm", id=pending.id, name=name, description=description
        )
        answered = pending.done.wait(self._confirm_timeout())
        with self._pending_lock:
            self._pending.pop(pending.id, None)
        if not answered:
            self.write_error(f"{name.upper()} - NOT ANSWERED, SO NOT AUTHORISED")
            return False
        return pending.allowed

    def answer_confirm(self, request_id: str, allowed: bool) -> bool:
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.allowed = allowed
        pending.done.set()
        self.events.publish("confirmed", id=request_id, allowed=allowed)
        return True

    def _expire_all(self, why: str) -> None:
        with self._pending_lock:
            outstanding = list(self._pending.values())
        for pending in outstanding:
            if not pending.done.is_set():
                pending.allowed = False
                pending.done.set()
                log.info("denied %s: %s", pending.name, why)

    def _confirm_timeout(self) -> float:
        ui = self.config.get("ui")
        ui = ui if isinstance(ui, dict) else {}
        try:
            return float(ui.get("confirm_timeout_seconds", CONFIRM_TIMEOUT))
        except (TypeError, ValueError):
            return CONFIRM_TIMEOUT

    # -------------------------------------------------------------- closing

    def close(self) -> None:
        self.closing.set()
        self.cancel_event.set()
        self._expire_all("vox is shutting down")
        if self.client is not None:
            self.client.close()
        self.mcp.close()
        self.mesh.stop()
        self.search_server.stop()


class _Pending:
    """One confirmation the page has been asked for and has not answered."""

    def __init__(self, name: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.allowed = False
        self.done = threading.Event()
        self.asked_at = time.monotonic()
