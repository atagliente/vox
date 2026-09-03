"""The sandbox: what it builds, and what it refuses to do without.

Nothing here starts bubblewrap or Docker. What is worth testing is the part
that is a decision rather than a syscall — that a sandbox nobody asked for
does nothing, that one that was asked for and cannot be provided stops the
command instead of quietly running it anyway, and that what ends up on the
command line says what the docstring says it says.

The one integration test that does run a real sandbox is marked slow and
skips itself where bwrap is not installed, which is every machine this was
written on and most of CI.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vox_chat import tools
from vox_chat.config import validate_config
from vox_chat.sandbox import Sandbox, SandboxError, errors_in


@pytest.fixture
def workspace(tmp_path):
    return tools.Workspace(tmp_path)


# ------------------------------------------------------------ reading it


def test_no_sandbox_unless_one_is_named():
    assert Sandbox.from_config({}).on is False
    assert Sandbox.from_config(None).mode == "off"
    assert Sandbox.from_config({}).describe() == "no sandbox"


def test_a_bare_string_is_the_mode():
    # `sandbox: bwrap` is what people write; making them write
    # `sandbox: {mode: bwrap}` to get it would be a papercut with no purpose.
    assert Sandbox.from_config({"sandbox": "bwrap"}).mode == "bwrap"


def test_the_network_is_off_unless_asked_for():
    box = Sandbox.from_config({"sandbox": {"mode": "docker"}})
    assert box.network is False
    assert "no network" in box.describe()
    loud = Sandbox.from_config({"sandbox": {"mode": "docker", "network": True}})
    assert "network allowed" in loud.describe()


def test_a_misspelt_mode_is_an_error_and_not_a_silent_off():
    """`sandbox: bwarp` reads two ways and only one is safe to guess."""
    assert errors_in({"sandbox": "bwarp"}) == [
        "agent.sandbox.mode must be one of: off, bwrap, docker"
    ]
    assert errors_in({"sandbox": {"mode": "bwrap", "nework": True}}) == [
        "agent.sandbox.nework is not a setting"
    ]
    assert errors_in({}) == []
    assert errors_in({"sandbox": {"mode": "off"}}) == []


def test_the_config_validator_sees_it():
    problems = validate_config({"agent": {"sandbox": {"mode": "nonsense"}}})
    assert any("agent.sandbox.mode" in problem for problem in problems)


# --------------------------------------------------------- what it builds


def test_bubblewrap_binds_the_workspace_and_nothing_else_writable(tmp_path):
    box = Sandbox(mode="bwrap")
    line = box._bwrap(["ls", "-l"], tmp_path / "sub", tmp_path)
    assert line[0] == "bwrap"
    assert "--unshare-all" in line
    assert "--share-net" not in line
    assert "--die-with-parent" in line
    # One --bind, and it is the workspace. Everything else is --ro-bind.
    binds = [line[i + 1] for i, word in enumerate(line) if word == "--bind"]
    assert binds == [str(tmp_path)]
    assert line[-2:] == ["ls", "-l"]
    assert line[line.index("--chdir") + 1] == str(tmp_path / "sub")


def test_bubblewrap_shares_the_network_only_when_told_to(tmp_path):
    line = Sandbox(mode="bwrap", network=True)._bwrap(["curl", "x"], tmp_path, tmp_path)
    assert "--share-net" in line


def test_docker_mounts_the_workspace_and_starts_in_the_right_place(tmp_path):
    box = Sandbox(mode="docker", image="python:3.12-slim")
    line = box._docker(["pytest"], tmp_path / "a" / "b", tmp_path)
    assert line[:3] == ["docker", "run", "--rm"]
    assert line[line.index("--network") + 1] == "none"
    assert line[line.index("-v") + 1] == f"{tmp_path}:/workspace"
    assert line[line.index("-w") + 1] == "/workspace/a/b"
    assert line[-1] == "pytest"
    assert "python:3.12-slim" in line


# ------------------------------------------------- what it refuses to do


def test_a_sandbox_that_cannot_be_provided_stops_the_command(workspace, monkeypatch):
    """The whole point. A fallback to an unsandboxed run would mean the
    setting stops applying on exactly the machine where it was needed."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    box = Sandbox(mode="bwrap")
    assert "not on the PATH" in (box.unavailable() or "")
    with pytest.raises(SandboxError):
        box.wrap(["ls"], Path(workspace.root), Path(workspace.root))

    result = tools.run_command(workspace, "echo hello", sandbox=box)
    assert result.ok is False
    assert "not on the PATH" in result.output
    # And it really did not run: no output from the command anywhere.
    assert "hello" not in result.output


def test_without_a_sandbox_the_command_runs_as_before(workspace):
    result = tools.run_command(workspace, "python -c \"print('plain')\"")
    assert result.ok is True
    assert "plain" in result.output
    assert "sandbox:" not in result.output


def test_a_sandboxed_run_says_which_sandbox(workspace, monkeypatch):
    # A command that behaves differently inside one should say so, or the
    # difference looks like the command being flaky.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    box = Sandbox(mode="docker")
    monkeypatch.setattr(
        Sandbox, "_docker", lambda self, argv, d, r: ["python", "-c", "print('boxed')"]
    )
    result = tools.run_command(workspace, "true", sandbox=box)
    assert "sandbox: docker" in result.output
    assert "boxed" in result.output


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
def test_bubblewrap_really_does_confine_a_command(workspace):
    """The one test that starts a real sandbox, where there is one to start.

    Two claims at once: the workspace is writable from inside and the write
    is visible outside, and the home directory is not there to be written to.
    """
    box = Sandbox(mode="bwrap")
    made = tools.run_command(
        workspace,
        "python -c \"open('made','w').write('yes')\"",
        sandbox=box,
    )
    assert made.ok is True, made.output
    assert (Path(workspace.root) / "made").read_text() == "yes"

    outside = tools.run_command(
        workspace,
        "python -c \"open('/etc/passwd','a').write('x')\"",
        sandbox=box,
    )
    assert outside.ok is False
