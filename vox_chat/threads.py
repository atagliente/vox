"""Where VOX's own threads are made.

Three places started background threads independently — the discovery agent,
the whois server, the search server — each choosing its own name and its own
answer to the one question that matters on the way out: is this thread going
to hold the interpreter open?

The answer is always the same here. Everything VOX starts for itself is a
daemon, because the shutdown path in ``__main__`` already decides how long to
wait for work in flight and forces the point when a worker is stuck. A
non-daemon thread would take that decision away from it and hang the process
after the screen is gone — which is the failure ``__main__._stuck_threads``
exists to catch.

Names matter too, and for the same reason: ``_stuck_threads`` reports them,
so a thread called ``Thread-7`` is a thread nobody can identify at the moment
they need to.

Textual's own workers are not made here. ``@work`` is how the UI puts
something on a thread and Textual owns that pool; this is for the threads VOX
starts outside it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .logging_setup import get_logger

log = get_logger("threads")


def start(target: Callable[[], object], name: str) -> threading.Thread:
    """Start one named daemon thread, and say so in the log.

    The name is required rather than defaulted: a thread worth starting is a
    thread worth being able to find again.
    """
    if not name:
        raise ValueError("a background thread needs a name")
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    log.debug("started %s", name)
    return thread


def serve(server: object, name: str) -> threading.Thread:
    """Run a ``socketserver``-style server's ``serve_forever`` on its own thread."""
    serve_forever = server.serve_forever  # type: ignore[attr-defined]
    return start(serve_forever, name)
