"""Prompt CRUD and safe variable substitution."""

from __future__ import annotations

import pytest

from vox_chat.prompts import (
    MissingVariables,
    PromptStore,
    find_variables,
    render,
)


def test_find_variables_keeps_order_and_deduplicates() -> None:
    text = "{{workspace}} then {{file}} and {{workspace}} again"
    assert find_variables(text) == ["workspace", "file"]


def test_render_substitutes_known_values() -> None:
    out = render(
        "in {{workspace}} open {{ file }}", {"workspace": "/tmp/p", "file": "a.py"}
    )
    assert out == "in /tmp/p open a.py"


def test_render_reports_missing_variables() -> None:
    with pytest.raises(MissingVariables) as excinfo:
        render("{{workspace}} {{selection}}", {"workspace": "/tmp/p"})
    assert excinfo.value.names == ["selection"]


def test_render_can_leave_unknown_placeholders_alone() -> None:
    out = render("{{a}} {{b}}", {"a": "1"}, strict=False)
    assert out == "1 {{b}}"


def test_substitution_never_evaluates_code() -> None:
    dangerous = {"workspace": "__import__('os').system('echo pwned')"}
    out = render("run {{workspace}}", dangerous)
    assert out == "run __import__('os').system('echo pwned')"


def test_template_syntax_that_is_not_a_variable_is_left_untouched() -> None:
    text = "{{ 1 + 1 }} and {{}} and { {single} }"
    assert find_variables(text) == []
    assert render(text, {}, strict=False) == text


def test_prompt_crud_round_trip() -> None:
    store = PromptStore()
    store.save_prompt("mine", "hello {{workspace}}", "desc", ["tag"])
    assert PromptStore().require("mine").content == "hello {{workspace}}"

    store.update("mine", description="changed")
    assert store.require("mine").description == "changed"

    store.rename("mine", "yours")
    assert "mine" not in store and "yours" in store

    store.duplicate("yours", "copy")
    assert store.require("copy").tags == ["tag"]

    store.delete("copy")
    assert "copy" not in PromptStore()


def test_search_matches_name_description_and_tags() -> None:
    store = PromptStore()
    store.save_prompt("alpha", "x", "about beta", ["gamma"])
    assert [p.name for p in store.search("gamma")] == ["alpha"]
    assert [p.name for p in store.search("beta")] == ["alpha"]
    assert [p.name for p in store.search("alph")] == ["alpha"]


def test_saving_twice_overwrites_and_refreshes_the_date() -> None:
    store = PromptStore()
    first = store.save_prompt("dup", "one")
    second = store.save_prompt("dup", "two")
    assert len([p for p in store.all() if p.name == "dup"]) == 1
    assert second.content == "two"
    assert second.updated_at >= first.updated_at
