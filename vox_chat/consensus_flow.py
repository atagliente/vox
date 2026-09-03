"""Asking the other agents, and answering when they ask.

The three longest methods in ``VoxApp`` were here: starting a round, folding
what came back into the next request, and serving a question that arrived
from the mesh. None of them is drawing, and all three of them were reaching
across the whole class to do their work.

Same arrangement as generation.py: the controller holds the application, and
the ``@work`` decorators stay behind on ``VoxApp`` because scheduling a thread
is Textual's business. What is here is what happens once the thread is running
and what to do with the answers.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import consensus as cns
from . import reputation
from .logging_setup import get_logger
from .models import Message

if TYPE_CHECKING:
    from .app import VoxApp

log = get_logger("consensus")


class ConsensusController:
    """A round of CONSENSUS, from the marked span to the reconciled answer."""

    def __init__(self, app: VoxApp) -> None:
        self.app = app

    def consensus_settings(self) -> dict:
        app = self.app
        section = app.config.get("consensus", {})
        return dict(section) if isinstance(section, dict) else {}

    def consensus_refusal(self) -> str | None:
        """Why this machine must not distribute the marked text, if it must not."""
        app = self.app
        settings = self.consensus_settings()
        if not settings.get("enabled", True):
            return "CONSENSUS IS OFF - /consensus on"
        if not app.mesh.online:
            return "CONSENSUS NEEDS THE MESH - F3, OR /mesh on"
        if app.mesh.demo_ca and not settings.get("allow_sample_ca", True):
            return (
                "REFUSING TO DISTRIBUTE ON THE SAMPLE CERTIFICATE - anyone on "
                "this network holding VOX could read it. /mesh new-ca first, "
                "or set consensus.allow_sample_ca"
            )
        return None

    def start_consensus(self, question: str) -> None:
        app = self.app
        settings = self.consensus_settings()
        limit = int(settings.get("max_question_chars", 4000))
        if len(question) > limit:
            app.write_error(
                f"THE MARKED TEXT IS {len(question)} CHARACTERS, OVER THE "
                f"{limit} ALLOWED"
            )
            return
        peers = app.mesh.processors(
            str(settings.get("verb", "infer")), int(settings.get("max_peers", 5))
        )
        if not peers:
            app.write_system(
                "NO AGENT TO ASK - answering locally. F4 shows who is out there"
            )
            app.start_generation()
            return

        named = ", ".join(peer.name or peer.agent_id for peer in peers)
        if app.mesh.demo_ca:
            # Said every round, not once: the certificate is public, so the
            # peer list is not the same thing as who can read this.
            app.write_error(
                "SAMPLE CERTIFICATE - this text is readable by anyone on the "
                "segment running VOX. /mesh new-ca for a mesh only yours"
            )
        app.write_system(
            f"CONSENSUS - sending {len(question)} characters to {len(peers)} "
            f"agents: {named}"
        )
        app._consensus_answers = []
        app._consensus_running = True
        app._consensus_round += 1
        app.consensus_log.begin(question, time.time(), app.session.id)
        if app._round_screen is not None:
            app._round_screen.refresh_view()
        app.show_thinking(f"ASKING {len(peers)} AGENTS")
        app.refresh_status()
        app.ask_the_mesh(question, settings)

    def consensus_event(self, agent: str, kind: str, text: str, ts: float) -> None:
        """One fragment from one peer, live."""
        app = self.app
        app.consensus_log.add(agent, kind, text, ts)
        if app._round_screen is not None:
            app._round_screen.refresh_view()
        if app.panel_mode == "consensus":
            app.refresh_panel()

    def consensus_answered(
        self, question: str, answers: list, settings: dict, round_number: int = 0
    ) -> None:
        app = self.app
        if round_number and round_number != app._consensus_round:
            # The operator abandoned this round, or started another one. Its
            # answers belong to a question that is no longer on screen.
            log.info("dropping answers from round %s", round_number)
            return
        app._consensus_running = False
        app._consensus_answers = answers
        app.clear_thinking()
        for answer in answers:
            label = answer.name or answer.agent_id
            if answer.ok:
                body = f"{answer.answer.strip()}\n\n[{answer.model or 'model unknown'} · {answer.elapsed:.1f}s]"
            else:
                body = f"no answer: {answer.error or 'silent'}"
            message = Message(role="peer", content=body, name=label)
            app.session.messages.append(message)
            app.write_message(message)
        app.remember_round(question, answers)
        app.write_system(f"CONSENSUS - {cns.describe(answers)}")
        if app.config.get("ui", {}).get("code_panel", True):
            app.panel_mode = "consensus"
            app.query_one("#side-panel").set_class(True, "visible")
            app.refresh_panel()

        quorum = int(settings.get("quorum", cns.DEFAULT_QUORUM))
        kind, winner, clusters = cns.verdict(answers, quorum=quorum)

        # Fold the round into the record, and name whoever stood alone. Marked
        # rather than hidden: an outlier is sometimes the only one who read
        # the question properly.
        if settings.get("weigh_by_record", True):
            alone = app.reputation.note_round(answers, clusters)
            app.save_reputation()
            if alone:
                names = ", ".join(
                    next(
                        (a.name or a.agent_id for a in answers if a.agent_id == one),
                        one,
                    )
                    for one in alone
                )
                app.write_system(
                    f"CONSENSUS - {names} said something nobody else did. "
                    "Shown above, and counted: VOX has no way to tell which "
                    "of them is right."
                )
            weighted, weighted_winner, margin = reputation.weighted_verdict(
                answers, clusters, app.reputation, quorum
            )
            if weighted != kind or weighted_winner != winner:
                # The weights only ever break a tie, so a disagreement here is
                # worth a line rather than a silent override.
                app.write_system(
                    f"CONSENSUS - the record breaks a tie (margin {margin}); "
                    f"taking the {weighted} route"
                )
                kind, winner = weighted, weighted_winner
        if kind == "vote":
            agreed = len(clusters[0][1])
            usable = sum(len(members) for _, members in clusters)
            app.write_system(
                f"CONSENSUS - {agreed} of {usable} agents gave the same answer"
            )
            reply = Message(role="assistant", content=winner or "")
            app.session.messages.append(reply)
            app.write_message(reply)
            app.dirty = True
            app.refresh_status()
            return

        if not any(answer.ok for answer in answers):
            app.write_error("NO AGENT ANSWERED - answering locally")
        else:
            app.write_system("CONSENSUS - the agents differ; reconciling locally")
        app.consensus_prompt = cns.synthesis_prompt(question, answers)
        app.start_generation()

    def install_answer_hook(self) -> None:
        """Let peers ask this node questions, if the operator allows it."""
        app = self.app
        settings = self.consensus_settings()
        allowed = settings.get("enabled", True) and settings.get(
            "answer_requests", True
        )
        app.mesh.set_answer_hook(self.answer_for_peer if allowed else None)

    def answer_for_peer(
        self, caller: str, question: str, emit=None, conversation: str = ""
    ) -> dict:
        """Run the local model for another agent. Called on a mesh thread.

        Nothing from this machine's conversation, role or workspace goes into
        it: a peer's answer must not leak the answerer's context either.

        ``emit(kind, text)`` sends each fragment to the asker as it is
        produced, reasoning included, so a slow answer is visible rather than
        silent.
        """
        app = self.app
        settings = self.consensus_settings()
        if not settings.get("enabled", True) or not settings.get(
            "answer_requests", True
        ):
            raise RuntimeError("this node does not answer questions")
        if app.mesh.demo_ca and not settings.get("allow_sample_ca", True):
            raise RuntimeError("this node does not answer on the sample certificate")
        limit = int(settings.get("max_question_chars", 4000))
        if len(question) > limit:
            raise RuntimeError(f"question over {limit} characters")
        if app.client is None:
            raise RuntimeError("no provider configured")

        model = str(app.config.get("active_model", ""))
        app.call_from_thread(
            app.write_system,
            f"ASKED BY {caller} - {len(question)} characters, answering with "
            f"{model}" + (f" · conversation {conversation}" if conversation else ""),
        )
        app.call_from_thread(self.answering_started, caller, question, conversation)
        started = time.monotonic()
        pieces: list[str] = []
        for event in app.client.stream_chat(
            messages=[
                Message(role="system", content=cns.ANSWER_SYSTEM_PROMPT),
                Message(role="user", content=question),
            ],
            model=model,
            temperature=float(app.config.get("generation", {}).get("temperature", 0.2)),
            max_tokens=int(settings.get("answer_max_tokens", 512)),
        ):
            if event.type == "text":
                pieces.append(event.text)
                if emit is not None:
                    emit("text", event.text)
                app.call_from_thread(self.answering_event, "text", event.text)
            elif event.type == "reasoning":
                # The asker sees the thinking too, marked as thinking — and so
                # does whoever is sitting at this machine.
                if emit is not None:
                    emit("reasoning", event.text)
                app.call_from_thread(self.answering_event, "reasoning", event.text)
        answer = "".join(pieces).strip()
        elapsed = time.monotonic() - started
        app.call_from_thread(self.answering_finished, caller, answer, elapsed)
        return {"answer": answer, "model": model}

    def answering_started(self, caller: str, question: str, conversation: str) -> None:
        app = self.app
        if app._consensus_running:
            # We are mid-round ourselves; that log is the one on screen and
            # overwriting it would lose what we are waiting for.
            return
        app.consensus_log.begin(question, time.time(), conversation, asked_by=caller)
        if app._round_screen is not None:
            app._round_screen.refresh_view()

    def answering_event(self, kind: str, text: str) -> None:
        app = self.app
        if app._consensus_running:
            return
        app.consensus_log.add(app.mesh.agent_id, kind, text, time.time())
        if app._round_screen is not None:
            app._round_screen.refresh_view()

    def answering_finished(self, caller: str, answer: str, elapsed: float) -> None:
        app = self.app
        app.write_system(
            f"ANSWERED {caller} - {len(answer)} characters in {elapsed:.1f}s"
        )
        # What we told them, kept out of the session: it is their conversation,
        # not ours, and it must never reach our own model as context.
        if answer:
            app.write_message(
                Message(role="peer", content=answer.strip(), name=f"to {caller}")
            )
