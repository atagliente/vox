"""Filesystem layout and safe JSON persistence for VOX.

Everything that touches disk goes through here so that atomic writes and
corruption handling are implemented exactly once.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_DIR_NAME = ".vox"


def vox_home() -> Path:
    """Return the global VOX directory (``$VOX_HOME`` or ``~/.vox``)."""
    env = os.environ.get("VOX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / APP_DIR_NAME


def ensure_home() -> Path:
    home = vox_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def global_config_path() -> Path:
    return vox_home() / "config.json"


def roles_path() -> Path:
    return vox_home() / "roles.json"


def prompts_path() -> Path:
    return vox_home() / "prompts.json"


def sessions_dir() -> Path:
    return vox_home() / "sessions"


def logs_dir() -> Path:
    return vox_home() / "logs"


def project_dir(workspace: Path) -> Path:
    """Return the per-project config directory for ``workspace``."""
    return Path(workspace) / APP_DIR_NAME


def project_config_path(workspace: Path) -> Path:
    return project_dir(workspace) / "config.json"


@dataclass(frozen=True)
class ReadResult:
    """Outcome of a tolerant JSON read."""

    data: Any
    error: str | None = None
    quarantined: Path | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as UTF-8 JSON, replacing ``path`` atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def quarantine(path: Path) -> Path:
    """Move a corrupt file aside so the app can keep starting."""
    target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        os.replace(path, target)
    except OSError:
        return path
    return target


def read_json_safe(path: Path, default: Any) -> ReadResult:
    """Read JSON, never raising for a missing, unreadable or corrupt file."""
    path = Path(path)
    if not path.exists():
        return ReadResult(default)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ReadResult(default, f"cannot read {path.name}: {exc.strerror or exc}")
    try:
        return ReadResult(json.loads(text))
    except json.JSONDecodeError as exc:
        moved = quarantine(path)
        return ReadResult(
            default,
            f"invalid JSON in {path.name} at line {exc.lineno} column {exc.colno}: "
            f"{exc.msg}",
            moved,
        )
