"""Persistent prompt library (``~/.vox/prompts.json``).

Templates use ``{{name}}`` placeholders. Substitution is a plain regex
replacement: no ``eval``, no code execution, ever.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Prompt, utc_now
from .storage import prompts_path, read_json_safe, write_json_atomic

VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")

BUILTIN_VARIABLES = ("workspace", "file", "selection")

DEFAULT_PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "name": "explain-file",
        "description": "Ask for a structured explanation of a file",
        "content": (
            "Workspace: {{workspace}}\n"
            "File: {{file}}\n\n"
            "Explain what this file does, what it is responsible for and "
            "where its fragile points are."
        ),
        "tags": ["analysis"],
    },
    {
        "name": "review-selection",
        "description": "Focused review of the current selection",
        "content": (
            "Review this code and report bugs, risks and possible "
            "simplifications, ordered by severity:\n\n{{selection}}"
        ),
        "tags": ["review"],
    },
    {
        "name": "write-tests",
        "description": "Generate pytest tests for the selection",
        "content": (
            "Write focused pytest tests for the code below. Cover the edge "
            "cases and do not use the network or the real filesystem:"
            "\n\n{{selection}}"
        ),
        "tags": ["test"],
    },
)


class MissingVariables(Exception):
    """Raised when a template still has unresolved placeholders."""

    def __init__(self, names: list[str]) -> None:
        super().__init__("missing values for: " + ", ".join(names))
        self.names = names


def find_variables(text: str) -> list[str]:
    """Return the distinct variable names used by ``text``, in order."""
    seen: list[str] = []
    for match in VARIABLE_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def render(text: str, values: Mapping[str, str], strict: bool = True) -> str:
    """Substitute ``{{name}}`` placeholders with ``values``.

    With ``strict`` the call raises :class:`MissingVariables` when a
    placeholder has no value; otherwise unknown placeholders are left as-is.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        if name not in missing:
            missing.append(name)
        return match.group(0)

    rendered = VARIABLE_RE.sub(replace, text)
    if missing and strict:
        raise MissingVariables(missing)
    return rendered


class PromptStore:
    """CRUD over the prompt library."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else prompts_path()
        self.warnings: list[str] = []
        self._prompts: dict[str, Prompt] = {}
        self.load()

    def load(self) -> None:
        result = read_json_safe(self.path, None)
        if result.error is not None:
            self.warnings.append(result.error)
        raw = result.data
        if not isinstance(raw, list):
            self._prompts = {
                item["name"]: Prompt.from_dict(item) for item in DEFAULT_PROMPTS
            }
            self.save()
            return
        prompts: dict[str, Prompt] = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                self.warnings.append("skipped a prompt entry without a name")
                continue
            try:
                prompt = Prompt.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                self.warnings.append(f"skipped invalid prompt: {exc}")
                continue
            prompts[prompt.name] = prompt
        self._prompts = prompts

    def save(self) -> None:
        write_json_atomic(
            self.path, [prompt.to_dict() for prompt in self._prompts.values()]
        )

    def names(self) -> list[str]:
        return list(self._prompts)

    def all(self) -> list[Prompt]:
        return list(self._prompts.values())

    def get(self, name: str) -> Prompt | None:
        return self._prompts.get(name)

    def require(self, name: str) -> Prompt:
        prompt = self._prompts.get(name)
        if prompt is None:
            raise KeyError(f"unknown prompt: {name}")
        return prompt

    def save_prompt(
        self,
        name: str,
        content: str,
        description: str = "",
        tags: list[str] | None = None,
    ) -> Prompt:
        """Create or overwrite a prompt with the given name."""
        prompt = Prompt(
            name=name,
            content=content,
            description=description,
            tags=list(tags or []),
            updated_at=utc_now(),
        )
        self._prompts[name] = prompt
        self.save()
        return prompt

    def update(self, current_name: str, **changes: Any) -> Prompt:
        prompt = self.require(current_name)
        for key, value in changes.items():
            if not hasattr(prompt, key):
                raise KeyError(f"unknown prompt field: {key}")
            setattr(prompt, key, value)
        prompt.updated_at = utc_now()
        if prompt.name != current_name:
            del self._prompts[current_name]
        self._prompts[prompt.name] = prompt
        self.save()
        return prompt

    def rename(self, name: str, new_name: str) -> Prompt:
        if new_name in self._prompts:
            raise KeyError(f"prompt already exists: {new_name}")
        return self.update(name, name=new_name)

    def duplicate(self, name: str, new_name: str) -> Prompt:
        source = self.require(name)
        if new_name in self._prompts:
            raise KeyError(f"prompt already exists: {new_name}")
        return self.save_prompt(
            new_name, source.content, source.description, list(source.tags)
        )

    def delete(self, name: str) -> None:
        self.require(name)
        del self._prompts[name]
        self.save()

    def search(self, query: str) -> list[Prompt]:
        """Match by name, description or tag."""
        needle = query.strip().lower()
        if not needle:
            return self.all()
        found: list[Prompt] = []
        for prompt in self._prompts.values():
            haystack = [prompt.name.lower(), prompt.description.lower()]
            haystack.extend(tag.lower() for tag in prompt.tags)
            if any(needle in item for item in haystack):
                found.append(prompt)
        return found

    def __len__(self) -> int:
        return len(self._prompts)

    def __contains__(self, name: object) -> bool:
        return name in self._prompts

    def __iter__(self) -> Iterable[Prompt]:
        return iter(self._prompts.values())
