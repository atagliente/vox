"""
The agents' cryptographic identity: an internal CA, a certificate per agent,
and the TLS contexts.

The binding that matters is this one: **the agent_id announced over multicast
must match the SAN of the certificate presented during the WHOIS**. Without
that constraint the announcement is authenticated but the conversation is not,
and anyone on the network can answer in place of the legitimate peer.

The agent_id appears in two SANs:
  DNS:<agent_id>                        -> checked by Python itself
                                           (check_hostname + server_hostname)
  URI:spiffe://<trust-domain>/agent/<agent_id>
                                        -> the canonical, SPIFFE-compatible form

On lifetime: the certificates are deliberately short-lived, 24h by default. A
short life is the most practical form of revocation there is: with no CRL and
no OCSP, a stolen certificate stops being worth anything within a day. It does
require automatic renewal — see `needs_renewal`.
"""

from __future__ import annotations

import datetime as dt
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TRUST_DOMAIN = "agents.internal"
DEFAULT_CERT_LIFETIME = dt.timedelta(hours=24)
CA_LIFETIME = dt.timedelta(days=365)
RENEWAL_THRESHOLD = 0.5  # renew at half life

# The agent_id ends up in a DNS SAN, so it has to be a valid DNS label:
# letters, digits and hyphens. No underscores, no dots.
AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


class IdentityError(Exception):
    pass


def validate_agent_id(agent_id: str) -> str:
    if not AGENT_ID_RE.match(agent_id):
        raise IdentityError(
            f"agent_id '{agent_id}' is not a valid DNS label "
            "(letters, digits and hyphens only; 63 characters at most)"
        )
    return agent_id


def spiffe_id(agent_id: str, trust_domain: str = TRUST_DOMAIN) -> str:
    return f"spiffe://{trust_domain}/agent/{agent_id}"


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------

def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)  # nobody else may read the private key


def create_ca(directory: Path, common_name: str = "agent-mesh-ca",
              lifetime: dt.timedelta = CA_LIFETIME) -> tuple[Path, Path]:
    """Create the internal CA. Done once, on a protected machine.

    In production the CA key does not belong on the agents: whoever takes that
    file can issue any identity they like.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ca_key_path = directory / "ca.key"
    ca_crt_path = directory / "ca.crt"

    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + lifetime)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, None)
    )

    _write_private(ca_key_path, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    ca_crt_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return ca_crt_path, ca_key_path


def issue_agent_cert(
    directory: Path,
    agent_id: str,
    ca_crt_path: Path,
    ca_key_path: Path,
    lifetime: dt.timedelta = DEFAULT_CERT_LIFETIME,
    trust_domain: str = TRUST_DOMAIN,
) -> tuple[Path, Path]:
    """Issue an agent's certificate, with the agent_id in the SANs."""
    validate_agent_id(agent_id)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    ca_cert = x509.load_pem_x509_certificate(Path(ca_crt_path).read_bytes())
    ca_key = serialization.load_pem_private_key(Path(ca_key_path).read_bytes(), password=None)

    key = ed25519.Ed25519PrivateKey.generate()
    now = dt.datetime.now(dt.timezone.utc)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, agent_id)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + lifetime)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(agent_id),
                x509.UniformResourceIdentifier(spiffe_id(agent_id, trust_domain)),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # The same certificate serves both as a server (answering WHOIS) and
        # as a client (interrogating others): there is only one identity.
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .sign(ca_key, None)
    )

    crt_path = directory / f"{agent_id}.crt"
    key_path = directory / f"{agent_id}.key"
    _write_private(key_path, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    crt_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return crt_path, key_path


# ---------------------------------------------------------------------------
# Runtime use
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    """This agent's cryptographic material."""

    agent_id: str
    cert_path: Path
    key_path: Path
    ca_path: Path
    trust_domain: str = TRUST_DOMAIN

    @classmethod
    def load(cls, agent_id: str, directory: Path, ca_path: Path | None = None,
             trust_domain: str = TRUST_DOMAIN) -> "Identity":
        directory = Path(directory)
        identity = cls(
            agent_id=validate_agent_id(agent_id),
            cert_path=directory / f"{agent_id}.crt",
            key_path=directory / f"{agent_id}.key",
            ca_path=Path(ca_path) if ca_path else directory / "ca.crt",
            trust_domain=trust_domain,
        )
        for path in (identity.cert_path, identity.key_path, identity.ca_path):
            if not path.exists():
                raise IdentityError(f"missing file: {path}")

        # The certificate really must belong to who it claims to be: a
        # mismatch here shows up as an opaque failure at the first handshake.
        cert = x509.load_pem_x509_certificate(identity.cert_path.read_bytes())
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if agent_id not in names:
            raise IdentityError(f"the certificate carries no DNS SAN '{agent_id}'")
        return identity

    @property
    def certificate(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate(self.cert_path.read_bytes())

    def expires_at(self) -> dt.datetime:
        return self.certificate.not_valid_after_utc

    def needs_renewal(self, threshold: float = RENEWAL_THRESHOLD) -> bool:
        """True once the certificate is past half its life.

        Renewing at half life rather than at expiry leaves room to try again
        if the CA happens to be unreachable.
        """
        cert = self.certificate
        start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
        elapsed = (dt.datetime.now(dt.timezone.utc) - start).total_seconds()
        total = (end - start).total_seconds()
        return total <= 0 or (elapsed / total) >= threshold


def server_context(identity: Identity) -> ssl.SSLContext:
    """The server-side TLS context: it demands and checks the client certificate.

    `CERT_REQUIRED` is what makes the authentication *mutual*. Without it you
    would have only an encrypted channel to anybody at all.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(identity.ca_path))
    context.load_cert_chain(certfile=str(identity.cert_path), keyfile=str(identity.key_path))
    return context


def client_context(identity: Identity) -> ssl.SSLContext:
    """The client-side TLS context: it checks the server against the internal CA.

    `check_hostname=True` together with `server_hostname=<agent_id>` makes
    Python perform the check that matters here: the server's SAN must match
    the agent_id seen in the announcement.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(identity.ca_path))
    context.load_cert_chain(certfile=str(identity.cert_path), keyfile=str(identity.key_path))
    return context


def peer_agent_id(ssl_socket: ssl.SSLSocket, trust_domain: str = TRUST_DOMAIN) -> str:
    """Pull the agent_id out of the peer's already validated certificate.

    Used server-side for authorisation: TLS says *that* the peer is genuine,
    this says *who* it is.
    """
    cert = ssl_socket.getpeercert()
    if not cert:
        raise IdentityError("no client certificate was presented")

    prefix = f"spiffe://{trust_domain}/agent/"
    for kind, value in cert.get("subjectAltName", ()):
        if kind == "URI" and value.startswith(prefix):
            return value[len(prefix):]
    for kind, value in cert.get("subjectAltName", ()):
        if kind == "DNS":
            return value
    raise IdentityError("no agent_id among the peer certificate SANs")
