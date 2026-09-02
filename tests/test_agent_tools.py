"""What the agent loop gained: an exact edit, an undo, a plan, and parallelism.

The theme running through all four is that a model working on a long task
should have to reproduce less, be able to be wrong once without cost, say
where it has got to, and not wait in a queue for three files it could have
read at once.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vox_chat import tools
from vox_chat.agent import _batches, _run_batch
from vox_chat.models import ToolCall
from vox_chat.tools import TodoList, ToolError, Workspace
from vox_chat.undo import UndoStack


@pytest.fixture
def ws(workspace: Path) -> Workspace:
    (workspace / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    return Workspace(workspace)


# ---------------------------------------------------------------- edit_file


def test_an_edit_changes_only_what_it_names(ws: Workspace) -> None:
    result = tools.edit_file(ws, "a.py", "two", "TWO")
    assert result.ok
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_text_that_is_not_there_is_refused_not_guessed(ws: Workspace) -> None:
    """No match means the model was working from something other than what is
    on disk, and the honest answer says so."""
    with pytest.raises(ToolError, match="not what the edit was written against"):
        tools.edit_file(ws, "a.py", "four", "FOUR")
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_an_ambiguous_edit_is_refused(ws: Workspace) -> None:
    """More matches than expected means it is about to change something it
    did not look at."""
    (ws.root / "b.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="appears 2 times"):
        tools.edit_file(ws, "b.py", "x = 1", "x = 2")
    assert (ws.root / "b.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_several_matches_are_allowed_when_they_are_expected(ws: Workspace) -> None:
    (ws.root / "b.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    tools.edit_file(ws, "b.py", "x = 1", "x = 2", expect=2)
    assert (ws.root / "b.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_an_empty_replacement_deletes(ws: Workspace) -> None:
    tools.edit_file(ws, "a.py", "two\n", "")
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "one\nthree\n"


def test_edit_file_will_not_invent_a_file(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="no such path"):
        tools.edit_file(ws, "nowhere.py", "x", "y")


def test_the_preview_is_the_diff_the_operator_authorises(ws: Workspace) -> None:
    preview = tools.preview_edit(ws, "a.py", "two", "TWO")
    assert "-two" in preview and "+TWO" in preview


def test_the_preview_reports_an_edit_that_would_be_refused(ws: Workspace) -> None:
    """Checked before the operator is asked, not after they said yes."""
    assert "would be refused" in tools.preview_edit(ws, "a.py", "four", "x")


def test_edit_file_is_confirmed_like_every_other_write() -> None:
    from vox_chat.agent import needs_confirmation

    assert "edit_file" in tools.WRITE_TOOLS
    assert needs_confirmation("edit_file", {"confirm_writes": True}) is True


def test_the_model_is_told_to_prefer_it() -> None:
    schema = next(s for s in tools.TOOL_SCHEMAS if s["function"]["name"] == "edit_file")
    assert "Prefer this over" in schema["function"]["description"]


# --------------------------------------------------------------------- undo


def test_the_bytes_are_kept_before_the_write_not_after(ws: Workspace) -> None:
    """After is too late: the file has already been overwritten."""
    taken = tools.snapshot(ws, ["a.py"])
    tools.write_file(ws, "a.py", "replaced\n")
    assert taken[0].before == "one\ntwo\nthree\n"


def test_undo_puts_a_file_back(ws: Workspace) -> None:
    stack = UndoStack()
    stack.remember("write_file", tools.snapshot(ws, ["a.py"]))
    tools.write_file(ws, "a.py", "replaced\n")
    message = stack.undo()
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert "restored a.py" in message


def test_undoing_a_new_file_removes_it(ws: Workspace) -> None:
    stack = UndoStack()
    stack.remember("write_file", tools.snapshot(ws, ["new.py"]))
    tools.write_file(ws, "new.py", "x\n")
    assert (ws.root / "new.py").exists()
    stack.undo()
    assert not (ws.root / "new.py").exists()


def test_it_is_one_step_and_says_so(ws: Workspace) -> None:
    """Anything deeper is what git is for, and half-promising otherwise is
    the kind of thing that gets trusted once."""
    stack = UndoStack()
    stack.remember("write_file", tools.snapshot(ws, ["a.py"]))
    tools.write_file(ws, "a.py", "x\n")
    message = stack.undo()
    assert "not version control" in message
    assert stack.available is False
    with pytest.raises(ToolError, match="nothing to undo"):
        stack.undo()


def test_a_patch_remembers_every_file_it_touches(ws: Workspace) -> None:
    (ws.root / "b.py").write_text("bee\n", encoding="utf-8")
    patch = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-bee\n+BEE\n"
    )
    touched = tools.paths_touched("apply_patch", {"patch": patch}, ws)
    assert set(touched) == {"a.py", "b.py"}


def test_nothing_to_undo_says_so_plainly() -> None:
    assert "no write has been authorised" in UndoStack().describe()


# --------------------------------------------------------------------- plan


def test_a_plan_renders_where_it_has_got_to() -> None:
    todos = TodoList()
    rendered = todos.replace(
        [
            {"text": "read the module", "state": "done"},
            {"text": "write the test", "state": "doing"},
            {"text": "make it pass", "state": "todo"},
        ]
    )
    assert "[x] read the module" in rendered
    assert "[>] write the test" in rendered
    assert "[ ] make it pass" in rendered
    assert "1/3 done" in rendered


def test_plain_strings_are_accepted_as_steps() -> None:
    assert "[ ] first" in TodoList().replace(["first", "second"])


def test_only_one_step_may_be_in_progress() -> None:
    """A plan where everything is being done at once is one nobody follows."""
    with pytest.raises(ToolError, match="work on one at a time"):
        TodoList().replace(
            [{"text": "a", "state": "doing"}, {"text": "b", "state": "doing"}]
        )


def test_an_empty_plan_is_refused() -> None:
    with pytest.raises(ToolError, match="at least one step"):
        TodoList().replace([])


def test_an_unknown_state_falls_back_to_todo() -> None:
    assert "[ ] a" in TodoList().replace([{"text": "a", "state": "nonsense"}])


def test_the_plan_tool_changes_nothing_on_disk() -> None:
    """Which is why it needs no confirmation."""
    from vox_chat.agent import needs_confirmation

    assert "plan" not in tools.WRITE_TOOLS
    assert "plan" not in tools.COMMAND_TOOLS
    assert needs_confirmation("plan", {}) is False


# ---------------------------------------------------------------- parallel


def a_call(name: str) -> ToolCall:
    return ToolCall(id=name, name=name)


def test_reads_are_grouped_and_writes_are_not() -> None:
    calls = [a_call(n) for n in ("read_file", "search_text", "write_file", "read_file")]
    groups = [[call.name for call in group] for group in _batches(calls, 4)]
    assert groups == [
        ["read_file", "search_text"],
        ["write_file"],
        ["read_file"],
    ]


def test_a_command_never_shares_a_batch() -> None:
    """Two confirmations on screen at once is a question nobody can answer."""
    calls = [a_call(n) for n in ("run_command", "run_command")]
    assert [len(group) for group in _batches(calls, 4)] == [1, 1]


def test_the_width_is_respected() -> None:
    calls = [a_call("read_file") for _ in range(5)]
    assert [len(group) for group in _batches(calls, 2)] == [2, 2, 1]


def test_one_at_a_time_is_still_available() -> None:
    calls = [a_call("read_file") for _ in range(3)]
    assert [len(group) for group in _batches(calls, 1)] == [1, 1, 1]


def test_results_keep_the_order_the_model_asked_for() -> None:
    """A conversation that reorders itself run to run is one nobody can
    reason about."""
    from vox_chat.models import Message

    def run_one(call: ToolCall) -> Message:
        # The first call is the slowest, so finishing order is not asking
        # order unless something puts it back.
        time.sleep(0.05 if call.id == "first" else 0.0)
        return Message(role="tool", content=call.id)

    calls = [ToolCall(id=name, name="read_file") for name in ("first", "second")]
    results = _run_batch(calls, run_one)
    assert [message.content for message in results] == ["first", "second"]


def test_they_really_do_run_at_once() -> None:
    """Otherwise this is a grouping that buys nothing."""
    from vox_chat.models import Message

    started = threading.Barrier(3, timeout=5)

    def run_one(call: ToolCall) -> Message:
        started.wait()  # times out unless all three are running together
        return Message(role="tool", content=call.id)

    calls = [ToolCall(id=str(n), name="read_file") for n in range(3)]
    results = _run_batch(calls, run_one)
    assert len(results) == 3


# ------------------------------------------------------------ authorisation


def test_one_switch_for_ls_and_rm_was_not_one() -> None:
    """Three levels, decided per command."""
    from vox_chat.authorisation import Policy

    policy = Policy.from_config(
        {"commands": {"allow": ["git", "pytest"], "deny": ["curl"]}}
    )
    assert policy.level_for("git status") == "allow"
    assert policy.level_for("curl http://x") == "deny"
    assert policy.level_for("rm -rf /") == "ask"


def test_a_rule_holds_however_the_program_is_spelled() -> None:
    """A rule that /usr/bin/git or GIT could get past would only hold when
    nobody was trying, which is not when it matters."""
    from vox_chat.authorisation import Policy

    policy = Policy.from_config({"commands": {"deny": ["git"]}})
    for spelling in ("git", "/usr/bin/git", "GIT", "git.exe", "C:/tools/Git.EXE"):
        assert policy.level_for(f"{spelling} push --force") == "deny", spelling


def test_a_quoted_path_with_spaces_is_one_program() -> None:
    """Split the way it will be run, not on whitespace."""
    from vox_chat.agent import command_level

    level = command_level(
        '"C:/Program Files/Git/git.exe" log', {"commands": {"allow": ["git"]}}
    )
    assert level == "allow"


def test_the_old_switch_still_means_what_it_meant() -> None:
    """An existing configuration behaves exactly as it did before."""
    from vox_chat.authorisation import Policy

    assert Policy.from_config({"confirm_commands": True}).level_for("anything") == "ask"
    assert (
        Policy.from_config({"confirm_commands": False}).level_for("anything") == "allow"
    )


def test_nothing_is_allowed_or_denied_out_of_the_box() -> None:
    """A default allowlist would be this file deciding what is safe on
    somebody else's machine."""
    from vox_chat.authorisation import Policy
    from vox_chat.config import default_config

    policy = Policy.from_config(default_config()["agent"])
    assert policy.rules == {}
    assert policy.level_for("rm -rf /") == "ask"


def test_a_denied_command_is_refused_without_a_dialog(ws: Workspace) -> None:
    """Being asked at three in the morning is how the answer becomes yes."""
    from vox_chat.agent import _run_tool

    asked: list[str] = []

    def confirm(name: str, description: str) -> bool:
        asked.append(name)
        return True

    call = ToolCall(
        id="1", name="run_command", arguments='{"command": "curl http://x"}'
    )
    message = _run_tool(
        call,
        workspace=ws,
        agent_config={"commands": {"deny": ["curl"]}},
        confirm=confirm,
        command_timeout=5,
        max_output=1000,
    )
    assert asked == [], "nobody was asked"
    assert call.approved is False
    assert "deny list" in message.content


def test_an_allowed_command_is_not_confirmed(ws: Workspace) -> None:
    from vox_chat.agent import _run_tool

    asked: list[str] = []
    import sys

    call = ToolCall(
        id="1",
        name="run_command",
        arguments=json.dumps({"command": f'"{sys.executable}" -c print(1)'}),
    )
    message = _run_tool(
        call,
        workspace=ws,
        agent_config={"commands": {"allow": [sys.executable]}},
        confirm=lambda name, description: (asked.append(name), True)[1],
        command_timeout=30,
        max_output=2000,
    )
    assert asked == [], "an allowed command does not interrupt"
    assert call.approved is True
    assert "1" in message.content


def test_a_broken_command_line_is_asked_about_not_allowed() -> None:
    from vox_chat.agent import command_level

    assert command_level('git "unclosed', {"commands": {"allow": ["git"]}}) == "ask"


def test_the_policy_is_checked_when_the_configuration_is_saved() -> None:
    from vox_chat.config import default_config, validate_config

    config = default_config()
    config["agent"]["commands"] = {"nonsense": ["x"]}
    assert any("agent.commands.nonsense" in e for e in validate_config(config))
    config["agent"]["commands"] = {"allow": "git"}
    assert any("list of program names" in e for e in validate_config(config))
    config["agent"]["commands"] = {"default": "maybe"}
    assert any("commands.default" in e for e in validate_config(config))


# ------------------------------------------------------------------ limits


def test_limits_are_off_until_set() -> None:
    from vox_chat.authorisation import Limits
    from vox_chat.config import default_config

    limits = Limits.from_config(default_config()["agent"])
    assert limits.describe() == "time only"
    assert limits.preexec() is None


def test_what_cannot_be_applied_here_says_so(monkeypatch) -> None:
    """A setting that quietly does nothing is worse than one that says it
    does nothing."""
    import vox_chat.authorisation as auth
    from vox_chat.authorisation import Limits

    limits = Limits(memory_mb=512, processes=32)
    monkeypatch.setattr(auth.os, "name", "nt")
    assert limits.enforceable is False
    assert "not applied" in limits.describe()
    assert limits.preexec() is None

    monkeypatch.setattr(auth.os, "name", "posix")
    assert limits.enforceable is True
    assert "512 MB" in limits.describe() and "32 processes" in limits.describe()
    assert callable(limits.preexec())


def test_the_limits_are_checked_too() -> None:
    from vox_chat.config import default_config, validate_config

    config = default_config()
    config["agent"]["memory_limit_mb"] = -1
    assert any("memory_limit_mb" in e for e in validate_config(config))
