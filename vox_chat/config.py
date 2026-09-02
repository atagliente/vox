"""Two-level configuration: global (``~/.vox``) and per-project (``.vox``).

The project file overrides only the keys it declares; everything else is
inherited from the global file through a predictable recursive merge.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import (
    ReadResult,
    global_config_path,
    project_config_path,
    read_json_safe,
    write_json_atomic,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "active_provider": "local-ollama",
    "active_model": "qwen2.5-coder:3b",
    "active_role": "python-developer",
    "providers": {
        "local-ollama": {
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "timeout_seconds": 600,
            "models": ["qwen2.5-coder:1.5b", "qwen2.5-coder:3b"],
        }
    },
    "generation": {
        "temperature": 0.2,
        "max_tokens": 1800,
        "context_window": 8192,
        "include_usage": True,
        "preload": True,
        "preload_timeout_seconds": 180,
    },
    "agent": {
        "enabled": False,
        "confirm_writes": True,
        "confirm_commands": True,
        "confirm_web": True,
        "command_timeout_seconds": 60,
        "max_tool_cycles": 8,
        "max_output_bytes": 8192,
    },
    "inspect": {
        "enabled": False,
        "top_k": 5,
        "entropy_threshold": 1.0,
        "margin_threshold": 0.35,
        "min_distance": 3,
        "skip_punctuation": True,
    },
    "mesh": {
        "enabled": False,
        "agent_id": "",
        "name": "vox",
        "verbs": ["infer"],
        "announce_interval": 60.0,
        "group": "239.17.42.1",
        "port": 45177,
        "pki_dir": "",
        "auto_provision": True,
        # On by default, so a fresh install joins a mesh with no setup. The
        # sample authority's private key is public; /mesh new-ca turns this off
        # and generates one only this machine holds.
        "demo_ca": True,
    },
    "consensus": {
        "enabled": True,
        "verb": "infer",
        "max_peers": 5,
        "ask_timeout_seconds": 90.0,
        "max_question_chars": 4000,
        "answer_requests": True,
        "answer_max_tokens": 512,
        "quorum": 2,
        # The sample authority is public, so a round on it is readable by
        # anyone on the segment running VOX. Allowed, and said out loud every
        # time; set this to false to refuse instead.
        "allow_sample_ca": True,
    },
    "web": {
        "enabled": False,
        "provider": "searxng",
        "endpoint": "http://localhost:8888",
        "api_key": "",
        "max_results": 5,
        "timeout_seconds": 15.0,
        "fetch_max_bytes": 409600,
        "allow_fetch": True,
        "allow_private_addresses": False,
        "language": "",
    },
    "ui": {
        "theme": "nasa",
        "show_timestamps": True,
        "show_usage": True,
        "show_reasoning": True,
        "code_panel": True,
        "logo": "frame",
        "splash": True,
    },
}

# Legacy names from 0.1.0 configuration files stay valid.
_LOGOS = {"frame", "bar", "none", "norad"}
_THEMES = {"nasa", "dark", "light", "wopr"}


class ConfigError(Exception):
    """Raised for malformed configuration text or structure."""

    def __init__(self, message: str, line: int | None = None,
                 column: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column

    def __str__(self) -> str:
        if self.line is not None and self.column is not None:
            return f"{self.message} (line {self.line}, column {self.column})"
        return self.message


@dataclass
class LoadedConfig:
    """The effective configuration plus where it came from."""

    data: dict[str, Any]
    global_path: Path
    project_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


def default_config() -> dict[str, Any]:
    """A fresh, independent copy of the built-in defaults."""
    return copy.deepcopy(DEFAULT_CONFIG)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Only keys present in ``override`` are replaced; nested dicts are merged,
    every other value (lists included) is overwritten wholesale.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate_config(data: Any) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["configuration root must be a JSON object"]

    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("providers must be a non-empty object")
        providers = {}
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            errors.append(f"provider {name}: must be an object")
            continue
        base_url = provider.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            errors.append(f"provider {name}: base_url must be an http(s) URL")
        if not isinstance(provider.get("api_key", ""), str):
            errors.append(f"provider {name}: api_key must be a string")
        timeout = provider.get("timeout_seconds", 600)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append(f"provider {name}: timeout_seconds must be a positive number")
        if not isinstance(provider.get("extra_body", {}), dict):
            errors.append(f"provider {name}: extra_body must be an object")
        models = provider.get("models", [])
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            errors.append(f"provider {name}: models must be a list of strings")

    active = data.get("active_provider")
    if not isinstance(active, str) or (providers and active not in providers):
        errors.append("active_provider must name one of the configured providers")
    if not isinstance(data.get("active_model", ""), str):
        errors.append("active_model must be a string")
    if not isinstance(data.get("active_role", ""), str):
        errors.append("active_role must be a string")

    generation = data.get("generation", {})
    if not isinstance(generation, dict):
        errors.append("generation must be an object")
    else:
        temperature = generation.get("temperature", 0.2)
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            errors.append("generation.temperature must be a number between 0 and 2")
        elif not 0 <= temperature <= 2:
            errors.append("generation.temperature must be a number between 0 and 2")
        window = generation.get("context_window", 8192)
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            errors.append("generation.context_window must be a positive integer")
        for key in ("include_usage", "preload"):
            if not isinstance(generation.get(key, True), bool):
                errors.append(f"generation.{key} must be a boolean")
        preload_timeout = generation.get("preload_timeout_seconds", 180)
        if (not isinstance(preload_timeout, int) or isinstance(preload_timeout, bool)
                or preload_timeout <= 0):
            errors.append("generation.preload_timeout_seconds must be a positive integer")
        max_tokens = generation.get("max_tokens", 1800)
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
                errors.append("generation.max_tokens must be a positive integer or null")

    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        errors.append("agent must be an object")
    else:
        for key in ("enabled", "confirm_writes", "confirm_commands", "confirm_web"):
            if not isinstance(agent.get(key, False), bool):
                errors.append(f"agent.{key} must be a boolean")
        for key in ("command_timeout_seconds", "max_tool_cycles", "max_output_bytes"):
            value = agent.get(key, 1)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"agent.{key} must be a positive integer")

    inspection = data.get("inspect", {})
    if not isinstance(inspection, dict):
        errors.append("inspect must be an object")
    else:
        for key in ("enabled", "skip_punctuation"):
            if not isinstance(inspection.get(key, False), bool):
                errors.append(f"inspect.{key} must be a boolean")
        top_k = inspection.get("top_k", 5)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
            # The endpoint refuses anything above 20 with HTTP 400.
            errors.append("inspect.top_k must be an integer between 1 and 20")
        for key in ("entropy_threshold", "margin_threshold"):
            value = inspection.get(key, 1.0)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                errors.append(f"inspect.{key} must be a non-negative number")
        distance = inspection.get("min_distance", 3)
        if not isinstance(distance, int) or isinstance(distance, bool) or distance < 0:
            errors.append("inspect.min_distance must be a non-negative integer")

    mesh = data.get("mesh", {})
    if not isinstance(mesh, dict):
        errors.append("mesh must be an object")
    else:
        for key in ("enabled", "auto_provision", "demo_ca"):
            if not isinstance(mesh.get(key, False), bool):
                errors.append(f"mesh.{key} must be a boolean")
        for key in ("agent_id", "name", "group", "pki_dir"):
            if not isinstance(mesh.get(key, ""), str):
                errors.append(f"mesh.{key} must be a string")
        # A configuration written before the mesh existed simply has no block;
        # each key falls back to its default rather than being reported missing.
        verbs = mesh.get("verbs", ["infer"])
        if not isinstance(verbs, list) or not all(isinstance(v, str) for v in verbs):
            errors.append("mesh.verbs must be a list of strings")
        elif not verbs:
            errors.append("mesh.verbs must name at least one verb")
        interval = mesh.get("announce_interval", 60.0)
        if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
                or interval <= 0):
            errors.append("mesh.announce_interval must be a positive number")
        port = mesh.get("port", 45177)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append("mesh.port must be a port number")
        # The announce group has to be multicast, or nobody hears it.
        group = mesh.get("group", "239.17.42.1")
        if isinstance(group, str):
            first = group.split(".")[0]
            if not first.isdigit() or not 224 <= int(first) <= 239:
                errors.append("mesh.group must be a multicast address (224-239.x.x.x)")

    web = data.get("web", {})
    if not isinstance(web, dict):
        errors.append("web must be an object")
    else:
        for key in ("enabled", "allow_fetch", "allow_private_addresses"):
            if not isinstance(web.get(key, False), bool):
                errors.append(f"web.{key} must be a boolean")
        provider = web.get("provider", "searxng")
        if provider not in ("searxng", "brave"):
            errors.append("web.provider must be searxng or brave")
        for key in ("endpoint", "api_key", "language"):
            if not isinstance(web.get(key, ""), str):
                errors.append(f"web.{key} must be a string")
        for key, default in (("max_results", 5), ("fetch_max_bytes", 409600)):
            value = web.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"web.{key} must be a positive whole number")
        timeout = web.get("timeout_seconds", 15.0)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or timeout <= 0):
            errors.append("web.timeout_seconds must be a positive number")

    consensus = data.get("consensus", {})
    if not isinstance(consensus, dict):
        errors.append("consensus must be an object")
    else:
        for key in ("enabled", "answer_requests", "allow_sample_ca"):
            if not isinstance(consensus.get(key, False), bool):
                errors.append(f"consensus.{key} must be a boolean")
        verb = consensus.get("verb", "infer")
        if not isinstance(verb, str) or not verb.strip():
            errors.append("consensus.verb must be a non-empty string")
        for key, default in (
            ("max_peers", 5),
            ("max_question_chars", 4000),
            ("answer_max_tokens", 512),
            ("quorum", 2),
        ):
            value = consensus.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"consensus.{key} must be a positive whole number")
        timeout = consensus.get("ask_timeout_seconds", 90.0)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or timeout <= 0):
            errors.append("consensus.ask_timeout_seconds must be a positive number")

    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        errors.append("ui must be an object")
    else:
        if ui.get("theme", "nasa") not in _THEMES:
            errors.append(f"ui.theme must be one of {sorted(_THEMES)}")
        if ui.get("logo", "frame") not in _LOGOS:
            errors.append(f"ui.logo must be one of {sorted(_LOGOS)}")
        for key in (
            "show_timestamps", "splash", "show_usage", "show_reasoning",
            "code_panel",
        ):
            if not isinstance(ui.get(key, True), bool):
                errors.append(f"ui.{key} must be a boolean")
    return errors


def parse_config_text(text: str) -> dict[str, Any]:
    """Parse and validate configuration text, raising :class:`ConfigError`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON: {exc.msg}", exc.lineno, exc.colno) from exc
    errors = validate_config(data)
    if errors:
        raise ConfigError("; ".join(errors))
    return data


def ensure_global_config() -> tuple[dict[str, Any], list[str]]:
    """Load the global config, creating a valid default file on first run."""
    path = global_config_path()
    warnings: list[str] = []
    if not path.exists():
        write_json_atomic(path, default_config())
        return default_config(), warnings
    result: ReadResult = read_json_safe(path, None)
    if result.error is not None:
        warnings.append(result.error)
        if result.quarantined is not None:
            warnings.append(f"previous file kept as {result.quarantined.name}")
        write_json_atomic(path, default_config())
        return default_config(), warnings
    errors = validate_config(result.data)
    if errors:
        warnings.append("global config rejected: " + "; ".join(errors))
        return default_config(), warnings
    return deep_merge(default_config(), result.data), warnings


def load_config(workspace: Path | None = None) -> LoadedConfig:
    """Merge defaults, the global file and the project file, in that order."""
    data, warnings = ensure_global_config()
    project_path: Path | None = None
    if workspace is not None:
        candidate = project_config_path(Path(workspace))
        if candidate.exists():
            result = read_json_safe(candidate, None)
            if result.error is not None:
                warnings.append(result.error)
            elif not isinstance(result.data, dict):
                warnings.append("project config rejected: root must be a JSON object")
            else:
                merged = deep_merge(data, result.data)
                errors = validate_config(merged)
                if errors:
                    warnings.append("project config rejected: " + "; ".join(errors))
                else:
                    data = merged
                    project_path = candidate
    return LoadedConfig(
        data=data,
        global_path=global_config_path(),
        project_path=project_path,
        warnings=warnings,
    )


def save_global_config(data: dict[str, Any]) -> None:
    """Validate then persist the global configuration atomically."""
    errors = validate_config(data)
    if errors:
        raise ConfigError("; ".join(errors))
    write_json_atomic(global_config_path(), data)


def active_provider(data: dict[str, Any]) -> dict[str, Any]:
    """Return the provider block currently selected."""
    name = data.get("active_provider", "")
    providers = data.get("providers", {})
    provider = providers.get(name)
    if not isinstance(provider, dict):
        raise ConfigError(f"active provider {name} is not defined")
    return provider


def mask_secret(value: str) -> str:
    """Render an API key for display without revealing it."""
    if not value:
        return "(none)"
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 2) + value[-2:]


def redacted_copy(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the config with every API key masked, safe to display."""
    copied = copy.deepcopy(data)
    for provider in copied.get("providers", {}).values():
        if isinstance(provider, dict) and "api_key" in provider:
            provider["api_key"] = mask_secret(str(provider["api_key"]))
    return copied


def editor_command() -> list[str] | None:
    """Resolve the user's editor from $VISUAL / $EDITOR, else a fallback."""
    for env_var in ("VISUAL", "EDITOR"):
        value = os.environ.get(env_var, "").strip()
        if value:
            try:
                parts = shlex.split(value, posix=os.name != "nt")
            except ValueError:
                parts = [value]
            if parts:
                return parts
    if sys.platform == "win32":
        return ["notepad"]
    for candidate in ("nano", "vim", "vi"):
        found = shutil.which(candidate)
        if found:
            return [found]
    return None


def open_in_editor(path: Path) -> tuple[bool, str]:
    """Open ``path`` in the user's editor and wait for it to close."""
    command = editor_command()
    if command is None:
        return False, "no editor found: set VISUAL or EDITOR"
    try:
        subprocess.run([*command, str(path)], check=False)
    except OSError as exc:
        return False, f"cannot launch editor {command[0]}: {exc}"
    return True, " ".join(command)


def reload_from_disk(
    current: dict[str, Any], workspace: Path | None = None
) -> tuple[dict[str, Any], str | None]:
    """Re-read configuration files, keeping ``current`` if the new one is bad.

    Returns ``(config, error)``; on error the returned config is ``current``,
    so a broken edit never destroys the running configuration.
    """
    path = global_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return current, f"cannot read {path}: {exc}"
    try:
        parse_config_text(text)
    except ConfigError as exc:
        return current, str(exc)
    loaded = load_config(workspace)
    if loaded.warnings:
        return current, "; ".join(loaded.warnings)
    return loaded.data, None
