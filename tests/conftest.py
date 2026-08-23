"""Shared fixtures: every test runs against a throwaway VOX home."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def vox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.vox to a temporary directory for the whole test session."""
    home = tmp_path / "vox-home"
    home.mkdir()
    monkeypatch.setenv("VOX_HOME", str(home))
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty project directory to use as the agent workspace."""
    root = tmp_path / "project"
    root.mkdir()
    return root
