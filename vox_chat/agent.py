"""The tool-calling loop used when coding-agent mode is on.

The loop is a generator so the UI stays in control: it consumes events, and
supplies a ``confirm`` callback that must return ``True`` before any write,
patch or command is executed.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .llm_client import LLMClient, LLMError, StreamEvent, supports_tools_error
from .models import Message, ToolCall, utc_now
from .tools import (
    COMMAND_TOOLS,
    TOOL_SCHEMAS,
    WEB_SCHEMAS,
    WEB_TOOLS,
    WRITE_TOOLS,
    ToolError,
    ToolResult,
    Workspace,
    describe_call,
    execute,
    truncate,
)
from .usage import TurnUsage, estimate_tokens

EventType = Literal[
    "text",
    "reasoning",
    "token",
    "tool_start",
    "tool_result",
    "tool_denied",
    "notice",
    "usage",
    "assistant_done",
    "cancelled",
    "limit",
]

ConfirmCallback = Callable[[str, str], bool]
"""Called with ``(tool_name, description)``; returns True to authorise."""


@dataclass
class AgentEvent:
    """Something the UI should render."""

    type: EventType
    text: str = ""
    tool_call: ToolCall | None = None
    messages: list[Message] = field(default_factory=list)
    usage: TurnUsage | None = None
    token: Any | None = None
    phase: str = ""


def parse_arguments(raw: str) -> dict[str, Any]:
    """Decode tool arguments, tolerating an empty string."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"invalid tool arguments: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ToolError("tool arguments must be a JSON object")
    return data


def needs_confirmation(name: str, agent_config: dict[str, Any]) -> bool:
    """Whether this tool must be authorised by the user before running."""
    if name in WRITE_TOOLS:
        return bool(agent_config.get("confirm_writes", True))
    if name in COMMAND_TOOLS:
        return bool(agent_config.get("confirm_commands", True))
    if name in WEB_TOOLS:
        # Reading is not writing: a search leaves the machine but changes
        # nothing on it, and /web on already said yes to that.
        return bool(agent_config.get("confirm_web", False))
    return False


def run_turn(
    client: LLMClient,
    messages: Sequence[Message],
    model: str,
    temperature: float,
    max_tokens: int | None,
    workspace: Workspace | None,
    agent_config: dict[str, Any],
    confirm: ConfirmCallback,
    cancel: threading.Event | None = None,
    agent_enabled: bool = False,
    include_usage: bool = True,
    top_logprobs: int | None = None,
    web_settings=None,
    mcp_registry=None,
    sampling: dict[str, Any] | None = None,
    native: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> Iterator[AgentEvent]:
    """Drive one user turn, including any tool cycles it triggers.

    Yields events as they happen, a ``usage`` event with the token and timing
    figures for the whole turn, and finishes with ``assistant_done`` carrying
    the new messages to append to the conversation.
    """
    history = list(messages)
    produced: list[Message] = []
    use_tools = bool(agent_enabled and workspace is not None)
    schemas = list(TOOL_SCHEMAS)
    if web_settings is not None and getattr(web_settings, "enabled", False):
        # The model is only told the internet exists when it is switched on.
        schemas += WEB_SCHEMAS
    if mcp_registry is not None:
        # Somebody else's tools, named by their server so two of them may
        # both offer "search" without the model having to guess.
        schemas += mcp_registry.schemas()
    max_cycles = int(agent_config.get("max_tool_cycles", 8))
    max_output = int(agent_config.get("max_output_bytes", 8192))
    command_timeout = int(agent_config.get("command_timeout_seconds", 60))

    started = time.monotonic()
    first_token_at: float | None = None
    reported_prompt = 0
    reported_completion = 0
    any_reported = False
    produced_text: list[str] = []

    def usage_event() -> AgentEvent:
        """Exact counts when the provider reported them, estimates otherwise."""
        elapsed = time.monotonic() - started
        latency = None if first_token_at is None else first_token_at - started
        if any_reported:
            return AgentEvent(
                "usage",
                usage=TurnUsage(
                    prompt_tokens=reported_prompt,
                    completion_tokens=reported_completion,
                    elapsed=elapsed,
                    estimated=False,
                    first_token_latency=latency,
                ),
            )
        prompt_text = "".join(message.content for message in messages)
        return AgentEvent(
            "usage",
            usage=TurnUsage(
                prompt_tokens=estimate_tokens(prompt_text),
                completion_tokens=estimate_tokens("".join(produced_text)),
                elapsed=elapsed,
                estimated=True,
                first_token_latency=latency,
            ),
        )

    for cycle in range(max_cycles + 1):
        if cancel is not None and cancel.is_set():
            yield usage_event()
            yield AgentEvent("cancelled", messages=produced)
            return

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        try:
            stream = client.stream_chat(
                history,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=schemas if use_tools else None,
                cancel=cancel,
                include_usage=include_usage,
                top_logprobs=top_logprobs,
                sampling=sampling,
                native=native,
                response_format=response_format,
            )
            for event in stream:
                if event.type == "text":
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    produced_text.append(event.text)
                elif event.type == "usage" and event.usage is not None:
                    any_reported = True
                    reported_prompt += event.usage.prompt_tokens
                    reported_completion += event.usage.completion_tokens
                yield from _relay(event, text_parts, reasoning_parts, tool_calls)
                if event.type == "cancelled":
                    yield usage_event()
                    yield AgentEvent("cancelled", messages=produced)
                    return
        except LLMError as exc:
            if use_tools and supports_tools_error(exc):
                use_tools = False
                yield AgentEvent(
                    "notice",
                    text=(
                        "MODEL DOES NOT SUPPORT TOOL CALLING - AGENT MODE "
                        "DISABLED FOR THIS TURN"
                    ),
                )
                continue
            raise

        assistant = Message(
            role="assistant",
            content="".join(text_parts),
            reasoning="".join(reasoning_parts),
            timestamp=utc_now(),
            tool_calls=tool_calls,
        )
        history.append(assistant)
        produced.append(assistant)

        if not tool_calls:
            yield usage_event()
            yield AgentEvent("assistant_done", messages=produced)
            return

        if cycle == max_cycles:
            yield AgentEvent(
                "limit",
                text=f"TOOL CYCLE LIMIT REACHED ({max_cycles}) - STOPPING",
            )
            yield usage_event()
            yield AgentEvent("assistant_done", messages=produced)
            return

        for call in tool_calls:
            if cancel is not None and cancel.is_set():
                yield usage_event()
                yield AgentEvent("cancelled", messages=produced)
                return
            result_message = _run_tool(
                call,
                workspace,
                agent_config,
                confirm,
                command_timeout,
                max_output,
                web_settings,
                mcp_registry,
            )
            history.append(result_message)
            produced.append(result_message)
            yield AgentEvent(
                "tool_result" if call.approved is not False else "tool_denied",
                text=result_message.content,
                tool_call=call,
            )

    yield usage_event()
    yield AgentEvent("assistant_done", messages=produced)


def _relay(
    event: StreamEvent,
    text_parts: list[str],
    reasoning_parts: list[str],
    tool_calls: list[ToolCall],
) -> Iterator[AgentEvent]:
    """Translate a stream event, accumulating the assistant message."""
    if event.type == "text":
        text_parts.append(event.text)
        yield AgentEvent("text", text=event.text)
    elif event.type == "reasoning":
        reasoning_parts.append(event.text)
        yield AgentEvent("reasoning", text=event.text)
    elif event.type == "token":
        yield AgentEvent("token", token=event.token, phase=event.phase)
    elif event.type == "tool_calls":
        tool_calls.extend(event.tool_calls)
        for call in event.tool_calls:
            yield AgentEvent("tool_start", tool_call=call)


def _run_mcp_tool(
    call: ToolCall,
    registry,
    confirm: ConfirmCallback,
    max_output: int,
) -> Message:
    """Run one call against an MCP server.

    Confirmed by the same modal as everything else, and by default confirmed
    even for reads: a local tool is code in this repository that a test
    covers, and an MCP tool is somebody else's program described by its own
    author. The registry decides; a tool the server itself marks destructive
    is confirmed whatever the configuration says.
    """
    from .mcp import McpError

    try:
        arguments = parse_arguments(call.arguments)
    except ToolError as exc:
        call.result = str(exc)
        return _tool_message(call)

    if registry.needs_confirmation(call.name) and not confirm(
        call.name, registry.describe_call(call.name, arguments)
    ):
        call.approved = False
        call.result = "user denied this operation"
        return _tool_message(call)
    call.approved = True

    try:
        ok, text = registry.call(call.name, arguments)
    except McpError as exc:
        call.result = f"error: {exc}"
        return _tool_message(call)
    body, cut = truncate(text or "(the tool returned nothing)", max_output)
    call.result = body + ("\n... truncated" if cut else "")
    if not ok:
        call.result = f"the tool reported a failure:\n{call.result}"
    return _tool_message(call)


def _run_tool(
    call: ToolCall,
    workspace: Workspace | None,
    agent_config: dict[str, Any],
    confirm: ConfirmCallback,
    command_timeout: int,
    max_output: int,
    web_settings=None,
    mcp_registry=None,
) -> Message:
    """Execute one tool call, asking for confirmation when required."""
    if mcp_registry is not None and mcp_registry.owns(call.name):
        return _run_mcp_tool(call, mcp_registry, confirm, max_output)
    if workspace is None and call.name not in WEB_TOOLS:
        call.result = "no workspace configured"
        call.approved = False
        return _tool_message(call)
    try:
        arguments = parse_arguments(call.arguments)
    except ToolError as exc:
        call.result = str(exc)
        return _tool_message(call)

    if needs_confirmation(call.name, agent_config):
        try:
            description = describe_call(call.name, arguments, workspace)
        except ToolError as exc:
            # Building the preview reads the workspace, so it can refuse a
            # path just as the tool itself would.
            call.approved = False
            call.result = f"error: {exc}"
            return _tool_message(call)
        if not confirm(call.name, description):
            call.approved = False
            call.result = "user denied this operation"
            return _tool_message(call)
    call.approved = True

    try:
        result: ToolResult = execute(
            call.name,
            arguments,
            workspace,
            command_timeout,
            max_output,
            web_settings,
        )
        call.result = result.as_text()
    except ToolError as exc:
        call.result = f"error: {exc}"
    except KeyError as exc:
        call.result = f"error: missing argument {exc}"
    return _tool_message(call)


def _tool_message(call: ToolCall) -> Message:
    return Message(
        role="tool",
        content=call.result or "",
        tool_call_id=call.id,
        name=call.name,
        timestamp=utc_now(),
    )
