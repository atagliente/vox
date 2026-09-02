"""Persistent agent roles (``~/.vox/roles.json``).

The active role supplies the ``system`` message sent to the model.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Role
from .storage import read_json_safe, roles_path, write_json_atomic

DEFAULT_ROLES: tuple[dict[str, Any], ...] = (
    {
        "name": "general-assistant",
        "description": "General purpose assistant, short and concrete answers",
        "system_prompt": (
            "You are a concise technical assistant. Answer in the language "
            "the user writes in, get to the point, and never invent facts. "
            "If you do not know something, say so."
        ),
        "temperature": 0.4,
        "agent_enabled": False,
    },
    {
        "name": "python-developer",
        "description": "Pragmatic Python developer",
        "system_prompt": (
            "You are a senior Python engineer. Write readable Python 3.12 "
            "code with type hints and no unnecessary dependencies. Show only "
            "the code that is needed and explain your choices in a few "
            "lines. Never invent APIs that do not exist."
        ),
        "temperature": 0.2,
        "agent_enabled": False,
    },
    {
        "name": "code-reviewer",
        "description": "Strict but constructive code reviewer",
        "system_prompt": (
            "You are a code reviewer. Analyse the code you are given and "
            "report real bugs, security problems, race conditions and "
            "needless complexity. Order your findings by severity and "
            "propose a concrete fix for each one. Do not rewrite everything "
            "without a reason."
        ),
        "temperature": 0.1,
        "agent_enabled": False,
    },
    {
        "name": "embedded-developer",
        "description": "Firmware for microcontrollers",
        "system_prompt": (
            "You are a firmware developer for microcontrollers. Write "
            "compact C/C++ that respects memory, timing and interrupt "
            "constraints. Always state the hardware wiring you assume."
        ),
        "temperature": 0.2,
        "agent_enabled": False,
    },
    {
        "name": "debugging-assistant",
        "description": "Guided diagnosis of errors and stack traces",
        "system_prompt": (
            "You are a debugging assistant. Start from the symptoms and the "
            "stack trace, list hypotheses ordered by likelihood, and propose "
            "one minimal check for each. Ask for the data you are missing "
            "instead of guessing."
        ),
        "temperature": 0.2,
        "agent_enabled": False,
    },
)


class RoleStore:
    """CRUD over the roles file, seeded with the built-in roles."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else roles_path()
        self.warnings: list[str] = []
        self._roles: dict[str, Role] = {}
        self.load()

    def load(self) -> None:
        result = read_json_safe(self.path, None)
        if result.error is not None:
            self.warnings.append(result.error)
        raw = result.data
        if not isinstance(raw, list) or not raw:
            self._roles = {item["name"]: Role.from_dict(item) for item in DEFAULT_ROLES}
            self.save()
            return
        roles: dict[str, Role] = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                self.warnings.append("skipped a role entry without a name")
                continue
            try:
                role = Role.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                self.warnings.append(f"skipped invalid role: {exc}")
                continue
            roles[role.name] = role
        if not roles:
            roles = {item["name"]: Role.from_dict(item) for item in DEFAULT_ROLES}
        self._roles = roles

    def save(self) -> None:
        write_json_atomic(self.path, [role.to_dict() for role in self._roles.values()])

    def names(self) -> list[str]:
        return list(self._roles)

    def all(self) -> list[Role]:
        return list(self._roles.values())

    def get(self, name: str) -> Role | None:
        return self._roles.get(name)

    def require(self, name: str) -> Role:
        role = self._roles.get(name)
        if role is None:
            raise KeyError(f"unknown role: {name}")
        return role

    def create(self, role: Role) -> Role:
        if role.name in self._roles:
            raise KeyError(f"role already exists: {role.name}")
        self._roles[role.name] = role
        self.save()
        return role

    def update(self, current_name: str, **changes: Any) -> Role:
        role = self.require(current_name)
        for key, value in changes.items():
            if not hasattr(role, key):
                raise KeyError(f"unknown role field: {key}")
            setattr(role, key, value)
        if role.name != current_name:
            del self._roles[current_name]
        self._roles[role.name] = role
        self.save()
        return role

    def duplicate(self, name: str, new_name: str) -> Role:
        source = self.require(name)
        if new_name in self._roles:
            raise KeyError(f"role already exists: {new_name}")
        clone = Role(
            name=new_name,
            description=source.description,
            system_prompt=source.system_prompt,
            temperature=source.temperature,
            agent_enabled=source.agent_enabled,
        )
        self._roles[new_name] = clone
        self.save()
        return clone

    def delete(self, name: str) -> None:
        self.require(name)
        del self._roles[name]
        self.save()

    def search(self, query: str) -> list[Role]:
        needle = query.strip().lower()
        if not needle:
            return self.all()
        return [
            role
            for role in self._roles.values()
            if needle in role.name.lower() or needle in role.description.lower()
        ]

    def __len__(self) -> int:
        return len(self._roles)

    def __contains__(self, name: object) -> bool:
        return name in self._roles

    def __iter__(self) -> Iterable[Role]:
        return iter(self._roles.values())
