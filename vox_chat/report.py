"""The end-of-chat report, in HTML, JSON, Markdown and TOON.

Written into the directory VOX was started in, as vox-<timestamp>.<ext>.

One structure, four writers, the same figures in each. The HTML carries no
JavaScript at all, which is the simplest way to keep the promise that it reads
correctly with JavaScript disabled.

TOON (Token-Oriented Object Notation) is a line-oriented, indentation-based
encoding of the JSON data model (spec v4.1, https://toonformat.dev/). It is
added as a fourth format because it is the most token-efficient rendering of
the same figures for an LLM to read back.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .inspection import InspectionRun
from .models import Message
from .storage import write_json_atomic

FORMATS = ("html", "json", "md", "toon")

_ROLE_LABEL = {
    "user": "QUESTION",
    "assistant": "ANSWER",
    "system": "SYSTEM",
    "tool": "TOOL",
    "error": "ERROR",
    "reasoning": "THINKING",
}


def default_stem(when: datetime | None = None) -> str:
    moment = when or datetime.now()
    return f"vox-{moment:%Y%m%d-%H%M%S}"


@dataclass
class Report:
    """Everything a saved run contains, before it is rendered."""

    title: str = "VOX session"
    created_at: str = ""
    # The session this came from, so a report, a saved session and a peer's
    # log of being asked can all be tied back to the same conversation.
    conversation_id: str = ""
    vox_version: str = __version__
    provider: str = ""
    endpoint: str = ""
    model: str = ""
    role: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    inspection: InspectionRun | None = None
    inspect_enabled: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def question(self) -> str:
        """The first thing the operator asked, which titles the report."""
        for message in self.messages:
            if message.role == "user" and message.content.strip():
                return message.content.strip()
        return "(no question in this session)"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": "vox.report/1",
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "vox_version": self.vox_version,
            "question": self.question,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "role": self.role,
            "parameters": self.parameters,
            "messages": [message.to_dict() for message in self.messages],
            "usage": self.usage,
            "inspection_enabled": self.inspect_enabled,
            "notes": self.notes,
        }
        if self.inspection is not None:
            data["inspection"] = self.inspection.to_dict(include_tokens=True)
        return data


# ------------------------------------------------------------------ helpers


def _pairs(mapping: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten a mapping into label/value pairs, one level deep."""
    rows: list[tuple[str, str]] = []
    for key, value in mapping.items():
        label = key.replace("_", " ")
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                rows.append((f"{label} · {sub_key.replace('_', ' ')}", _fmt(sub_value)))
        else:
            rows.append((label, _fmt(value)))
    return rows


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _phase_line(run: InspectionRun) -> str:
    thinking = run.phase_stats("thinking")
    answer = run.phase_stats("answer")
    if thinking.tokens and answer.tokens:
        return (
            f"thinking {thinking.mean_probability:.2f} mean probability over "
            f"{thinking.tokens} tokens · answer {answer.mean_probability:.2f} "
            f"over {answer.tokens}"
        )
    if answer.tokens:
        return f"answer only: {answer.mean_probability:.2f} mean probability"
    return "no per-phase figures"


# --------------------------------------------------------------------- JSON


def write_json(report: Report, path: Path) -> Path:
    write_json_atomic(path, report.to_dict())
    return path


# ----------------------------------------------------------------- Markdown


def render_markdown(report: Report) -> str:
    lines = [
        f"# {report.title}",
        "",
        f"**Question** — {report.question}",
        "",
        "## Setup",
        "",
        "| | |",
        "| --- | --- |",
        f"| model | `{report.model}` |",
        f"| provider | `{report.provider}` |",
        f"| endpoint | `{report.endpoint}` |",
        f"| role | `{report.role}` |",
        f"| conversation | `{report.conversation_id}` |",
    ]
    lines.extend(f"| {label} | {value} |" for label, value in _pairs(report.parameters))
    lines.extend(["", "## Exchange", ""])
    for message in report.messages:
        if message.role == "error":
            continue
        label = _ROLE_LABEL.get(message.role, message.role.upper())
        if message.reasoning:
            lines.extend(["**THINKING**", "", "```text", message.reasoning, "```", ""])
        lines.extend([f"**{label}**", "", message.content or "_(empty)_", ""])

    lines.extend(["## Statistics", "", "| | |", "| --- | --- |"])
    lines.extend(f"| {label} | {value} |" for label, value in _pairs(report.usage))

    run = report.inspection
    if run is not None and len(run):
        stats = run.stats()
        lines.extend(
            f"| {label} | {value} |"
            for label, value in _pairs(
                {
                    k: v
                    for k, v in stats.items()
                    if k not in ("criteria", "thinking", "answer")
                }
            )
        )
        lines.extend(["", f"_{_phase_line(run)}_", ""])
        decisions = run.decisions
        if decisions:
            # The full token table is unreadable in Markdown; the decision
            # points are the part worth reading here.
            lines.extend(
                [
                    "### Decision points",
                    "",
                    "| # | token | p | top-k entropy | margin | runners-up |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for record in decisions:
                runners = " · ".join(
                    f"`{a.token}` {a.probability:.2f}" for a in record.runners_up[:3]
                )
                lines.append(
                    f"| {record.index} | `{record.text}` | {record.probability:.2f} "
                    f"| {record.entropy:.2f} bit | {record.margin:.2f} | {runners} |"
                )
    elif report.inspect_enabled:
        lines.extend(["", "_Inspection was on but no token data arrived._"])
    else:
        lines.extend(["", "_Inspection was off, so there are no token figures._"])

    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- VOX {report.vox_version}, {report.created_at}",
            "- entropy is computed over the returned top-k only; the API does not "
            "return the tail of the vocabulary",
        ]
    )
    lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines) + "\n"


def write_markdown(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------- HTML

_CSS = """
:root { color-scheme: dark; }
body { background:#16150f; color:#cfc7b0; margin:0; padding:2rem;
       font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       font-size:14px; line-height:1.55; }
main { max-width:64rem; margin:0 auto; }
h1 { font-size:1.4rem; color:#c9a15a; margin:0 0 .3rem; }
h2 { font-size:1rem; color:#c9a15a; letter-spacing:.12em; text-transform:uppercase;
     margin:2.2rem 0 .6rem; border-bottom:1px solid #45402f; padding-bottom:.3rem; }
h3 { font-size:.95rem; color:#8da287; margin:1.4rem 0 .4rem; }
p.question { color:#8ea7bb; margin:0 0 1.4rem; }
table { border-collapse:collapse; width:100%; margin:.4rem 0 1rem; }
td, th { border-bottom:1px solid #2b2820; padding:.28rem .5rem; text-align:left;
         vertical-align:top; }
th { color:#8b8266; font-weight:normal; }
td.k { color:#8b8266; width:16rem; }
pre { background:#1c1a14; border:1px solid #2b2820; padding:.8rem; overflow-x:auto;
      white-space:pre-wrap; word-break:break-word; }
.msg { margin:0 0 1.2rem; }
.who { color:#c9a15a; letter-spacing:.1em; font-size:.8rem; }
.thinking pre { color:#6b6349; font-style:italic; }
.decision td { background:#211d14; }
.decision td:first-child::after { content:" ◄"; color:#c9a15a; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.hot { color:#c9a15a; } .calm { color:#8da287; }
footer { color:#6b6349; margin-top:2.5rem; border-top:1px solid #2b2820;
         padding-top:.8rem; font-size:.85rem; }
"""


def _rows_html(pairs: Sequence[tuple[str, str]]) -> str:
    return "\n".join(
        f"<tr><td class='k'>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>"
        for label, value in pairs
    )


def render_html(report: Report) -> str:
    """A single self-contained page. No script tags, by design."""
    parts: list[str] = [
        "<!doctype html>",
        "<html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(report.title)}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>{html.escape(report.title)}</h1>",
        f"<p class='question'>{html.escape(report.question)}</p>",
        "<h2>Setup</h2><table>",
        _rows_html(
            [
                ("model", report.model),
                ("provider", report.provider),
                ("endpoint", report.endpoint),
                ("role", report.role),
                ("conversation", report.conversation_id),
                *_pairs(report.parameters),
            ]
        ),
        "</table>",
        "<h2>Exchange</h2>",
    ]

    for message in report.messages:
        if message.role == "error":
            continue
        label = _ROLE_LABEL.get(message.role, message.role.upper())
        if message.reasoning:
            parts.append(
                "<div class='msg thinking'><div class='who'>THINKING</div>"
                f"<pre>{html.escape(message.reasoning)}</pre></div>"
            )
        parts.append(
            f"<div class='msg'><div class='who'>{html.escape(label)}</div>"
            f"<pre>{html.escape(message.content or '(empty)')}</pre></div>"
        )

    parts.append("<h2>Statistics</h2><table>")
    parts.append(_rows_html(_pairs(report.usage)))
    run = report.inspection
    if run is not None and len(run):
        stats = run.stats()
        parts.append(
            _rows_html(
                _pairs({k: v for k, v in stats.items() if k not in ("criteria",)})
            )
        )
        parts.append("</table>")
        parts.append(f"<p>{html.escape(_phase_line(run))}</p>")
        parts.append("<h3>Decision points</h3><table>")
        parts.append(
            "<tr><th>#</th><th>token</th><th class='num'>p</th>"
            "<th class='num'>top-k entropy</th><th class='num'>margin</th>"
            "<th>runners-up</th></tr>"
        )
        if run.decisions:
            for record in run.decisions:
                runners = " · ".join(
                    f"{a.token!r} {a.probability:.2f}" for a in record.runners_up[:3]
                )
                parts.append(
                    "<tr class='decision'>"
                    f"<td>{record.index}</td><td>{html.escape(repr(record.text))}</td>"
                    f"<td class='num'>{record.probability:.2f}</td>"
                    f"<td class='num'>{record.entropy:.2f}</td>"
                    f"<td class='num'>{record.margin:.2f}</td>"
                    f"<td>{html.escape(runners)}</td></tr>"
                )
        else:
            parts.append("<tr><td colspan='6'>none met the criteria</td></tr>")
        parts.append("</table>")
        parts.append("<h3>Criteria used</h3><table>")
        parts.append(_rows_html(_pairs(run.criteria.to_dict())))
        parts.append("</table>")
        parts.append("<h3>Every token</h3><table>")
        parts.append(
            "<tr><th>#</th><th>token</th><th>phase</th><th class='num'>p</th>"
            "<th class='num'>top-k entropy</th><th class='num'>margin</th></tr>"
        )
        for record in run.records:
            css = "decision" if record.is_decision else ""
            tone = "hot" if record.entropy >= run.criteria.entropy_threshold else "calm"
            parts.append(
                f"<tr class='{css}'><td>{record.index}</td>"
                f"<td>{html.escape(repr(record.text))}</td>"
                f"<td>{record.phase}</td>"
                f"<td class='num'>{record.probability:.2f}</td>"
                f"<td class='num {tone}'>{record.entropy:.2f}</td>"
                f"<td class='num'>{record.margin:.2f}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("</table>")
        reason = (
            "Inspection was on, but no token data arrived from the provider."
            if report.inspect_enabled
            else "Inspection was off, so there are no token figures."
        )
        parts.append(f"<p>{html.escape(reason)}</p>")

    notes = "".join(f"<div>{html.escape(note)}</div>" for note in report.notes)
    parts.append(
        "<footer>"
        f"<div>VOX {html.escape(report.vox_version)} · {html.escape(report.created_at)}</div>"
        "<div>Entropy is computed over the returned top-k only; the API does not "
        "return the tail of the vocabulary.</div>"
        "<div>These are measurements of the output distribution, not statements "
        "about what the model was doing.</div>"
        f"{notes}</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"


def write_html(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------- TOON

# TOON, Token-Oriented Object Notation, spec v4.1 (https://toonformat.dev/).
# A line-oriented, indentation-based encoding of the JSON data model. The
# report's figures are rendered here from the very same dict that JSON uses,
# so every format shows the same numbers.

_INDENT_SIZE = 2
_TOON_DOC_DELIMITER = ","

_UNQUOTED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# An unquoted token that would decode as a number must be quoted (spec §7.2).
_TOON_NUMBER_LIKE_RE = re.compile(
    r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.IGNORECASE
)
_TOON_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toon_indent(depth: int) -> str:
    return " " * (_INDENT_SIZE * depth)


def _toon_quote(s: str) -> str:
    """Quote and escape a string per TOON §7.1."""
    out = ['"']
    for ch in s:
        esc = _TOON_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toon_needs_quotes(s: str, delimiter: str) -> bool:
    """TOON §7.2: the conditions under which a string value must be quoted."""
    if s == "":
        return True
    if s[0] in " \t" or s[-1] in " \t":
        return True
    if s in ("true", "false", "null"):
        return True
    if _TOON_NUMBER_LIKE_RE.match(s):
        return True
    for ch in ':"\\[]{}':
        if ch in s:
            return True
    if delimiter in s:
        return True
    if any(ord(ch) < 0x20 for ch in s):
        return True
    return s[0] in "#-"


def _toon_string(s: str, delimiter: str) -> str:
    return _toon_quote(s) if _toon_needs_quotes(s, delimiter) else s


def _toon_key(key: str) -> str:
    """Encode an object key / field name per TOON §7.3."""
    if _UNQUOTED_KEY_RE.match(key):
        return key
    return _toon_quote(key)


def _toon_number(value: int | float) -> str:
    """Canonical number form (§2): integers, -0→0, floats without exponents."""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        value = 0.0
    if value.is_integer():
        return str(int(value))
    return repr(value)


def _toon_scalar(value: Any, delimiter: str) -> str:
    """Render a primitive (null/bool/number/string) as a TOON token."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _toon_number(value)
    return _toon_string(str(value), delimiter)


def _is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _is_uniform_column(column: Sequence[Any]) -> bool:
    """A column is uniform-primitive or nested-uniform (§9.3)."""
    if not column:
        return False
    if all(_is_primitive(v) for v in column):
        return True
    if all(isinstance(v, dict) and v for v in column):
        subkeys = set(column[0])
        if any(set(v) != subkeys for v in column):
            return False
        return all(_is_uniform_column([v[k] for v in column]) for k in column[0])
    return False


def _is_tabular(elements: Sequence[Any]) -> bool:
    """Does this array of objects qualify for the tabular form (§9.3)?"""
    if not elements:
        return False
    if any(not isinstance(v, dict) or not v for v in elements):
        return False
    keys = set(elements[0])
    if not keys or any(set(v) != keys for v in elements):
        return False
    return all(_is_uniform_column([v[k] for v in elements]) for k in elements[0])


def _is_keyed_tabular(mapping: dict[str, Any]) -> bool:
    """Does this object qualify for the keyed tabular form (§9.5)?"""
    values = list(mapping.values())
    if len(values) < 2:
        return False
    if any(not isinstance(v, dict) or not v for v in values):
        return False
    keys = set(values[0])
    if any(set(v) != keys for v in values):
        return False
    return all(_is_uniform_column([v[k] for v in values]) for k in values[0])


def _field_entry(k: str, elements: Sequence[dict[str, Any]]) -> Any:
    """One field in a tabular header: a leaf name or a (name, [entries]) group."""
    column = [e[k] for e in elements]
    if all(_is_primitive(v) for v in column):
        return k
    return (k, [_field_entry(sk, [e[k] for e in elements]) for sk in elements[0][k]])


def _field_entries(elements: Sequence[dict[str, Any]]) -> list[Any]:
    return [_field_entry(k, elements) for k in elements[0]]


def _field_header(entry: Any, delimiter: str) -> str:
    """Render a field entry for the header's brace list."""
    if isinstance(entry, str):
        return _toon_key(entry)
    name, subs = entry
    inner = delimiter.join(_field_header(s, delimiter) for s in subs)
    return f"{_toon_key(name)}{{{inner}}}"


def _row_cells(
    element: dict[str, Any], fields: Sequence[Any], delimiter: str
) -> list[str]:
    """Leaf cells for one tabular/entry row, in depth-first pre-order."""
    cells: list[str] = []
    for entry in fields:
        if isinstance(entry, str):
            cells.append(_toon_scalar(element[entry], delimiter))
        else:
            name, subs = entry
            cells.extend(_row_cells(element[name], subs, delimiter))
    return cells


def _delim_symbol(delimiter: str) -> str:
    """The optional delimiter symbol declared inside the header brackets."""
    return "" if delimiter == "," else delimiter


def _toon_object(
    mapping: dict[str, Any], depth: int, lines: list[str], delimiter: str
) -> None:
    for key, value in mapping.items():
        _toon_field(key, value, depth, lines, delimiter)


def _toon_field(
    key: str, value: Any, depth: int, lines: list[str], delimiter: str
) -> None:
    prefix = _toon_indent(depth)
    if isinstance(value, dict):
        if value and _is_keyed_tabular(value):
            _toon_keyed_field(key, value, depth, lines, delimiter)
        else:
            lines.append(f"{prefix}{_toon_key(key)}:")
            if value:
                _toon_object(value, depth + 1, lines, delimiter)
    elif isinstance(value, list):
        _toon_array_field(key, value, depth, lines, delimiter)
    else:
        lines.append(f"{prefix}{_toon_key(key)}: {_toon_scalar(value, delimiter)}")


def _toon_keyed_field(
    key: str, value: dict[str, Any], depth: int, lines: list[str], delimiter: str
) -> None:
    entries = list(value.items())
    fields = _field_entries([v for _, v in entries])
    fieldstr = delimiter.join(_field_header(f, delimiter) for f in fields)
    header = (
        f"{_toon_indent(depth)}{_toon_key(key)}"
        f"[{len(entries)}:{_delim_symbol(delimiter)}]{{{fieldstr}}}:"
    )
    lines.append(header)
    for entry_key, entry_value in entries:
        cells = _row_cells(entry_value, fields, delimiter)
        lines.append(
            f"{_toon_indent(depth + 1)}{_toon_key(entry_key)}: {delimiter.join(cells)}"
        )


def _toon_array_field(
    key: str, value: list[Any], depth: int, lines: list[str], delimiter: str
) -> None:
    prefix = _toon_indent(depth)
    if not value:
        lines.append(f"{prefix}{_toon_key(key)}: []")
    elif _is_tabular(value):
        fields = _field_entries(value)
        fieldstr = delimiter.join(_field_header(f, delimiter) for f in fields)
        lines.append(
            f"{prefix}{_toon_key(key)}"
            f"[{len(value)}{_delim_symbol(delimiter)}]{{{fieldstr}}}:"
        )
        for element in value:
            lines.append(
                _toon_indent(depth + 1)
                + delimiter.join(_row_cells(element, fields, delimiter))
            )
    elif all(_is_primitive(e) for e in value):
        inline = delimiter.join(_toon_scalar(e, delimiter) for e in value)
        lines.append(
            f"{prefix}{_toon_key(key)}"
            f"[{len(value)}{_delim_symbol(delimiter)}]: {inline}"
        )
    else:
        lines.append(
            f"{prefix}{_toon_key(key)}[{len(value)}{_delim_symbol(delimiter)}]:"
        )
        for element in value:
            _toon_list_item(element, depth + 1, lines, delimiter)


def _toon_list_item(element: Any, depth: int, lines: list[str], delimiter: str) -> None:
    """One element of a list-form array at ``depth`` (§9.2, §9.4, §10)."""
    prefix = _toon_indent(depth)
    if isinstance(element, dict):
        if not element:
            lines.append(prefix + "-")
            return
        items = list(element.items())
        first_key, first_value = items[0]
        _toon_list_object_first(prefix, first_key, first_value, depth, lines, delimiter)
        for key, value in items[1:]:
            _toon_field(key, value, depth + 1, lines, delimiter)
    elif isinstance(element, list):
        if not element:
            lines.append(prefix + f"- [0{_delim_symbol(delimiter)}]:")
        elif all(_is_primitive(e) for e in element):
            inline = delimiter.join(_toon_scalar(e, delimiter) for e in element)
            lines.append(
                f"{prefix}- [{len(element)}{_delim_symbol(delimiter)}]: {inline}"
            )
        else:
            lines.append(f"{prefix}- [{len(element)}{_delim_symbol(delimiter)}]:")
            for e in element:
                _toon_list_item(e, depth + 1, lines, delimiter)
    else:
        lines.append(prefix + "- " + _toon_scalar(element, delimiter))


def _toon_list_object_first(
    prefix: str, key: str, value: Any, depth: int, lines: list[str], delimiter: str
) -> None:
    """The first field of a list-item object sits on the hyphen line (§10)."""
    if isinstance(value, dict):
        if value and _is_keyed_tabular(value):
            entries = list(value.items())
            fields = _field_entries([v for _, v in entries])
            fieldstr = delimiter.join(_field_header(f, delimiter) for f in fields)
            lines.append(
                f"{prefix}- {_toon_key(key)}"
                f"[{len(entries)}:{_delim_symbol(delimiter)}]{{{fieldstr}}}:"
            )
            for entry_key, entry_value in entries:
                cells = _row_cells(entry_value, fields, delimiter)
                lines.append(
                    f"{_toon_indent(depth + 2)}{_toon_key(entry_key)}: "
                    f"{delimiter.join(cells)}"
                )
        else:
            lines.append(f"{prefix}- {_toon_key(key)}:")
            if value:
                _toon_object(value, depth + 2, lines, delimiter)
    elif isinstance(value, list):
        _toon_list_object_array(prefix, key, value, depth, lines, delimiter)
    else:
        lines.append(f"{prefix}- {_toon_key(key)}: {_toon_scalar(value, delimiter)}")


def _toon_list_object_array(
    prefix: str,
    key: str,
    value: list[Any],
    depth: int,
    lines: list[str],
    delimiter: str,
) -> None:
    if not value:
        lines.append(f"{prefix}- {_toon_key(key)}: []")
    elif _is_tabular(value):
        fields = _field_entries(value)
        fieldstr = delimiter.join(_field_header(f, delimiter) for f in fields)
        lines.append(
            f"{prefix}- {_toon_key(key)}"
            f"[{len(value)}{_delim_symbol(delimiter)}]{{{fieldstr}}}:"
        )
        for element in value:
            lines.append(
                _toon_indent(depth + 2)
                + delimiter.join(_row_cells(element, fields, delimiter))
            )
    elif all(_is_primitive(e) for e in value):
        inline = delimiter.join(_toon_scalar(e, delimiter) for e in value)
        lines.append(
            f"{prefix}- {_toon_key(key)}"
            f"[{len(value)}{_delim_symbol(delimiter)}]: {inline}"
        )
    else:
        lines.append(
            f"{prefix}- {_toon_key(key)}[{len(value)}{_delim_symbol(delimiter)}]:"
        )
        for element in value:
            _toon_list_item(element, depth + 2, lines, delimiter)


def render_toon(report: Report) -> str:
    """Render the report as a TOON document (spec v4.1).

    The figures come from the same ``Report.to_dict`` the JSON writer uses,
    so the TOON output carries exactly the same data.
    """
    lines: list[str] = []
    _toon_object(report.to_dict(), 0, lines, _TOON_DOC_DELIMITER)
    return "\n".join(lines)


def write_toon(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # TOON forbids a trailing newline (§12).
    path.write_text(render_toon(report), encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------------ writing


def write(
    report: Report,
    formats: Sequence[str] = FORMATS,
    directory: Path | None = None,
    stem: str | None = None,
) -> list[Path]:
    """Write the report in each requested format; returns the files written."""
    # Reports belong to the work, so they land in the current directory
    # unless the caller names another one.
    target = Path(directory) if directory is not None else Path.cwd()
    name = stem or default_stem()
    written: list[Path] = []
    for fmt in formats:
        path = target / f"{name}.{fmt}"
        if fmt == "json":
            written.append(write_json(report, path))
        elif fmt == "md":
            written.append(write_markdown(report, path))
        elif fmt == "html":
            written.append(write_html(report, path))
        elif fmt == "toon":
            written.append(write_toon(report, path))
        else:
            raise ValueError(f"unknown report format: {fmt}")
    return written
