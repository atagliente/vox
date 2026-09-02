"""Configuration defaults, merging, validation and safe reloading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat.config import (
    ConfigError,
    deep_merge,
    default_config,
    ensure_global_config,
    load_config,
    mask_secret,
    parse_config_text,
    redacted_copy,
    reload_from_disk,
    save_global_config,
    validate_config,
)
from vox_chat.storage import global_config_path, project_config_path


def test_first_run_creates_a_valid_global_config(vox_home: Path) -> None:
    data, warnings = ensure_global_config()
    assert warnings == []
    assert global_config_path().exists()
    assert validate_config(data) == []
    assert data["active_provider"] == "local-ollama"
    assert data["agent"]["enabled"] is False


def test_default_config_is_a_fresh_copy() -> None:
    first = default_config()
    first["providers"]["local-ollama"]["base_url"] = "http://changed/v1"
    assert (
        default_config()["providers"]["local-ollama"]["base_url"] != "http://changed/v1"
    )


def test_deep_merge_replaces_only_declared_keys() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2]}
    merged = deep_merge(base, {"nested": {"y": 9}, "list": [3]})
    assert merged == {"a": 1, "nested": {"x": 1, "y": 9}, "list": [3]}
    assert base["nested"]["y"] == 2, "the base must not be mutated"


def test_project_config_overrides_only_what_it_declares(workspace: Path) -> None:
    ensure_global_config()
    project_path = project_config_path(workspace)
    project_path.parent.mkdir(parents=True)
    project_path.write_text(
        json.dumps({"active_model": "qwen2.5-coder:1.5b", "agent": {"enabled": True}}),
        encoding="utf-8",
    )
    loaded = load_config(workspace)
    assert loaded.warnings == []
    assert loaded.project_path == project_path
    assert loaded.data["active_model"] == "qwen2.5-coder:1.5b"
    assert loaded.data["agent"]["enabled"] is True
    assert loaded.data["agent"]["confirm_writes"] is True, "other keys survive"
    assert loaded.data["active_provider"] == "local-ollama"


def test_invalid_project_config_is_rejected_with_a_warning(workspace: Path) -> None:
    ensure_global_config()
    project_path = project_config_path(workspace)
    project_path.parent.mkdir(parents=True)
    project_path.write_text(json.dumps({"active_provider": "nope"}), encoding="utf-8")
    loaded = load_config(workspace)
    assert loaded.project_path is None
    assert any("rejected" in warning for warning in loaded.warnings)
    assert loaded.data["active_provider"] == "local-ollama"


def test_parse_config_text_reports_line_and_column() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_config_text('{\n  "active_provider": \n}')
    assert excinfo.value.line == 3
    assert excinfo.value.column is not None
    assert "line 3" in str(excinfo.value)


def test_reload_keeps_the_previous_config_when_the_file_is_broken() -> None:
    data, _ = ensure_global_config()
    data["active_model"] = "known-good"
    save_global_config(data)
    global_config_path().write_text("{ this is not json", encoding="utf-8")

    reloaded, error = reload_from_disk(data)
    assert error is not None
    assert "invalid JSON" in error
    assert reloaded is data
    assert reloaded["active_model"] == "known-good"


def test_reload_accepts_a_valid_edit() -> None:
    data, _ = ensure_global_config()
    edited = default_config()
    edited["active_model"] = "qwen2.5-coder:1.5b"
    global_config_path().write_text(json.dumps(edited), encoding="utf-8")

    reloaded, error = reload_from_disk(data)
    assert error is None
    assert reloaded["active_model"] == "qwen2.5-coder:1.5b"


def test_corrupt_global_config_is_quarantined_not_fatal(vox_home: Path) -> None:
    global_config_path().write_text("not json at all", encoding="utf-8")
    data, warnings = ensure_global_config()
    assert validate_config(data) == []
    assert any("invalid JSON" in warning for warning in warnings)
    assert list(vox_home.glob("config.json.corrupt-*")), "the bad file is kept aside"


@pytest.mark.parametrize(
    "mutation, fragment",
    [
        ({"active_provider": "missing"}, "active_provider"),
        ({"generation": {"temperature": 5}}, "temperature"),
        ({"agent": {"max_tool_cycles": 0}}, "max_tool_cycles"),
        ({"ui": {"theme": "neon"}}, "ui.theme"),
        ({"providers": {"local-ollama": {"base_url": "ftp://x"}}}, "base_url"),
    ],
)
def test_validation_rejects_bad_values(mutation: dict, fragment: str) -> None:
    candidate = deep_merge(default_config(), mutation)
    errors = validate_config(candidate)
    assert any(fragment in error for error in errors), errors


def test_api_keys_are_masked_for_display() -> None:
    assert mask_secret("ollama") == "****ma"
    assert mask_secret("") == "(none)"
    redacted = redacted_copy(default_config())
    assert redacted["providers"]["local-ollama"]["api_key"] == "****ma"
    assert default_config()["providers"]["local-ollama"]["api_key"] == "ollama"


def test_the_installer_keeps_unix_line_endings() -> None:
    """A CRLF install.sh dies on Debian with "set: Illegal option"."""
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "install.sh"
    if not script.exists():
        pytest.skip("running outside a checkout")
    data = script.read_bytes()
    assert b"\r\n" not in data, "install.sh must stay LF or sh cannot read it"
    assert data.startswith(b"#!/bin/sh\n")


def test_every_doctor_check_runs(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """Regression: doctor imported a module that had been deleted, so `vox
    doctor` — the first thing the installer runs — died with an ImportError."""
    import json

    from vox_chat import doctor
    from vox_chat.storage import global_config_path

    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 1
    # The parts that are off by default still have to be checkable.
    config["web"].update({"enabled": True, "auto": True})
    config["mesh"]["enabled"] = True
    global_config_path().write_text(json.dumps(config), encoding="utf-8")

    checks = doctor.run_checks(workspace=workspace, timeout=1.0)
    labels = [check.label for check in checks]
    for expected in ("PYTHON", "DEPS", "CONFIG", "LINK", "MESH", "CONSENSUS", "WEB"):
        assert expected in labels, f"{expected} is missing from the report"
    assert all(check.status in ("OK", "FAIL", "WARN", "--") for check in checks)
    # And it renders, which is what the installer prints.
    assert doctor.report(checks, plain=True)
