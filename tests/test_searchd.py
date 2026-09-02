"""The little search server VOX starts for itself.

The parsing is checked against captured markup, and the server against a real
socket on localhost with the upstream stubbed. Nothing here reaches the
internet: that would make the suite depend on somebody else's uptime.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from vox_chat import searchd

# Shaped like the page the HTML endpoint really returns, links and all.
DDG_PAGE = """
<div class="result results_links">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fguide&amp;rut=x">
      A <b>guide</b> to buffers
    </a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fguide">
    Everything about <b>ring&nbsp;buffers</b>, at length.
  </a>
</div>
<div class="result results_links">
  <h2 class="result__title">
    <a class="result__a" href="https://b.example/direct">Second result</a>
  </h2>
  <a class="result__snippet">Shorter.</a>
</div>
"""

LITE_PAGE = """
<table><tr><td>
  <a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fc.example%2Fpage">
    From the lite page
  </a>
</td></tr></table>
"""


# ------------------------------------------------------------------ parsing


def test_results_are_read_out_of_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(searchd, "_post", lambda url, query: DDG_PAGE)
    hits = searchd.duckduckgo("ring buffers")

    assert [hit.url for hit in hits] == [
        "https://a.example/guide", "https://b.example/direct"
    ]
    # Markup out, entities in, whitespace collapsed.
    assert hits[0].title == "A guide to buffers"
    assert hits[0].content == "Everything about ring buffers, at length."


def test_the_redirect_wrapper_is_unwrapped() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=zz"
    assert searchd._direct(wrapped) == "https://example.com/a?b=1"
    # Anything not wrapped is left exactly as it is.
    assert searchd._direct("https://plain.example/x") == "https://plain.example/x"


def test_the_lite_page_is_the_second_try(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {searchd.DDG_HTML: "<html>nothing useful</html>",
             searchd.DDG_LITE: LITE_PAGE}
    monkeypatch.setattr(searchd, "_post", lambda url, query: pages[url])

    hits = searchd.duckduckgo("x")
    assert [hit.url for hit in hits] == ["https://c.example/page"]
    assert hits[0].title == "From the lite page"


def test_a_captcha_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero results and a captcha are different things, and saying so is the
    difference between 'nothing found' and 'you are being rate limited'."""
    monkeypatch.setattr(
        searchd, "_post", lambda url, query: "<html>Anomaly detected</html>"
    )
    with pytest.raises(searchd.SearchdError, match="captcha"):
        searchd.duckduckgo("x")


def test_an_upstream_that_is_down_says_which(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(searchd.urllib.request, "urlopen", fail)
    with pytest.raises(searchd.SearchdError, match="cannot reach"):
        searchd.duckduckgo("x")


def test_wikipedia_is_added_without_duplicating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(searchd, "duckduckgo",
                        lambda query, limit=10: [searchd.Hit("Ring buffer", "https://a.example")])
    monkeypatch.setattr(searchd, "wikipedia", lambda query, limit=3: [
        searchd.Hit("Circular buffer", "https://en.wikipedia.org/wiki/Circular_buffer",
                    "A data structure."),
        searchd.Hit("Dup", "https://a.example"),
    ])
    hits = searchd.search("ring buffer", limit=10)
    assert [hit.url for hit in hits] == [
        "https://a.example", "https://en.wikipedia.org/wiki/Circular_buffer"
    ]


def test_a_broken_wikipedia_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(searchd.urllib.request, "urlopen", fail)
    assert searchd.wikipedia("x") == []


def test_the_port_comes_from_the_endpoint() -> None:
    assert searchd.port_of("http://127.0.0.1:8888") == 8888
    assert searchd.port_of("http://localhost") == 80
    assert searchd.port_of("https://search.example") == 443


# ------------------------------------------------------------------- server


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    """A real socket on a free port, with the upstream stubbed."""
    monkeypatch.setattr(searchd, "search", lambda query, limit=10: [
        searchd.Hit(f"Result for {query}", "https://a.example", "snippet"),
    ])
    running = searchd.LocalSearch(port=0)   # 0: let the OS choose
    running.start()
    running.port = running._server.server_address[1]
    try:
        yield running
    finally:
        running.stop()


def get(port: int, path: str):
    url = f"http://{searchd.HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_it_answers_the_json_a_searxng_client_expects(server) -> None:
    status, payload = get(
        server.port, "/search?" + urllib.parse.urlencode({"q": "ring buffers",
                                                          "format": "json"})
    )
    assert status == 200
    assert payload["results"][0]["url"] == "https://a.example"
    # The keys are SearXNG's, so the same client code reads both.
    assert set(payload["results"][0]) == {"title", "url", "content"}


def test_a_search_for_nothing_is_a_bad_request(server) -> None:
    status, payload = get(server.port, "/search?q=")
    assert status == 400
    assert "no q" in payload["error"]


def test_any_other_path_is_not_found(server) -> None:
    status, _payload = get(server.port, "/admin")
    assert status == 404


def test_an_upstream_failure_is_reported_as_such(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(query, limit=10):
        raise searchd.SearchdError("the search endpoint asked for a captcha")

    monkeypatch.setattr(searchd, "search", refuse)
    status, payload = get(server.port, "/search?q=x")
    assert status == 502, "the upstream failed, not this server"
    assert "captcha" in payload["error"]


def test_it_listens_only_on_this_machine(server) -> None:
    """Nobody else's browser gets to use it."""
    assert server._server.server_address[0] == "127.0.0.1"
    assert searchd.HOST == "127.0.0.1"


def test_starting_twice_is_harmless(server) -> None:
    assert "already listening" in server.start()


def test_a_port_in_use_says_so(server) -> None:
    second = searchd.LocalSearch(port=server.port)
    with pytest.raises(searchd.SearchdError, match="cannot listen"):
        second.start()


def test_stopping_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    running = searchd.LocalSearch(port=0)
    running.start()
    running.stop()
    running.stop()
    assert running.running is False
