"""Running a confirmed command somewhere it can do less harm.

VOX already refuses paths outside the workspace, runs without a shell, caps
memory and process count, and asks before every command. What none of that
stops is a command that is *allowed* doing something outside the workspace on
purpose — `pip install`, a test suite that writes to the home directory, a
build that phones home. Confinement of the arguments is not confinement of the
process.

A sandbox is the thing that changes that, and it changes the promise the agent
makes, so it is **off by default and asked for by name**. Two backends,
because the honest answer to "which one" is "it depends on the machine":

- **`bwrap`** (bubblewrap) — Linux only, no daemon, no images, present on most
  desktop distributions already. The workspace is bound writable, the rest of
  the filesystem read-only or not there at all, and the network is gone unless
  asked for.
- **`docker`** — everywhere Docker is, at the price of an image, a daemon, and
  a container start per command. The workspace is a bind mount, `/workspace`
  inside, and the container is removed afterwards.

The rule that keeps this from being worse than nothing: **if a sandbox was
asked for and cannot be used, the command does not run.** Falling back to an
unsandboxed run would mean the setting silently stops applying on exactly the
machine where it was needed, which is how a security feature becomes a lie.
That is also why there is no autodetection — `agent.sandbox` naming a backend
is a decision, and a program guessing "docker is installed, so you probably
meant it" is a program making that decision for you.

Neither backend is a boundary against a determined adversary; both are a
boundary against a mistake. The distinction is written down in SECURITY.md
rather than implied here.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODES = ("off", "bwrap", "docker")

DEFAULT_IMAGE = "python:3.12-slim"

# Read-only for anything the command might need to *be* — an interpreter, a
# libc, a certificate bundle. Anything absent is skipped rather than being an
# error, because these differ between distributions and a missing /lib64 on a
# machine that has no /lib64 is not a problem.
BWRAP_READONLY = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/resolv.conf",
)


class SandboxError(Exception):
    """A sandbox was asked for and cannot be provided."""


@dataclass(frozen=True)
class Sandbox:
    """Which backend, and what it is allowed to reach."""

    mode: str = "off"
    network: bool = False
    image: str = DEFAULT_IMAGE

    @classmethod
    def from_config(cls, agent: dict[str, Any] | None) -> Sandbox:
        agent = agent if isinstance(agent, dict) else {}
        block = agent.get("sandbox")
        if isinstance(block, str):  # sandbox: "bwrap" as shorthand
            block = {"mode": block}
        block = block if isinstance(block, dict) else {}
        mode = str(block.get("mode", "off")).strip().lower()
        if mode not in MODES:
            mode = "off"
        return cls(
            mode=mode,
            network=bool(block.get("network", False)),
            image=str(block.get("image", DEFAULT_IMAGE)) or DEFAULT_IMAGE,
        )

    @property
    def on(self) -> bool:
        return self.mode != "off"

    def describe(self) -> str:
        if not self.on:
            return "no sandbox"
        net = "network allowed" if self.network else "no network"
        where = f" ({self.image})" if self.mode == "docker" else ""
        return f"{self.mode}{where}, {net}"

    # ------------------------------------------------------------- checking

    def unavailable(self) -> str | None:
        """Why this sandbox cannot be used here, or None if it can."""
        if not self.on:
            return None
        if shutil.which(self.mode) is None:
            return (
                f"agent.sandbox asks for {self.mode}, which is not on the PATH. "
                "The command was not run: a sandbox that silently stops "
                "applying is worse than none."
            )
        if self.mode == "bwrap" and os.name != "posix":
            return (
                "agent.sandbox asks for bwrap, which is Linux only. "
                "The command was not run."
            )
        return None

    # -------------------------------------------------------------- wrapping

    def wrap(self, argv: list[str], directory: Path, root: Path) -> list[str]:
        """The command line that runs ``argv`` inside the sandbox.

        ``root`` is the workspace — the one writable place — and ``directory``
        is where within it the command starts, which is already resolved
        inside ``root`` by the caller.
        """
        problem = self.unavailable()
        if problem is not None:
            raise SandboxError(problem)
        if self.mode == "bwrap":
            return self._bwrap(argv, directory, root)
        if self.mode == "docker":
            return self._docker(argv, directory, root)
        return argv

    def _bwrap(self, argv: list[str], directory: Path, root: Path) -> list[str]:
        line = [
            "bwrap",
            # Without this a command that outlives the request keeps running
            # in a namespace nobody is watching.
            "--die-with-parent",
            "--unshare-all",
        ]
        if self.network:
            line.append("--share-net")
        for path in BWRAP_READONLY:
            if Path(path).exists():
                line += ["--ro-bind", path, path]
        line += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            # The one writable place, at the same path it has outside, so
            # anything the command prints about a file is a path that means
            # something here too.
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(directory),
            "--new-session",  # no terminal to inject keystrokes into
            "--",
        ]
        return line + argv

    def _docker(self, argv: list[str], directory: Path, root: Path) -> list[str]:
        inside = Path("/workspace") / directory.relative_to(root)
        line = [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if self.network else "none",
            # Not root inside the container: a file the command creates should
            # belong to the person who ran VOX, not to somebody who has to
            # chown it afterwards.
            *(
                ["--user", f"{os.getuid()}:{os.getgid()}"]
                if hasattr(os, "getuid")
                else []
            ),
            "-v",
            f"{root}:/workspace",
            "-w",
            inside.as_posix(),
            self.image,
        ]
        return line + argv


def errors_in(agent: Any) -> list[str]:
    """What is wrong with the ``agent.sandbox`` block, for validate_config.

    A misspelt mode is an error here rather than a silent "off", because the
    two readings of ``sandbox: bwarp`` are "you meant bubblewrap" and "you
    asked for no sandbox at all", and only one of them is safe to guess.
    """
    if not isinstance(agent, dict):
        return []
    block = agent.get("sandbox")
    if block is None:
        return []
    if isinstance(block, str):
        block = {"mode": block}
    if not isinstance(block, dict):
        return ["agent.sandbox must be an object or one of: " + ", ".join(MODES)]
    found: list[str] = []
    mode = block.get("mode", "off")
    if not isinstance(mode, str) or mode.strip().lower() not in MODES:
        found.append("agent.sandbox.mode must be one of: " + ", ".join(MODES))
    if not isinstance(block.get("network", False), bool):
        found.append("agent.sandbox.network must be a boolean")
    image = block.get("image", DEFAULT_IMAGE)
    if not isinstance(image, str) or not image.strip():
        found.append("agent.sandbox.image must be a non-empty string")
    for key in block:
        if key not in ("mode", "network", "image"):
            found.append(f"agent.sandbox.{key} is not a setting")
    return found
