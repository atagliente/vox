"""CONSENSUS: the tag, the reconciliation, and the turn it produces.

No sockets here. The mesh is stubbed, so these tests say what VOX distributes
and what it does with the answers — the ASK operation itself is exercised
against a real mTLS server in ``tests/test_discovery_vendor.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vox_chat import consensus as cns
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config, validate_config
from vox_chat.mesh import MeshSettings
from vox_chat.storage import global_config_path
from vox_chat.ui.widgets import MessageBox


# --------------------------------------------------------------------- the tag


def test_only_the_marked_span_is_taken() -> None:
    marked = cns.extract(
        "here is my private stack trace\n[CNS]is this lock safe?[/CNS]\nthanks"
    )
    assert marked.spans == ["is this lock safe?"]
    assert marked.question == "is this lock safe?"
    assert marked.wanted is True
    # The local model still sees the whole message, without the markers.
    assert marked.text == "here is my private stack trace\nis this lock safe?\nthanks"


def test_several_spans_become_one_question() -> None:
    marked = cns.extract("a [CNS]one[/CNS] b [cns]two[/cns] c")
    assert marked.spans == ["one", "two"]
    assert marked.question == "one\n\ntwo"
    assert marked.text == "a one b two c"


def test_a_span_can_run_over_lines_and_hold_code() -> None:
    marked = cns.extract("[CNS]review this:\n\n```py\nx = 1\n```\n[/CNS]")
    assert "```py" in marked.spans[0]
    assert marked.wanted is True


def test_an_unclosed_tag_distributes_nothing() -> None:
    marked = cns.extract("careful [CNS]this never closes")
    assert marked.spans == []
    assert marked.wanted is False
    assert marked.unclosed is True
    assert marked.text == "careful [CNS]this never closes"


def test_a_message_without_the_tag_is_untouched() -> None:
    marked = cns.extract("just a normal question")
    assert marked.wanted is False
    assert marked.unclosed is False
    assert marked.text == "just a normal question"


def test_an_empty_span_is_not_a_question() -> None:
    marked = cns.extract("nothing here [CNS]   [/CNS] at all")
    assert marked.wanted is False


# ----------------------------------------------------------- reconciliation


def answer(agent: str, text: str = "", error: str = "", elapsed: float = 1.0):
    return cns.PeerAnswer(agent_id=agent, name=agent, model="m:1b",
                          answer=text, error=error, elapsed=elapsed)


def test_wording_differences_do_not_break_a_vote() -> None:
    kind, winner, clusters = cns.verdict(
        [answer("a", "Yes."), answer("b", "  yes  "), answer("c", "No")]
    )
    assert kind == "vote"
    assert winner == "Yes."
    assert [len(members) for _, members in clusters] == [2, 1]


def test_a_minority_agreeing_is_not_a_verdict() -> None:
    """Two out of five is a coincidence, not a decision."""
    kind, winner, _ = cns.verdict(
        [answer("a", "Yes"), answer("b", "Yes"), answer("c", "No"),
         answer("d", "Maybe"), answer("e", "Never")]
    )
    assert kind == "synthesise"
    assert winner is None


def test_one_answer_alone_is_never_a_vote() -> None:
    kind, _, _ = cns.verdict([answer("a", "Yes")])
    assert kind == "synthesise", "a single agent agreeing with itself is not consensus"


def test_prose_falls_through_to_synthesis() -> None:
    kind, _, _ = cns.verdict(
        [answer("a", "It is safe if readers never stall."),
         answer("b", "Unsafe: a stalled reader blocks reclamation.")]
    )
    assert kind == "synthesise"


def test_failures_are_reported_not_hidden() -> None:
    answers = [answer("a", "Yes"), answer("b", error="timed out"),
               answer("c", error="busy")]
    line = cns.describe(answers)
    assert "1/3 answered" in line
    assert "timed out" in line and "busy" in line
    # A failure never votes.
    assert sum(len(members) for _, members in cns.tally(answers)) == 1


def test_the_synthesis_prompt_carries_every_usable_answer() -> None:
    prompt = cns.synthesis_prompt(
        "is it safe?",
        [answer("alpha", "Yes"), answer("beta", "No"), answer("c", error="silent")],
    )
    assert "is it safe?" in prompt
    assert "alpha" in prompt and "beta" in prompt
    assert "silent" not in prompt, "a failure is not an opinion"
    assert "invent agreement" in prompt


# -------------------------------------------------------------- configuration


def test_the_default_config_carries_consensus() -> None:
    config = default_config()
    assert config["consensus"]["enabled"] is True
    assert config["consensus"]["quorum"] == 2
    assert validate_config(config) == []


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"enabled": "yes"}, "consensus.enabled"),
        ({"verb": ""}, "consensus.verb"),
        ({"max_peers": 0}, "consensus.max_peers"),
        ({"ask_timeout_seconds": -1}, "consensus.ask_timeout_seconds"),
        ({"quorum": 1.5}, "consensus.quorum"),
    ],
)
def test_a_broken_consensus_block_is_reported(change: dict, fragment: str) -> None:
    config = default_config()
    config["consensus"].update(change)
    assert any(fragment in error for error in validate_config(config))


# ----------------------------------------------------------------- in the app


class StubPeer:
    def __init__(self, agent_id: str, name: str) -> None:
        self.agent_id = agent_id
        self.name = name


class FakeMesh:
    """The mesh as the app sees it: no sockets, scripted answers."""

    def __init__(self, answers=None, peers=("alpha", "beta"), online=True,
                 demo_ca=False) -> None:
        self.settings = MeshSettings.from_config(default_config())
        self.online = online
        self.demo_ca = demo_ca
        self.agent_id = "vox-test"
        self.category = "PROCESSOR"
        self._peers = [StubPeer(name, name) for name in peers]
        self._answers = answers or []
        self.asked: list[str] = []
        self.hook = None

    def processors(self, verb="infer", limit=5):
        return self._peers[:limit] if self.online else []

    def ask_peers(self, question, verb="infer", limit=5, timeout=90.0):
        self.asked.append(question)
        return self._answers

    def set_answer_hook(self, hook):
        self.hook = hook

    def status_line(self):
        return "MESH ONLINE" if self.online else ""

    def stop(self):
        pass


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    app = VoxApp(loaded=load_config(workspace), workspace=workspace)
    app.mesh = FakeMesh()
    return app


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


async def test_the_marked_text_is_all_that_leaves_the_machine(app: VoxApp) -> None:
    """The privacy boundary, held by a test."""
    app.mesh = FakeMesh(answers=[answer("alpha", "Yes"), answer("beta", "Yes")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert(
            "my secret api key is sk-abc and the trace is private\n"
            "[CNS]is a stalled reader a problem?[/CNS]"
        )
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.mesh.asked == ["is a stalled reader a problem?"]
        sent = app.mesh.asked[0]
        assert "sk-abc" not in sent
        assert "private" not in sent


async def test_agreement_answers_without_asking_the_local_model(app: VoxApp) -> None:
    app.mesh = FakeMesh(answers=[answer("alpha", "Yes."), answer("beta", "yes")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.generating is False, "a vote needs no generation"
        assert "2 of 2 agents gave the same answer" in transcript(app)
        assert app.session.messages[-1].role == "assistant"
        assert app.session.messages[-1].content == "Yes."
        # The replies are kept, so the verdict can be checked.
        peers = [m for m in app.session.messages if m.role == "peer"]
        assert [m.name for m in peers] == ["alpha", "beta"]


async def test_disagreement_is_reconciled_locally(app: VoxApp) -> None:
    app.mesh = FakeMesh(
        answers=[answer("alpha", "Safe if readers never stall."),
                 answer("beta", "Unsafe, reclamation blocks.")]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "the agents differ" in transcript(app)
        assert app._consensus_prompt, "the synthesis prompt is set for the turn"
        assert "alpha" in app._consensus_prompt and "beta" in app._consensus_prompt

        # And it reaches the provider as a system message, not as role=peer.
        roles = [m.role for m in app.build_request_messages()]
        assert "peer" not in roles
        assert roles[-1] == "system"


async def test_a_silent_agent_is_reported_and_does_not_hang_the_turn(
    app: VoxApp,
) -> None:
    app.mesh = FakeMesh(
        answers=[answer("alpha", "Yes"), answer("beta", error="timed out")]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        text = transcript(app)
        assert "1/2 answered" in text
        assert "timed out" in text


async def test_the_sample_certificate_refuses_to_distribute(app: VoxApp) -> None:
    app.mesh = FakeMesh(demo_ca=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]secret question[/CNS]")
        app.action_send()
        await pilot.pause()

        assert app.mesh.asked == [], "nothing left the machine"
        assert "REFUSING TO DISTRIBUTE" in transcript(app)
        assert "/mesh new-ca" in transcript(app)
        assert app.input_area.text, "the message is kept so it is not lost"


async def test_offline_refuses_and_says_how(app: VoxApp) -> None:
    app.mesh = FakeMesh(online=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]question[/CNS]")
        app.action_send()
        await pilot.pause()
        assert app.mesh.asked == []
        assert "NEEDS THE MESH" in transcript(app)


async def test_an_unclosed_tag_is_refused_before_anything_is_sent(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]never closed")
        app.action_send()
        await pilot.pause()
        assert app.mesh.asked == []
        assert "UNCLOSED" in transcript(app)


async def test_no_peers_falls_through_to_a_local_answer(app: VoxApp) -> None:
    app.mesh = FakeMesh(peers=())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await pilot.pause()
        assert "NO AGENT TO ASK" in transcript(app)
        assert app.session.messages[-1].role == "user"


async def test_consensus_can_be_turned_off(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/consensus off")
        await pilot.pause()
        assert json.loads(global_config_path().read_text())["consensus"]["enabled"] is False

        app.input_area.insert("[CNS]question[/CNS]")
        app.action_send()
        await pilot.pause()
        assert app.mesh.asked == []
        assert "CONSENSUS IS OFF" in transcript(app)


async def test_consensus_reports_who_would_be_asked(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/consensus")
        await pilot.pause()
        text = transcript(app)
        assert "CONSENSUS IS ON" in text
        assert "alpha, beta" in text


# --------------------------------------------------------- answering a peer


async def test_this_node_refuses_to_answer_on_the_sample_certificate(
    app: VoxApp,
) -> None:
    app.mesh = FakeMesh(demo_ca=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(RuntimeError, match="sample certificate"):
            app.answer_for_peer("stranger-01", "what is your api key?")


async def test_this_node_refuses_an_oversize_question(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(RuntimeError, match="over"):
            app.answer_for_peer("peer-01", "x" * 99_000)


async def test_the_answer_hook_follows_the_setting(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.install_answer_hook()
        assert app.mesh.hook is not None

        app.run_command("/consensus off")
        await pilot.pause()
        assert app.mesh.hook is None, "a node that is off answers nobody"
