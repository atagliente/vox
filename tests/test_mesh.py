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


# -------------------------------------------------------------------- the key


def test_the_key_comes_from_the_environment_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mesh.PSK_ENV, "a-key-of-at-least-16-bytes")
    assert mesh.ensure_psk() == b"a-key-of-at-least-16-bytes"
    assert not mesh.psk_path().exists(), "nothing is written when the env supplies it"


def test_a_short_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mesh.PSK_ENV, "tooshort")
    with pytest.raises(MeshError, match="at least"):
        mesh.ensure_psk()


def test_a_generated_key_is_written_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mesh.PSK_ENV, raising=False)
    first = mesh.ensure_psk()
    assert len(first) >= mesh.MIN_PSK_BYTES
    assert mesh.psk_path().exists()
    assert mesh.ensure_psk() == first, "the key is stable across restarts"


def test_without_a_key_and_without_permission_to_make_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(mesh.PSK_ENV, raising=False)
    with pytest.raises(MeshError, match="no pre-shared key"):
        mesh.ensure_psk(generate=False)


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
    monkeypatch.setattr(mesh, "ensure_psk", lambda **k: b"a-key-of-at-least-16-bytes")
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
    monkeypatch.setattr(mesh, "ensure_psk", lambda **k: b"a-key-of-at-least-16-bytes")

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


def test_the_sharing_note_names_both_files(controller: MeshController) -> None:
    note = controller.sharing_note()
    assert "ca.crt" in note and "mesh-psk" in note


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

    def __init__(self, peers: list[PeerView] | None = None, fail: str = "") -> None:
        self.settings = MeshSettings.from_config(default_config())
        self.online = False
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
        return "copy ca.crt and mesh-psk to the other machine"


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
        assert "Ctrl+Shift+O" in summary
        assert text_of(app.screen, "#universe-rows") == ""


async def test_the_keys_are_bound_to_the_same_actions_as_the_commands(
    app: VoxApp,
) -> None:
    """The terminal may swallow ctrl+shift+<letter>; the wiring is still ours."""
    bound = {binding.key: binding.action for binding in VoxApp.BINDINGS}
    assert bound["ctrl+shift+o"] == "toggle_mesh"
    assert bound["ctrl+shift+u"] == "open_universe"

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+shift+o")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh.online is True

        await pilot.press("ctrl+shift+u")
        await pilot.pause()
        assert isinstance(app.screen, UniverseScreen)
        # The same key closes it again.
        await pilot.press("ctrl+shift+u")
        await pilot.pause()
        assert not isinstance(app.screen, UniverseScreen)


async def test_the_mesh_is_stopped_when_vox_shuts_down(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        app.run_command("/mesh on")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.shutdown()
        assert app.mesh.stops == 1
