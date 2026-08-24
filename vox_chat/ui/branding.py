"""Visual identity: header, boot banner and the colour palettes.

The default look is 1970s mission control: muted, low-saturation colours on a
warm dark panel, thin rules, no neon. Every frame is sized from its own
content, so a long endpoint can never break the alignment.
"""

from __future__ import annotations

from .. import __version__

SUBTITLE = "TERMINAL FOR LOCAL MODELS"
READY_LINE = "SYSTEM READY."

_SEPARATOR = "  ·  "


MIN_FRAME_WIDTH = 40


def fit(line: str, width: int) -> str:
    """Clip ``line`` to ``width`` columns, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    return line[: max(1, width - 1)] + "…"


def _frame(lines: list[str], width: int | None = None) -> str:
    """Draw a thin box, either sized to its content or to ``width`` columns."""
    inner = max(len(line) for line in lines) if width is None else max(1, width - 4)
    top = "┌" + "─" * (inner + 2) + "┐"
    bottom = "└" + "─" * (inner + 2) + "┘"
    body = [f"│ {fit(line, inner).ljust(inner)} │" for line in lines]
    return "\n".join([top, *body, bottom])


def header_lines(link: str, logon: str, provider: str, model: str,
                 role: str) -> list[str]:
    """The two information rows shown at the top of the screen."""
    return [
        f"VOX {__version__}{_SEPARATOR}LINK {link}",
        _SEPARATOR.join(
            [f"PROVIDER {provider}", f"MODEL {model}", f"ROLE {role}", f"LOGON {logon}"]
        ),
    ]


def logo(link: str = "OFFLINE", logon: str = "********", provider: str = "",
         model: str = "", role: str = "", style: str = "frame",
         width: int | None = None) -> str:
    """Render the header in the requested style, fitted to ``width`` columns.

    ``frame`` draws a thin box, ``bar`` drops the box and relies on the CSS
    rule underneath, ``none`` compresses everything onto one line. A terminal
    too narrow for a box degrades to ``bar`` instead of wrapping.
    """
    lines = header_lines(link, logon, provider, model, role)
    compact = f"VOX {__version__}{_SEPARATOR}{model}{_SEPARATOR}LINK {link}"
    if style == "none":
        return fit(compact, width) if width else compact
    if style == "frame" and (width is None or width >= MIN_FRAME_WIDTH):
        return _frame(lines, width)
    if width:
        return "\n".join(fit(line, width) for line in lines)
    return "\n".join(lines)


def splash(model: str, role: str, workspace: str = "") -> str:
    """The boot banner written into an empty transcript.

    It deliberately does not repeat the header frame: the header is already
    on screen, one row above.
    """
    title = f"VOX {__version__}  —  {SUBTITLE}"
    lines = [
        title,
        "─" * len(title),
        READY_LINE,
        "",
        f"MODEL      {model}",
        f"ROLE       {role}",
    ]
    if workspace:
        lines.append(f"WORKSPACE  {workspace}")
    lines.extend(["", "TYPE /help FOR THE COMMAND LIST."])
    return "\n".join(lines)


def status_line(connected: bool, generating: bool, agent: bool,
                workspace: str, spinner: str = "") -> str:
    """The bottom status bar text, in terminal-report style."""
    link = "ONLINE" if connected else "OFFLINE"
    state = f"{spinner} GENERATING".strip() if generating else "IDLE"
    mode = "AGENT" if agent else "CHAT"
    return _SEPARATOR.join([f"LINK {link}", state, f"MODE {mode}", f"WS {workspace}"])


# Mission control: bone white and muted amber on a warm charcoal panel.
NASA_CSS = """
Screen {
    background: #16150f;
    color: #cfc7b0;
}
#header {
    height: auto;
    padding: 0 1;
    color: #c9a15a;
    background: #1c1a14;
    border-bottom: solid #45402f;
}
#body { height: 1fr; }
#transcript {
    height: 1fr;
    padding: 0 1;
    background: #16150f;
    scrollbar-background: #1c1a14;
    scrollbar-color: #45402f;
    scrollbar-color-hover: #6b6349;
}
#side-panel {
    width: 34;
    display: none;
    padding: 0 1;
    border-left: solid #45402f;
    background: #1c1a14;
}
#side-panel.visible { display: block; }
#side-panel.code { width: 58; max-width: 60%; }
#side-title { text-style: bold; color: #c9a15a; }
#input-area {
    height: auto;
    max-height: 12;
    border-top: solid #45402f;
    background: #16150f;
}
#input {
    height: auto;
    min-height: 3;
    max-height: 10;
    background: #1c1a14;
    color: #d8d2c0;
    border: none;
}
#status {
    height: 1;
    padding: 0 1;
    color: #8b8266;
    background: #1c1a14;
}
#keybar {
    height: 1;
    padding: 0 1;
    color: #c9a15a;
    background: #24211a;
}
.msg { margin-bottom: 1; }
.msg-user { color: #8ea7bb; }
.msg-assistant { color: #cfc7b0; }
.msg-system { color: #8da287; }
.msg-tool { color: #c9a15a; }
.msg-error { color: #c47a5d; }
.msg-reasoning { color: #6b6349; text-style: italic; }
ModalScreen { align: center middle; }
#modal-box {
    width: 82;
    max-width: 96%;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: #1c1a14;
    border: round #6b6349;
}
#modal-title { text-style: bold; color: #c9a15a; margin-bottom: 1; }
#modal-buttons { height: auto; align-horizontal: right; }
#modal-hint { height: auto; margin-top: 1; color: #8b8266; }
Button {
    background: #24211a;
    color: #cfc7b0;
    border: tall #45402f;
}
Button:hover { background: #45402f; }
Button.-success { color: #8da287; }
Button.-error { color: #c47a5d; }
ListView { background: #1c1a14; height: auto; max-height: 20; }
ListItem { background: #1c1a14; color: #cfc7b0; }
ListItem.--highlight { background: #45402f; color: #e6dfc9; }
Input { background: #24211a; color: #d8d2c0; border: tall #45402f; }
TextArea { background: #1c1a14; color: #d8d2c0; }
"""

DARK_CSS = """
Screen { background: #101014; color: #d6d6d6; }
#header { height: auto; padding: 0 1; color: #9aa7b8; border-bottom: solid #35353d; }
#body { height: 1fr; }
#transcript { height: 1fr; padding: 0 1; }
#side-panel {
    width: 34; display: none; padding: 0 1; border-left: solid #35353d;
}
#side-panel.visible { display: block; }
#side-panel.code { width: 58; max-width: 60%; }
#side-title { text-style: bold; }
#input-area { height: auto; max-height: 12; border-top: solid #35353d; }
#input { height: auto; min-height: 3; max-height: 10; }
#status { height: 1; padding: 0 1; background: #1a1a20; color: #8b8b98; }
#keybar { height: 1; padding: 0 1; background: #24242c; color: #a8b0bd; }
.msg { margin-bottom: 1; }
.msg-user { color: #8ab4d8; }
.msg-assistant { color: #d6d6d6; }
.msg-system { color: #98a898; }
.msg-tool { color: #d0b070; }
.msg-error { color: #d08070; }
.msg-reasoning { color: #6c6c78; text-style: italic; }
ModalScreen { align: center middle; }
#modal-box {
    width: 82; max-width: 96%; height: auto; max-height: 90%;
    padding: 1 2; background: #1a1a20; border: round #45454f;
}
#modal-title { text-style: bold; margin-bottom: 1; }
#modal-buttons { height: auto; align-horizontal: right; }
#modal-hint { height: auto; margin-top: 1; text-style: dim; }
ListView { height: auto; max-height: 20; }
"""

LIGHT_CSS = """
Screen { background: #f4f2ec; color: #26241e; }
#header { height: auto; padding: 0 1; color: #7a5c23; border-bottom: solid #cbc5b4; }
#body { height: 1fr; }
#transcript { height: 1fr; padding: 0 1; }
#side-panel {
    width: 34; display: none; padding: 0 1; border-left: solid #cbc5b4;
}
#side-panel.visible { display: block; }
#side-panel.code { width: 58; max-width: 60%; }
#side-title { text-style: bold; }
#input-area { height: auto; max-height: 12; border-top: solid #cbc5b4; }
#input { height: auto; min-height: 3; max-height: 10; }
#status { height: 1; padding: 0 1; background: #e6e2d6; color: #5c584a; }
#keybar { height: 1; padding: 0 1; background: #dcd7c8; color: #4a463a; }
.msg { margin-bottom: 1; }
.msg-user { color: #305a7a; }
.msg-assistant { color: #26241e; }
.msg-system { color: #4d6b48; }
.msg-tool { color: #85601c; }
.msg-error { color: #9c4a2f; }
.msg-reasoning { color: #7c7768; text-style: italic; }
ModalScreen { align: center middle; }
#modal-box {
    width: 82; max-width: 96%; height: auto; max-height: 90%;
    padding: 1 2; background: #fffdf7; border: round #cbc5b4;
}
#modal-title { text-style: bold; margin-bottom: 1; }
#modal-buttons { height: auto; align-horizontal: right; }
#modal-hint { height: auto; margin-top: 1; text-style: dim; }
ListView { height: auto; max-height: 20; }
"""

THEMES = {"nasa": NASA_CSS, "dark": DARK_CSS, "light": LIGHT_CSS}

LEGACY_THEMES = {"wopr": "nasa"}
"""Names accepted from older configuration files."""

LEGACY_LOGOS = {"norad": "frame"}


def theme_css(name: str) -> str:
    """Return the stylesheet for ``name``, falling back to the default theme."""
    return THEMES.get(LEGACY_THEMES.get(name, name), NASA_CSS)


def logo_style(name: str) -> str:
    """Normalise a configured logo style, accepting the legacy names."""
    return LEGACY_LOGOS.get(name, name)
