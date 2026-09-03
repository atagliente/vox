"""The life of one turn, from the send key to the last token.

Pulled out of ``VoxApp`` because it was the largest thing in it that is not
drawing: assembling the request, running the provider on a worker thread,
turning each event into something on screen, and tidying up afterwards.

The controller holds the application rather than inheriting from it. The one
piece that has to stay behind is the ``@work`` decorator on ``VoxApp.generate``
— scheduling a thread is Textual's business, and it belongs to the widget —
so that method is two lines that call :meth:`GenerationController.run`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from . import coalesce, compaction, fitting, sampling, turn
from .agent import AgentEvent, run_turn
from .llm_client import LLMError, context_overflow
from .logging_setup import get_logger
from .models import Message
from .ui.modals import ConfirmModal
from .usage import LiveMeter

if TYPE_CHECKING:
    from .app import VoxApp

log = get_logger("generation")


class GenerationController:
    """Everything a turn does, with the screen kept at arm's length."""

    def __init__(self, app: VoxApp) -> None:
        self.app = app

    def build_request_messages(self) -> list[Message]:
        """The conversation as the provider will receive it.

        The rules live in :mod:`vox_chat.turn`, because what to send is not a
        drawing decision and a second host needs the same answer.
        """
        return turn.build_request_messages(self.app)

    def start(self) -> None:
        app = self.app
        if app.client is None:
            app.write_error("NO PROVIDER CONFIGURED")
            return
        role = app.role_store.get(str(app.config.get("active_role", "")))
        generation = app.config.get("generation", {})
        temperature = float(
            role.temperature if role is not None else generation.get("temperature", 0.2)
        )
        app.cancel_event = threading.Event()
        app.inspection = app._new_inspection()
        if app._inspect_screen is not None:
            app._inspect_screen.run = app.inspection
            app._inspect_screen.refresh_view()
        app.generating = True
        app._assistant_box = None
        app.live_meter = LiveMeter()
        app.show_thinking("WAITING FOR MODEL")
        app._usage_timer = app.set_interval(0.1, app._tick_status)
        app.refresh_status()
        app.generate(
            temperature=temperature,
            max_tokens=generation.get("max_tokens"),
            model=str(app.config.get("active_model", "")),
        )

    def _fit_to_window(
        self, exc: LLMError, messages: list[Message], produced: bool
    ) -> list[Message] | None:
        """A smaller request when the refusal was lack of room, else None.

        Only before anything has been produced: retrying halfway through a
        turn would repeat text the operator has already read.
        """
        if exc.kind != "context" or produced:
            return None
        room = context_overflow(exc.detail) or context_overflow(exc.message)
        if room is None:
            return None
        used, window = room
        return fitting.shrink(messages, used, window)

    def confirm_from_thread(self, name: str, description: str) -> bool:
        """Blocking confirmation request from the worker thread."""
        app = self.app
        result = app.call_from_thread(
            app.push_screen_wait,
            ConfirmModal(f"AUTHORIZE {name.upper()}?", description),
        )
        return bool(result)

    def append_text(self, text: str) -> None:
        """A batch of streamed text, on the UI thread.

        The same work ``handle_event`` does for a text event, done once for
        several of them: one refresh, one scroll, one look for a fence.
        """
        app = self.app
        app.clear_thinking()
        if app.live_meter is not None:
            app.live_meter.add_text(text)
        if app._assistant_box is None:
            app._assistant_box = app.write_message(
                Message(role="assistant", content="")
            )
        app._assistant_box.append_text(text)
        app.transcript.scroll_end(animate=False)
        # Re-parsing on every batch would still be wasteful; a fence or a
        # line break is the only thing that can change the blocks.
        if any(marker in text for marker in ("`", "~", "\n")):
            app.refresh_code_blocks(app._assistant_box.message.content)

    def handle_event(self, event: AgentEvent) -> None:
        app = self.app
        if event.type == "text":
            app.clear_thinking()
            if app.live_meter is not None:
                app.live_meter.add_text(event.text)
            if app._assistant_box is None:
                app._assistant_box = app.write_message(
                    Message(role="assistant", content="")
                )
            app._assistant_box.append_text(event.text)
            app.transcript.scroll_end(animate=False)
            # Re-parsing on every token would be wasteful; a fence or a line
            # break is the only thing that can change the blocks.
            if any(marker in event.text for marker in ("`", "~", "\n")):
                app.refresh_code_blocks(app._assistant_box.message.content)
        elif event.type == "token":
            app.record_token(event.token, event.phase)
        elif event.type == "reasoning":
            self.show_reasoning(event.text)
        elif event.type == "tool_start" and event.tool_call is not None:
            app.show_thinking(f"RUNNING {event.tool_call.name}")
            app.write_message(
                Message(
                    role="tool",
                    name=event.tool_call.name,
                    content=f"REQUESTED {event.tool_call.name}",
                )
            )
        elif (
            event.type in ("tool_result", "tool_denied") and event.tool_call is not None
        ):
            app.show_thinking("WAITING FOR MODEL")
            app.write_message(
                Message(
                    role="tool" if event.type == "tool_result" else "error",
                    name=event.tool_call.name,
                    content=event.text,
                )
            )
        elif event.type in ("notice", "limit"):
            app.write_system(event.text)
            if event.type == "notice":
                app.config.setdefault("agent", {})["enabled"] = False
                app.refresh_status()
        elif event.type == "usage" and event.usage is not None:
            app.usage.add(event.usage)
            app.refresh_status()
        elif event.type == "cancelled":
            app.write_system("GENERATION STOPPED BY OPERATOR")
            app._commit(event.messages)
        elif event.type == "assistant_done":
            app._commit(event.messages)

    def finish(self, error: str | None) -> None:
        app = self.app
        app.consensus_prompt = ""
        app.web_prompt = ""
        app.note_logprob_refusal()
        app.generating = False
        app._assistant_box = None
        app.live_meter = None
        app._reasoning_box = None
        app.clear_thinking()
        app._stop_status_timer()
        if error:
            app.write_error(error)
            app.connected = False
            app.refresh_header()
        app.refresh_status()

    def show_reasoning(self, chunk: str) -> None:
        """Stream the model's thinking into its own block above the answer."""
        app = self.app
        if not app.config.get("ui", {}).get("show_reasoning", True):
            return
        app.clear_thinking()
        if app._reasoning_box is None:
            app._reasoning_box = app.write_message(
                Message(role="reasoning", content="")
            )
        app._reasoning_box.append_text(chunk)
        app.transcript.scroll_end(animate=False)

    def workspace_context(self) -> str:
        """The indexed files that look relevant. See :mod:`vox_chat.turn`."""
        return turn.workspace_context(self.app)

    def compact_if_needed(self) -> None:
        """Summarise the older turns before the window fills, not after.

        Runs on the worker thread, before the turn's own request: it is a
        real call to the model, and doing it here means the operator watches
        it happen rather than discovering an unexplained pause.
        """
        app = self.app
        generation = app.config.get("generation", {})
        messages = app.session.messages
        if not compaction.should_compact(
            messages,
            app.context_window(),
            float(generation.get("compact_at", 0.75)),
            bool(generation.get("compact", False)),
        ):
            return
        proposed = compaction.plan(messages)
        if not proposed.worth_doing:
            return
        app.call_from_thread(
            app.write_system, "COMPACTING - summarising the earlier turns..."
        )
        assert app.client is not None
        try:
            parts = [
                event.text
                for event in app.client.stream_chat(
                    compaction.request_for(proposed.older),
                    model=str(app.config.get("active_model", "")),
                    temperature=0.0,
                    max_tokens=800,
                    include_usage=False,
                )
                if event.type == "text"
            ]
        except LLMError as exc:
            # A summary that could not be written is not a reason to lose the
            # turn: the request goes out full-size, and fitting.py is still
            # there if the provider refuses it.
            app.call_from_thread(
                app.write_error, f"COMPACTION FAILED - {exc.message}; carrying on"
            )
            return
        summary = "".join(parts).strip()
        if not summary:
            return
        after = compaction.apply(summary, proposed)
        app.call_from_thread(self._replace_conversation, after, proposed)

    def _replace_conversation(
        self, after: list[Message], proposed: compaction.Plan
    ) -> None:
        """Swap the conversation for its compacted form, on the UI thread."""
        app = self.app
        app.session.messages[:] = after
        app.dirty = True
        app.write_system(compaction.describe(proposed, after))

    def run(self, temperature: float, max_tokens: int | None, model: str) -> None:
        """The turn itself, on the worker thread.

        Called by VoxApp.generate, which carries the @work decorator: that
        is Textual's business and stays with the widget. What happens
        inside the thread is this module's.
        """
        app = self.app
        assert app.client is not None
        # Before the request, not after a refusal: summarising the older
        # turns is what keeps the window from filling in the first place.
        self.compact_if_needed()
        agent_config = dict(app.config.get("agent", {}))
        role = app.role_store.get(str(app.config.get("active_role", "")))
        parameters = sampling.resolve(app.config, role)
        agent_enabled = bool(agent_config.get("enabled", False))
        if app.web_mode_active():
            # In web mode the operator has already asked for the internet.
            agent_config["confirm_web"] = False
        messages = self.build_request_messages()
        # Two shrinks at most: each one is a real request, and a window that
        # small is a problem to report rather than to keep working around.
        for _ in range(3):
            produced = False
            try:
                turn = run_turn(
                    client=app.client,
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    workspace=app.workspace,
                    agent_config=agent_config,
                    confirm=self.confirm_from_thread,
                    cancel=app.cancel_event,
                    agent_enabled=agent_enabled,
                    include_usage=bool(
                        app.config.get("generation", {}).get("include_usage", True)
                    ),
                    top_logprobs=app.inspect_top_k(),
                    web_settings=app.web_settings(),
                    mcp_registry=app.mcp if app.mcp.clients else None,
                    sampling=parameters.request_fields(),
                    native=parameters.native_fields(),
                    response_format=app.response_format,
                    undo=app.undo,
                    todos=app.todos,
                )
                # One hop to the UI thread per frame rather than per token.
                # The cost was never the hop: it was the transcript refresh
                # and re-layout it caused at the far end, once per token.
                for batch in coalesce.coalesce(turn):
                    produced = True
                    if batch.is_text:
                        app.call_from_thread(self.append_text, batch.text)
                    else:
                        app.call_from_thread(self.handle_event, batch.event)
            except LLMError as exc:
                if app._shutting_down:
                    return
                smaller = self._fit_to_window(exc, messages, produced)
                if smaller is not None:
                    app.call_from_thread(
                        app.write_system, fitting.describe(messages, smaller)
                    )
                    messages = smaller
                    continue
                log.warning("generation failed: %s", exc.kind)
                app.call_from_thread(self.finish, f"{exc.message}")
                return
            app.call_from_thread(self.finish, None)
            return
        app.call_from_thread(self.finish, "the prompt does not fit this model's window")
