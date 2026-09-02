"""The end-of-chat report: four formats, one set of figures."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from vox_chat import report as reporting
from vox_chat.inspection import Alternative, InspectionRun
from vox_chat.models import Message


def sample_run() -> InspectionRun:
    run = InspectionRun(model="qwen2.5:3b", provider="local-ollama", top_k=3)
    run.add(
        "The",
        math.log(0.99),
        [Alternative("The", math.log(0.99)), Alternative("A", math.log(0.01))],
    )
    run.add(
        " of",
        math.log(0.4),
        [
            Alternative(" of", math.log(0.4)),
            Alternative(" in", math.log(0.35)),
            Alternative(" to", math.log(0.25)),
        ],
    )
    return run


def sample_report(
    inspection: InspectionRun | None = None, enabled: bool = True
) -> reporting.Report:
    return reporting.Report(
        title="VOX session · qwen2.5:3b",
        created_at="2026-08-25T11:46:59+02:00",
        provider="local-ollama",
        endpoint="http://localhost:11434/v1",
        model="qwen2.5:3b",
        role="python-developer",
        parameters={"temperature": 0.2, "max_tokens": 60, "agent_mode": False},
        messages=[
            Message(role="user", content="why is the sky blue?"),
            Message(
                role="assistant",
                content="Because of scattering.",
                reasoning="Rayleigh, probably.",
            ),
        ],
        usage={"turns": 1, "completion_tokens": 33, "average_tokens_per_second": 13.63},
        inspection=inspection,
        inspect_enabled=enabled,
    )


def test_the_question_titles_the_report() -> None:
    assert sample_report().question == "why is the sky blue?"
    empty = reporting.Report()
    assert "no question" in empty.question


def test_all_formats_are_written(tmp_path: Path) -> None:
    written = reporting.write(
        sample_report(sample_run()), directory=tmp_path, stem="run"
    )
    assert [path.name for path in written] == [
        "run.html",
        "run.json",
        "run.md",
        "run.toon",
    ]
    assert all(path.exists() and path.stat().st_size > 0 for path in written)


def test_an_unknown_format_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        reporting.write(sample_report(), formats=("pdf",), directory=tmp_path)


def test_the_html_carries_no_javascript(tmp_path: Path) -> None:
    """The page has to read correctly with JavaScript disabled."""
    html = reporting.render_html(sample_report(sample_run()))
    assert "<script" not in html.lower()
    assert "onclick" not in html.lower()
    assert html.strip().startswith("<!doctype html>")


def test_the_html_states_question_setup_output_and_figures() -> None:
    html = reporting.render_html(sample_report(sample_run()))
    assert "why is the sky blue?" in html
    assert "qwen2.5:3b" in html and "http://localhost:11434/v1" in html
    assert "temperature" in html and "0.2" in html
    assert "Because of scattering." in html
    assert "Rayleigh, probably." in html, "thinking is kept, and kept separate"
    assert "top-k only" in html, "the entropy basis is always stated"


def test_the_html_escapes_what_the_model_wrote() -> None:
    report = sample_report()
    report.messages = [Message(role="assistant", content="<script>alert(1)</script>")]
    html = reporting.render_html(report)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_the_json_is_one_documented_schema(tmp_path: Path) -> None:
    path = reporting.write(
        sample_report(sample_run()), formats=("json",), directory=tmp_path, stem="run"
    )[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "vox.report/1"
    assert data["question"] == "why is the sky blue?"
    assert data["model"] == "qwen2.5:3b"
    assert data["parameters"]["max_tokens"] == 60
    assert len(data["inspection"]["tokens"]) == 2
    assert data["inspection"]["statistics"]["decision_points"] == 1


def test_markdown_keeps_only_the_decision_points() -> None:
    markdown = reporting.render_markdown(sample_report(sample_run()))
    assert "## Statistics" in markdown
    assert "### Decision points" in markdown
    assert "` of`" in markdown, "the flat position is listed"
    assert markdown.count("|") < 120, "the full token table would be unreadable here"


def test_the_same_figures_appear_in_every_format(tmp_path: Path) -> None:
    run = sample_run()
    report = sample_report(run)
    html = reporting.render_html(report)
    markdown = reporting.render_markdown(report)
    data = json.loads(
        reporting.write(report, formats=("json",), directory=tmp_path, stem="r")[
            0
        ].read_text(encoding="utf-8")
    )
    decisions = data["inspection"]["statistics"]["decision_points"]
    assert decisions == len(run.decisions) == 1
    for rendered in (html, markdown):
        assert "13.63" in rendered, "the speed is the same everywhere"
        assert str(decisions) in rendered


def test_toon_is_a_valid_document_and_carries_the_same_figures(tmp_path: Path) -> None:
    """TOON keeps the same figures, rendered in the most token-efficient way."""
    run = sample_run()
    report = sample_report(run)
    toon = reporting.render_toon(report)
    assert toon.startswith("schema: vox.report/1")
    assert "question: why is the sky blue?" in toon
    assert 'model: "qwen2.5:3b"' in toon, "a colon forces quoting"
    assert "Because of scattering." in toon
    assert "Rayleigh, probably." in toon, "thinking is kept"
    assert "13.63" in toon, "the speed is the same here as everywhere"
    assert "top-k only" in toon, "the entropy basis is always stated"
    assert not toon.endswith("\n"), "TOON forbids a trailing newline"
    # The notes and empty arrays use the compact forms the format provides.
    assert "notes: []" in toon
    # Every array carries its declared length.
    assert "messages[2]:" in toon
    assert "tokens[2]:" in toon


def test_toon_escapes_and_quotes_what_the_model_wrote() -> None:
    report = sample_report()
    report.messages = [
        Message(role="assistant", content="<script>alert(1)</script>"),
        Message(role="user", content="- a dash-led string with: a colon"),
    ]
    toon = reporting.render_toon(report)
    assert "<script>alert(1)</script>" in toon
    # A dash-led, colon-bearing string must be quoted so it never reads as a
    # list marker or a key-value line.
    assert '"- a dash-led string with: a colon"' in toon


def test_toon_written_by_the_write_path_matches_render(tmp_path: Path) -> None:
    report = sample_report()
    path = reporting.write(report, directory=tmp_path, stem="r", formats=("toon",))[0]
    assert path.suffix == ".toon"
    assert path.exists() and path.stat().st_size > 0
    assert reporting.render_toon(report) == path.read_text(encoding="utf-8")


def test_a_session_without_inspection_still_exports(tmp_path: Path) -> None:
    report = sample_report(inspection=None, enabled=False)
    html = reporting.render_html(report)
    markdown = reporting.render_markdown(report)
    assert "Inspection was off" in html
    assert "Inspection was off" in markdown
    assert "Because of scattering." in html, "the exchange is there regardless"
    assert reporting.write(report, directory=tmp_path, stem="off")


def test_inspection_on_but_empty_says_so() -> None:
    html = reporting.render_html(sample_report(inspection=None, enabled=True))
    assert "no token data arrived" in html


def test_reports_default_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They belong to the work, not to a hidden folder in the home."""
    monkeypatch.chdir(tmp_path)
    written = reporting.write(sample_report(), formats=("md",), stem="here")
    assert written[0] == tmp_path / "here.md"
    assert written[0].exists()


def test_the_default_stem_is_sortable() -> None:
    from datetime import datetime

    stem = reporting.default_stem(datetime(2026, 8, 25, 14, 32, 5))
    assert stem == "vox-20260825-143205"
