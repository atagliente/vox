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
        self.conversations: list[str] = []
        self.hook = None

    def processors(self, verb="infer", limit=5):
        return self._peers[:limit] if self.online else []

    def ask_peers(self, question, verb="infer", limit=5, timeout=90.0,
                  on_event=None, conversation=""):
        self.asked.append(question)
        self.conversations.append(conversation)
        # A real peer streams what it writes before the answer lands.
        if on_event is not None:
            for reply in self._answers:
                if reply.ok:
                    on_event(reply.name, "text", reply.answer, 1_000_000.0)
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


def text_of(screen, selector: str) -> str:
    content = screen.query_one(selector)._Static__content
    return content.plain if hasattr(content, "plain") else str(content)


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


async def test_the_sample_certificate_warns_every_round_but_sends(app: VoxApp) -> None:
    """It works on the shipped authority, and says what that costs each time."""
    app.mesh = FakeMesh(answers=[answer("alpha", "Yes"), answer("beta", "Yes")],
                        demo_ca=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.mesh.asked == ["is it safe?"]
        text = transcript(app)
        assert "SAMPLE CERTIFICATE" in text
        assert "readable by anyone" in text
        assert "/mesh new-ca" in text

        # Not once per session: every round says it again.
        app.input_area.insert("[CNS]and this one?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert transcript(app).count("SAMPLE CERTIFICATE") == 2


async def test_the_sample_certificate_can_still_be_refused(app: VoxApp) -> None:
    app.mesh = FakeMesh(demo_ca=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.config.setdefault("consensus", {})["allow_sample_ca"] = False

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


async def test_answering_on_the_sample_certificate_can_be_refused(
    app: VoxApp,
) -> None:
    """Allowed by default, so a fresh install answers; refusable in one line."""
    app.mesh = FakeMesh(demo_ca=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.config.setdefault("consensus", {})["allow_sample_ca"] = False
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


# ------------------------------------------------------------- the live round


def test_the_log_joins_a_stream_of_tokens_into_readable_lines() -> None:
    log = cns.RoundLog()
    log.begin("is it safe?", 1_000_000.0)
    log.add("node-b", "reasoning", "the question ", 1_000_000.0)
    log.add("node-b", "reasoning", "is about buffers", 1_000_000.5)
    log.add("node-c", "text", "Yes", 1_000_001.0)
    log.add("node-b", "text", "No", 1_000_002.0)

    # Consecutive fragments from one agent join; a different agent starts a row.
    assert [(f.agent, f.kind, f.text) for f in log.fragments] == [
        ("node-b", "reasoning", "the question is about buffers"),
        ("node-c", "text", "Yes"),
        ("node-b", "text", "No"),
    ]
    assert log.agents() == ["node-b", "node-c"]


def test_the_log_is_timestamped_per_agent() -> None:
    log = cns.RoundLog()
    log.add("node-b", "text", "hello", 1_000_000.0)
    rows = log.lines(width=60)
    stamp, agent, kind, body = rows[0]
    assert len(stamp) == 8 and stamp.count(":") == 2
    assert agent == "node-b"
    assert kind == "text" and body == "hello"
    assert f"{stamp} node-b: hello" == log.as_text(60)


def test_the_log_wraps_without_repeating_the_stamp() -> None:
    log = cns.RoundLog()
    log.add("node-b", "text", "word " * 40, 1_000_000.0)
    rows = log.lines(width=40)
    assert len(rows) > 1
    assert rows[0][0] and not rows[1][0], "the timestamp belongs to the first row"


def test_the_log_forgets_the_oldest_rather_than_growing_for_ever() -> None:
    log = cns.RoundLog(limit=10)
    for index in range(50):
        log.add(f"agent-{index}", "text", str(index), 1_000_000.0 + index)
    assert len(log.fragments) == 10
    assert log.fragments[-1].text == "49"


def test_an_empty_fragment_is_not_a_line() -> None:
    log = cns.RoundLog()
    log.add("node-b", "text", "", 1_000_000.0)
    log.add("node-b", "text", "   ", 1_000_000.0)
    assert log.lines() == []


async def test_the_round_view_fills_while_the_agents_write(app: VoxApp) -> None:
    from vox_chat.ui.round_screen import RoundScreen

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.run_command("/round")
        await pilot.pause()
        assert isinstance(app.screen, RoundScreen)

        app.consensus_log.begin("is it safe?", 1_000_000.0)
        app.consensus_event("node-b", "reasoning", "weighing it up", 1_000_000.0)
        app.consensus_event("node-c", "text", "Yes, with care.", 1_000_001.0)
        await pilot.pause()

        rendered = text_of(app.screen, "#round-lines")
        assert "weighing it up" in rendered
        assert "Yes, with care." in rendered
        assert "node-b" in rendered and "node-c" in rendered
        assert "is it safe?" in text_of(app.screen, "#round-question")

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RoundScreen)


async def test_a_round_starts_a_fresh_log(app: VoxApp) -> None:
    app.mesh = FakeMesh(answers=[answer("alpha", "Yes"), answer("beta", "Yes")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.consensus_event("old-agent", "text", "from a previous round", 1.0)
        assert app.consensus_log.fragments

        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.consensus_log.question == "is it safe?"
        assert all(f.agent != "old-agent" for f in app.consensus_log.fragments)


# ------------------------------------------------------- the conversation id


async def test_the_round_carries_the_conversation_id(app: VoxApp) -> None:
    """One exchange, two machines, one id to tie them together."""
    app.mesh = FakeMesh(answers=[answer("alpha", "Yes"), answer("beta", "Yes")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]is it safe?[/CNS]")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.mesh.conversations == [app.session.id]
        assert app.consensus_log.conversation_id == app.session.id
        # And the report of that session names it too.
        assert app.build_report().conversation_id == app.session.id


async def test_the_round_view_shows_the_conversation(app: VoxApp) -> None:
    from vox_chat.ui.round_screen import RoundScreen

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.consensus_log.begin("is it safe?", 1_000_000.0, "abc123def456")
        app.run_command("/round")
        await pilot.pause()
        assert isinstance(app.screen, RoundScreen)
        assert "abc123def456" in text_of(app.screen, "#round-question")


async def test_answering_names_the_conversation_it_belongs_to(app: VoxApp) -> None:
    import asyncio

    async with app.run_test() as pilot:
        await pilot.pause()

        def answer_off_thread() -> None:
            # The hook runs on a mesh thread, which is what makes its
            # call_from_thread work. No provider is reachable here, so it fails
            # after writing the line that matters: the request is recorded
            # before any model is called.
            try:
                app.answer_for_peer("peer-01", "is it safe?", conversation="abc123")
            except Exception:
                pass

        await asyncio.to_thread(answer_off_thread)
        await pilot.pause()
        assert "ASKED BY peer-01" in transcript(app)
        assert "conversation abc123" in transcript(app)


# ------------------------------------------------- the round, seen from the far side


class Streaming:
    """A client whose stream_chat produces a couple of fragments."""

    def __init__(self, events=(("reasoning", "weighing it"), ("text", "Yes."))) -> None:
        self._events = events

    def stream_chat(self, **kwargs):
        from vox_chat.llm_client import StreamEvent

        for kind, text in self._events:
            yield StreamEvent(type=kind, text=text)

    def close(self):
        pass


async def test_the_answering_machine_sees_the_round_too(app: VoxApp) -> None:
    """The other side of the exchange is a round as well, and shows as one."""
    import asyncio

    from vox_chat.ui.round_screen import RoundScreen

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.client = Streaming()
        app.run_command("/round")
        await pilot.pause()
        assert isinstance(app.screen, RoundScreen)

        await asyncio.to_thread(
            app.answer_for_peer, "asker-01", "is it safe?", None, "conv-42"
        )
        await pilot.pause()

        # The question, who asked it, and which conversation it belongs to.
        header = text_of(app.screen, "#round-question")
        assert "asked by asker-01" in header
        assert "is it safe?" in header
        assert "conv-42" in header
        assert "ANSWERING" in text_of(app.screen, "#round-title")

        # And the reasoning, not only the answer.
        body = text_of(app.screen, "#round-lines")
        assert "weighing it" in body
        assert "Yes." in body


async def test_the_answering_machine_shows_what_it_replied(app: VoxApp) -> None:
    import asyncio

    async with app.run_test() as pilot:
        await pilot.pause()
        app.client = Streaming()
        await asyncio.to_thread(
            app.answer_for_peer, "asker-01", "is it safe?", None, "conv-42"
        )
        await pilot.pause()

        text = transcript(app)
        assert "ASKED BY asker-01" in text
        assert "conversation conv-42" in text
        assert "ANSWERED asker-01" in text
        assert "Yes." in text

        # Their conversation, not ours: it never enters our session or the
        # context our own model is given.
        assert all(m.role != "peer" for m in app.session.messages)
        assert "Yes." not in "".join(
            m.content for m in app.build_request_messages()
        )


async def test_answering_does_not_overwrite_our_own_round(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.client = Streaming()
        app.consensus_log.begin("our own question", 1_000_000.0, "ours")
        app._consensus_running = True

        app.answering_started("asker-01", "their question", "theirs")
        app.answering_event("text", "their answer")
        await pilot.pause()

        assert app.consensus_log.question == "our own question"
        assert app.consensus_log.asked_by == ""
        assert all(f.text != "their answer" for f in app.consensus_log.fragments)


# ------------------------------------------------------- one round at a time


class SlowMesh(FakeMesh):
    """A mesh whose peers take a moment, like real ones."""

    def __init__(self, *args, delay: float = 0.4, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.delay = delay

    def ask_peers(self, question, verb="infer", limit=5, timeout=90.0,
                  on_event=None, conversation=""):
        import time

        time.sleep(self.delay)
        return super().ask_peers(question, verb, limit, timeout, on_event,
                                 conversation)


async def test_a_message_sent_mid_round_is_refused(app: VoxApp) -> None:
    """Regression: the round answered the question typed while waiting.

    While peers were out the app was not "generating", so a second message was
    accepted, answered at once, and then answered again with the first
    question's peer replies attached.
    """
    app.mesh = SlowMesh(answers=[answer("alpha", "It depends.")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]FIRST[/CNS]")
        app.action_send()
        await pilot.pause()
        assert app._consensus_running is True

        app.input_area.insert("SECOND, typed while waiting")
        app.action_send()
        await pilot.pause()

        assert "STILL ASKING THE MESH" in transcript(app)
        assert app.input_area.text, "the second message is kept, not swallowed"
        assert [m.content for m in app.session.messages if m.role == "user"] == ["FIRST"]

        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.mesh.asked == ["FIRST"]
        assert app._consensus_prompt, "the round reconciles the question it asked"
        assert "FIRST" in app._consensus_prompt


async def test_a_round_can_be_abandoned(app: VoxApp) -> None:
    app.mesh = SlowMesh(answers=[answer("alpha", "It depends.")])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]FIRST[/CNS]")
        app.action_send()
        await pilot.pause()

        app.action_stop()
        await pilot.pause()
        assert app._consensus_running is False
        assert "CONSENSUS ABANDONED" in transcript(app)

        # The peers still answer; those answers belong to nobody now.
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app._consensus_prompt
        assert all(m.role != "peer" for m in app.session.messages)

        # And the next question goes through normally.
        app.input_area.insert("SECOND")
        app.action_send()
        await pilot.pause()
        assert [m.content for m in app.session.messages if m.role == "user"] == [
            "FIRST", "SECOND"
        ]


async def test_the_status_bar_says_a_round_is_out(app: VoxApp) -> None:
    app.mesh = SlowMesh(answers=[answer("alpha", "It depends.")])
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        app.input_area.insert("[CNS]FIRST[/CNS]")
        app.action_send()
        await pilot.pause()
        assert "ASKING THE MESH" in str(app.query_one("#status").render())
        await app.workers.wait_for_complete()
