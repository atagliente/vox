"""Serializable domain objects shared by the UI, the client and the stores."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

MessageRole = Literal[
    # "peer" is another agent's answer and "web" is what a search returned.
    # Neither is a role any provider accepts: build_request_messages folds them
    # into system messages, and to_api refuses to send them raw.
    "system", "user", "assistant", "tool", "error", "reasoning", "peer", "web"
]


def utc_now() -> str:
    """ISO-8601 timestamp in UTC, used for every persisted date."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: str = ""
    result: str | None = None
    approved: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data.get("id", new_id())),
            name=str(data.get("name", "")),
            arguments=str(data.get("arguments", "")),
            result=data.get("result"),
            approved=data.get("approved"),
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class Message:
    """One entry of the conversation.

    ``error`` is a UI-only role: it is displayed but never sent to the model.
    """

    role: MessageRole
    content: str = ""
    reasoning: str = ""
    timestamp: str = field(default_factory=utc_now)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }
        if self.reasoning:
            data["reasoning"] = self.reasoning
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=data.get("role", "user"),
            content=data.get("content") or "",
            reasoning=data.get("reasoning") or "",
            timestamp=data.get("timestamp") or utc_now(),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )

    def to_api(self) -> dict[str, Any] | None:
        """Render this message for the chat completions API.

        Returns ``None`` for messages that must not be sent upstream.
        Reasoning is deliberately left out: it is kept for the operator, not
        replayed to the model.
        """
        if self.role in ("error", "reasoning", "peer", "web"):
            return None
        if self.role == "tool":
            # A tool message answers a tool call. Without an id it answers
            # nothing, and a provider is right to reject the whole request —
            # which is what it did.
            if not self.tool_call_id:
                return None
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id,
            }
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_api() for call in self.tool_calls]
        return payload


@dataclass
class Role:
    """An agent persona; the active one supplies the ``system`` message."""

    name: str
    description: str = ""
    system_prompt: str = ""
    temperature: float = 0.2
    agent_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Role:
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            system_prompt=str(data.get("system_prompt", "")),
            temperature=float(data.get("temperature", 0.2)),
            agent_enabled=bool(data.get("agent_enabled", False)),
        )


@dataclass
class Prompt:
    """A reusable prompt template with ``{{variable}}`` placeholders."""

    name: str
    content: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Prompt:
        return cls(
            name=str(data["name"]),
            content=str(data.get("content", "")),
            description=str(data.get("description", "")),
            tags=[str(t) for t in data.get("tags", [])],
            updated_at=str(data.get("updated_at") or utc_now()),
        )


@dataclass
class Session:
    """A full conversation plus the context it was produced in."""

    id: str = field(default_factory=new_id)
    title: str = "untitled"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    provider: str = ""
    model: str = ""
    role: str = ""
    workspace: str = ""
    agent_enabled: bool = False
    messages: list[Message] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "workspace": self.workspace,
            "agent_enabled": self.agent_enabled,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data.get("id") or new_id()),
            title=str(data.get("title") or "untitled"),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            role=str(data.get("role", "")),
            workspace=str(data.get("workspace", "")),
            agent_enabled=bool(data.get("agent_enabled", False)),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
        )
