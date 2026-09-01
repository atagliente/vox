"""
The orchestration: four threads sharing one registry.

  announcer  -> sends the signed announcement every interval, with jitter
  listener   -> receives announcements and decides whether a WHOIS is needed
  whois      -> answers everyone else's interrogations
  reaper     -> downgrades silent peers (SUSPECT -> DEAD)
"""

from __future__ import annotations

import logging
import os
import queue
import random
import ssl
import threading
import time

from . import protocol, transport, whois
from .identity import Identity
from .registry import Observation, PeerState, Registry

log = logging.getLogger("discovery")


class DiscoveryAgent:
    def __init__(
        self,
        identity: Identity,
        name: str,
        capabilities: dict,
        psk: bytes,
        group: str = transport.DEFAULT_GROUP,
        port: int = transport.DEFAULT_PORT,
        announce_interval: float = 60.0,
        jitter: float = 0.2,
        whois_port: int = 0,
        authorizer=None,
    ):
        if len(psk) < 16:
            raise ValueError("the pre-shared key is too short")

        self.identity = identity
        self.agent_id = identity.agent_id
        self.name = name
        self.capabilities = capabilities
        self._psk = psk
        self._group = group
        self._port = port
        self._interval = announce_interval
        self._jitter = jitter

        # The incarnation tells the process's "lives" apart. With persistent
        # storage this would be a monotonic counter; the start timestamp is a
        # fair approximation as long as the clock never goes backwards.
        self.incarnation = int(time.time())
        self._caps_digest = protocol.caps_digest(capabilities)

        self.registry = Registry(announce_interval=announce_interval)
        self._replay = protocol.ReplayGuard()

        self._whois = whois.WhoisServer(
            self._descriptor(), identity=identity, port=whois_port, authorizer=authorizer
        )
        # Peers whose certificate did not check out. They are not interrogated
        # again on every announcement, or one misconfigured node would produce
        # an endless stream of handshakes.
        self._rejected: set[str] = set()
        self._whois_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ----------------------------------------------------------------- lifecycle

    def _descriptor(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "incarnation": self.incarnation,
            "capabilities": self.capabilities,
            "protocol_version": protocol.PROTO_VERSION,
        }

    def start(self) -> None:
        self._whois.start()
        log.info("[%s] whois listening on :%d", self.name, self._whois.port)
        for target, label in (
            (self._announce_loop, "announcer"),
            (self._listen_loop, "listener"),
            (self._whois_loop, "whois-worker"),
            (self._reap_loop, "reaper"),
            (self._cert_watch_loop, "cert-watch"),
        ):
            thread = threading.Thread(target=target, name=label, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._whois.stop()
        for thread in self._threads:
            thread.join(timeout=3.0)

    # ---------------------------------------------------------------- announcer

    def _next_delay(self) -> float:
        """Jitter: without it, after a mass restart everyone announces in the same
        instant and N simultaneous WHOIS handshakes go off at once."""
        spread = self._interval * self._jitter
        return max(0.5, random.uniform(self._interval - spread, self._interval + spread))

    def _announce_loop(self) -> None:
        sock = transport.make_sender(self._group)
        try:
            # The first announcement comes almost at once, but offset: it
            # avoids the thundering herd when several agents start together.
            self._stop.wait(random.uniform(0, min(2.0, self._interval * 0.1)))
            while not self._stop.is_set():
                announce = protocol.new_announce(
                    self.agent_id, self.incarnation, self._whois.port, self._caps_digest
                )
                try:
                    sock.sendto(protocol.encode(announce, self._psk), (self._group, self._port))
                except OSError as exc:
                    log.warning("[%s] sending the announcement failed: %s", self.name, exc)
                self._stop.wait(self._next_delay())
        finally:
            sock.close()

    # ---------------------------------------------------------------- listener

    def _listen_loop(self) -> None:
        sock = transport.make_receiver(self._group, self._port)
        try:
            while not self._stop.is_set():
                try:
                    raw, (address, _) = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                self._handle_packet(raw, address)
        finally:
            sock.close()

    def _handle_packet(self, raw: bytes, address: str) -> None:
        try:
            announce = protocol.decode(raw, self._psk)
            self._replay.check(announce)
        except protocol.ProtocolError as exc:
            # The payload is not logged: it would be noise, and a log
            # injection vector.
            log.debug("announcement from %s dropped: %s", address, exc)
            return

        if announce.agent_id == self.agent_id:
            return  # an echo of ourselves

        if announce.agent_id in self._rejected:
            return  # already failed the handshake: do not retry every time

        observation, peer = self.registry.observe(announce, address)

        # This is where the design differs from "ignore a known peer's calls":
        # the packet is always processed, because it is the heartbeat. What is
        # skipped, for a peer already interrogated, is only the WHOIS.
        if observation is Observation.HEARTBEAT or observation is Observation.STALE:
            return

        log.info("[%s] %s -> %s (%s)", self.name, peer.agent_id, observation.value, address)
        self._whois_queue.put(peer.agent_id)

    # ---------------------------------------------------------------- whois worker

    def _whois_loop(self) -> None:
        while not self._stop.is_set():
            try:
                agent_id = self._whois_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            peer = next((p for p in self.registry.snapshot() if p.agent_id == agent_id), None)
            if peer is None or peer.state is not PeerState.PROBATION:
                continue

            # A random delay: if N agents see the same newcomer, they do not
            # all interrogate it in the same millisecond.
            self._stop.wait(random.uniform(0, 1.5))

            try:
                descriptor = whois.query(
                    peer.address, peer.whois_port, peer.agent_id, self.identity
                )
            except ssl.SSLCertVerificationError as exc:
                # The identity cannot be verified: either the peer is not who
                # it says it is, or it sits outside our CA. Neither is a
                # transient condition, so: no retry, and a loud log line.
                log.error("[%s] the identity of %s was NOT verified (%s@%s): %s",
                          self.name, agent_id, peer.whois_port, peer.address, exc)
                self._rejected.add(agent_id)
                self.registry.forget(agent_id)
                continue
            except (OSError, ValueError) as exc:
                log.warning("[%s] the whois to %s failed: %s", self.name, agent_id, exc)
                # It stays in PROBATION: the next announcement will not retry
                # on its own, so it goes back on the queue with a backoff.
                threading.Timer(5.0, lambda: self._whois_queue.put(agent_id)).start()
                continue

            capabilities = descriptor.get("capabilities", {})
            category = whois.classify(capabilities)
            self.registry.promote(
                agent_id=agent_id,
                name=descriptor.get("name", agent_id),
                capabilities=capabilities,
                category=category,
                taxonomy_version=whois.TAXONOMY_VERSION,
            )
            log.info("[%s] %s classified as %s%s", self.name,
                     descriptor.get("name", agent_id), category,
                     " (passive, kept out of routing)"
                     if category in whois.PASSIVE_CATEGORIES else "")

    # ---------------------------------------------------------------- reaper

    def _reap_loop(self) -> None:
        while not self._stop.is_set():
            for peer, previous in self.registry.reap():
                log.info("[%s] %s: %s -> %s", self.name, peer.agent_id,
                         previous.value, peer.state.value)
            self._stop.wait(max(1.0, self._interval / 3))

    # -------------------------------------------------------------- certificates

    def _cert_watch_loop(self) -> None:
        """Watch our own certificate's expiry.

        The certificates are short-lived on purpose: it is the most practical
        form of revocation there is. But an expired certificate means refused
        handshakes and an agent invisible to the rest of the mesh, so the
        expiry has to be seen coming.
        """
        while not self._stop.is_set():
            try:
                if self.identity.needs_renewal():
                    log.warning("[%s] certificate past half life (expires %s): renew it",
                                self.name, self.identity.expires_at().isoformat())
            except OSError as exc:
                log.error("[%s] the certificate cannot be read: %s", self.name, exc)
            self._stop.wait(300.0)

    def renew(self, identity: Identity) -> None:
        """Adopt a freshly issued certificate, without restarting.

        The agent_id cannot change: that would be a different identity rather
        than a renewal, and peers would see it as a new agent.
        """
        if identity.agent_id != self.agent_id:
            raise ValueError("a renewal cannot change the agent_id")
        self.identity = identity
        self._whois.reload_identity(identity)
        log.info("[%s] certificate renewed, valid until %s",
                 self.name, identity.expires_at().isoformat())

    # ---------------------------------------------------------------- query API

    def peers_for(self, verb: str) -> list:
        """Active, non-passive peers that declare they can do `verb`."""
        return [
            p for p in self.registry.routable()
            if p.category not in whois.PASSIVE_CATEGORIES
            and verb in (p.capabilities.get("verbs") or [])
        ]


def load_psk(env_var: str = "DISCOVERY_PSK") -> bytes:
    """The key does not belong in the source. In production it comes from a
    secret manager, and it can be rotated. Note that a shared PSK is an
    all-or-nothing model: whoever holds it can announce itself as anyone. A
    real per-agent identity needs asymmetric keys (an Ed25519 signature) or
    SPIFFE/SPIRE."""
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(f"{env_var} is not set")
    return value.encode()
