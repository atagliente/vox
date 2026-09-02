"""The mesh layer: settings, identity, the controller, and the way in.

Nothing here binds a socket. The discovery agent is stubbed, so these tests say
what VOX does with the mesh, not whether multicast works — that is
``tests/test_discovery_vendor.py``, which is skipped by default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat import mesh
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config, validate_config
from vox_chat.mesh import MeshController, MeshError, MeshSettings, PeerView
from vox_chat.storage import global_config_path
from vox_chat.ui.universe_screen import UniverseScreen
from vox_chat.ui.widgets import MessageBox


# --------------------------------------------------------------- configuration


def test_the_default_config_carries_a_mesh_block() -> None:
    config = default_config()
    assert config["mesh"]["enabled"] is False
    assert config["mesh"]["verbs"] == ["infer"]
    assert validate_config(config) == []


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"enabled": "yes"}, "mesh.enabled"),
        ({"verbs": []}, "mesh.verbs"),
        ({"announce_interval": 0}, "mesh.announce_interval"),
        ({"port": 70000}, "mesh.port"),
        ({"group": "10.0.0.1"}, "mesh.group"),
    ],
)
def test_a_broken_mesh_block_is_reported(change: dict, fragment: str) -> None:
    config = default_config()
    config["mesh"].update(change)
    errors = validate_config(config)
    assert any(fragment in error for error in errors), errors


def test_a_config_written_before_the_mesh_existed_is_still_valid() -> None:
    config = default_config()
    config.pop("mesh")
    assert validate_config(config) == []
    assert MeshSettings.from_config(config).verbs == ["infer"]


def test_settings_come_from_the_config_and_fall_back() -> None:
    settings = MeshSettings.from_config({"mesh": {"verbs": ["store", "index"]}})
    assert settings.verbs == ["store", "index"]
    assert settings.port == 45177
    assert settings.capabilities()["verbs"] == ["store", "index"]
    # An empty agent_id means "derive one", and it must be a DNS label.
    from vox_chat.discovery.identity import validate_agent_id

    assert validate_agent_id(settings.resolved_agent_id())


def test_one_config_on_two_machines_gives_two_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the identity cannot be dictated by the config file."""
    from vox_chat.discovery.identity import validate_agent_id

    config = {"mesh": {"agent_id": "workstation"}}

    monkeypatch.setattr(mesh.uuid, "getnode", lambda: 0x1C1BB5A82EEB)
    first = MeshSettings.from_config(config).resolved_agent_id()
    monkeypatch.setattr(mesh.uuid, "getnode", lambda: 0xAABBCCDDEEF0)
    second = MeshSettings.from_config(config).resolved_agent_id()

    assert first != second
    assert first.startswith("workstation-") and second.startswith("workstation-")
    for name in (first, second):
        assert validate_agent_id(name)

    # Resolving an already resolved id must not keep growing it.
    assert MeshSettings.from_config(
        {"mesh": {"agent_id": second}}
    ).resolved_agent_id() == second


def test_an_awkward_label_is_still_a_valid_name() -> None:
    from vox_chat.discovery.identity import validate_agent_id

    settings = MeshSettings.from_config({"mesh": {"agent_id": "My Laptop! (spare)"}})
    name = settings.resolved_agent_id()
    assert validate_agent_id(name)
    assert name.startswith("my-laptop-spare-")
    assert len(name) <= 63, "a DNS label has a limit"


def test_the_agent_id_is_the_hash_of_the_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    from vox_chat.discovery.identity import validate_agent_id

    monkeypatch.setattr(mesh.uuid, "getnode", lambda: 0x1C1BB5A82EEB)
    first = mesh.default_agent_id()
    assert validate_agent_id(first), "it goes into a certificate SAN"
    assert first == mesh.default_agent_id(), "stable across restarts"
    assert first.startswith("vox-") and len(first) == 4 + mesh.ID_HASH_CHARS

    # The address itself is never in the name, in any of the usual spellings.
    for spelling in ("1c1bb5a82eeb", "1c:1b:b5:a8:2e:eb", "1c-1b-b5-a8-2e-eb"):
        assert spelling not in first

    monkeypatch.setattr(mesh.uuid, "getnode", lambda: 0x1C1BB5A82EEC)
    assert mesh.default_agent_id() != first, "another machine, another name"


def test_a_machine_without_a_mac_still_gets_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uuid.getnode invents a random node and flags it with the multicast bit;
    it changes every run, so it must not become an identity."""
    from vox_chat.discovery.identity import validate_agent_id

    monkeypatch.setattr(mesh.uuid, "getnode", lambda: 0x010000000000 | 0x123456789A)
    assert mesh.network_mac() is None
    fallback = mesh.default_agent_id()
    assert validate_agent_id(fallback)
    assert fallback.startswith("vox-")
    assert fallback == mesh.default_agent_id(), "and it does not change per run"


@pytest.mark.parametrize(
    "verbs, category",
    [
        (["infer"], "PROCESSOR"),
        (["ingest"], "SOURCE"),
        (["store"], "SINK"),
        (["dispatch"], "ORCHESTRATOR"),
        (["observe"], "OBSERVER"),
        (["nonsense"], "UNKNOWN"),
    ],
)
def test_the_category_follows_the_declared_verbs(verbs: list[str], category: str) -> None:
    assert mesh.category_for(verbs) == category


# ------------------------------------------------------------- the signature


@pytest.fixture
def pki(tmp_path: Path):
    """One authority, a second foreign one, and identities under each."""
    pytest.importorskip("cryptography")
    from vox_chat.discovery.identity import Identity, create_ca, issue_agent_cert

    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    create_ca(ours)
    create_ca(theirs, common_name="rogue-ca")

    def identity(agent_id: str, directory: Path):
        issue_agent_cert(
            directory, agent_id, directory / "ca.crt", directory / "ca.key"
        )
        return Identity.load(agent_id, directory, ca_path=directory / "ca.crt")

    return ours, theirs, identity


def _material(identity):
    from cryptography.hazmat.primitives import serialization

    from vox_chat.discovery import protocol

    return (
        protocol.load_signing_key(identity.key_path),
        identity.certificate.public_bytes(serialization.Encoding.DER),
    )


def test_every_agent_signs_with_its_own_key(pki) -> None:
    """No pre-shared key: two agents produce two different signatures."""
    import json

    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")

    alice_key, alice_cert = _material(identity("alice-01", ours))
    bob_key, bob_cert = _material(identity("bob-01", ours))

    announce = protocol.new_announce("alice-01", 1, 9000, "cafe")
    packet = protocol.encode(announce, alice_key, alice_cert)
    assert protocol.decode(packet, ca_public) == announce
    assert len(packet) <= protocol.MAX_PACKET_BYTES

    # The same body signed by two agents gives two different signatures, which
    # is the whole point: version 1 gave both of them the same one.
    body = announce.canonical()
    assert alice_key.sign(body) != bob_key.sign(body)
    assert json.loads(packet)["v"] == 2


def test_a_member_cannot_announce_as_somebody_else(pki) -> None:
    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    identity("alice-01", ours)
    mallory_key, mallory_cert = _material(identity("mallory-01", ours))

    forged = protocol.encode(
        protocol.new_announce("alice-01", 1, 9000, "cafe"), mallory_key, mallory_cert
    )
    with pytest.raises(protocol.ProtocolError, match="does not name"):
        protocol.decode(forged, ca_public)


def test_a_certificate_from_another_authority_is_refused(pki) -> None:
    from vox_chat.discovery import protocol

    ours, theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    key, cert = _material(identity("stranger-01", theirs))

    outside = protocol.encode(
        protocol.new_announce("stranger-01", 1, 9000, "cafe"), key, cert
    )
    with pytest.raises(protocol.ProtocolError, match="not issued by this mesh"):
        protocol.decode(outside, ca_public)


def test_a_borrowed_certificate_does_not_help(pki) -> None:
    """Holding someone's certificate is useless without their private key."""
    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    alice_key, _ = _material(identity("alice-01", ours))
    _, mallory_cert = _material(identity("mallory-01", ours))

    mixed = protocol.encode(
        protocol.new_announce("mallory-01", 1, 9000, "cafe"), alice_key, mallory_cert
    )
    with pytest.raises(protocol.ProtocolError, match="invalid signature"):
        protocol.decode(mixed, ca_public)


def test_an_edited_announcement_is_refused(pki) -> None:
    import base64
    import json

    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    key, cert = _material(identity("alice-01", ours))

    packet = protocol.encode(
        protocol.new_announce("alice-01", 1, 9000, "cafe"), key, cert
    )
    envelope = json.loads(packet)
    body = json.loads(base64.b64decode(envelope["body"]))
    body["whois_port"] = 31337  # send the traffic somewhere else
    envelope["body"] = base64.b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).decode()

    with pytest.raises(protocol.ProtocolError, match="invalid signature"):
        protocol.decode(json.dumps(envelope).encode(), ca_public)


def test_an_expired_certificate_is_refused(pki) -> None:
    import datetime as dt

    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    key, cert = _material(identity("alice-01", ours))
    packet = protocol.encode(
        protocol.new_announce("alice-01", 1, 9000, "cafe"), key, cert
    )

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    with pytest.raises(protocol.ProtocolError, match="expired"):
        protocol.decode(packet, ca_public, now=later)


def test_a_replayed_announcement_is_refused(pki) -> None:
    from vox_chat.discovery import protocol

    ours, _theirs, identity = pki
    ca_public = protocol.load_ca_public_key(ours / "ca.crt")
    key, cert = _material(identity("alice-01", ours))

    packet = protocol.encode(
        protocol.new_announce("alice-01", 1, 9000, "cafe"), key, cert
    )
    guard = protocol.ReplayGuard()
    guard.check(protocol.decode(packet, ca_public))
    # A recording of a valid packet is still a valid packet; the nonce is what
    # stops it being useful.
    with pytest.raises(protocol.ProtocolError, match="replay"):
        guard.check(protocol.decode(packet, ca_public))


# ------------------------------------------------------- the sample authority


def test_the_sample_authority_ships_with_vox() -> None:
    pytest.importorskip("cryptography")
    from cryptography import x509

    directory = mesh.demo_pki_dir()
    assert (directory / "ca.crt").exists() and (directory / "ca.key").exists()

    certificate = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes())
    name = certificate.subject.rfc4514_string()
    assert "demo" in name.lower(), f"the name must say what it is: {name}"
    # It has to outlive the release, or a fresh download stops working.
    import datetime as dt

    remaining = certificate.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
    assert remaining > dt.timedelta(days=365 * 5), remaining


def test_a_fresh_install_starts_on_the_sample_authority(tmp_path: Path) -> None:
    """The point of shipping it: no provisioning, and it still works."""
    pytest.importorskip("cryptography")
    directory = tmp_path / "pki"
    assert mesh.using_demo_ca(directory) is False, "nothing there yet"

    mesh.ensure_identity("vox-aaaaaaaaaaaa", directory)
    assert mesh.using_demo_ca(directory) is True
    assert (directory / "vox-aaaaaaaaaaaa.crt").exists()


def test_two_fresh_installs_trust_each_other(tmp_path: Path) -> None:
    """Two machines, no shared setup: each accepts the other's announcement."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization

    from vox_chat.discovery import protocol

    first = mesh.ensure_identity("vox-aaaaaaaaaaaa", tmp_path / "a")
    second = mesh.ensure_identity("vox-bbbbbbbbbbbb", tmp_path / "b")

    packet = protocol.encode(
        protocol.new_announce("vox-bbbbbbbbbbbb", 1, 9000, "cafe"),
        protocol.load_signing_key(second.key_path),
        second.certificate.public_bytes(serialization.Encoding.DER),
    )
    seen = protocol.decode(packet, protocol.load_ca_public_key(first.ca_path))
    assert seen.agent_id == "vox-bbbbbbbbbbbb"


def test_the_sample_authority_is_the_default(tmp_path: Path) -> None:
    settings = MeshSettings.from_config(default_config())
    assert settings.demo_ca is True, "a fresh install must join a mesh"
    assert MeshSettings.from_config({"mesh": {"demo_ca": False}}).demo_ca is False


def test_asking_for_a_private_authority_gets_one(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    directory = tmp_path / "pki"
    mesh.ensure_identity("vox-aaaaaaaaaaaa", directory, demo=False)
    assert mesh.using_demo_ca(directory) is False
    assert (directory / "vox-aaaaaaaaaaaa.crt").exists()


def test_swapping_twice_in_one_second_keeps_both_backups(tmp_path: Path) -> None:
    """Regression: the timestamped name collided, and Windows refuses that."""
    pytest.importorskip("cryptography")
    directory = tmp_path / "pki"
    mesh.ensure_identity("vox-aaaaaaaaaaaa", directory)
    mesh.new_ca(directory, "vox-aaaaaaaaaaaa")
    mesh.use_demo_ca(directory, "vox-aaaaaaaaaaaa")
    mesh.new_ca(directory, "vox-aaaaaaaaaaaa")
    assert len(list(directory.glob("ca.crt.replaced-*"))) == 3
    assert mesh.using_demo_ca(directory) is False


def test_a_private_authority_replaces_the_sample_one(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization

    from vox_chat.discovery import protocol

    directory = tmp_path / "pki"
    mesh.ensure_identity("vox-aaaaaaaaaaaa", directory)
    assert mesh.using_demo_ca(directory) is True

    identity = mesh.new_ca(directory, "vox-aaaaaaaaaaaa")
    assert mesh.using_demo_ca(directory) is False
    assert identity.agent_id == "vox-aaaaaaaaaaaa"
    # The old authority is kept, not destroyed.
    assert list(directory.glob("ca.crt.replaced-*"))

    # And a machine still on the sample authority is now a stranger.
    outsider = mesh.ensure_identity("vox-bbbbbbbbbbbb", tmp_path / "elsewhere")
    packet = protocol.encode(
        protocol.new_announce("vox-bbbbbbbbbbbb", 1, 9000, "cafe"),
        protocol.load_signing_key(outsider.key_path),
        outsider.certificate.public_bytes(serialization.Encoding.DER),
    )
    with pytest.raises(protocol.ProtocolError, match="not issued by this mesh"):
        protocol.decode(packet, protocol.load_ca_public_key(identity.ca_path))


# ------------------------------------------------------------- the controller


class StubPeer:
    def __init__(self, agent_id, name, category, state, address, verbs, last_seen):
        self.agent_id = agent_id
        self.name = name
        self.category = category
        self.state = type("S", (), {"value": state})()
        self.address = address
        self.whois_port = 41000
        self.capabilities = {"verbs": verbs}
        self.last_seen = last_seen


class StubRegistry:
    def __init__(self, peers):
        self._peers = peers

    def snapshot(self):
        return self._peers


class StubAgent:
    """Stands in for DiscoveryAgent: no threads, no sockets."""

    def __init__(self, peers=(), **kwargs):
        self.kwargs = kwargs
        self.registry = StubRegistry(list(peers))
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> MeshController:
    """A controller whose identity and agent are both stubbed out."""
    import time

    now = time.time()
    peers = [
        StubPeer("ingestor-01", "ingestor", "SOURCE", "ACTIVE", "10.0.0.1",
                 ["ingest"], now - 2.0),
        StubPeer("legacy-01", "legacy", None, "PROBATION", "10.0.0.2", [], now - 0.5),
    ]
    monkeypatch.setattr(mesh, "ensure_identity", lambda *a, **k: object())
    monkeypatch.setattr(
        "vox_chat.discovery.agent.DiscoveryAgent",
        lambda **kwargs: StubAgent(peers, **kwargs),
    )
    return MeshController(MeshSettings.from_config(default_config()))


def test_starting_says_exactly_what_it_did(controller: MeshController) -> None:
    assert controller.online is False
    assert controller.status_line() == ""

    detail = controller.start()
    assert controller.online is True
    assert controller.category in detail
    assert "239.17.42.1:45177" in detail, "the operator is told where it announces"
    assert controller.agent.started is True

    controller.stop()
    assert controller.online is False


def test_the_peers_are_flattened_for_the_screen(controller: MeshController) -> None:
    controller.start()
    peers = controller.peers()
    assert [peer.name for peer in peers] == ["legacy", "ingestor"], "newest sighting first"
    assert peers[1].category == "SOURCE"
    assert peers[0].category == "—", "a peer that has not answered WHOIS has no category"
    assert peers[1].address == "10.0.0.1:41000"
    assert controller.active_count() == 1
    assert "1 active" in controller.status_line()


def test_a_failure_to_join_is_reported_as_a_mesh_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mesh, "ensure_identity", lambda *a, **k: object())

    class Refusing(StubAgent):
        def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(
        "vox_chat.discovery.agent.DiscoveryAgent", lambda **kwargs: Refusing(**kwargs)
    )
    controller = MeshController(MeshSettings.from_config(default_config()))
    with pytest.raises(MeshError, match="cannot join"):
        controller.start()
    assert controller.online is False


def test_the_sharing_note_names_the_authority_and_no_secret(
    controller: MeshController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh, "using_demo_ca", lambda directory=None: False)
    note = controller.sharing_note()
    assert "ca.crt" in note
    assert "own key" in note, "a second machine needs a certificate, not a secret"
    assert "psk" not in note.lower()


def test_the_sample_authority_is_never_quiet_about_itself(
    controller: MeshController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mesh, "using_demo_ca", lambda directory=None: True)
    assert controller.demo_ca is True
    note = controller.sharing_note()
    assert "DEMO CERTIFICATE" in note
    assert "new-ca" in note, "the warning names its own remedy"

    controller.start()
    assert "DEMO CERT" in controller.status_line()


def test_an_identity_is_provisioned_on_first_use(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    directory = tmp_path / "pki"
    identity = mesh.ensure_identity("vox-test-node", directory)
    assert identity.agent_id == "vox-test-node"
    assert (directory / "ca.crt").exists()
    assert (directory / "vox-test-node.crt").exists()
    # Loading again must not reissue: the identity has to be stable.
    before = (directory / "vox-test-node.crt").read_bytes()
    mesh.ensure_identity("vox-test-node", directory)
    assert (directory / "vox-test-node.crt").read_bytes() == before


def test_provisioning_can_be_refused(tmp_path: Path) -> None:
    with pytest.raises(MeshError, match="auto_provision"):
        mesh.ensure_identity("vox-test-node", tmp_path / "pki", provision=False)


# --------------------------------------------------------------------- the app


class FakeController:
    """The controller as the app sees it, without any discovery underneath."""

    def __init__(self, peers: list[PeerView] | None = None, fail: str = "",
                 demo_ca: bool = False) -> None:
        self.settings = MeshSettings.from_config(default_config())
        self.online = False
        self.demo_ca = demo_ca
        self.agent_id = "vox-test-node"
        self.category = "PROCESSOR"
        self.last_error: str | None = None
        self._peers = peers or []
        self._fail = fail
        self.stops = 0

    def start(self) -> str:
        if self._fail:
            raise MeshError(self._fail)
        self.online = True
        return f"{self.agent_id} · {self.category} · announcing on 239.17.42.1:45177"

    def stop(self) -> None:
        self.stops += 1
        self.online = False

    def peers(self) -> list[PeerView]:
        return self._peers if self.online else []

    def active_count(self) -> int:
        return len([p for p in self.peers() if p.state == "ACTIVE"])

    def status_line(self) -> str:
        return f"MESH ONLINE  ·  {len(self.peers())} agents" if self.online else ""

    def sharing_note(self) -> str:
        return "issue the other machine a certificate from ca.crt; its own key stays there"


PEERS = [
    PeerView("ingestor-01", "ingestor", "SOURCE", "ACTIVE", "10.0.0.1:41000",
             ["ingest"], 2.1),
    PeerView("watcher-09", "watcher", "OBSERVER", "SUSPECT", "10.0.0.9:41000",
             ["observe"], 91.4),
    PeerView("legacy-01", "legacy", "—", "PROBATION", "10.0.0.2:41000", [], 0.9),
]


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    app = VoxApp(loaded=load_config(workspace), workspace=workspace)
    app.mesh = FakeController(PEERS)
    return app


def text_of(screen, selector: str) -> str:
    """The text a Static is showing, whatever it was handed to render."""
    content = screen.query_one(selector)._Static__content
    return content.plain if hasattr(content, "plain") else str(content)


def transcript(app: VoxApp) -> list[str]:
    return [
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    ]


async def test_going_online_turns_the_border_red_and_says_so(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen_stack[0].has_class("mesh-online") is False

        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.mesh.online is True
        assert app.screen_stack[0].has_class("mesh-online") is True
        joined = "\n".join(transcript(app))
        assert "MESH ONLINE" in joined
        assert "239.17.42.1:45177" in joined, "it names where it announces"
        assert "ca.crt" in joined, "and what a second machine needs"


async def test_going_offline_clears_the_border(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.run_command("/mesh off")
        await pilot.pause()
        assert app.mesh.online is False
        assert app.mesh.stops == 1
        assert app.screen_stack[0].has_class("mesh-online") is False
        assert "MESH OFFLINE" in transcript(app)[-1]


async def test_the_choice_is_remembered(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert json.loads(global_config_path().read_text())["mesh"]["enabled"] is True


async def test_a_refusal_reaches_the_transcript_and_leaves_the_chat_alone(
    app: VoxApp,
) -> None:
    app.mesh = FakeController(fail="cannot join 239.17.42.1:45177: permission denied")
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh.online is False
        assert app.screen_stack[0].has_class("mesh-online") is False
        assert "permission denied" in " ".join(transcript(app))


async def test_mesh_without_an_argument_reports_the_state(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh")
        await pilot.pause()
        assert "MESH IS OFFLINE" in transcript(app)[-1]

        app.run_command("/mesh sideways")
        await pilot.pause()
        assert "USAGE" in transcript(app)[-1]


async def test_the_universe_lists_every_agent_with_its_category(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.run_command("/universe")
        await pilot.pause()
        assert isinstance(app.screen, UniverseScreen)

        rows = text_of(app.screen, "#universe-rows")
        for expected in ("ingestor", "SOURCE", "ACTIVE", "10.0.0.1:41000",
                         "watcher", "OBSERVER", "SUSPECT", "legacy", "PROBATION"):
            assert expected in rows
        assert "3 agents" in text_of(app.screen, "#universe-title")

        # Ctrl+L explains the states and the taxonomy, as on the inspect screen.
        assert app.screen.query_one("#universe-legend").has_class("visible") is False
        await pilot.press("ctrl+l")
        legend = app.screen.query_one("#universe-legend")
        assert legend.has_class("visible") is True
        assert "ORCHESTRATOR" in text_of(app.screen, "#universe-legend")

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, UniverseScreen)


async def test_the_universe_says_what_to_do_when_offline(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/universe")
        await pilot.pause()
        summary = text_of(app.screen, "#universe-summary")
        assert "F3" in summary
        assert text_of(app.screen, "#universe-rows") == ""


async def test_the_keys_are_bound_to_the_same_actions_as_the_commands(
    app: VoxApp,
) -> None:
    """Regression: neither ctrl+shift+<letter> nor ctrl+o reached the app on a
    real terminal, so the mesh is on function keys and nothing else."""
    from textual.widgets import TextArea

    bound: dict[str, list[str]] = {}
    for binding in VoxApp.BINDINGS:
        bound.setdefault(binding.action, []).append(binding.key)
    assert bound["toggle_mesh"] == ["f3"]
    assert bound["open_universe"] == ["f4"]

    # No ctrl combination at all, and nothing the input box already owns.
    mesh_keys = set(bound["toggle_mesh"]) | set(bound["open_universe"])
    assert not any(key.startswith("ctrl") for key in mesh_keys)
    input_keys = {key for binding in TextArea.BINDINGS for key in binding.key.split(",")}
    assert not mesh_keys & input_keys

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh.online is True

        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, UniverseScreen)
        # The same key closes it again.
        await pilot.press("f4")
        await pilot.pause()
        assert not isinstance(app.screen, UniverseScreen)

        await pilot.press("f3")
        await pilot.pause()
        assert app.mesh.online is False


async def test_the_header_says_when_the_certificate_is_the_sample_one(
    app: VoxApp,
) -> None:
    app.mesh = FakeController(PEERS, demo_ca=True)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert app.mesh_label() == "LOCAL"

        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh_label() == "ON-LINE (SAMPLE CERT)"
        assert "SAMPLE CERT" in str(app.query_one("#header").render())


async def test_a_private_authority_drops_the_label(app: VoxApp) -> None:
    app.mesh = FakeController(PEERS, demo_ca=False)
    async with app.run_test(size=(130, 30)) as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh_label() == "ON-LINE"
        assert "SAMPLE CERT" not in str(app.query_one("#header").render())


async def test_the_authority_can_be_swapped_both_ways(app: VoxApp) -> None:
    asked: list[bool] = []

    def replace_ca(demo: bool = False):
        asked.append(demo)
        app.mesh.demo_ca = demo
        return "sample authority" if demo else "new certificate authority"

    app.mesh = FakeController(PEERS, demo_ca=True)
    app.mesh.replace_ca = replace_ca
    async with app.run_test() as pilot:
        await pilot.pause()

        app.run_command("/mesh new-ca")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked == [False]
        assert "CERTIFICATE AUTHORITY" in " ".join(transcript(app))
        # Persisted, or the next start would move the authority straight back.
        assert json.loads(global_config_path().read_text())["mesh"]["demo_ca"] is False

        app.run_command("/mesh sample-ca")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked == [False, True]
        assert json.loads(global_config_path().read_text())["mesh"]["demo_ca"] is True

        app.run_command("/mesh sideways-ca")
        await pilot.pause()
        assert "USAGE" in transcript(app)[-1]


async def test_the_mesh_is_stopped_when_vox_shuts_down(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.shutdown()
        assert app.mesh.stops == 1
