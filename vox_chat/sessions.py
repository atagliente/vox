"""Session persistence under ``~/.vox/sessions/``.

Writes go through :func:`vox_chat.storage.write_json_atomic`; a corrupt file
is reported and skipped instead of blocking startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import Session
from .storage import read_json_safe, sessions_dir, write_json_atomic

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME = 64


def slugify(name: str) -> str:
    """Turn an arbitrary title into a safe file stem."""
    cleaned = _SAFE_NAME_RE.sub("-", name.strip()).strip("-.")
    cleaned = cleaned[:_MAX_NAME]
    return cleaned or "session"


@dataclass(frozen=True)
class SessionInfo:
    """Lightweight listing entry, cheap enough to build for every file."""

    name: str
    path: Path
    title: str
    updated_at: str
    model: str
    role: str
    message_count: int
    error: str | None = None


class SessionStore:
    """Save, load, list and delete sessions."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else sessions_dir()
        self.warnings: list[str] = []

    def path_for(self, name: str) -> Path:
        return self.directory / f"{slugify(name)}.json"

    def save(self, session: Session, name: str | None = None) -> Path:
        """Persist ``session``; returns the file it was written to."""
        session.touch()
        target = self.path_for(name or session.title or session.id)
        write_json_atomic(target, session.to_dict())
        return target

    def load(self, name: str) -> Session:
        """Load a session by name, raising for missing or corrupt files."""
        path = self.path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"no session named {name}")
        result = read_json_safe(path, None)
        if result.error is not None:
            raise ValueError(result.error)
        if not isinstance(result.data, dict):
            raise ValueError(f"session {name} is not a JSON object")
        return Session.from_dict(result.data)

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"no session named {name}")
        path.unlink()

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def list(self) -> list[SessionInfo]:
        """List every session, newest first, tolerating corrupt files."""
        if not self.directory.exists():
            return []
        entries: list[SessionInfo] = []
        for path in sorted(self.directory.glob("*.json")):
            result = read_json_safe(path, None)
            if result.error is not None or not isinstance(result.data, dict):
                message = result.error or "not a JSON object"
                self.warnings.append(f"{path.name}: {message}")
                entries.append(
                    SessionInfo(
                        name=path.stem,
                        path=path,
                        title=path.stem,
                        updated_at="",
                        model="",
                        role="",
                        message_count=0,
                        error=message,
                    )
                )
                continue
            data = result.data
            entries.append(
                SessionInfo(
                    name=path.stem,
                    path=path,
                    title=str(data.get("title") or path.stem),
                    updated_at=str(data.get("updated_at") or ""),
                    model=str(data.get("model") or ""),
                    role=str(data.get("role") or ""),
                    message_count=len(data.get("messages") or []),
                )
            )
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return entries
