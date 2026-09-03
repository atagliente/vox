"""Answering one question and getting out of the way.

`vox --ask "..."` is for the case the TUI is wrong for: a pipe, a script, a
key binding in an editor, a question asked from another program. It starts no
screen, draws nothing, and writes the answer to stdout and nothing else —
which is the whole contract, because anything else on stdout makes the output
unusable to whatever asked.

What it deliberately does not do:

- **No agent mode.** Confirming a write needs somebody to confirm it, and
  there is nobody here. A tool call would either hang waiting for a human or
  run unattended, and the second is not something to arrive at by accident.
- **No mesh, no web, no index.** Each is a thing that reaches out, and
  reaching out on somebody's behalf without a screen to say so is worse in a
  script than in a session.
- **No session written.** A one-shot answer is not a conversation, and
  leaving one on disk every time a shell script runs would fill the directory
  with things nobody meant to keep.

Errors go to stderr and the exit code, so `vox --ask ... || echo failed`
behaves the way anyone would expect.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from . import sampling
from .config import ConfigError, LoadedConfig, active_provider
from .llm_client import LLMClient, LLMError
from .models import Message
from .project import find as find_project_notes
from .roles import RoleStore


def ask(
    question: str,
    loaded: LoadedConfig,
    workspace: Path,
    *,
    stream: bool = True,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Answer one question. Returns the exit code.

    ``stream`` writes tokens as they arrive, which is what a person watching
    a pipe wants; a script capturing the output gets the same bytes either
    way.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    config = loaded.data

    try:
        provider = active_provider(config)
    except ConfigError as exc:
        # A traceback is the wrong answer to a misconfiguration when the
        # caller is a shell script: it gets a line and an exit code.
        print(f"vox: {exc}; run vox doctor", file=err)
        return 2
    if provider is None:
        print("vox: no provider configured; run vox doctor", file=err)
        return 2

    role = RoleStore().get(str(config.get("active_role", "")))
    messages: list[Message] = []
    if role is not None and role.system_prompt:
        messages.append(Message(role="system", content=role.system_prompt))
    notes = find_project_notes(workspace)
    if notes is not None:
        messages.append(Message(role="system", content=notes.as_prompt()))
    messages.append(Message(role="user", content=question))

    generation = config.get("generation", {})
    parameters = sampling.resolve(config, role)
    client = LLMClient.from_provider(provider)
    try:
        for event in client.stream_chat(
            messages,
            model=str(config.get("active_model", "")),
            temperature=float(
                role.temperature
                if role is not None
                else generation.get("temperature", 0.2)
            ),
            max_tokens=generation.get("max_tokens"),
            include_usage=False,
            sampling=parameters.request_fields(),
            native=parameters.native_fields(),
        ):
            # Reasoning is not the answer. It goes nowhere here: a script
            # asked a question, and a model's thinking arriving on stdout
            # would be the caller's problem to strip.
            if event.type == "text" and event.text:
                out.write(event.text)
                if stream:
                    out.flush()
    except LLMError as exc:
        print(f"vox: {exc.message}", file=err)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - a person, not a test
        print("", file=err)
        return 130
    finally:
        client.close()

    out.write("\n")
    out.flush()
    return 0
