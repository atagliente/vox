"""
The format of an announcement packet, and how it is verified.

An announcement is deliberately minimal: it says "I exist, I am X, talk to me
on port Y". It carries no capabilities, no full endpoints and no tokens —
those are exchanged during WHOIS, over an authenticated unicast channel.

Every agent signs its own announcements with its own Ed25519 private key — the
same key as its certificate — and carries that certificate in the packet. A
receiver checks the certificate against the local certificate authority, checks
that the announced ``agent_id`` is the certificate's own SAN, and only then
checks the signature.

This is what version 2 changed. Version 1 signed with a pre-shared key: every
member of the mesh held the same secret, so every announcement carried the same
signature, and anyone holding that secret could announce as anyone else. The
only shared file now is ``ca.crt``, which is public by nature; the private half
of every signature never leaves the machine that made it.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

PROTO_VERSION = 2

# How far the timestamp may drift, in seconds. It covers the clock skew you
# can expect between nodes kept in step by NTP.
CLOCK_SKEW_TOLERANCE = 30.0

MAX_PACKET_BYTES = 4096


class ProtocolError(Exception):
    """A malformed packet, an invalid signature, or an unsupported version."""


@dataclass(frozen=True)
class Announce:
    """The presence payload every agent repeats on its own interval."""

    agent_id: str  # a stable identity, surviving restarts
    incarnation: int  # the process epoch: it changes on every restart
    whois_port: int  # where to reach me for the real conversation
    caps_digest: str  # a hash of the capabilities declared
    ts: float  # when this was emitted
    nonce: str  # anti-replay

    def canonical(self) -> bytes:
        """A deterministic serialisation: the signature has to be reproducible."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()


def new_announce(
    agent_id: str, incarnation: int, whois_port: int, caps_digest: str
) -> Announce:
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


# --------------------------------------------------------------- certificates


def certificate_names(certificate: x509.Certificate) -> list[str]:
    """The DNS SANs of a certificate: the agent ids it is allowed to claim."""
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return []
    return list(san.get_values_for_type(x509.DNSName))


def check_certificate(
    certificate: x509.Certificate, ca_public_key, now: dt.datetime | None = None
) -> None:
    """Was this certificate issued by our CA, and is it still valid?

    The chain is one level deep by construction, so verifying the leaf against
    the CA's public key is the whole of it — no path building, no CRL.
    """
    now = now or dt.datetime.now(dt.UTC)
    try:
        ca_public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
    except InvalidSignature as exc:
        raise ProtocolError("certificate was not issued by this mesh's CA") from exc
    except (TypeError, ValueError) as exc:  # a key of the wrong kind
        raise ProtocolError(f"certificate cannot be checked: {exc}") from exc

    if now < certificate.not_valid_before_utc:
        raise ProtocolError("certificate is not valid yet")
    if now > certificate.not_valid_after_utc:
        raise ProtocolError("certificate has expired")


# ------------------------------------------------------------ wire encoding


def encode(announce: Announce, private_key, certificate_der: bytes) -> bytes:
    """Sign an announcement with this agent's own key and attach its certificate."""
    body = announce.canonical()
    envelope = {
        "v": PROTO_VERSION,
        "body": base64.b64encode(body).decode(),
        "sig": base64.b64encode(private_key.sign(body)).decode(),
        "cert": base64.b64encode(certificate_der).decode(),
    }
    packet = json.dumps(envelope, separators=(",", ":")).encode()
    if len(packet) > MAX_PACKET_BYTES:  # pragma: no cover - a 374-byte cert leaves room
        raise ProtocolError(f"announcement is {len(packet)} bytes, over the limit")
    return packet


def decode(raw: bytes, ca_public_key, now: dt.datetime | None = None) -> Announce:
    """Verify the packet completely, then rebuild the announcement.

    In order: the envelope parses, the version matches, the certificate comes
    from our CA and is in date, the signature is that certificate's, and the
    announced agent_id is one the certificate is entitled to claim. Anything
    out of order raises ProtocolError and the caller simply drops the packet,
    without logging the payload (noise, and a log injection risk).
    """
    if len(raw) > MAX_PACKET_BYTES:
        raise ProtocolError("payload oversize")
    try:
        envelope = json.loads(raw)
        version = envelope["v"]
        body = base64.b64decode(envelope["body"])
        sig = base64.b64decode(envelope["sig"])
        certificate_der = base64.b64decode(envelope["cert"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ProtocolError(f"malformed envelope: {exc}") from exc

    if version != PROTO_VERSION:
        raise ProtocolError(f"protocol version {version} is not supported")

    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except ValueError as exc:
        raise ProtocolError(f"malformed certificate: {exc}") from exc

    check_certificate(certificate, ca_public_key, now=now)

    try:
        certificate.public_key().verify(sig, body)
    except InvalidSignature as exc:
        raise ProtocolError("invalid signature") from exc
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"signature cannot be checked: {exc}") from exc

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

    # The signature proves the packet came from the holder of that certificate.
    # This is what stops the holder announcing itself as somebody else.
    if announce.agent_id not in certificate_names(certificate):
        raise ProtocolError(f"the certificate does not name {announce.agent_id!r}")

    return announce


def load_signing_key(path):
    """The agent's own private key, used to sign what it announces."""
    from pathlib import Path

    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def load_ca_public_key(path):
    """The public half of the CA: everything a receiver needs to check a packet."""
    from pathlib import Path

    return x509.load_pem_x509_certificate(Path(path).read_bytes()).public_key()


class ReplayGuard:
    """Drops announcements out of the time window, or with a nonce seen before.

    Without it, anyone who intercepts a valid announcement can retransmit it
    and pass for its sender: the signature alone proves the origin, not the
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
