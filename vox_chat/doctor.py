"""VOX SYSTEM CHECK: the preflight diagnostic used by the installers.

Runs without a TUI, prints a terminal-style report and exits non-zero when a
blocking problem is found.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import __version__
from .clipboard import describe as describe_clipboard
from .config import (
    ConfigError,
    LoadedConfig,
    active_provider,
    editor_command,
    load_config,
)
from .storage import vox_home

Status = Literal["OK", "FAIL", "WARN", "--"]

_MIN_PYTHON = (3, 11)
_REQUIRED = ("openai", "textual", "rich")


@dataclass
class Check:
    """One line of the report."""

    status: Status
    label: str
    detail: str = ""

    def render(self, plain: bool = False) -> str:
        mark = f"[{self.status:^4}]"
        line = f"{mark} {self.label.ljust(8)} {self.detail}".rstrip()
        if plain:
            return line
        colors = {"OK": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m", "--": "\033[90m"}
        return f"{colors.get(self.status, '')}{line}\033[0m"


def check_python() -> Check:
    version = sys.version_info
    detail = f"{platform.python_version()} ({sys.executable})"
    if (version.major, version.minor) < _MIN_PYTHON:
        return Check("FAIL", "PYTHON", detail + " - 3.11 or newer required")
    return Check("OK", "PYTHON", detail)


def check_dependencies() -> Check:
    missing: list[str] = []
    found: list[str] = []
    for name in _REQUIRED:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
            continue
        try:
            found.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            found.append(name)
    if missing:
        return Check("FAIL", "DEPS", "missing: " + ", ".join(missing))
    return Check("OK", "DEPS", " | ".join(found))


def check_home() -> Check:
    home = vox_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("FAIL", "CONFIG", f"{home} not writable: {exc}")
    return Check("OK", "CONFIG", str(home))


def check_editor() -> Check:
    command = editor_command()
    source = "VISUAL" if os.environ.get("VISUAL") else (
        "EDITOR" if os.environ.get("EDITOR") else "fallback"
    )
    if command is None:
        return Check("WARN", "EDITOR", "none found - /config will not work")
    return Check("OK", "EDITOR", f"{' '.join(command)} ({source})")


def check_clipboard() -> Check:
    detail = describe_clipboard()
    if detail == "none found":
        return Check("WARN", "CLIPBOARD", "no helper found - copy and paste will fail")
    return Check("OK", "CLIPBOARD", detail)


def check_link(loaded: LoadedConfig, timeout: float = 5.0) -> tuple[Check, list[str]]:
    """Probe ``/v1/models`` on the active provider without changing anything."""
    try:
        provider = active_provider(loaded.data)
    except ConfigError as exc:
        return Check("FAIL", "LINK", str(exc)), []
    base_url = str(provider.get("base_url", ""))
    if importlib.util.find_spec("openai") is None:
        return Check("--", "LINK", f"{base_url} - openai not installed"), []
    from .llm_client import LLMClient, LLMError

    try:
        client = LLMClient.from_provider(provider)
        models = client.list_models(timeout=timeout)
    except LLMError as exc:
        # Connection and timeout messages already name the endpoint.
        detail = exc.message if base_url in exc.message else f"{base_url} - {exc.message}"
        return Check("FAIL", "LINK", detail), []
    except (OSError, ValueError) as exc:
        return Check("FAIL", "LINK", f"{base_url} - {exc}"), []
    return Check("OK", "LINK", f"{base_url} - {len(models)} models"), models


def check_model(loaded: LoadedConfig, models: list[str]) -> Check:
    wanted = str(loaded.data.get("active_model", ""))
    if not models:
        return Check("--", "MODEL", f"{wanted} - not verified")
    if wanted in models:
        return Check("OK", "MODEL", wanted)
    return Check(
        "WARN", "MODEL", f"{wanted} not served; available: {', '.join(models[:5])}"
    )


def check_mesh(loaded: LoadedConfig) -> Check:
    """What the mesh would find if it were asked to go online right now."""
    from .mesh import MeshSettings, pki_dir, using_demo_ca

    settings = MeshSettings.from_config(loaded.data)
    where = f"{settings.group}:{settings.port}"
    if importlib.util.find_spec("cryptography") is None:
        return Check("FAIL", "MESH", f"{where} - cryptography not installed")

    from .mesh import category_for

    parts = [where, f"{settings.resolved_agent_id()} ({category_for(settings.verbs)})"]
    directory = pki_dir(settings)
    certificate = directory / f"{settings.resolved_agent_id()}.crt"
    if certificate.exists():
        try:
            from .discovery.identity import Identity

            identity = Identity.load(settings.resolved_agent_id(), directory)
            parts.append(f"cert valid until {identity.expires_at():%Y-%m-%d %H:%M}")
        except Exception as exc:  # the certificate is there but unusable
            return Check("WARN", "MESH", f"{where} - {certificate}: {exc}")
    elif settings.auto_provision:
        parts.append("no certificate yet; one is issued on going online")
    else:
        return Check(
            "WARN", "MESH", f"{where} - no certificate and auto_provision is off"
        )
    if not (directory / "ca.crt").exists():
        parts.append("the sample authority is copied in on going online")
        return Check("OK", "MESH", " - ".join(parts))
    if using_demo_ca(directory):
        # Not a failure: it is what makes a fresh install work. It is still
        # the thing an operator most needs to know about their mesh.
        parts.append("SAMPLE CA, private key public - /mesh new-ca replaces it")
        return Check("WARN", "MESH", " - ".join(parts))
    parts.append(f"CA {directory / 'ca.crt'}")
    return Check("OK", "MESH", " - ".join(parts))


def check_consensus(loaded: LoadedConfig) -> Check:
    """Whether this node would distribute a [CNS] span, and answer others."""
    from .mesh import MeshSettings, pki_dir, using_demo_ca

    section = loaded.data.get("consensus", {})
    section = section if isinstance(section, dict) else {}
    if not section.get("enabled", True):
        return Check("--", "CONSENSUS", "off - /consensus on")

    verb = str(section.get("verb", "infer"))
    answers = "answers peers" if section.get("answer_requests", True) else "asks only"
    detail = f"{verb} - {answers}"
    if using_demo_ca(pki_dir(MeshSettings.from_config(loaded.data))):
        return Check(
            "WARN", "CONSENSUS",
            f"{detail} - refused on the sample CA; /mesh new-ca first",
        )
    return Check("OK", "CONSENSUS", detail)


def run_checks(workspace: Path | None = None, timeout: float = 5.0) -> list[Check]:
    """Run every check in order and return the report lines."""
    checks = [
        check_python(),
        check_dependencies(),
        check_home(),
        check_editor(),
        check_clipboard(),
    ]
    loaded = load_config(workspace)
    for warning in loaded.warnings:
        checks.append(Check("WARN", "CONFIG", warning))
    link, models = check_link(loaded, timeout=timeout)
    checks.append(link)
    checks.append(check_model(loaded, models))
    checks.append(check_mesh(loaded))
    checks.append(check_consensus(loaded))
    return checks


def report(checks: list[Check], plain: bool = False) -> str:
    header = f"VOX SYSTEM CHECK  -  v{__version__}  -  {platform.system()}"
    lines = [header, "=" * len(header)]
    lines.extend(check.render(plain=plain) for check in checks)
    return "\n".join(lines)


def main(workspace: Path | None = None, plain: bool = False,
         timeout: float = 5.0) -> int:
    """Print the report; return 0 unless a blocking check failed."""
    checks = run_checks(workspace=workspace, timeout=timeout)
    print(report(checks, plain=plain or not sys.stdout.isatty()))
    blocking = [c for c in checks if c.status == "FAIL" and c.label != "LINK"]
    link_failed = any(c.status == "FAIL" and c.label == "LINK" for c in checks)
    if blocking:
        print("\nRESULT: NOT READY")
        return 2
    if link_failed:
        print("\nRESULT: READY - PROVIDER UNREACHABLE (the TUI still starts)")
        return 1
    print("\nRESULT: ALL SYSTEMS NOMINAL")
    return 0
