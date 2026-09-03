"""VOX in a browser: the host, the server, and the page.

`vox ui` serves one page on 127.0.0.1 and drives it with the same pieces the
TUI uses — `agent.run_turn` for the turn, `commands.dispatch` for the slash
commands, `coalesce` for the streaming. What is new here is only the surface:
an SSE stream out, a handful of POST routes in.

No dependencies were added for it. `http.server` for the server, Server-Sent
Events for the stream — one GET that stays open, which is what SSE is for and,
unlike WebSockets, is in the standard library on both sides — and the page
written by hand in one Python constant, the way `report.py` already writes its
HTML export.
"""

from __future__ import annotations

from .host import WebHost
from .server import UiServer, serve

__all__ = ["UiServer", "WebHost", "serve"]
