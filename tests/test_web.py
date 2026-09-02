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
        web.search("x", web.WebSettings(enabled=True))


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
    assert config["web"]["provider"] == "searxng"
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
    app: VoxApp, wire
) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
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


async def test_a_failed_search_says_why_and_changes_nothing(app: VoxApp, wire) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_command("/web on")
        await pilot.pause()

        wire.answers.append(FakeResponse(b"<html>not json</html>"))
        app.run_command("/search anything")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "SEARCH FAILED" in transcript(app)
        assert not [m for m in app.session.messages if m.name == "web_search"]
