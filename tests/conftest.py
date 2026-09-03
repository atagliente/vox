"""Shared fixtures: every test runs against a throwaway VOX home."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Fail once, with an explanation, when pytest cannot make its temp dirs.

    Every test here takes ``tmp_path``, so a base temp directory that is not
    writable — a locked-down %TEMP%, a corporate policy, a leftover
    ``pytest-of-<user>`` owned by someone else — produces one identical
    PermissionError per test and buries whatever the run was actually meant
    to tell you. Saying it once, at the top, costs a stat call.
    """
    if config.option.basetemp:
        return
    root = Path(tempfile.gettempdir()) / f"pytest-of-{_username()}"
    target = root if root.exists() else Path(tempfile.gettempdir())
    try:
        # An actual write, not os.access: on Windows that only reports the
        # read-only attribute and cheerfully calls a directory writable that
        # refuses every write in it.
        probe = target / ".vox-write-probe"
        probe.mkdir()
        probe.rmdir()
    except OSError as exc:
        raise pytest.UsageError(
            f"{target} cannot be written to ({exc.strerror or exc}), so pytest "
            "cannot create the temporary directories every test in this suite "
            "asks for. Left alone this produces one identical error per test "
            "and hides the real result.\n"
            "Point it somewhere you can write instead:\n"
            "    pytest --basetemp=<a writable directory>\n"
            "or set TMPDIR (TEMP on Windows) before running."
        ) from exc


def _username() -> str:
    """The name pytest builds its base temp directory from."""
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - no account name to be had
        return "unknown"


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


@pytest.fixture(autouse=True)
def no_startup_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the connection probe from writing into somebody else's test.

    `VoxApp.check_connection` is a thread worker started at mount that opens a
    real socket to the configured provider. Every UI fixture here points at
    127.0.0.1:1 to make that fail, and on this machine it fails in under a
    millisecond — so its "LINK DOWN" line always landed before the test did
    anything, and nobody noticed it was landing at all.

    On CI it does not. A refused connection on a loaded macOS or Windows
    runner can take long enough that the line arrives after the assertion —
    which failed `test_mesh_without_an_argument_reports_the_state`, reading
    the last transcript line — or after the app has torn down, which failed
    `test_model_gpu_is_ollama_only` with NoMatches from inside a worker.
    Two different tests, on two different runners, from one shared source of
    nondeterminism.

    No test asserts on the probe, so the fix is to not run it. Anything that
    wants to test the probe itself can call `_connection_result` directly,
    which is where the behaviour actually is.
    """
    from vox_chat.app import VoxApp

    monkeypatch.setattr(VoxApp, "check_connection", lambda self: None)
