"""Which commands may run, and which have to be asked about each time.

``confirm_commands`` was one switch for every command there is: on, and
``ls`` interrupts as often as ``rm -rf``; off, and nothing does. Neither is
what anyone wants, so both get turned off, which is the worst outcome
available.

Three levels instead, decided per command:

- **allow** — runs without asking. For the ones an operator would always say
  yes to, and would stop reading the dialog for.
- **ask** — confirmed, with the diff or the command line in front of them.
  The default, and what everything unlisted gets.
- **deny** — refused outright, without a dialog. For the ones where the
  answer is no and being asked at three in the morning is how it becomes yes.

Matching is on the **program**, not the command line. A rule about ``git``
that could be dodged by writing ``/usr/bin/git`` or ``GIT`` would be a rule
that only holds when nobody is trying, and the point of ``deny`` is the case
where something is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal

Level = Literal["allow", "ask", "deny"]
LEVELS: tuple[Level, ...] = ("allow", "ask", "deny")

# Nothing is allowed and nothing is denied out of the box. A default
# allowlist would be this file deciding what is safe on somebody else's
# machine, which it is in no position to know.
DEFAULT_RULES: dict[str, str] = {}


def program(command: str, argv: list[str] | None = None) -> str:
    """The name a rule is written against: the program, lowercased, unadorned.

    ``/usr/bin/git``, ``git.exe`` and ``GIT`` are all ``git``. A rule that
    those three could get past would only hold when nobody was trying.
    """
    first = (
        (argv[0] if argv else command.split()[0]) if (argv or command.split()) else ""
    )
    if not first:
        return ""
    name = PurePath(first).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


@dataclass(frozen=True)
class Policy:
    """The rules, and the level everything unlisted gets."""

    rules: dict[str, Level]
    default: Level = "ask"

    @classmethod
    def from_config(cls, agent: dict[str, Any] | None) -> Policy:
        """Read the ``commands`` block, falling back to ``confirm_commands``.

        The old switch still works and still means what it meant: it becomes
        the default level, so an existing configuration behaves exactly as it
        did before this module existed.
        """
        agent = agent if isinstance(agent, dict) else {}
        block = agent.get("commands")
        block = block if isinstance(block, dict) else {}
        rules: dict[str, Level] = {}
        for level in LEVELS:
            for name in block.get(level, []) or []:
                rules[program(str(name))] = level
        default: Level = "ask" if agent.get("confirm_commands", True) else "allow"
        configured = str(block.get("default", "")).lower()
        if configured in LEVELS:
            default = configured  # type: ignore[assignment]
        return cls(rules, default)

    def level_for(self, command: str, argv: list[str] | None = None) -> Level:
        return self.rules.get(program(command, argv), self.default)

    def describe(self) -> str:
        if not self.rules:
            return f"every command: {self.default}"
        by_level: dict[str, list[str]] = {}
        for name, level in sorted(self.rules.items()):
            by_level.setdefault(level, []).append(name)
        parts = [f"{level}: {', '.join(names)}" for level, names in by_level.items()]
        parts.append(f"everything else: {self.default}")
        return "  ·  ".join(parts)


def errors_in(agent: Any) -> list[str]:
    """What is wrong with the ``agent.commands`` block, for validate_config."""
    if not isinstance(agent, dict):
        return []
    block = agent.get("commands")
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["agent.commands must be an object"]
    found: list[str] = []
    for key, value in block.items():
        if key == "default":
            if str(value).lower() not in LEVELS:
                found.append(
                    f"agent.commands.default must be one of: {', '.join(LEVELS)}"
                )
            continue
        if key not in LEVELS:
            found.append(f"agent.commands.{key} is not one of: {', '.join(LEVELS)}")
            continue
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            found.append(f"agent.commands.{key} must be a list of program names")
    return found


# ------------------------------------------------------------------- limits


@dataclass(frozen=True)
class Limits:
    """What one command may consume besides time.

    Time was already capped. Memory and process count were not, so a command
    that allocates without bound takes the machine down with it, and one that
    forks in a loop takes it down faster — neither of which a timeout helps
    with, because the damage is done long before it fires.
    """

    memory_mb: int = 0
    processes: int = 0

    @classmethod
    def from_config(cls, agent: dict[str, Any] | None) -> Limits:
        agent = agent if isinstance(agent, dict) else {}
        return cls(
            memory_mb=int(agent.get("memory_limit_mb", 0) or 0),
            processes=int(agent.get("max_processes", 0) or 0),
        )

    @property
    def enforceable(self) -> bool:
        """Can these be applied here?

        POSIX only: ``resource.setrlimit`` in the child before it execs.
        Windows has job objects, which are a different mechanism and not one
        this reaches for — so on Windows the honest answer is that these are
        not applied, rather than a setting that quietly does nothing.
        """
        return os.name == "posix" and bool(self.memory_mb or self.processes)

    def preexec(self):  # pragma: no cover - POSIX only, exercised in CI
        """A callable for ``subprocess``'s ``preexec_fn``, or None."""
        if not self.enforceable:
            return None
        memory_mb, processes = self.memory_mb, self.processes

        def apply() -> None:
            import resource

            if memory_mb:
                limit = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            if processes:
                resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))

        return apply

    def describe(self) -> str:
        if not (self.memory_mb or self.processes):
            return "time only"
        parts = []
        if self.memory_mb:
            parts.append(f"{self.memory_mb} MB")
        if self.processes:
            parts.append(f"{self.processes} processes")
        if not self.enforceable:
            return ", ".join(parts) + " (not applied: POSIX only)"
        return ", ".join(parts)
