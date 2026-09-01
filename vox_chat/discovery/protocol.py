"""
The format of an announcement packet, and how it is verified.

An announcement is deliberately minimal: it says "I exist, I am X, talk to me
on port Y". It carries no capabilities, no full endpoints and no tokens —
those are exchanged during WHOIS, over an authenticated unicast channel.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, asdict

PROTO_VERSION = 1

# How far the timestamp may drift, in seconds. It covers the clock skew you
# can expect between nodes kept in step by NTP.
CLOCK_SKEW_TOLERANCE = 30.0


class ProtocolError(Exception):
    """A malformed packet, an invalid signature, or an unsupported version."""


@dataclass(frozen=True)
class Announce:
    """The presence payload every agent repeats on its own interval."""

    agent_id: str          # a stable identity, surviving restarts
    incarnation: int       # the process epoch: it changes on every restart
    whois_port: int        # where to reach me for the real conversation
    caps_digest: str       # a hash of the capabilities declared
    ts: float              # when this was emitted
    nonce: str             # anti-replay

    def canonical(self) -> bytes:
        """A deterministic serialisation: the signature has to be reproducible."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()


def new_announce(agent_id: str, incarnation: int, whois_port: int, caps_digest: str) -> Announce:
    return Announce(
        agent_id=agent_id,
        incarnation=incarnation,
        whois_port=whois_port,
        caps_digest=caps_digest,
        ts=time.time(),
        nonce=base64.b64encode(os.urandom(9)).decode(),
    )


def caps_digest(capabilities: dict) -> str:
    """A hash of the capabilities: when it changes, peers know to redo the WHOIS."""
    blob = json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def encode(announce: Announce, key: bytes) -> bytes:
    body = announce.canonical()
    sig = hmac.new(key, body, hashlib.sha256).digest()
    envelope = {
        "v": PROTO_VERSION,
        "body": base64.b64encode(body).decode(),
        "sig": base64.b64encode(sig).decode(),
    }
    return json.dumps(envelope, separators=(",", ":")).encode()


def decode(raw: bytes, key: bytes) -> Announce:
    """Check the signature and the version, then rebuild the announcement.

    Anything out of order raises ProtocolError: the caller simply drops the
    packet, without logging the payload (noise, and a log injection risk).
    """
    if len(raw) > 4096:
        raise ProtocolError("payload oversize")
    try:
        envelope = json.loads(raw)
        version = envelope["v"]
        body = base64.b64decode(envelope["body"])
        sig = base64.b64decode(envelope["sig"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError(f"malformed envelope: {exc}") from exc

    if version != PROTO_VERSION:
        raise ProtocolError(f"protocol version {version} is not supported")

    expected = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ProtocolError("invalid signature")

    try:
        fields = json.loads(body)
        announce = Announce(
            agent_id=str(fields["agent_id"]),
            incarnation=int(fields["incarnation"]),
            whois_port=int(fields["whois_port"]),
            caps_digest=str(fields["caps_digest"]),
            ts=float(fields["ts"]),
            nonce=str(fields["nonce"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError(f"malformed body: {exc}") from exc

    if not 1 <= announce.whois_port <= 65535:
        raise ProtocolError("whois port out of range")
    if not 1 <= len(announce.agent_id) <= 128:
        raise ProtocolError("agent_id length out of range")

    return announce


class ReplayGuard:
    """Drops announcements out of the time window, or with a nonce seen before.

    Without it, anyone who intercepts a valid announcement can retransmit it
    and pass for its sender: the HMAC alone proves the origin, not the
    freshness.
    """

    def __init__(self, tolerance: float = CLOCK_SKEW_TOLERANCE):
        self._tolerance = tolerance
        self._seen: dict[str, float] = {}

    def check(self, announce: Announce, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if abs(now - announce.ts) > self._tolerance:
            raise ProtocolError("timestamp outside the window")

        key = f"{announce.agent_id}:{announce.nonce}"
        if key in self._seen:
            raise ProtocolError("nonce already seen (replay)")

        self._seen[key] = now
        self._evict(now)

    def _evict(self, now: float) -> None:
        # Nonces older than the window are of no use: the timestamp check
        # already covers them.
        cutoff = now - (self._tolerance * 2)
        if len(self._seen) > 64:
            self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
