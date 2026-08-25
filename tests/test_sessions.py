"""Atomic session persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat.models import Message, Session, ToolCall
from vox_chat.sessions import SessionStore, slugify
from vox_chat.storage import sessions_dir


def make_session() -> Session:
    session = Session(title="my session", provider="local-ollama",
                      model="qwen2.5-coder:3b", role="python-developer",
                      workspace="/tmp/project", agent_enabled=True)
    session.messages = [
        Message(role="user", content="hello, unicode: àèìòù ✓"),
        Message(
            role="assistant",
            content="line one\nline two",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}')],
        ),
        Message(role="tool", content="file body", tool_call_id="c1", name="read_file"),
    ]
    return session


def test_save_and_load_round_trip() -> None:
    store = SessionStore()
    original = make_session()
    path = store.save(original, "my session")
    assert path.parent == sessions_dir()

    loaded = store.load("my session")
    assert loaded.title == "my session"
    assert loaded.agent_enabled is True
    assert loaded.workspace == "/tmp/project"
    assert [m.content for m in loaded.messages] == [m.content for m in original.messages]
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.messages[2].tool_call_id == "c1"


def test_save_is_atomic_and_leaves_no_temporary_files() -> None:
    store = SessionStore()
    store.save(make_session(), "atomic")
    leftovers = [p.name for p in sessions_dir().iterdir() if p.suffix != ".json"]
    assert leftovers == []


def test_save_overwrites_in_place() -> None:
    store = SessionStore()
    session = make_session()
    store.save(session, "same")
    session.messages.append(Message(role="user", content="more"))
    store.save(session, "same")
    assert len(list(sessions_dir().glob("*.json"))) == 1
    assert len(store.load("same").messages) == 4


def test_updated_at_moves_forward_on_save() -> None:
    store = SessionStore()
    session = make_session()
    session.updated_at = "2000-01-01T00:00:00+00:00"
    store.save(session, "stamped")
    assert store.load("stamped").updated_at > "2000-01-01T00:00:00+00:00"


def test_listing_tolerates_a_corrupt_file() -> None:
    store = SessionStore()
    store.save(make_session(), "good")
    (sessions_dir() / "vox-session-broken.json").write_text("{oops", encoding="utf-8")

    entries = store.list()
    names = {entry.name for entry in entries}
    assert {"good", "broken"} == names
    broken = next(entry for entry in entries if entry.name == "broken")
    assert broken.error is not None
    assert store.warnings


def test_loading_a_corrupt_session_raises_but_does_not_crash_startup() -> None:
    store = SessionStore()
    (sessions_dir()).mkdir(parents=True, exist_ok=True)
    (sessions_dir() / "vox-session-bad.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load("bad")
    with pytest.raises(FileNotFoundError):
        store.load("never-saved")


def test_slugify_refuses_path_traversal() -> None:
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("  ") == "session"
    assert "/" not in slugify("a/b")


def test_session_file_is_utf8_json(tmp_path: Path) -> None:
    store = SessionStore()
    path = store.save(make_session(), "utf8")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "àèìòù ✓" in data["messages"][0]["content"]


def test_sessions_are_named_so_one_glob_ignores_them(tmp_path: Path) -> None:
    """They sit next to the work, so they have to be recognisable."""
    store = SessionStore(tmp_path)
    path = store.save(make_session(), "refactor plan")
    assert path == tmp_path / "vox-session-refactor-plan.json"
    assert path.name.startswith("vox-")

    assert store.load("refactor plan").title == "my session"
    # The listing hands back the slug, which load() accepts as it stands.
    assert [entry.name for entry in store.list()] == ["refactor-plan"]
    assert store.load("refactor-plan").title == "my session"


def test_other_json_in_the_directory_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "mine"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    store = SessionStore(tmp_path)
    store.save(make_session(), "mine")

    assert [entry.name for entry in store.list()] == ["mine"]
    assert store.warnings == [], "a project's own JSON is not a corrupt session"
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == '{"name": "mine"}'


def test_a_name_already_carrying_the_prefix_is_not_doubled(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    path = store.save(make_session(), "vox-session-plan")
    assert path.name == "vox-session-plan.json"
