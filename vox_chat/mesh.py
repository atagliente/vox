"""Joining the agent mesh: identity, keys, and the discovery agent's life.

Everything the interface must not know about lives here — where the
certificate comes from, where the pre-shared key is kept, and how the
announcer is started and stopped. The UI asks for ``online`` and gets peers.

Going online announces this machine's presence on the local network segment.
That is a deliberate act, so the controller reports exactly what it did.
"""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import os
import re
import secrets
import socket
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .storage import vox_home

log = get_logger("mesh")

PSK_ENV = "DISCOVERY_PSK"
MIN_PSK_BYTES = 16
CERT_LIFETIME = dt.timedelta(hours=24)

_LABEL_RE = re.compile(r"[^a-zA-Z0-9-]+")


class MeshError(Exception):
    """Something stopped the mesh; the chat carries on regardless."""


def pki_dir(settings: "MeshSettings" | None = None) -> Path:
    if settings is not None and settings.pki_dir:
        return Path(settings.pki_dir).expanduser()
    return vox_home() / "pki"


def psk_path() -> Path:
    return vox_home() / "mesh-psk"


ID_HASH_CHARS = 12


def network_mac() -> int | None:
    """The MAC of a network interface, or None when there is no real one.

    ``uuid.getnode`` invents a random 48-bit number when it cannot find a
    hardware address, and marks it by setting the multicast bit — which no
    genuine interface has. That invented value changes on every process, so it
    is useless as an identity and is rejected here.
    """
    node = uuid.getnode()
    if node >> 40 & 1:
        return None
    return node


def machine_hash() -> str:
    """A short, stable fingerprint of this machine, from its MAC address.

    The address is hashed and never announced: what goes on the wire is 12 hex
    characters, stable across restarts and reinstalls, different on every
    machine, and giving away neither the hardware address nor the user and
    host names.

    With no real MAC we fall back to hashing user and host. That is weaker —
    two identically named accounts on identically named hosts collide — but it
    still never comes from the configuration file, which is the point: a
    configuration copied onto a second machine must not hand it the same
    identity as the first.
    """
    node = network_mac()
    if node is not None:
        seed = node.to_bytes(6, "big")
    else:
        log.warning(
            "no hardware MAC address found; fingerprinting this machine from "
            "user and host instead"
        )
        try:
            user = getpass.getuser()
        except Exception:  # pragma: no cover - depends on the environment
            user = "vox"
        host = socket.gethostname().split(".")[0] or "host"
        seed = f"{user}@{host}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:ID_HASH_CHARS]


def default_agent_id() -> str:
    """The agent id when the configuration names no label of its own."""
    return f"vox-{machine_hash()}"


@dataclass
class MeshSettings:
    """The ``mesh`` block of the configuration, already typed."""

    enabled: bool = False
    agent_id: str = ""
    name: str = "vox"
    verbs: list[str] = field(default_factory=lambda: ["infer"])
    announce_interval: float = 60.0
    group: str = "239.17.42.1"
    port: int = 45177
    pki_dir: str = ""
    auto_provision: bool = True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MeshSettings":
        section = config.get("mesh", {}) if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            section = {}
        defaults = cls()
        verbs = section.get("verbs") or defaults.verbs
        return cls(
            enabled=bool(section.get("enabled", defaults.enabled)),
            agent_id=str(section.get("agent_id", "") or ""),
            name=str(section.get("name", defaults.name)),
            verbs=[str(verb) for verb in verbs],
            announce_interval=float(
                section.get("announce_interval", defaults.announce_interval)
            ),
            group=str(section.get("group", defaults.group)),
            port=int(section.get("port", defaults.port)),
            pki_dir=str(section.get("pki_dir", "") or ""),
            auto_provision=bool(section.get("auto_provision", defaults.auto_provision)),
        )

    def resolved_agent_id(self) -> str:
        """The label from the configuration, always ending in this machine.

        The machine fingerprint is appended rather than replaced, so the same
        configuration file on two machines still yields two identities — and
        two certificates. Anything else would put the same name, and the same
        SAN, on the wire twice.
        """
        machine = machine_hash()
        label = _LABEL_RE.sub("-", self.agent_id.lower()).strip("-")
        label = label[: 62 - ID_HASH_CHARS].rstrip("-")
        if label.endswith(machine):  # already resolved once
            return label
        return f"{label}-{machine}" if label else f"vox-{machine}"

    def capabilities(self) -> dict[str, Any]:
        return {
            "verbs": list(self.verbs),
            "formats": ["application/json"],
            "max_concurrency": 1,
        }


def category_for(verbs: list[str]) -> str:
    """The category the taxonomy gives these verbs, computed by the mesh code."""
    from .discovery.whois import classify

    return classify({"verbs": list(verbs)})


# ------------------------------------------------------------------ identity


def ensure_psk(generate: bool = True) -> bytes:
    """The pre-shared key: the environment first, then a file we may create.

    Every member of a mesh must hold the same key, so a generated one only
    ever gives you a mesh of one until it is copied.
    """
    from_env = os.environ.get(PSK_ENV, "").strip()
    if from_env:
        key = from_env.encode("utf-8")
        if len(key) < MIN_PSK_BYTES:
            raise MeshError(
                f"${PSK_ENV} is only {len(key)} bytes; the mesh needs at least "
                f"{MIN_PSK_BYTES}"
            )
        return key

    path = psk_path()
    if path.exists():
        key = path.read_bytes().strip()
        if len(key) < MIN_PSK_BYTES:
            raise MeshError(f"{path} holds a key shorter than {MIN_PSK_BYTES} bytes")
        return key

    if not generate:
        raise MeshError(f"no pre-shared key: set ${PSK_ENV} or write {path}")
    key = secrets.token_hex(32).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows has no POSIX modes
        pass
    log.info("generated a mesh pre-shared key at %s", path)
    return key


def ensure_identity(agent_id: str, directory: Path, provision: bool = True):
    """Load this agent's identity, creating the CA and certificate if needed."""
    from .discovery.identity import (
        Identity,
        IdentityError,
        create_ca,
        issue_agent_cert,
        validate_agent_id,
    )

    validate_agent_id(agent_id)
    directory = Path(directory).expanduser()
    ca_crt, ca_key = directory / "ca.crt", directory / "ca.key"

    def issue() -> None:
        if not provision:
            raise MeshError(
                f"no certificate for {agent_id} in {directory}; provision one or "
                "turn mesh.auto_provision on"
            )
        directory.mkdir(parents=True, exist_ok=True)
        if not (ca_crt.exists() and ca_key.exists()):
            create_ca(directory)
            log.info("created a local certificate authority in %s", directory)
        issue_agent_cert(directory, agent_id, ca_crt, ca_key, lifetime=CERT_LIFETIME)
        log.info("issued a certificate for %s", agent_id)

    if not (directory / f"{agent_id}.crt").exists():
        issue()
    try:
        identity = Identity.load(agent_id, directory)
    except IdentityError:
        # A certificate that does not match this agent id is worse than none.
        issue()
        identity = Identity.load(agent_id, directory)

    if identity.needs_renewal():
        issue()
        identity = Identity.load(agent_id, directory)
    return identity


# ---------------------------------------------------------------- controller


@dataclass(frozen=True)
class PeerView:
    """One row of the universe, already flattened for display."""

    agent_id: str
    name: str
    category: str
    state: str
    address: str
    verbs: list[str]
    age: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "category": self.category,
            "state": self.state,
            "address": self.address,
            "verbs": self.verbs,
            "seconds_since_last_seen": round(self.age, 1),
        }


class MeshController:
    """Owns the discovery agent, and nothing about the screen."""

    def __init__(self, settings: MeshSettings) -> None:
        self.settings = settings
        self.agent = None
        self.last_error: str | None = None
        self.identity = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def online(self) -> bool:
        return self.agent is not None

    @property
    def agent_id(self) -> str:
        return self.settings.resolved_agent_id()

    @property
    def category(self) -> str:
        return category_for(self.settings.verbs)

    def start(self) -> str:
        """Go online. Returns a line describing exactly what was started."""
        with self._lock:
            if self.agent is not None:
                return "already online"
            try:
                from .discovery.agent import DiscoveryAgent
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise MeshError(
                    f"the mesh needs the cryptography package: {exc}"
                ) from exc

            settings = self.settings
            agent_id = self.agent_id
            identity = ensure_identity(
                agent_id, pki_dir(settings), provision=settings.auto_provision
            )
            psk = ensure_psk(generate=settings.auto_provision)

            agent = DiscoveryAgent(
                identity=identity,
                name=settings.name,
                capabilities=settings.capabilities(),
                psk=psk,
                group=settings.group,
                port=settings.port,
                announce_interval=settings.announce_interval,
            )
            try:
                agent.start()
            except OSError as exc:
                raise MeshError(f"cannot join {settings.group}:{settings.port}: {exc}") from exc
            self.agent = agent
            self.identity = identity
            self.last_error = None
            log.info("mesh online as %s (%s)", agent_id, self.category)
            return (
                f"{agent_id} · {self.category} · announcing on "
                f"{settings.group}:{settings.port} every "
                f"{settings.announce_interval:g}s"
            )

    def stop(self) -> None:
        with self._lock:
            agent, self.agent = self.agent, None
            if agent is None:
                return
            try:
                agent.stop()
            except OSError as exc:  # pragma: no cover - best effort teardown
                log.warning("stopping the mesh agent: %s", exc)
            log.info("mesh offline")

    # ----------------------------------------------------------------- peers

    def peers(self) -> list[PeerView]:
        """Everyone the registry has seen, newest sighting first."""
        agent = self.agent
        if agent is None:
            return []
        import time

        now = time.time()
        views = []
        for peer in agent.registry.snapshot():
            capabilities = peer.capabilities or {}
            views.append(
                PeerView(
                    agent_id=peer.agent_id,
                    name=peer.name or peer.agent_id,
                    category=peer.category or "—",
                    state=peer.state.value,
                    address=f"{peer.address}:{peer.whois_port}",
                    verbs=[str(v) for v in capabilities.get("verbs", [])],
                    age=max(0.0, now - peer.last_seen),
                )
            )
        views.sort(key=lambda view: view.age)
        return views

    def active_count(self) -> int:
        return len([peer for peer in self.peers() if peer.state == "ACTIVE"])

    def status_line(self) -> str:
        if not self.online:
            return ""
        peers = self.peers()
        active = len([peer for peer in peers if peer.state == "ACTIVE"])
        return f"MESH ONLINE  ·  {len(peers)} agents, {active} active"

    def sharing_note(self) -> str:
        """What another machine needs before it can see this one."""
        return (
            f"a second machine joins only with the same {pki_dir(self.settings) / 'ca.crt'} "
            f"and the same key from {psk_path()}"
        )
