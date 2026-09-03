"""Assembling what gets sent, for whichever host is asking.

The list of messages a turn sends is not a drawing decision, but it lived
inside `GenerationController`, which is one — that class owns the assistant's
message box, Textual's thread hand-offs and the confirmation modal. A second
host that only wants to know *what to send* would have had to copy this or
inherit a screen.

So the two functions that answer that question live here instead, and both
hosts call them. The rules they carry are the interesting part and are worth
stating once:

- The role's system prompt goes first, then the workspace context, then the
  project's own notes. The role decides how to behave; the notes describe the
  codebase; behaviour outranks description.
- `peer` and `web` are VOX's roles, not any provider's. A `web` message is
  context and becomes a system message; a `peer` answer is folded in by the
  consensus flow and is dropped here.
- Everything gathered *for* this turn — a search, a round of peers — is moved
  to sit in front of the question that caused it. Without that it arrives
  after, and a model reads "answer, then here is some reading".
"""

from __future__ import annotations

from typing import Any

from . import indexing
from .models import Message


def workspace_context(host: Any) -> str:
    """The indexed files that look relevant to the question being asked.

    Only when the index is switched on, and never at the cost of the turn: if
    the embedding server is not there the question goes out on its own, which
    is what it did before the index existed.
    """
    settings = host.index_settings()
    if not settings.get("enabled"):
        return ""
    question = next(
        (
            message.content
            for message in reversed(host.session.messages)
            if message.role == "user" and message.content
        ),
        "",
    )
    hits = host.relevant_chunks(question)
    if not hits:
        return ""
    block = indexing.render(hits)
    limit = int(settings.get("max_chars", 6000))
    if len(block) > limit:
        block = block[:limit] + "\n[trimmed to fit]"
    return block


def build_request_messages(host: Any) -> list[Message]:
    """The conversation as the provider will receive it."""
    role = host.role_store.get(str(host.config.get("active_role", "")))
    messages: list[Message] = []
    if role is not None and role.system_prompt:
        messages.append(Message(role="system", content=role.system_prompt))
    relevant = workspace_context(host)
    if relevant:
        messages.append(Message(role="system", content=relevant))
    if host.project_notes is not None:
        messages.append(Message(role="system", content=host.project_notes.as_prompt()))

    history: list[Message] = []
    for message in host.session.messages:
        if message.role in ("error", "peer"):
            continue
        if message.role == "web":
            history.append(Message(role="system", content=message.content))
            continue
        history.append(message)

    research = [
        Message(role="system", content=prompt)
        for prompt in (host.web_prompt, host.consensus_prompt)
        if prompt
    ]
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
