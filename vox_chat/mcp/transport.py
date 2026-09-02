"""How a JSON-RPC message reaches an MCP server, and how the reply comes back.

Two transports, because those are the two the specification defines and the
two anything in the wild actually offers:

- **stdio** — the server is a subprocess and the channel is its pipes, one
  JSON object per line. This is what almost every published MCP server uses.
- **streamable HTTP** — the server is a URL. A request is a POST; the answer
  is either a JSON body or an SSE stream, and a server may choose either per
  request, so both are handled.

Neither knows what the messages *mean*; that is ``client.py``. What is here is
framing, timeouts, and making sure a server that dies, hangs or answers with
rubbish becomes an ``McpError`` rather than a stuck thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .. import http, threads
from ..logging_setup import get_logger

log = get_logger("mcp")

# A server that has not answered by now is not going to. Long enough for a
# cold `npx` start, short enough that the operator is not left staring.
DEFAULT_TIMEOUT = 30.0


class McpError(Exception):
    """A server could not be reached, or would not do what was asked."""


class Transport(ABC):
    """One channel to one server."""

    @abstractmethod
    def start(self) -> None:
        """Open the channel. Raises :class:`McpError` if it cannot be opened."""

    @abstractmethod
    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        """Send one message; return the reply, or None for a notification."""

    @abstractmethod
    def close(self) -> None:
        """Shut the channel down. Safe to call more than once."""

    @property
    @abstractmethod
    def describe(self) -> str:
        """Where this server is, for the operator to read."""


class StdioTransport(Transport):
    """A server running as a child process, one JSON object per line.

    The child's stderr is drained on its own thread and logged. Without that
    a chatty server fills its pipe buffer and blocks, which looks exactly like
    a hang and is the most common way a stdio server appears broken.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None
        self._process: subprocess.Popen[str] | None = None
        self._errors: queue.Queue[str] = queue.Queue(maxsize=200)
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closed = threading.Event()
        self._lock = threading.Lock()

    @property
    def describe(self) -> str:
        return " ".join([self.command, *self.args])

    def start(self) -> None:
        binary = shutil.which(self.command)
        if binary is None:
            raise McpError(
                f"{self.command} is not on the PATH; install it, or give the "
                "full path in the mcp configuration"
            )
        environment = {**os.environ, **self.env}
        try:
            self._process = subprocess.Popen(
                [binary, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=environment,
            )
        except OSError as exc:
            raise McpError(f"cannot start {self.command}: {exc}") from exc
        threads.start(self._drain_stderr, f"mcp-stderr-{self.command}")
        threads.start(self._read_forever, f"mcp-read-{self.command}")

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:  # pragma: no cover - defensive
            return
        for line in process.stderr:
            text = line.rstrip()
            if not text:
                continue
            log.debug("%s: %s", self.command, text)
            if not self._errors.full():
                self._errors.put(text)

    def _recent_errors(self) -> str:
        lines = []
        while not self._errors.empty() and len(lines) < 5:
            lines.append(self._errors.get_nowait())
        return "; ".join(lines)

    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.poll() is not None:
            detail = self._recent_errors()
            raise McpError(
                f"{self.command} is not running" + (f": {detail}" if detail else "")
            )
        assert process.stdin is not None and process.stdout is not None
        with self._lock:
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise McpError(f"{self.command} closed its input: {exc}") from exc
            if "id" not in message:
                return None
            return self._read_reply(message["id"], timeout)

    def _read_forever(self) -> None:
        """One reader for the life of the process, feeding the inbox.

        One, not one per call: a reader started for a call that then timed out
        would still be sitting on the pipe, and would eat the *next* reply.
        That is a race that only shows up under load, which is the worst kind
        to leave in.
        """
        process = self._process
        if process is None or process.stdout is None:  # pragma: no cover - defensive
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    log.debug("%s: not JSON: %s", self.command, line[:200])
                    continue
                if isinstance(payload, dict):
                    self._inbox.put(payload)
        except (OSError, ValueError) as exc:  # pragma: no cover - the pipe went away
            log.debug("%s: reader stopped: %s", self.command, exc)
        finally:
            self._closed.set()

    def _read_reply(self, wanted: Any, timeout: float) -> dict[str, Any]:
        """Wait for the reply with this id, keeping anything else for later.

        A server may send notifications of its own while a call is in flight.
        Those are not the answer, and they are not thrown away either: they go
        back on the queue so a later call is not starved by having eaten them.
        """
        deadline = time.monotonic() + timeout
        held: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = self._recent_errors()
                    raise McpError(
                        f"{self.command} did not answer within {timeout:g}s"
                        + (f": {detail}" if detail else "")
                    )
                try:
                    payload = self._inbox.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    if self._closed.is_set() and self._inbox.empty():
                        detail = self._recent_errors()
                        raise McpError(
                            f"{self.command} closed its output"
                            + (f": {detail}" if detail else "")
                        ) from None
                    continue
                if payload.get("id") == wanted:
                    return payload
                held.append(payload)
        finally:
            for payload in held:
                self._inbox.put(payload)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        self._closed.set()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):  # it may already be gone
                    stream.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - a stubborn child
            process.kill()


class HttpTransport(Transport):
    """A server at a URL, spoken to with the streamable HTTP transport.

    The server chooses per request whether to answer with a JSON body or an
    SSE stream, so both are read. The session id it hands back on initialise
    is echoed on every later request, which is what makes a sequence of POSTs
    one conversation.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: str | None = None

    @property
    def describe(self) -> str:
        return self.url

    def start(self) -> None:
        """Nothing to open: each request stands on its own."""

    def send(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            reply = http.request(
                self.url,
                data=json.dumps(message).encode("utf-8"),
                headers=headers,
                timeout=timeout,
                max_bytes=4_000_000,
            )
        except http.HttpError as exc:
            raise McpError(f"{self.url}: {exc.message}") from exc
        session = reply.headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if "id" not in message:
            return None
        return self._decode(reply)

    def _decode(self, reply: http.Reply) -> dict[str, Any]:
        body = reply.text()
        if reply.content_type == "text/event-stream":
            body = _last_sse_data(body)
        if not body.strip():
            raise McpError(f"{self.url} answered with an empty body")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise McpError(
                f"{self.url} answered with something that is not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise McpError(f"{self.url} answered with a {type(payload).__name__}")
        return payload

    def close(self) -> None:
        self.session_id = None


def _last_sse_data(body: str) -> str:
    """The payload of the final ``data:`` event in an SSE response.

    A server may narrate progress before the answer; the answer is the last
    thing it says.
    """
    payloads: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
        elif not line.strip() and current:
            payloads.append("\n".join(current))
            current = []
    if current:
        payloads.append("\n".join(current))
    return payloads[-1] if payloads else ""
