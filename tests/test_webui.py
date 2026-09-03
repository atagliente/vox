"""`vox ui`, over real HTTP.

The server is started for real and talked to with `urllib`, the way
`test_mcp_server.py` starts `vox mcp-serve` as a real subprocess. A fake
transport would prove the host works and say nothing about the two things
that actually go wrong in a server: framing and who is allowed to ask.

The provider is faked at `LLMClient`, so no test here reaches a network.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vox_chat.agent import AgentEvent
from vox_chat.config import default_config, load_config
from vox_chat.models import Message
from vox_chat.storage import global_config_path
from vox_chat.webui import UiServer, WebHost
from vox_chat.webui.page import PAGE

# Port 0: the operating system picks a free one. A fixed number here is a
# race as soon as the suite runs with -n auto, which it does by default.
FREE = 0


@pytest.fixture
def host(workspace: Path) -> WebHost:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    made = WebHost(load_config(workspace), workspace)
    yield made
    made.close()


@pytest.fixture
def server(host: WebHost):
    running = UiServer(host, FREE)
    running.start()
    yield running
    running.stop()


def get(server: UiServer, path: str) -> bytes:
    with urllib.request.urlopen(server.url + path, timeout=10) as reply:
        return reply.read()


def post(server: UiServer, path: str, payload: dict, origin: str | None = None):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        server.url + path, data=json.dumps(payload).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=10) as reply:
        return reply.status, json.loads(reply.read() or b"{}")


def state(server: UiServer) -> dict:
    return json.loads(get(server, "/api/state"))


def frames(server: UiServer, count: int, timeout: float = 5.0) -> list[dict]:
    """Read up to ``count`` SSE frames, then stop."""
    collected: list[dict] = []
    done = threading.Event()

    def read() -> None:
        try:
            with urllib.request.urlopen(server.url + "/events", timeout=timeout) as s:
                for line in s:
                    text = line.decode().strip()
                    if text.startswith("data: "):
                        collected.append(json.loads(text[6:]))
                        if len(collected) >= count:
                            break
        except OSError:  # pragma: no cover - the server going away mid-read
            pass
        done.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return collected, done


# ------------------------------------------------------------------ the page


def test_the_page_fetches_nothing_from_anywhere(server: UiServer) -> None:
    """The promise is that it works offline, so it has to be checkable."""
    page = get(server, "/").decode()
    assert "<title>VOX</title>" in page
    for marker in ('src="http', "src='http", 'href="http', "href='http", "@import"):
        assert marker not in page, f"the page reaches out: {marker}"


def test_the_page_declares_a_policy_that_enforces_that(server: UiServer) -> None:
    # A habit anybody can break; a header the browser keeps.
    with urllib.request.urlopen(server.url + "/", timeout=10) as reply:
        policy = reply.headers.get("Content-Security-Policy") or ""
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy


def test_an_unknown_route_is_a_404(server: UiServer) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(server, "/nonsense")
    assert caught.value.code == 404


# --------------------------------------------------------------- who may ask


def test_a_page_on_another_site_cannot_drive_vox(server: UiServer) -> None:
    """Localhost is an address, not a permission.

    Any page in any browser can POST here. It cannot read the reply, which
    stops it learning anything — but "run this command" needs no reply.
    """
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(server, "/command", {"text": "/new"}, origin="https://evil.test")
    assert caught.value.code == 403


def test_the_page_itself_may(server: UiServer) -> None:
    status, _ = post(server, "/command", {"text": "/new"}, origin=server.url)
    assert status == 200


def test_a_request_with_no_origin_is_a_script_and_is_allowed(server: UiServer) -> None:
    # curl and the tests. Not a browser being aimed at this port by a page
    # somebody is reading.
    status, _ = post(server, "/command", {"text": "/new"})
    assert status == 200


def test_a_body_that_is_not_json_is_refused(server: UiServer) -> None:
    request = urllib.request.Request(
        server.url + "/send",
        data=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 400


# ------------------------------------------------------------- the commands


def test_a_slash_command_runs_through_the_same_dispatch(server: UiServer) -> None:
    """The whole point of the host protocol: forty-nine commands, unchanged."""
    post(server, "/command", {"text": "/help"})
    messages = state(server)["messages"]
    assert messages[-1]["role"] == "system"
    assert "AVAILABLE COMMANDS" in messages[-1]["content"]


def test_a_command_nobody_has_heard_of_says_so(server: UiServer) -> None:
    post(server, "/command", {"text": "/nonsense"})
    messages = state(server)["messages"]
    assert messages[-1]["role"] == "error"
    assert "UNKNOWN COMMAND" in messages[-1]["content"]


def test_a_command_that_wants_a_screen_asks_the_page_for_one(
    server: UiServer,
) -> None:
    collected, _ = frames(server, 2)
    time.sleep(0.3)
    post(server, "/command", {"text": "/sessions"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(collected) < 2:
        time.sleep(0.05)
    assert [f["kind"] for f in collected] == ["state", "view"]
    assert collected[1]["name"] == "sessions"


# ------------------------------------------------------------------ a turn


def test_a_message_is_answered_and_lands_in_the_session(
    server: UiServer, host: WebHost, monkeypatch
) -> None:
    def fake_turn(**kwargs):
        yield AgentEvent(type="text", text="hello ")
        yield AgentEvent(type="text", text="there")
        yield AgentEvent(
            type="assistant_done",
            messages=[Message(role="assistant", content="hello there")],
        )

    monkeypatch.setattr("vox_chat.webui.host.run_turn", fake_turn)
    post(server, "/send", {"text": "a question"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and host.generating:
        time.sleep(0.05)
    roles = [m["role"] for m in state(server)["messages"]]
    assert roles == ["user", "assistant"]
    assert state(server)["messages"][-1]["content"] == "hello there"


def test_a_second_message_mid_turn_is_refused(
    server: UiServer, host: WebHost, monkeypatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def fake_turn(**kwargs):
        started.set()
        release.wait(5)
        yield AgentEvent(type="text", text="done")

    monkeypatch.setattr("vox_chat.webui.host.run_turn", fake_turn)
    post(server, "/send", {"text": "first"})
    assert started.wait(5)
    post(server, "/send", {"text": "second"})
    contents = [m["content"] for m in state(server)["messages"]]
    assert any("ALREADY GENERATING" in text for text in contents)
    release.set()


# ------------------------------------------------------- the confirmations


def test_a_confirmation_nobody_answers_denies(host: WebHost) -> None:
    """The decision this design had to make, and it has to be no.

    A tab can be closed with a diff on screen. Whatever the timeout does is
    what happens when nobody read the confirmation, and a timeout that
    authorises is a confirmation nobody read — which SECURITY.md already
    says is not one.
    """
    host.config.setdefault("ui", {})["confirm_timeout_seconds"] = 0.3
    listener = host.events.subscribe()
    try:
        assert host.confirm("write_file", "--- a\n+++ b") is False
    finally:
        host.events.unsubscribe(listener)
    said = [m.content for m in host.session.messages]
    assert any("NOT AUTHORISED" in text for text in said)


def test_a_confirmation_with_no_tab_open_denies_at_once(host: WebHost) -> None:
    # Nobody is watching, so nobody can authorise. Waiting five minutes to
    # reach the same answer would only look like a hang.
    host.config.setdefault("ui", {})["confirm_timeout_seconds"] = 30
    started = time.monotonic()
    assert host.confirm("run_command", "rm -rf /") is False
    assert time.monotonic() - started < 1.0


def test_an_answered_confirmation_is_the_answer(host: WebHost) -> None:
    listener = host.events.subscribe()
    result: list[bool] = []

    def ask() -> None:
        result.append(host.confirm("write_file", "a diff"))

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    frame = listener.get(timeout=5)
    assert frame["kind"] == "confirm"
    assert host.answer_confirm(frame["id"], True) is True
    thread.join(timeout=5)
    host.events.unsubscribe(listener)
    assert result == [True]


def test_shutting_down_denies_everything_outstanding(host: WebHost) -> None:
    listener = host.events.subscribe()
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(host.confirm("write_file", "x")), daemon=True
    )
    thread.start()
    listener.get(timeout=5)
    host.close()
    thread.join(timeout=5)
    host.events.unsubscribe(listener)
    assert result == [False]


# ------------------------------------------------------------------- state


def test_the_draft_reaches_the_commands_that_read_it(server: UiServer) -> None:
    # /prompt-save saves what is in the input box, which in a browser is in
    # the browser. It has to be told.
    post(server, "/draft", {"text": "a prompt worth keeping"})
    post(server, "/command", {"text": "/prompt-save keeper"})
    messages = state(server)["messages"]
    assert any("PROMPT SAVED" in m["content"] for m in messages)


def test_the_page_and_the_state_agree_about_the_workspace(
    server: UiServer, workspace: Path
) -> None:
    assert state(server)["workspace"] == str(workspace.resolve())


def test_the_module_constant_and_the_served_page_are_the_same(
    server: UiServer,
) -> None:
    assert get(server, "/").decode() == PAGE


# ------------------------------------------- what the browser host is missing


def test_every_command_reaches_an_answer_rather_than_a_traceback(
    host: WebHost,
) -> None:
    """The test that the census should have been from the start.

    `hosting.Host` documents the surface a command may touch, but a protocol
    is documentation with a type checker behind it, and mypy does not run over
    `webui/`. Nothing said a word when `/model` reached for `refresh_status`,
    which `WebHost` did not have: the command wrote its message, raised, and
    took the HTTP request down with it — from the page it looked like the
    command had half worked, which is exactly what it had done.

    So every command is run here with no argument. Most print their usage,
    some do their work, a few say they are not available in the browser yet.
    None of them may raise.
    """
    from vox_chat.commands import dispatch, parse
    from vox_chat.commands.spec import COMMANDS

    # Straight through dispatch, under the guard rather than behind it: the
    # guard turns anything into a sentence, so a test that went through it
    # would pass with every one of these members still missing.
    #
    # These reach the network or the provider even with no argument, and this
    # test is about the shape of the calls rather than about what answers.
    reaches_out = {"connect", "mcp", "index", "web", "mesh", "peers", "ask"}
    failed: list[str] = []
    for command in COMMANDS:
        if command.name in reaches_out:
            continue
        try:
            result = dispatch.run(host, parse("/" + command.name))
            if dispatch.is_async(result):
                result.close()
        except Exception as exc:
            failed.append(f"/{command.name}: {exc!r}")
    assert not failed, "commands that raised instead of answering:\n" + "\n".join(
        failed
    )


def test_a_command_the_browser_cannot_do_says_so_and_carries_on(
    host: WebHost, monkeypatch
) -> None:
    # The guard, not the members: whatever a handler grows next, the page
    # gets a sentence rather than a dead connection.
    from vox_chat.commands import dispatch

    def explode(app, parsed):
        raise AttributeError("'WebHost' object has no attribute 'something_new'")

    monkeypatch.setattr(dispatch, "run", explode)
    host.run_command("/stats")
    assert "NOT AVAILABLE IN THE BROWSER YET" in host.session.messages[-1].content


def test_a_turn_that_crashes_says_so_instead_of_freezing(
    host: WebHost, monkeypatch
) -> None:
    """A turn runs on its own thread. Anything escaping it used to kill the
    thread quietly, leave `generating` set, and leave the page waiting for a
    token that was never coming."""

    def explode(**kwargs):
        raise RuntimeError("the provider library changed under us")
        yield  # pragma: no cover - never reached, makes this a generator

    monkeypatch.setattr("vox_chat.webui.host.run_turn", explode)
    host.session.messages.append(Message(role="user", content="a question"))
    host._turn()
    assert host.generating is False
    assert "the turn failed" in host.session.messages[-1].content


# --------------------------------------------------------- the command popup


def test_the_page_can_ask_for_every_command(server: UiServer) -> None:
    data = json.loads(get(server, "/api/commands"))
    names = [c["name"] for c in data["commands"]]
    assert len(names) == len(set(names)) == 49
    for command in data["commands"]:
        assert command["usage"].startswith("/")
        assert command["help"]
        assert command["group"]


def test_the_popup_and_the_legend_cannot_disagree(server: UiServer) -> None:
    """Both come from _grouped_commands, which fails at import if a command
    is in no group or in a group twice. A popup that quietly omitted a
    command would be worse than no popup."""
    from vox_chat.commands.spec import COMMANDS

    served = {c["name"] for c in json.loads(get(server, "/api/commands"))["commands"]}
    assert served == {spec.name for spec in COMMANDS}
