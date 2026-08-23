"""Role CRUD and seeding."""

from __future__ import annotations

from pathlib import Path

import pytest

from vox_chat.models import Role
from vox_chat.roles import DEFAULT_ROLES, RoleStore
from vox_chat.storage import roles_path


def test_first_load_seeds_the_builtin_roles() -> None:
    store = RoleStore()
    assert roles_path().exists()
    assert {role["name"] for role in DEFAULT_ROLES} <= set(store.names())
    assert "python-developer" in store


def test_create_update_duplicate_delete() -> None:
    store = RoleStore()
    store.create(Role(name="tester", description="d", system_prompt="p"))
    assert "tester" in RoleStore().names(), "persisted across instances"

    store.update("tester", description="updated", temperature=0.7)
    assert store.require("tester").description == "updated"
    assert RoleStore().require("tester").temperature == pytest.approx(0.7)

    store.duplicate("tester", "tester-copy")
    assert store.require("tester-copy").system_prompt == "p"

    store.delete("tester")
    assert "tester" not in store
    assert "tester" not in RoleStore()


def test_duplicate_and_create_refuse_existing_names() -> None:
    store = RoleStore()
    with pytest.raises(KeyError):
        store.create(Role(name="python-developer"))
    with pytest.raises(KeyError):
        store.duplicate("python-developer", "code-reviewer")
    with pytest.raises(KeyError):
        store.require("does-not-exist")


def test_renaming_through_update_moves_the_entry() -> None:
    store = RoleStore()
    store.create(Role(name="old"))
    store.update("old", name="new")
    assert "old" not in store and "new" in store


def test_corrupt_roles_file_falls_back_to_defaults(vox_home: Path) -> None:
    roles_path().write_text("[[[", encoding="utf-8")
    store = RoleStore()
    assert store.warnings, "the corruption is reported"
    assert "python-developer" in store


def test_search_matches_name_and_description() -> None:
    store = RoleStore()
    assert any(r.name == "code-reviewer" for r in store.search("review"))
    assert store.search("") == store.all()
