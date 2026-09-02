"""
The local registry of peers.

The point of the design: the registry never stops listening to the
announcements of an agent it already knows. The periodic announcement is the
heartbeat. What stops after the first contact is the WHOIS.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field, replace


class PeerState(enum.Enum):
    PROBATION = "PROBATION"  # seen, not yet interrogated: no work is routed to it
    ACTIVE = "ACTIVE"  # WHOIS completed, classified, usable
    SUSPECT = "SUSPECT"  # heartbeats missing: probably dead
    DEAD = "DEAD"  # to be dropped from routing


class Observation(enum.Enum):
    """What receiving an announcement turned out to mean."""

    NEW = "NEW"  # never seen: a WHOIS is needed
    REINCARNATED = "REINCARNATED"  # restarted or moved: the cache is void, WHOIS again
    CAPS_CHANGED = (
        "CAPS_CHANGED"  # same incarnation, different capabilities: WHOIS again
    )
    HEARTBEAT = "HEARTBEAT"  # known and unchanged: only last_seen moves
    STALE = "STALE"  # an announcement from an older incarnation: ignored


@dataclass
class Peer:
    agent_id: str
    address: str
    whois_port: int
    incarnation: int
    caps_digest: str
    state: PeerState = PeerState.PROBATION
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Filled in by the WHOIS
    name: str | None = None
    capabilities: dict = field(default_factory=dict)
    category: str | None = None
    taxonomy_version: str | None = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self.address, self.whois_port)


class Registry:
    """Thread-safe. Every agent keeps its own: the view is eventually
    consistent, and there is no central authority."""

    def __init__(
        self,
        suspect_after: float = 3.0,
        dead_after: float = 5.0,
        announce_interval: float = 60.0,
    ):
        # The thresholds are multiples of the announce interval rather than
        # fixed seconds, so changing the interval cannot break the detector.
        self._suspect_after = suspect_after * announce_interval
        self._dead_after = dead_after * announce_interval
        self._peers: dict[str, Peer] = {}
        self._lock = threading.RLock()

    def observe(self, announce, address: str) -> tuple[Observation, Peer]:
        """Record an announcement and tell the caller what to do about it."""
        now = time.time()
        with self._lock:
            known = self._peers.get(announce.agent_id)

            if known is None:
                peer = Peer(
                    agent_id=announce.agent_id,
                    address=address,
                    whois_port=announce.whois_port,
                    incarnation=announce.incarnation,
                    caps_digest=announce.caps_digest,
                )
                self._peers[peer.agent_id] = peer
                return Observation.NEW, peer

            if announce.incarnation < known.incarnation:
                # An old packet arriving late: it must not be able to drag a
                # restarted peer's state backwards.
                return Observation.STALE, known

            if announce.incarnation > known.incarnation:
                peer = replace(
                    known,
                    address=address,
                    whois_port=announce.whois_port,
                    incarnation=announce.incarnation,
                    caps_digest=announce.caps_digest,
                    state=PeerState.PROBATION,
                    last_seen=now,
                    name=None,
                    capabilities={},
                    category=None,
                    taxonomy_version=None,
                )
                self._peers[peer.agent_id] = peer
                return Observation.REINCARNATED, peer

            known.last_seen = now
            known.address = address
            if known.state in (PeerState.SUSPECT, PeerState.DEAD):
                # A false positive from the failure detector, or a return
                # after a network partition.
                known.state = (
                    PeerState.ACTIVE if known.capabilities else PeerState.PROBATION
                )

            if announce.caps_digest != known.caps_digest:
                known.caps_digest = announce.caps_digest
                known.state = PeerState.PROBATION
                return Observation.CAPS_CHANGED, known

            return Observation.HEARTBEAT, known

    def promote(
        self,
        agent_id: str,
        name: str,
        capabilities: dict,
        category: str,
        taxonomy_version: str,
    ) -> None:
        """End the probation after a successful WHOIS."""
        with self._lock:
            peer = self._peers.get(agent_id)
            if peer is None:
                return
            peer.name = name
            peer.capabilities = capabilities
            peer.category = category
            peer.taxonomy_version = taxonomy_version
            peer.state = PeerState.ACTIVE

    def reap(self) -> list[tuple[Peer, PeerState]]:
        """Downgrade silent peers. Meant to be called periodically.

        It returns the transitions that happened, so the caller can notify the
        router or emit metrics.
        """
        now = time.time()
        transitions = []
        with self._lock:
            for peer in list(self._peers.values()):
                silence = now - peer.last_seen
                previous = peer.state
                if silence > self._dead_after and peer.state is not PeerState.DEAD:
                    peer.state = PeerState.DEAD
                elif (
                    self._suspect_after < silence <= self._dead_after
                    and peer.state in (PeerState.ACTIVE, PeerState.PROBATION)
                ):
                    peer.state = PeerState.SUSPECT
                if peer.state is not previous:
                    transitions.append((peer, previous))
        return transitions

    def routable(self) -> list[Peer]:
        """Only the peers it makes sense to send work to."""
        with self._lock:
            return [p for p in self._peers.values() if p.state is PeerState.ACTIVE]

    def snapshot(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def forget(self, agent_id: str) -> None:
        with self._lock:
            self._peers.pop(agent_id, None)
