"""The vendored discovery suite: protocol, registry, taxonomy, mTLS, attacks.

These tests bind real multicast and TLS sockets and sleep for tens of seconds,
so they are skipped unless ``VOX_TEST_MESH=1`` asks for them:

    VOX_TEST_MESH=1 pytest tests/test_discovery_vendor.py
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import ssl
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("VOX_TEST_MESH") != "1",
    reason="set VOX_TEST_MESH=1 to run the mesh tests (real sockets, slow)",
)

cryptography = pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402

from vox_chat.discovery import protocol, whois  # noqa: E402
from vox_chat.discovery.agent import DiscoveryAgent  # noqa: E402
from vox_chat.discovery.identity import (  # noqa: E402
    Identity,
    IdentityError,
    create_ca,
    issue_agent_cert,
    spiffe_id,
)
from vox_chat.discovery.registry import Observation, PeerState, Registry  # noqa: E402

@pytest.fixture(scope="module")
def pki() -> tuple[Path, Path]:
    """A throwaway certificate authority, plus a second, foreign one."""
    directory = Path(tempfile.mkdtemp(prefix="pki-"))
    rogue = Path(tempfile.mkdtemp(prefix="rogue-"))
    create_ca(directory)
    create_ca(rogue, common_name="rogue-ca")
    try:
        yield directory, rogue
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        shutil.rmtree(rogue, ignore_errors=True)


def issue(agent_id: str, directory: Path, ca: Path | None = None) -> Identity:
    ca = ca or directory
    issue_agent_cert(directory, agent_id, ca / "ca.crt", ca / "ca.key")
    return Identity.load(agent_id, directory, ca_path=ca / "ca.crt")


def test_protocol() -> None:
    """The signing paths are covered in tests/test_mesh.py, which needs no
    sockets; what is left here is the replay window."""
    guard = protocol.ReplayGuard()
    announce = protocol.new_announce("a1", 1, 9000, "deadbeef")
    guard.check(announce)
    with pytest.raises(protocol.ProtocolError):
        guard.check(announce)  # the same nonce twice

    old = protocol.Announce("a1", 1, 9000, "x", time.time() - 120, "n2")
    with pytest.raises(protocol.ProtocolError):
        protocol.ReplayGuard().check(old)


def test_registry() -> None:
    registry = Registry(announce_interval=1.0, suspect_after=1, dead_after=2)
    first = registry.observe(protocol.new_announce("p1", 100, 9000, "c1"), "10.0.0.5")
    assert first[0] is Observation.NEW
    again = registry.observe(protocol.new_announce("p1", 100, 9000, "c1"), "10.0.0.5")
    assert again[0] is Observation.HEARTBEAT  # not discarded as a duplicate

    registry.promote("p1", "proc", {"verbs": ["transform"]}, "PROCESSOR", "v1")
    assert len(registry.routable()) == 1  # after WHOIS: ACTIVE and routable

    stale = registry.observe(protocol.new_announce("p1", 99, 9000, "c1"), "10.0.0.5")
    assert stale[0] is Observation.STALE  # an older incarnation

    restarted = registry.observe(
        protocol.new_announce("p1", 101, 9001, "c2"), "10.0.0.9"
    )
    assert restarted[0] is Observation.REINCARNATED
    assert not registry.routable()  # a restart drops it out of routing

    registry.promote("p1", "proc", {}, "PROCESSOR", "v1")
    time.sleep(1.2)
    registry.reap()
    assert registry.snapshot()[0].state is PeerState.SUSPECT
    time.sleep(1.1)
    registry.reap()
    assert registry.snapshot()[0].state is PeerState.DEAD


def test_identity(pki: tuple[Path, Path]) -> None:
    pki, _rogue = pki
    identity = issue("alpha-01", pki)
    san = identity.certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "alpha-01" in san.get_values_for_type(x509.DNSName)
    assert spiffe_id("alpha-01") in san.get_values_for_type(
        x509.UniformResourceIdentifier
    )
    assert not identity.needs_renewal()

    for bad in ("has_underscore", "with.dot", ""):
        with pytest.raises(IdentityError):
            issue(bad, pki)


def test_mtls_handshake(pki: tuple[Path, Path]) -> None:
    pki, rogue_dir = pki
    server_identity = issue("server-01", pki)
    client_identity = issue("client-01", pki)
    server = whois.WhoisServer(
        {"agent_id": "server-01", "name": "srv", "capabilities": {"verbs": ["store"]}},
        identity=server_identity,
    )
    server.start()
    try:
        described = whois.query("127.0.0.1", server.port, "server-01", client_identity)
        assert described["name"] == "srv"

        # 1. a certificate signed by another authority
        rogue = issue("intruder-01", rogue_dir, ca=rogue_dir)
        with pytest.raises((ssl.SSLError, OSError)):
            whois.query("127.0.0.1", server.port, "server-01", rogue)

        # 2. a client presenting no certificate at all
        with pytest.raises((ssl.SSLError, OSError)):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", server.port), timeout=3) as raw:
                with context.wrap_socket(raw) as tls:
                    tls.sendall(b'{"op":"WHOIS"}\n')
                    assert not tls.recv(100)
                    raise OSError("no answer")

        # 3. impersonation: the announcement claims another agent id
        with pytest.raises(ssl.SSLCertVerificationError):
            whois.query("127.0.0.1", server.port, "victim-01", client_identity)

        # 4. authorisation is separate from authentication
        restricted = whois.WhoisServer(
            {"agent_id": "server-02", "name": "s2", "capabilities": {}},
            identity=issue("server-02", pki),
            authorizer=lambda agent_id: agent_id.startswith("admin-"),
        )
        restricted.start()
        try:
            with pytest.raises(ValueError, match="forbidden"):
                whois.query("127.0.0.1", restricted.port, "server-02", client_identity)
        finally:
            restricted.stop()
    finally:
        server.stop()


def test_ask_over_mtls(pki: tuple[Path, Path]) -> None:
    """The ASK operation on the channel WHOIS already uses."""
    import threading

    pki, _rogue = pki
    server_identity = issue("answerer-01", pki)
    client_identity = issue("asker-01", pki)
    seen: list[tuple[str, str]] = []

    def handler(caller: str, question: str) -> dict:
        seen.append((caller, question))
        time.sleep(0.3)  # a model takes a moment; the 5s handshake cap must not bite
        return {"answer": f"{question} -> yes", "model": "test:1b"}

    server = whois.WhoisServer(
        {"agent_id": "answerer-01", "name": "answerer",
         "capabilities": {"verbs": ["infer"]}},
        identity=server_identity,
        ask_handler=handler,
    )
    server.start()
    try:
        reply = whois.ask("127.0.0.1", server.port, "answerer-01", client_identity,
                          "is it safe", timeout=10)
        assert reply["answer"] == "is it safe -> yes"
        assert reply["model"] == "test:1b"
        assert seen == [("asker-01", "is it safe")], "the peer sees the question, nothing else"

        # WHOIS still works on the same socket.
        assert whois.query("127.0.0.1", server.port, "answerer-01",
                           client_identity)["name"] == "answerer"

        # A question that is too large never reaches the model.
        with pytest.raises(ValueError, match="bytes"):
            whois.ask("127.0.0.1", server.port, "answerer-01", client_identity,
                      "x" * (whois.MAX_ASK_BYTES + 1), timeout=10)

        # One at a time: the second caller is told to go away, not queued.
        results: list[str] = []

        def fire() -> None:
            try:
                whois.ask("127.0.0.1", server.port, "answerer-01",
                          client_identity, "concurrent", timeout=10)
                results.append("answered")
            except ValueError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["answered", "busy"]
    finally:
        server.stop()


def test_a_node_that_does_not_answer_says_so(pki: tuple[Path, Path]) -> None:
    pki, _rogue = pki
    server = whois.WhoisServer(
        {"agent_id": "quiet-01", "name": "quiet", "capabilities": {}},
        identity=issue("quiet-01", pki),
    )
    server.start()
    try:
        with pytest.raises(ValueError, match="ask not supported"):
            whois.ask("127.0.0.1", server.port, "quiet-01",
                      issue("caller-01", pki), "hello", timeout=10)
    finally:
        server.stop()


def test_integration(pki: tuple[Path, Path]) -> None:
    pki, rogue_dir = pki
    logging.basicConfig(level=logging.INFO, format="        %(message)s")
    specs = [
        ("ingestor", "ingestor-90", ["ingest"]),
        ("enricher", "enricher-90", ["transform", "enrich"]),
        ("watcher", "watcher-90", ["observe"]),
    ]
    agents = [
        DiscoveryAgent(
            identity=issue(agent_id, pki),
            name=name,
            capabilities={"verbs": verbs},
            announce_interval=2.0,
        )
        for name, agent_id, verbs in specs
    ]
    for agent in agents:
        agent.start()
    time.sleep(8)
    try:
        for agent in agents:
            active = [p for p in agent.registry.snapshot() if p.state is PeerState.ACTIVE]
            assert len(active) == 2, f"{agent.name}: {len(active)}/2"

        assert [p.name for p in agents[0].peers_for("transform")] == ["enricher"]

        watcher = [p for p in agents[0].registry.snapshot() if p.name == "watcher"][0]
        # A passive agent is visible but deliberately kept out of routing.
        assert watcher.category == "OBSERVER"
        assert watcher not in agents[0].peers_for("observe")

        # An intruder from another CA: its announcements no longer even parse,
        # because the certificate they carry is not one this mesh trusts.
        intruder = DiscoveryAgent(
            identity=Identity.load("intruder-01", rogue_dir, ca_path=rogue_dir / "ca.crt"),
            name="intruder",
            capabilities={"verbs": ["observe"]},
            announce_interval=2.0,
        )
        intruder.start()
        time.sleep(7)
        seen = [p for p in agents[0].registry.snapshot() if p.agent_id == "intruder-01"]
        assert not seen, f"the intruder entered the registry: {seen}"
        intruder.stop()

        agents[2].stop()
        time.sleep(9)  # long enough for the reaper to notice
        watcher = [p for p in agents[0].registry.snapshot() if p.name == "watcher"][0]
        assert watcher.state in (PeerState.SUSPECT, PeerState.DEAD)
    finally:
        for agent in agents:
            try:
                agent.stop()
            except Exception:  # pragma: no cover - best effort teardown
                pass
