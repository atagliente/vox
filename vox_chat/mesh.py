"""Joining the agent mesh: identity, keys, and the discovery agent's life.

Everything the interface must not know about lives here — where the
certificate comes from, how it is renewed, and how the announcer is started
and stopped. The UI asks for ``online`` and gets peers.

There is no shared secret anywhere: an agent signs its announcements with its
own private key, and the only file a second machine needs from this one is the
certificate authority, whose private half stays behind.

Going online announces this machine's presence on the local network segment.
That is a deliberate act, so the controller reports exactly what it did.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import hashlib
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging_setup import get_logger
from .storage import vox_home

if TYPE_CHECKING:  # consensus imports mesh, so this stays out of runtime.
    from .consensus import PeerAnswer

log = get_logger("mesh")

CERT_LIFETIME = dt.timedelta(hours=24)

_LABEL_RE = re.compile(r"[^a-zA-Z0-9-]+")


class MeshError(Exception):
    """Something stopped the mesh; the chat carries on regardless."""


def pki_dir(settings: MeshSettings | None = None) -> Path:
    if settings is not None and settings.pki_dir:
        return Path(settings.pki_dir).expanduser()
    return vox_home() / "pki"


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
        seed = f"{user}@{host}".encode()
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
    demo_ca: bool = True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> MeshSettings:
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
            demo_ca=bool(section.get("demo_ca", defaults.demo_ca)),
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


def demo_pki_dir() -> Path:
    """Where the authority shipped with VOX lives inside the package."""
    return Path(__file__).resolve().parent / "demo_pki"


def _fingerprint(path: Path) -> str | None:
    """The SHA-256 of a certificate, or None when there is nothing to read."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None
    return certificate.fingerprint(hashes.SHA256()).hex()


def using_demo_ca(directory: Path | None = None) -> bool:
    """Is this machine trusting the authority that ships with VOX?

    Compared by fingerprint rather than by filename, so a copied or renamed
    demo authority is still recognised for what it is.
    """
    directory = Path(directory) if directory is not None else pki_dir()
    theirs = _fingerprint(Path(directory).expanduser() / "ca.crt")
    return theirs is not None and theirs == _fingerprint(demo_pki_dir() / "ca.crt")


def seed_demo_ca(directory: Path) -> None:
    """Copy the shipped authority into ``directory``.

    This is what makes a fresh download useful: two installations that have
    provisioned nothing still trust the same authority, so they see each other.
    The private key is public — it is in the package — so this is a
    demonstration, not a security boundary. ``new_ca`` replaces it.
    """
    import shutil

    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("ca.crt", "ca.key"):
        shutil.copy(demo_pki_dir() / name, directory / name)
    with contextlib.suppress(OSError):  # pragma: no cover - no POSIX modes on Windows
        (directory / "ca.key").chmod(0o600)
    log.warning(
        "seeded %s with the demo authority shipped with VOX; its private key is "
        "public, so replace it before the mesh carries anything that matters",
        directory,
    )


def _swap_ca(directory: Path, agent_id: str, seed: bool):
    """Put a different authority in ``directory`` and reissue this agent.

    The old authority is moved aside rather than deleted — certificates issued
    by it stay readable if you need to look at them — but every certificate it
    signed goes, because none of them mean anything under the new anchor.
    """
    from .discovery.identity import create_ca

    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in ("ca.crt", "ca.key"):
        existing = directory / name
        if not existing.exists():
            continue
        # Two swaps in the same second must not overwrite the first backup,
        # and on Windows a rename onto an existing name fails outright.
        target = directory / f"{name}.replaced-{stamp}"
        attempt = 1
        while target.exists():
            attempt += 1
            target = directory / f"{name}.replaced-{stamp}-{attempt}"
        existing.rename(target)
    for stale in directory.glob("*.crt"):
        if not stale.name.startswith("ca.crt"):
            stale.unlink()
    if seed:
        seed_demo_ca(directory)
    else:
        create_ca(directory)
        log.info("created a private certificate authority in %s", directory)
    return ensure_identity(agent_id, directory)


def new_ca(directory: Path, agent_id: str):
    """Replace the authority with one private to this machine."""
    return _swap_ca(directory, agent_id, seed=False)


def use_demo_ca(directory: Path, agent_id: str):
    """Go back to the authority shipped with VOX.

    Useful for meeting a fresh installation, and for seeing what a new user
    sees. It means trusting an authority whose private key is public.
    """
    return _swap_ca(directory, agent_id, seed=True)


def ensure_identity(
    agent_id: str, directory: Path, provision: bool = True, demo: bool = True
):
    """Load this agent's identity, creating the CA and certificate if needed.

    ``demo`` says which authority a machine with none should land on: the
    sample one shipped with VOX, or a fresh private one.
    """
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
            # A fresh installation starts on the sample authority, so it can
            # see other fresh installations with nothing provisioned.
            if demo:
                seed_demo_ca(directory)
            else:
                create_ca(directory)
                log.info("created a private certificate authority in %s", directory)
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
        # Set by the app when consensus is available; passed to the discovery
        # server so peers can ask this node questions.
        self.answer_hook = None
        self.last_error: str | None = None
        self.identity = None
        # Peers refused by hand, before their certificates expire. Local to
        # this process, and deliberately not persisted: a refusal that
        # outlives the reason for it is one nobody remembers making.
        self.revoked: set[str] = set()
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

    @property
    def demo_ca(self) -> bool:
        """True while this machine trusts the authority shipped with VOX."""
        return using_demo_ca(pki_dir(self.settings))

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
            directory = pki_dir(settings)
            # mesh.demo_ca is a statement about which authority to be on, not
            # only about what to do when there is none: a machine that asks for
            # the sample one is moved onto it.
            if (
                settings.demo_ca
                and settings.auto_provision
                and (directory / "ca.crt").exists()
                and not using_demo_ca(directory)
            ):
                log.info(
                    "mesh.demo_ca is on: moving %s to the sample authority", directory
                )
                use_demo_ca(directory, agent_id)
            identity = ensure_identity(
                agent_id,
                directory,
                provision=settings.auto_provision,
                demo=settings.demo_ca,
            )
            agent = DiscoveryAgent(
                identity=identity,
                name=settings.name,
                capabilities=settings.capabilities(),
                group=settings.group,
                port=settings.port,
                announce_interval=settings.announce_interval,
            )
            if self.answer_hook is not None:
                agent._whois.set_ask_handler(self.answer_hook)
            try:
                agent.start()
            except OSError as exc:
                raise MeshError(
                    f"cannot join {settings.group}:{settings.port}: {exc}"
                ) from exc
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

    def processors(self, verb: str = "infer", limit: int = 5) -> list[Any]:
        """Active peers that declare ``verb`` — who a question can go to.

        OBSERVERs are already excluded by ``peers_for``: an agent that declared
        itself passive is not asked to work.
        """
        agent = self.agent
        if agent is None:
            return []
        return list(agent.peers_for(verb))[:limit]

    def ask_peers(
        self,
        question: str,
        verb: str = "infer",
        limit: int = 5,
        timeout: float = 90.0,
        on_event=None,
        conversation: str = "",
        round_budget: float = 0.0,
    ) -> list[PeerAnswer]:
        """Ask every matching peer the same question, in parallel.

        A peer that fails or times out comes back with ``error`` set rather
        than vanishing: a silent agent and an agent that refused are different
        things, and the operator should see which happened.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .consensus import PeerAnswer
        from .discovery import whois

        agent = self.agent
        targets = self.processors(verb, limit)
        if agent is None or not targets:
            return []

        def one(peer) -> PeerAnswer:
            label = peer.name or peer.agent_id
            started = time.monotonic()

            def relay(kind: str, text: str, ts: float) -> None:
                if on_event is not None:
                    on_event(label, kind, text, ts)

            try:
                reply = whois.ask(
                    peer.address,
                    peer.whois_port,
                    peer.agent_id,
                    agent.identity,
                    question,
                    timeout=timeout,
                    on_event=relay if on_event is not None else None,
                    conversation=conversation,
                )
            except Exception as exc:
                # Wide on purpose: this runs once per peer, and one peer that
                # is broken in an unexpected way must come back as that peer's
                # error rather than sink the whole round.
                return PeerAnswer(
                    agent_id=peer.agent_id,
                    name=label,
                    error=str(exc)[:200] or exc.__class__.__name__,
                    elapsed=time.monotonic() - started,
                )
            return PeerAnswer(
                agent_id=peer.agent_id,
                name=label,
                model=reply.get("model", ""),
                answer=reply.get("answer", ""),
                elapsed=reply.get("elapsed") or (time.monotonic() - started),
            )

        # A round is as slow as its slowest peer, and one machine paging in a
        # model can hold up four that already answered. The budget is the cap
        # on the round rather than on each request: what has come back by then
        # is the answer, and the peers still thinking are reported as such.
        budget = float(round_budget or 0) or (timeout * len(targets))
        deadline = time.monotonic() + budget
        results: list[PeerAnswer] = []
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futures = {pool.submit(one, peer): peer for peer in targets}
            for future in as_completed(futures, timeout=None):
                results.append(future.result())
                if time.monotonic() >= deadline:
                    break
            waiting = [peer for future, peer in futures.items() if not future.done()]
            for peer in waiting:
                results.append(
                    PeerAnswer(
                        agent_id=peer.agent_id,
                        name=peer.name or peer.agent_id,
                        error=f"still thinking when the {budget:.0f}s round ended",
                        elapsed=budget,
                    )
                )
            # The threads are left to finish on their own rather than joined:
            # a peer that is slow should not also make leaving slow, and
            # nothing is waiting on what they return.
            pool.shutdown(wait=False, cancel_futures=True)
        # Back into the order the peers were asked in, so two rounds against
        # the same mesh read the same way.
        order = {peer.agent_id: index for index, peer in enumerate(targets)}
        results.sort(key=lambda answer: order.get(answer.agent_id, len(order)))
        return results

    def revoke(self, agent_id: str) -> str:
        """Refuse this peer from now on, without waiting for its certificate.

        Certificates last a day, which is short enough to be the practical
        form of revocation and far too long to be the only one: a peer found
        to be compromised at noon should not be answered until midnight.

        This is local. There is no revocation list on the network and no way
        to tell the other agents — saying so is better than implying a reach
        this does not have. Every VOX that wants this peer gone has to be
        told, and the effect lasts as long as this process does.
        """
        agent = self.agent
        if agent is None:
            return "the mesh is offline; there is nothing to revoke from"
        known = {peer.agent_id: peer for peer in agent.registry.snapshot()}
        agent._rejected.add(agent_id)
        agent.registry.forget(agent_id)
        self.revoked.add(agent_id)
        where = known.get(agent_id)
        name = (where.name if where else "") or agent_id
        return (
            f"{name} is refused from now on: dropped from the registry, and "
            "its announcements are ignored. This machine only — the other "
            "agents have not been told, and cannot be."
        )

    def unrevoke(self, agent_id: str) -> str:
        """Take a peer off the local refusal list."""
        agent = self.agent
        self.revoked.discard(agent_id)
        if agent is not None:
            agent._rejected.discard(agent_id)
        return f"{agent_id} may announce itself again"

    def set_answer_hook(self, hook) -> None:
        """Install (or remove) what answers other agents' questions."""
        self.answer_hook = hook
        agent = self.agent
        if agent is not None:
            agent._whois.set_ask_handler(hook)

    def peers(self) -> list[PeerView]:
        """Everyone the registry has seen, newest sighting first."""
        agent = self.agent
        if agent is None:
            return []
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
        line = f"MESH ONLINE  ·  {len(peers)} agents, {active} active"
        return f"{line}  ·  DEMO CERT" if self.demo_ca else line

    def replace_ca(self, demo: bool = False) -> str:
        """Swap the authority under this agent, and reissue it."""
        was_online = self.online
        self.stop()
        directory = pki_dir(self.settings)
        use_demo_ca(directory, self.agent_id) if demo else new_ca(
            directory, self.agent_id
        )
        if was_online:
            self.start()
        if demo:
            return (
                f"{directory} now holds the sample authority shipped with VOX: "
                f"any other VOX on this segment can join, and its private key "
                f"is public"
            )
        return (
            f"new certificate authority in {directory} — every other machine "
            f"needs a certificate issued by this one now"
        )

    def sharing_note(self) -> str:
        """What another machine needs before it can see this one."""
        directory = pki_dir(self.settings)
        if self.demo_ca:
            return (
                "USING THE DEMO CERTIFICATE shipped with VOX: any other VOX on "
                "this network segment can join, and its private key is public. "
                "/mesh new-ca replaces it with one only your machines hold"
            )
        return (
            f"a second machine joins with a certificate of its own issued by "
            f"{directory / 'ca.crt'} — copy that authority, not a shared secret: "
            f"every agent signs with its own key"
        )
