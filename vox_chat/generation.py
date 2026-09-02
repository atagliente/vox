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

from . import fitting, sampling
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
        """Prepend the active role system prompt to the conversation."""
        app = self.app
        role = app.role_store.get(str(app.config.get("active_role", "")))
        messages: list[Message] = []
        if role is not None and role.system_prompt:
            messages.append(Message(role="system", content=role.system_prompt))
        # "peer" and "error" are ours, not roles any provider accepts. The peer
        # answers are folded into one system message just before the question
        # they belong to, by start_consensus.
        history: list[Message] = []
        for message in app.session.messages:
            if message.role in ("error", "peer"):
                continue
            if message.role == "web":
                # What a search found is context, and "web" is not a role any
                # provider knows.
                history.append(Message(role="system", content=message.content))
                continue
            history.append(message)

        research = [
            Message(role="system", content=prompt)
            for prompt in (app._web_prompt, app._consensus_prompt)
            if prompt
        ]
        # Everything gathered for this turn belongs in front of the question it
        # was gathered for. The citations are appended to the session after the
        # question, so without this they — and the sources — arrive after it,
        # and a model reads that as "answer, then here is some reading".
        last_user = max(
            (index for index, message in enumerate(history) if message.role == "user"),
            default=None,
        )
        if last_user is not None:
            trailing = history[last_user + 1 :]
            del history[last_user + 1 :]
            history[last_user:last_user] = research + trailing
        else:
            history.extend(research)
        messages.extend(history)
        return messages

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
        app._consensus_prompt = ""
        app._web_prompt = ""
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

    def run(self, temperature: float, max_tokens: int | None, model: str) -> None:
        """The turn itself, on the worker thread.

        Called by VoxApp.generate, which carries the @work decorator: that
        is Textual's business and stays with the widget. What happens
        inside the thread is this module's.
        """
        app = self.app
        assert app.client is not None
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
                for event in run_turn(
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
                ):
                    produced = True
                    app.call_from_thread(self.handle_event, event)
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
