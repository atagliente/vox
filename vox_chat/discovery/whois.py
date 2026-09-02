"""
The WHOIS phase: a point-to-point unicast conversation after an announcement.

The multicast announcement only says "I exist, I am X". Who you are and what
you can do is asked here, over mTLS.

Three checks are what make TLS worth having in this setting:

  1. CLIENT -> SERVER  the server certificate must carry, as a SAN, the
     agent_id seen in the announcement. `check_hostname` enforces it once
     `server_hostname=expected_agent_id` is passed. Without it, anyone on the
     network can answer in place of the legitimate peer.

  2. SERVER -> CLIENT  `CERT_REQUIRED` turns away anyone who cannot present a
     certificate signed by the internal CA. This is the "mutual" half of mTLS:
     without it you would have an encrypted channel to anybody at all.

  3. AUTHORISATION     the identity in the client certificate goes through an
     `authorizer`. Being a member of the mesh must not imply the right to
     interrogate everyone: authentication and authorisation stay separate.

The application-level consistency check on `agent_id` in the descriptor stays
as defence in depth: redundant next to point 1, but it catches a misconfigured
TLS context.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable

from .. import threads
from .identity import (
    Identity,
    IdentityError,
    client_context,
    peer_agent_id,
    server_context,
)

log = logging.getLogger("discovery.whois")

TAXONOMY_VERSION = "v1"

# A declarative taxonomy: the agent declares the verbs it supports and the
# category is derived. Deterministic and reproducible, unlike a classification
# left to a model, where the same agent lands in a different category on every
# restart.
VERB_TO_CATEGORY = {
    "ingest": "SOURCE",
    "publish": "SOURCE",
    "transform": "PROCESSOR",
    "enrich": "PROCESSOR",
    "infer": "PROCESSOR",
    "store": "SINK",
    "index": "SINK",
    "notify": "SINK",
    "schedule": "ORCHESTRATOR",
    "dispatch": "ORCHESTRATOR",
    "observe": "OBSERVER",
    "audit": "OBSERVER",
}

# OBSERVERs listen and take no work: they stay out of routing even when they
# are perfectly healthy.
PASSIVE_CATEGORIES = {"OBSERVER"}

MAX_DESCRIPTOR_BYTES = 64 * 1024
HANDSHAKE_TIMEOUT = 5.0

# ASK carries a question for the peer's own model. It is the one operation that
# makes the mesh do work rather than describe itself, and it rides the channel
# that is already open and already authenticated.
MAX_ASK_BYTES = 8 * 1024
ASK_TIMEOUT = 120.0
# A peer streams what it is writing before the final reply. This caps how much
# of that a caller will read, so a chatty or hostile peer cannot stream for
# ever into the asker's memory.
MAX_STREAM_BYTES = 256 * 1024

# A callable given the client's agent_id, deciding whether to serve it.
Authorizer = Callable[[str], bool]

# A callable given (caller_agent_id, question, emit, conversation_id); it
# returns {"answer", "model"} or raises, and may call emit(kind, text) as it
# writes. Answering means running a model, so it is slow and it is capped.
AskHandler = Callable[[str, str, Callable[[str, str], None], str], dict]


def classify(capabilities: dict) -> str:
    """Derive the category from the declared verbs. No magic heuristics."""
    verbs = capabilities.get("verbs") or []
    categories = {VERB_TO_CATEGORY[v] for v in verbs if v in VERB_TO_CATEGORY}
    if not categories:
        return "UNKNOWN"
    if len(categories) == 1:
        return next(iter(categories))
    # An agent that does several things: the most "downstream" category wins,
    # so the router does not send it work outside its main trade.
    for candidate in ("ORCHESTRATOR", "OBSERVER", "SINK", "PROCESSOR", "SOURCE"):
        if candidate in categories:
            return candidate
    return "UNKNOWN"


class _Handler(socketserver.StreamRequestHandler):
    timeout = HANDSHAKE_TIMEOUT

    def handle(self) -> None:
        try:
            caller = peer_agent_id(self.connection, self.server.trust_domain)
        except (IdentityError, AttributeError) as exc:
            log.warning("whois: cannot identify the client: %s", exc)
            return

        if not self.server.authorizer(caller):
            log.warning("whois: %s is not authorised", caller)
            self._reply({"error": "forbidden"})
            return

        try:
            line = self.rfile.readline(MAX_DESCRIPTOR_BYTES)
            if not line:
                return
            request = json.loads(line)
            op = request.get("op")
            if op == "WHOIS":
                log.debug("whois: serving %s", caller)
                self._reply({"ok": True, "descriptor": self.server.descriptor})
                return
            if op == "ASK":
                self._answer(caller, request)
                return
            self._reply({"error": "unsupported op"})
        except (TimeoutError, json.JSONDecodeError, ValueError, OSError):
            # A malformed or slow peer: close without any noise.
            return

    def _answer(self, caller: str, request: dict) -> None:
        """Run this node's model for a peer, if it is willing and free."""
        handler = getattr(self.server, "ask_handler", None)
        if handler is None:
            self._reply({"error": "ask not supported"})
            return

        question = str(request.get("question", ""))
        # The asker's conversation, so both sides can be tied to one exchange.
        conversation = str(request.get("conversation", ""))[:64]
        if not question.strip():
            self._reply({"error": "empty question"})
            return
        if len(question.encode("utf-8")) > MAX_ASK_BYTES:
            self._reply({"error": f"question over {MAX_ASK_BYTES} bytes"})
            return

        # One answer at a time. Without this a peer could queue generations on
        # somebody else's hardware just by opening connections.
        if not self.server.ask_slot.acquire(blocking=False):
            self._reply({"error": "busy"})
            return
        try:
            # The 5s handshake timeout is nowhere near enough for a model.
            self.connection.settimeout(self.server.ask_timeout)
            started = time.monotonic()
            result = handler(caller, question, self._emit, conversation)
            elapsed = time.monotonic() - started
            log.info("ask: answered %s in %.1fs", caller, elapsed)
            self._reply(
                {
                    "ok": True,
                    "answer": str(result.get("answer", "")),
                    "model": str(result.get("model", "")),
                    "elapsed": round(elapsed, 2),
                }
            )
        except Exception as exc:  # the peer gets a reason, not a dropped socket
            log.warning("ask: refusing %s: %s", caller, exc)
            self._reply({"error": str(exc)[:200]})
        finally:
            self.server.ask_slot.release()

    def _emit(self, kind: str, text: str) -> None:
        """Send one line of the answer as it is produced.

        The asker sees the peer think rather than waiting in silence for a
        minute. These lines precede the final reply, and are told apart by
        carrying ``event``; anything that cannot be written is dropped, because
        losing the live view must never lose the answer.
        """
        try:
            self.wfile.write(
                json.dumps(
                    {"event": kind, "text": text, "ts": time.time()},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            self.wfile.flush()
        except OSError:
            pass

    def _reply(self, payload: dict) -> None:
        try:
            self.wfile.write(
                json.dumps(payload, separators=(",", ":")).encode() + b"\n"
            )
            self.wfile.flush()
        except OSError:
            pass


class _TLSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def get_request(self):
        """Accept, and complete the TLS handshake before reaching the handler.

        The handshake happens here rather than in the handler thread, but with
        an explicit timeout: a client that opens a connection and then says
        nothing must not be able to hold the listener. A failed handshake
        becomes an OSError, which socketserver drops without taking the server
        down.
        """
        sock, address = self.socket.accept()
        sock.settimeout(HANDSHAKE_TIMEOUT)
        try:
            tls_sock = self.ssl_context.wrap_socket(sock, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.debug("TLS handshake refused from %s: %s", address[0], exc)
            try:
                sock.close()
            finally:
                raise OSError("handshake failed") from exc
        return tls_sock, address


class WhoisServer:
    """Serves this agent's descriptor, over mTLS, to whoever asks for it."""

    def __init__(
        self,
        descriptor: dict,
        identity: Identity,
        host: str = "0.0.0.0",
        port: int = 0,
        authorizer: Authorizer | None = None,
        ask_handler: AskHandler | None = None,
        ask_timeout: float = ASK_TIMEOUT,
    ):
        self._identity = identity
        self._server = _TLSServer((host, port), _Handler)
        self._server.descriptor = descriptor
        self._server.ssl_context = server_context(identity)
        self._server.trust_domain = identity.trust_domain
        # By default anyone holding a valid certificate from the CA may ask.
        # Replace this with a real policy (by category, by tenant) as soon as
        # the mesh stops being uniformly trusted.
        self._server.authorizer = authorizer or (lambda agent_id: True)
        self._server.ask_handler = ask_handler
        self._server.ask_timeout = ask_timeout
        self._server.ask_slot = threading.Semaphore(1)
        self._thread: threading.Thread | None = None

    def set_ask_handler(self, handler: AskHandler | None) -> None:
        """Start or stop answering questions, without restarting the server."""
        self._server.ask_handler = handler

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threads.serve(self._server, "whois-server")

    def reload_identity(self, identity: Identity) -> None:
        """Swap the TLS context in after a certificate renewal.

        Connections already open carry on with the old context; new ones use
        the fresh certificate. No restart, and no window of unavailability.
        """
        self._identity = identity
        self._server.ssl_context = server_context(identity)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def query(
    address: str,
    port: int,
    expected_agent_id: str,
    identity: Identity,
    timeout: float = 5.0,
) -> dict:
    """Interrogate a peer over mTLS.

    Raises ssl.SSLCertVerificationError if the peer's certificate was not
    issued by the internal CA, or does not carry `expected_agent_id` in its
    SANs.
    """
    context = client_context(identity)
    # server_hostname is the agent_id, not the IP: this is exactly what
    # binds the multicast announcement to the certificate's identity.
    with (
        socket.create_connection((address, port), timeout=timeout) as raw_sock,
        context.wrap_socket(raw_sock, server_hostname=expected_agent_id) as sock,
    ):
        sock.settimeout(timeout)
        sock.sendall(json.dumps({"op": "WHOIS"}).encode() + b"\n")

        chunks, total = [], 0
        while total < MAX_DESCRIPTOR_BYTES:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\n" in chunk:
                break

    payload = json.loads(b"".join(chunks).decode())
    if not payload.get("ok"):
        raise ValueError(f"whois refused: {payload.get('error')}")

    descriptor = payload["descriptor"]
    # Defence in depth: TLS has already checked the SAN, but a misconfigured
    # context must not go unnoticed.
    if descriptor.get("agent_id") != expected_agent_id:
        raise ValueError(
            "the agent_id in the descriptor does not match the announcement"
        )
    return descriptor


def ask(
    address: str,
    port: int,
    expected_agent_id: str,
    identity: Identity,
    question: str,
    timeout: float = ASK_TIMEOUT,
    on_event=None,
    conversation: str = "",
) -> dict:
    """Ask a peer a question, over the same mTLS channel as WHOIS.

    The peer runs its own model with nothing but this question: no context
    travels in either direction. Returns ``{"answer", "model", "elapsed"}``.

    The peer streams what it is producing as newline-delimited JSON before the
    final reply — ``{"event": "text" | "reasoning", "text": ..., "ts": ...}``.
    ``on_event(kind, text, ts)`` is called for each, so the asker can watch a
    slow agent think instead of staring at nothing. No websocket is involved:
    this channel is already an authenticated bidirectional stream.
    """
    context = client_context(identity)
    payload: dict | None = None
    with (
        socket.create_connection((address, port), timeout=timeout) as raw_sock,
        context.wrap_socket(raw_sock, server_hostname=expected_agent_id) as sock,
    ):
        sock.settimeout(timeout)
        sock.sendall(
            json.dumps(
                {
                    "op": "ASK",
                    "v": 2,
                    "question": question,
                    "stream": True,
                    "conversation": conversation,
                }
            ).encode()
            + b"\n"
        )
        with sock.makefile("rb") as stream:
            streamed = 0
            while True:
                line = stream.readline(MAX_DESCRIPTOR_BYTES)
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    break
                if "event" in message:
                    streamed += len(line)
                    if streamed > MAX_STREAM_BYTES:
                        raise ValueError("peer streamed too much")
                    if on_event is not None:
                        on_event(
                            str(message.get("event", "text")),
                            str(message.get("text", "")),
                            float(message.get("ts", 0.0)),
                        )
                    continue
                payload = message
                break

    if payload is None:
        raise ValueError("the peer closed without answering")
    if not payload.get("ok"):
        raise ValueError(str(payload.get("error", "refused")))
    return {
        "answer": str(payload.get("answer", "")),
        "model": str(payload.get("model", "")),
        "elapsed": float(payload.get("elapsed", 0.0)),
    }
