"""Searching the internet: the backends, the guards, and the way in.

Nothing here touches the network. ``urlopen`` is replaced, so what is asserted
is the request VOX makes and what it does with the answer.
"""

from __future__ import annotations

import io
import json
from email.message import Message as EmailMessage
from pathlib import Path

import pytest

from vox_chat import web
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config, validate_config
from vox_chat.storage import global_config_path
from vox_chat.ui.widgets import MessageBox


# ------------------------------------------------------------------ a fake wire


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "text/html",
                 url: str = "https://example.com/page") -> None:
        super().__init__(body)
        self._url = url
        headers = EmailMessage()
        headers["Content-Type"] = content_type
        self.headers = headers

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    """The local search server, stubbed: no socket, no upstream."""
    started: list[int] = []

    class FakeServer:
        port = 8888
        running = False

        def start(self_inner):
            started.append(self_inner.port)
            self_inner.running = True
            return f"listening on 127.0.0.1:{self_inner.port}"

        def stop(self_inner):
            self_inner.running = False

    server = FakeServer()
    server.started = started
    return server


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Records every request, and answers with whatever the test queued."""
    calls: list = []
    answers: list = []

    def urlopen(request, timeout=None):
        calls.append((request.full_url, dict(request.headers), timeout))
        if not answers:
            raise AssertionError(f"unexpected request to {request.full_url}")
        return answers.pop(0)

    monkeypatch.setattr(web.urllib.request, "urlopen", urlopen)
    # Every host looks public unless a test says otherwise.
    monkeypatch.setattr(web, "_is_private", lambda host: False)
    return type("Wire", (), {"calls": calls, "answers": answers})()


# ----------------------------------------------------------------- extraction


def test_a_page_becomes_the_text_a_reader_would_see() -> None:
    title, text = web.to_text(
        "<html><head><title>  A Page  </title>"
        "<style>p{color:red}</style></head><body>"
        "<nav>menu junk</nav><h1>Heading</h1><p>First   paragraph.</p>"
        "<script>alert(1)</script><p>Second paragraph.</p>"
        "<footer>legal</footer></body></html>"
    )
    assert title == "A Page"
    assert text == "Heading\n\nFirst paragraph.\n\nSecond paragraph."
    for junk in ("alert(1)", "color:red", "menu junk", "legal"):
        assert junk not in text


def test_entities_and_odd_markup_survive() -> None:
    _title, text = web.to_text("<p>caf&eacute; &amp; cr&egrave;me<br>next</p>")
    assert "café & crème" in text
    assert "next" in text


# --------------------------------------------------------------- the guards


@pytest.mark.parametrize("url", [
    "ftp://example.com/file",
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_only_http_is_fetched(url: str) -> None:
    with pytest.raises(web.WebError, match="http"):
        web.check_url(url, web.WebSettings(enabled=True))


def test_private_addresses_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise 'read this URL' reaches your router, your model server, or a
    cloud metadata endpoint — from inside, where they trust you."""
    monkeypatch.setattr(web, "_is_private", lambda host: True)
    settings = web.WebSettings(enabled=True)
    with pytest.raises(web.WebError, match="private or local"):
        web.check_url("http://169.254.169.254/latest/meta-data/", settings)

    # And it is a decision, not a wall.
    allowed = web.WebSettings(enabled=True, allow_private_addresses=True)
    assert web.check_url("http://192.168.1.1/", allowed)


def test_the_configured_endpoint_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SearXNG on localhost is the normal case; a URL from a model is not."""
    monkeypatch.setattr(web, "_is_private", lambda host: True)
    settings = web.WebSettings(enabled=True)
    assert web.check_url("http://localhost:8888/search", settings, internal=True)


# ---------------------------------------------------------------- the search


def test_searxng_asks_for_json_and_flattens_the_answer(wire) -> None:
    wire.answers.append(FakeResponse(json.dumps({"results": [
        {"title": "First", "url": "https://a.example", "content": "  spaced   out "},
        {"title": "Second", "url": "https://b.example", "content": "second"},
        {"title": "No URL", "content": "dropped"},
    ]}).encode(), "application/json"))

    settings = web.WebSettings(enabled=True, endpoint="http://localhost:8888/")
    results = web.search("ring buffers", settings)

    url, headers, timeout = wire.calls[0]
    assert url.startswith("http://localhost:8888/search?")
    assert "q=ring+buffers" in url and "format=json" in url
    assert timeout == settings.timeout_seconds
    assert [r.url for r in results] == ["https://a.example", "https://b.example"]
    assert results[0].snippet == "spaced out"


def test_brave_sends_the_key_in_the_header(wire) -> None:
    wire.answers.append(FakeResponse(json.dumps({"web": {"results": [
        {"title": "First", "url": "https://a.example", "description": "one"},
    ]}}).encode(), "application/json"))

    settings = web.WebSettings(enabled=True, provider="brave", api_key="secret-key")
    results = web.search("ring buffers", settings)

    url, headers, _timeout = wire.calls[0]
    assert url.startswith(web.BRAVE_ENDPOINT)
    assert headers["X-subscription-token"] == "secret-key"
    assert "secret-key" not in url, "a key does not belong in a query string"
    assert [r.title for r in results] == ["First"]


def test_more_results_than_asked_for_are_trimmed(wire) -> None:
    wire.answers.append(FakeResponse(json.dumps({"results": [
        {"title": str(n), "url": f"https://{n}.example"} for n in range(10)
    ]}).encode(), "application/json"))
    results = web.search("x", web.WebSettings(enabled=True, max_results=3))
    assert len(results) == 3


def test_a_searxng_without_json_says_what_to_fix(wire) -> None:
    wire.answers.append(FakeResponse(b"<html>a web page</html>"))
    with pytest.raises(web.WebError, match="settings.yml"):
        web.search("x", web.WebSettings(enabled=True, provider="searxng"))


def test_the_local_server_answering_nonsense_says_so(wire) -> None:
    wire.answers.append(FakeResponse(b"<html>not json</html>"))
    with pytest.raises(web.WebError, match="local search server"):
        web.search("x", web.WebSettings(enabled=True, provider="local"))


def test_searching_while_off_is_refused() -> None:
    with pytest.raises(web.WebError, match="off"):
        web.search("x", web.WebSettings(enabled=False))


def test_brave_without_a_key_is_refused() -> None:
    settings = web.WebSettings(enabled=True, provider="brave")
    assert "api_key" in (settings.unusable() or "")
    with pytest.raises(web.WebError, match="api_key"):
        web.search("x", settings)


# ----------------------------------------------------------------- the page


def test_a_page_is_read_and_labelled(wire) -> None:
    wire.answers.append(FakeResponse(
        b"<html><head><title>Guide</title></head><body><p>Content here.</p></body></html>"
    ))
    page = web.fetch("https://example.com/page", web.WebSettings(enabled=True))
    assert page.title == "Guide"
    assert "Content here." in page.text
    assert page.truncated is False

    rendered = web.render_page(page)
    assert web.UNTRUSTED_NOTE in rendered
    assert "not instructions" in rendered, "the model is told what this text is"


def test_a_page_that_is_not_text_is_refused(wire) -> None:
    wire.answers.append(FakeResponse(b"\x89PNG...", "image/png"))
    with pytest.raises(web.WebError, match="not text"):
        web.fetch("https://example.com/logo.png", web.WebSettings(enabled=True))


def test_a_long_page_is_cut_and_says_so(wire) -> None:
    body = b"<html><body><p>" + b"word " * 5000 + b"</p></body></html>"
    wire.answers.append(FakeResponse(body))
    page = web.fetch(
        "https://example.com/long", web.WebSettings(enabled=True, fetch_max_bytes=500)
    )
    assert page.truncated is True
    assert len(page.text) < 600
    assert "truncated" in web.render_page(page)


def test_fetching_can_be_switched_off_on_its_own() -> None:
    settings = web.WebSettings(enabled=True, allow_fetch=False)
    with pytest.raises(web.WebError, match="allow_fetch"):
        web.fetch("https://example.com", settings)


# -------------------------------------------------------------- the settings


def test_the_default_config_carries_a_web_block() -> None:
    config = default_config()
    assert config["web"]["enabled"] is False, "the internet is opt-in"
    assert config["web"]["provider"] == "local", "no key and nothing to install"
    assert config["agent"]["confirm_web"] is True
    assert validate_config(config) == []


@pytest.mark.parametrize("change, fragment", [
    ({"enabled": "yes"}, "web.enabled"),
    ({"provider": "google"}, "web.provider"),
    ({"max_results": 0}, "web.max_results"),
    ({"timeout_seconds": 0}, "web.timeout_seconds"),
    ({"endpoint": 8888}, "web.endpoint"),
])
def test_a_broken_web_block_is_reported(change: dict, fragment: str) -> None:
    config = default_config()
    config["web"].update(change)
    assert any(fragment in error for error in validate_config(config))


# ------------------------------------------------------------------- as a tool


def test_the_model_is_only_offered_the_web_when_it_is_on() -> None:
    from vox_chat.agent import needs_confirmation
    from vox_chat.tools import WEB_SCHEMAS, WEB_TOOLS

    assert {s["function"]["name"] for s in WEB_SCHEMAS} == WEB_TOOLS
    # Outbound, so confirmed like a write or a command.
    assert needs_confirmation("web_search", {}) is True
    assert needs_confirmation("fetch_url", {}) is True
    assert needs_confirmation("fetch_url", {"confirm_web": False}) is False


def test_the_tool_refuses_when_the_web_is_not_configured(tmp_path: Path) -> None:
    from vox_chat.tools import ToolError, Workspace, execute

    workspace = Workspace(tmp_path)
    with pytest.raises(ToolError, match="not configured"):
        execute("web_search", {"query": "x"}, workspace)


def test_the_tool_returns_what_the_search_found(tmp_path: Path, wire) -> None:
    from vox_chat.tools import Workspace, execute

    wire.answers.append(FakeResponse(json.dumps({"results": [
        {"title": "First", "url": "https://a.example", "content": "one"},
    ]}).encode(), "application/json"))
    result = execute(
        "web_search", {"query": "ring buffers"}, Workspace(tmp_path),
        web_settings=web.WebSettings(enabled=True),
    )
    assert "https://a.example" in result.as_text()


def test_the_confirmation_says_the_query_leaves_the_machine(tmp_path: Path) -> None:
    from vox_chat.tools import Workspace, describe_call

    description = describe_call(
        "web_search", {"query": "how do I do X"}, Workspace(tmp_path)
    )
    assert "how do I do X" in description
    assert "leaves this machine" in description


# ------------------------------------------------------------------- in the app


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


async def test_the_web_is_off_until_asked_for(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/search anything")
        await pilot.pause()
        assert "CANNOT SEARCH" in transcript(app)
        assert "/web on" in transcript(app)


async def test_turning_it_on_is_remembered(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/web on")
        await pilot.pause()
        assert json.loads(global_config_path().read_text())["web"]["enabled"] is True
        assert app.web_settings().enabled is True


async def test_a_search_lands_in_the_conversation_labelled(
    app: VoxApp, wire, engine
) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        app.run_command("/web on")
        await pilot.pause()

        wire.answers.append(FakeResponse(json.dumps({"results": [
            {"title": "Ring buffers", "url": "https://a.example", "content": "one"},
        ]}).encode(), "application/json"))
        app.run_command("/search ring buffers")
        await app.workers.wait_for_complete()
        await pilot.pause()

        text = transcript(app)
        assert "SEARCHING" in text
        assert "https://a.example" in text
        # The model is told what this is before it reads it.
        stored = [m for m in app.session.messages if m.name == "web_search"]
        assert stored and web.UNTRUSTED_NOTE in stored[0].content


async def test_a_failed_search_says_why_and_changes_nothing(
    app: VoxApp, wire, engine
) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        app.run_command("/web on")
        await pilot.pause()

        wire.answers.append(FakeResponse(b"<html>not json</html>"))
        app.run_command("/search anything")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "SEARCH FAILED" in transcript(app)
        assert not [m for m in app.session.messages if m.name == "web_search"]


# --------------------------------------------------------------- web mode (F6)


def test_the_query_is_the_message_tidied() -> None:
    assert web.query_for("  what   is\n a ring buffer? ") == "what is a ring buffer?"
    assert len(web.query_for("x " * 400)) <= 300


def test_gathering_reads_the_first_results_only(wire) -> None:
    wire.answers.append(FakeResponse(json.dumps({"results": [
        {"title": f"R{n}", "url": f"https://{n}.example", "content": f"snippet {n}"}
        for n in range(5)
    ]}).encode(), "application/json"))
    for n in range(2):
        wire.answers.append(FakeResponse(
            f"<html><body><p>Body of page {n}.</p></body></html>".encode()
        ))

    settings = web.WebSettings(enabled=True, auto=True, auto_results=4, auto_fetch=2)
    sources = web.gather("ring buffers", settings)

    assert len(sources.results) == 4, "auto_results caps what is kept"
    assert len(sources.pages) == 2, "auto_fetch caps what is read"
    assert sources.failures == []
    assert "Body of page 0." in sources.pages[0].text


def test_one_dead_link_does_not_cost_the_other_sources(wire) -> None:
    wire.answers.append(FakeResponse(json.dumps({"results": [
        {"title": "Broken", "url": "https://gone.example", "content": "x"},
        {"title": "Fine", "url": "https://fine.example", "content": "y"},
    ]}).encode(), "application/json"))
    wire.answers.append(FakeResponse(b"\x89PNG", "image/png"))   # refused
    wire.answers.append(FakeResponse(b"<html><body><p>Good.</p></body></html>"))

    sources = web.gather("x", web.WebSettings(enabled=True, auto=True, auto_fetch=2))
    assert len(sources.pages) == 1
    assert sources.failures and "gone.example" in sources.failures[0]
    assert sources.found is True


def test_the_prompt_puts_the_sources_in_front_of_the_model() -> None:
    sources = web.Sources(
        query="ring buffers",
        results=[web.Result("A guide", "https://a.example", "about buffers")],
        pages=[web.Page("https://a.example", "A guide", "The full text.")],
    )
    prompt = web.research_prompt(sources)
    assert web.UNTRUSTED_NOTE in prompt
    assert "https://a.example" in prompt
    assert "The full text." in prompt
    assert "cite the URLs" in prompt
    assert "do not cover" in prompt


def test_the_prompt_shares_its_budget_between_pages() -> None:
    sources = web.Sources(
        query="x",
        results=[],
        pages=[web.Page(f"https://{n}.example", "T", "word " * 2000) for n in range(2)],
    )
    prompt = web.research_prompt(sources, limit=1000)
    assert len(prompt) < 2000
    assert prompt.count("truncated") == 2


def test_citations_say_what_was_read() -> None:
    sources = web.Sources(
        query="x",
        results=[web.Result("A", "https://a.example"),
                 web.Result("B", "https://b.example")],
        pages=[web.Page("https://a.example", "A", "text")],
        failures=["https://b.example: HTTP 404"],
    )
    text = sources.citations()
    assert "https://a.example" in text and "[read in full]" in text
    assert "HTTP 404" in text


async def test_f6_needs_a_backend_before_it_turns_on(app: VoxApp, engine) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        app.search_server = engine
        app.config["web"]["provider"] = "brave"   # and no key
        app.action_toggle_web_mode()
        await pilot.pause()
        assert "WEB MODE NEEDS A BACKEND" in transcript(app)
        assert app.web_mode_active() is False


async def test_f6_turns_the_mode_on_and_shows_it(app: VoxApp, engine) -> None:
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        app.search_server = engine
        await pilot.press("f6")
        await pilot.pause()

        assert app.web_mode_active() is True
        assert "WEB MODE ON" in transcript(app)
        assert "WEB" in str(app.query_one("#header").render())
        saved = json.loads(global_config_path().read_text())["web"]
        assert saved["enabled"] is True and saved["auto"] is True

        await pilot.press("f6")
        await pilot.pause()
        app.search_server = engine
        assert app.web_mode_active() is False
        assert "WEB MODE OFF" in transcript(app)


async def test_a_message_in_web_mode_is_answered_from_sources(
    app: VoxApp, wire, engine
) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        await pilot.press("f6")
        await pilot.pause()

        wire.answers.append(FakeResponse(json.dumps({"results": [
            {"title": "Ring buffers", "url": "https://a.example", "content": "snip"},
        ]}).encode(), "application/json"))
        wire.answers.append(FakeResponse(
            b"<html><body><p>A stalled reader blocks reclamation.</p></body></html>"
        ))

        app.input_area.insert("are lock-free ring buffers safe?")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # It searched for the question, and said what it found.
        searched = wire.calls[0][0].replace("+", " ").replace("%3F", "?")
        assert "are lock-free ring buffers safe?" in searched
        assert "WEB - 1 sources" in transcript(app)

        # The citations stay in the conversation; the page text is for this
        # turn only, or every later message would carry the whole internet.
        stored = [m for m in app.session.messages if m.name == "web"]
        assert stored and "https://a.example" in stored[0].content
        assert "A stalled reader" not in stored[0].content
        assert "A stalled reader" in app._web_prompt
        assert web.UNTRUSTED_NOTE in app._web_prompt


async def test_a_failed_search_still_answers(app: VoxApp, wire, engine) -> None:
    """A search that breaks must not swallow the question."""
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        await pilot.press("f6")
        await pilot.pause()

        wire.answers.append(FakeResponse(b"<html>not json</html>"))
        app.input_area.insert("a question")
        app.action_send()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.search_server = engine

        assert "answering without sources" in transcript(app)
        assert app._web_prompt == ""
        assert [m.content for m in app.session.messages if m.role == "user"] == [
            "a question"
        ]


async def test_a_message_sent_mid_lookup_is_refused(
    app: VoxApp, monkeypatch, engine
) -> None:
    import time as time_module

    def slow_gather(question, settings):
        time_module.sleep(0.4)
        return web.Sources(query=question,
                           results=[web.Result("A", "https://a.example")])

    monkeypatch.setattr(web, "gather", slow_gather)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        await pilot.press("f6")
        await pilot.pause()

        app.input_area.insert("first")
        app.action_send()
        await pilot.pause()
        assert app._web_running is True

        app.input_area.insert("second, while it looks things up")
        app.action_send()
        await pilot.pause()
        assert "STILL LOOKING THINGS UP" in transcript(app)
        assert [m.content for m in app.session.messages if m.role == "user"] == ["first"]
        await app.workers.wait_for_complete()


async def test_a_marked_question_goes_to_the_mesh_not_the_web(
    app: VoxApp, engine
) -> None:
    """[CNS] is a mesh round; web mode does not quietly take it over."""
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        await pilot.press("f6")
        await pilot.pause()

        app.input_area.insert("[CNS]ask the others[/CNS]")
        app.action_send()
        await pilot.pause()
        # There is no mesh here, so it refuses as a round rather than searching.
        assert "CONSENSUS NEEDS THE MESH" in transcript(app)


def test_a_loopback_endpoint_is_recognised_whatever_it_is_called() -> None:
    """Regression: a config written before the local server existed says
    provider 'searxng' and endpoint localhost, and got connection refused."""
    for endpoint in ("http://localhost:8888", "http://127.0.0.1:9999",
                     "http://[::1]:8888"):
        assert web.is_local_endpoint(endpoint) is True
    for endpoint in ("https://search.example", "http://192.168.1.5:8888"):
        assert web.is_local_endpoint(endpoint) is False


def test_nothing_listening_says_how_to_fix_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request, timeout=None):
        raise web.urllib.error.URLError("connection refused")

    monkeypatch.setattr(web.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(web, "_is_private", lambda host: False)

    with pytest.raises(web.WebError, match="/web start"):
        web.search("x", web.WebSettings(enabled=True, provider="searxng",
                                        endpoint="http://localhost:8888"))
    # A remote endpoint gets the plain reason instead.
    with pytest.raises(web.WebError, match="cannot reach"):
        web.search("x", web.WebSettings(enabled=True, provider="searxng",
                                        endpoint="https://search.example"))


async def test_an_old_config_still_gets_a_server(app: VoxApp, engine) -> None:
    """The provider is named searxng and points at this machine: VOX serves it."""
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_server = engine
        app.search_server = engine
        app.config["web"].update(
            {"enabled": True, "provider": "searxng",
             "endpoint": "http://localhost:8888"}
        )
        assert app.wants_local_server(app.web_settings()) is True

        app.run_command("/search anything")
        await pilot.pause()
        app.search_server = engine
        assert engine.started, "the server was started for it"
        assert "SEARCH SERVER" in transcript(app)
