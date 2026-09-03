"""The HTTP surface of `vox ui`.

Small on purpose: a page, a stream, and five things a person can do. Every
route is a thin translation between JSON and a :class:`WebHost` call, so the
behaviour lives in the host and this file stays readable.

It binds 127.0.0.1 and nothing else, and follows `searchd._Server` on the
one platform detail that matters — `allow_reuse_address` means two different
things on Windows and everywhere else.

**Being on localhost is not a permission check.** Any page in any browser can
POST to `http://127.0.0.1:8899`; what it cannot do is read the reply
cross-origin, which stops it *learning* anything but not from telling VOX to
run a tool. So every state-changing request is refused unless its `Origin`
is this server, or absent — a same-origin `fetch` from the page itself sends
one, and `curl` sends none. That is the whole of the defence and it is
written down here rather than assumed from the address.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import threads
from ..commands import command_index
from ..logging_setup import get_logger
from .host import WebHost
from .page import PAGE

log = get_logger("webui.server")

HOST = "127.0.0.1"
DEFAULT_PORT = 8899

# A body larger than this is not a chat message. Reading it would be the
# denial of service somebody could point at a server bound to localhost.
MAX_BODY = 4 * 1024 * 1024

# How often an idle stream sends a comment frame. Without it a proxy or a
# sleeping laptop closes a connection nobody has said anything on, and the
# page reconnects for no reason.
KEEPALIVE = 15.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR means "bind even if somebody already has this
    # port", which would let VOX quietly steal a port and answer half the
    # requests. Elsewhere it means "reuse a TIME_WAIT socket", which is what
    # makes a restart work, so it stays on there.
    allow_reuse_address = sys.platform != "win32"

    host: WebHost


class _Handler(BaseHTTPRequestHandler):
    server_version = "vox-ui"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._html(PAGE)
        elif path == "/events":
            self._stream()
        elif path == "/api/state":
            self._json(200, self.host.state())
        elif path == "/api/sessions":
            self._json(200, {"sessions": self._sessions()})
        elif path == "/api/commands":
            # Straight from the same table the legend is built from, so the
            # popup cannot offer a command that does not exist or miss one
            # that does.
            self._json(200, {"commands": command_index()})
        else:
            self._json(404, {"error": "no such route"})

    def _sessions(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "name": info.name,
                    "title": info.title,
                    "updated": info.updated_at,
                    "model": info.model,
                    "messages": info.message_count,
                }
                for info in self.host.session_store.list()
            ]
        except OSError as exc:  # pragma: no cover - an unreadable directory
            log.debug("cannot list sessions: %s", exc)
            return []

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if not self._same_origin():
            # Not paranoia: any page in any browser can post here. See the
            # module docstring for why the address alone proves nothing.
            self._json(403, {"error": "cross-origin request refused"})
            return
        body = self._body()
        if body is None:
            return
        host = self.host
        if path == "/send":
            host.set_draft("")
            host.send(str(body.get("text", "")))
            self._json(200, {"ok": True})
        elif path == "/command":
            host.run_command(str(body.get("text", "")))
            self._json(200, {"ok": True})
        elif path == "/stop":
            host.stop_generation()
            self._json(200, {"ok": True})
        elif path == "/draft":
            host.set_draft(str(body.get("text", "")))
            self._json(200, {"ok": True})
        elif path == "/confirm":
            found = host.answer_confirm(
                str(body.get("id", "")), bool(body.get("allowed", False))
            )
            self._json(200 if found else 404, {"ok": found})
        else:
            self._json(404, {"error": "no such route"})

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            # curl, a script, or a form that predates CORS. Not a browser
            # being aimed at this port by a page the person is reading.
            return True
        allowed = {
            f"http://{HOST}:{self.server.server_address[1]}",
            f"http://localhost:{self.server.server_address[1]}",
        }
        return origin in allowed

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": "body is not JSON"})
            return None
        return data if isinstance(data, dict) else {}

    # --------------------------------------------------------------- stream

    def _stream(self) -> None:
        listener = self.host.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # The state first, so a tab that opens mid-conversation draws the
            # whole of it rather than only what happens next.
            self._frame({"kind": "state", "state": self.host.state()})
            while not self.host.closing.is_set():
                try:
                    frame = listener.get(timeout=KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._frame(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("a tab went away")
        finally:
            self.host.events.unsubscribe(listener)

    def _frame(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    # --------------------------------------------------------------- replies

    @property
    def host(self) -> WebHost:
        return self.server.host  # type: ignore[attr-defined]

    def _html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # The page loads nothing from anywhere. Saying so means a mistake
        # that adds a CDN link is a blocked request rather than a silent
        # dependency on somebody else's uptime.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; img-src data:",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("ui %s", format % args)


class UiServer:
    """The server, owned by whoever started it."""

    def __init__(self, host: WebHost, port: int = DEFAULT_PORT) -> None:
        self.host = host
        # Port 0 asks the operating system for a free one, which is what the
        # tests use: a fixed port makes two of them a race rather than two
        # tests. `port` becomes the real number once start() has bound it.
        self.port = port
        self._server: _Server | None = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def start(self) -> str:
        if self._server is not None:
            return f"already listening on {self.url}"
        try:
            server = _Server((HOST, self.port), _Handler)
        except OSError as exc:
            raise OSError(
                f"cannot listen on {HOST}:{self.port}: {exc}. Something else "
                "may be using that port; pass --port"
            ) from exc
        server.host = self.host
        self.port = server.server_address[1]
        self._server = server
        threads.serve(server, "vox-ui")
        log.info("ui listening on %s", self.url)
        return f"listening on {self.url}"

    def stop(self) -> None:
        server, self._server = self._server, None
        self.host.closing.set()
        # Wake every open stream so its thread finishes rather than sitting
        # on a queue nobody will ever put to.
        self.host.events.publish("closing")
        if server is None:
            return
        server.shutdown()
        server.server_close()
        log.info("ui stopped")


def serve(host: WebHost, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    """Run until Ctrl+C. Returns the exit code."""
    server = UiServer(host, port)
    print(f"vox ui: {server.start()}")
    print("press ctrl+c to stop")
    if open_browser:
        _open(server.url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("")
    finally:
        server.stop()
        host.close()
    return 0


def _open(url: str) -> None:
    """Open the page, and do not care very much if it cannot be opened."""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - depends on the desktop
        # Wide on purpose: every platform fails at this differently, and none
        # of those failures is a reason not to serve the page.
        log.debug("cannot open a browser: %s", exc)
