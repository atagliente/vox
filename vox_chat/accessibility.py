"""Quiet mode: a terminal a screen reader can keep up with.

A screen reader on a terminal reads the terminal, not a widget tree, and it
re-announces what changes. VOX repaints its status bar ten times a second
while a model is answering, and the spinner in it is a braille character that
changes every one of those frames — which is a pleasant shimmer to look at and
a stream of interruptions to listen to. The thinking box does the same thing
one line above it.

None of that is a bug for a sighted user, so the answer is not to remove it
but to have a mode where it stops:

- the status bar refreshes **once a second** instead of ten times, and only
  the elapsed seconds change;
- the spinner is not drawn at all — braille frames are read out as characters
  by some readers and skipped entirely by others, and neither is information;
- the thinking box says the same thing in words, so what is announced is
  "waiting for model, 3 seconds" rather than a new glyph every 100ms;
- the boot splash is skipped, because an animation nobody can see is a wait
  nobody asked for.

What this file cannot do is tell you whether it worked. That takes running
VOX under NVDA, JAWS, VoiceOver or Orca and listening, which is the part of
the roadmap that stays open: someone who uses a screen reader daily would
find more in ten minutes than this comment did in a day.

The switch is `ui.screen_reader` in the configuration, or `VOX_SCREEN_READER`
in the environment for a single run. There is no autodetection: no assistive
technology announces itself to a terminal program, and guessing wrong in
either direction is worse than being asked.
"""

from __future__ import annotations

import os
from typing import Any

# How often the status line may change, in seconds. The fast number is the
# animation; the slow one is the smallest interval at which a changing
# "12.3s" is still useful and not yet chatter.
FAST_TICK = 0.1
QUIET_TICK = 1.0

ENV_VAR = "VOX_SCREEN_READER"


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def wanted(
    config: dict[str, Any] | None = None, environ: dict[str, str] | None = None
) -> bool:
    """Has quiet mode been asked for?

    The environment wins over the configuration, so a single run can turn it
    on without editing a file — and, unlike ``NO_COLOR``, ``VOX_SCREEN_READER=0``
    turns it *off*. That difference is deliberate: ``NO_COLOR`` is a standard
    with a stated rule about presence, and this is not.
    """
    env = environ if environ is not None else os.environ
    if ENV_VAR in env:
        return _truthy(env[ENV_VAR])
    ui = (config or {}).get("ui")
    ui = ui if isinstance(ui, dict) else {}
    return bool(ui.get("screen_reader", False))


def tick_seconds(quiet: bool) -> float:
    """How often the status line should be redrawn."""
    return QUIET_TICK if quiet else FAST_TICK


def waiting_text(label: str, elapsed: float, quiet: bool) -> str:
    """The thinking box's line, in words when nobody can see the spinner."""
    if quiet:
        seconds = int(elapsed)
        unit = "second" if seconds == 1 else "seconds"
        return f"{label}, {seconds} {unit}"
    return f"{label}… {elapsed:.1f}s"
