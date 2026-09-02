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


def test_both_endpoints_being_down_is_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset connection on one endpoint is tried on the other first."""
    tried: list[str] = []

    def fail(request, timeout=None):
        tried.append(request.full_url)
        raise urllib.error.URLError("connection reset by peer")

    monkeypatch.setattr(searchd.urllib.request, "urlopen", fail)
    with pytest.raises(searchd.SearchdError, match="nothing readable"):
        searchd.duckduckgo("x")
    assert tried == [searchd.DDG_HTML, searchd.DDG_LITE]


def _upstreams(monkeypatch, **replacements):
    """Replace the upstream table with scripted ones."""
    table = []
    for name, behaviour in replacements.items():
        table.append((name, behaviour))
    monkeypatch.setattr(searchd, "UPSTREAMS", tuple(table))


def test_every_upstream_contributes_without_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstreams(
        monkeypatch,
        web=lambda query, limit=10: [searchd.Hit("Ring buffer", "https://a.example")],
        wikipedia=lambda query, limit=3: [
            searchd.Hit("Circular buffer",
                        "https://en.wikipedia.org/wiki/Circular_buffer", "A structure."),
            searchd.Hit("Dup", "https://a.example"),   # already seen
        ],
    )
    hits, answered, failures = searchd.search("ring buffer", limit=10)
    assert [hit.url for hit in hits] == [
        "https://a.example", "https://en.wikipedia.org/wiki/Circular_buffer"
    ]
    assert answered == ["web", "wikipedia"]
    assert failures == []


def test_a_blocked_web_index_does_not_cost_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure that started this: a captcha killed the whole search."""
    def blocked(query, limit=10):
        raise searchd.SearchdError("asked for a captcha")

    _upstreams(
        monkeypatch,
        web=blocked,
        wikipedia=lambda query, limit=3: [
            searchd.Hit("Circular buffer", "https://en.wikipedia.org/wiki/Circular_buffer")
        ],
        stackoverflow=lambda query, limit=3: [
            searchd.Hit("efficient circular buffer?", "https://stackoverflow.com/q/4151320")
        ],
    )
    hits, answered, failures = searchd.search("circular buffer", limit=10)
    assert len(hits) == 2
    assert answered == ["wikipedia", "stackoverflow"]
    assert failures == ["web: asked for a captcha"]


def test_an_upstream_that_explodes_is_survived(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upstream is somebody else's code; it may fail in ways of its own."""
    def explode(query, limit=10):
        raise KeyError("hits")

    _upstreams(
        monkeypatch,
        web=explode,
        wikipedia=lambda query, limit=3: [searchd.Hit("A", "https://a.example")],
    )
    hits, answered, failures = searchd.search("x")
    assert [hit.url for hit in hits] == ["https://a.example"]
    assert failures == ["web: KeyError"]


def test_only_a_total_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(query, limit=10):
        raise searchd.SearchdError("down")

    _upstreams(monkeypatch, web=blocked, wikipedia=blocked)
    with pytest.raises(searchd.SearchdError, match="web: down; wikipedia: down"):
        searchd.search("x")


def test_stackoverflow_answers_are_summarised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(searchd, "_json", lambda url: {"items": [
        {"title": "efficient circular buffer?", "link": "https://stackoverflow.com/q/1",
         "score": 159, "answer_count": 15, "is_answered": True,
         "tags": ["python", "circular-buffer"]},
        {"title": "no link", "score": 1},
    ]})
    hits = searchd.stackexchange("circular buffer")
    assert len(hits) == 1, "an item without a link is not a result"
    assert "159 votes, 15 answers, accepted" in hits[0].content
    assert "python" in hits[0].content


def test_hacker_news_items_without_a_link_point_at_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(searchd, "_json", lambda url: {"hits": [
        {"title": "Ask HN: buffers?", "objectID": "42", "points": 7, "num_comments": 3},
    ]})
    hits = searchd.hackernews("buffers")
    assert hits[0].url == "https://news.ycombinator.com/item?id=42"
    assert "7 points" in hits[0].content


def test_a_json_upstream_that_is_down_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(searchd.urllib.request, "urlopen", fail)
    with pytest.raises(searchd.SearchdError, match="down"):
        searchd.wikipedia("x")


def test_the_port_comes_from_the_endpoint() -> None:
    assert searchd.port_of("http://127.0.0.1:8888") == 8888
    assert searchd.port_of("http://localhost") == 80
    assert searchd.port_of("https://search.example") == 443


# ------------------------------------------------------------------- server


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    """A real socket on a free port, with the upstream stubbed."""
    monkeypatch.setattr(searchd, "search", lambda query, limit=10: (
        [searchd.Hit(f"Result for {query}", "https://a.example", "snippet")],
        ["web"], ["wikipedia: down"],
    ))
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
    # And which upstreams answered, so a blocked index is visible.
    assert payload["sources"] == ["web"]
    assert payload["failures"] == ["wikipedia: down"]


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


# --------------------------------------------------- asking the right question


def test_the_language_is_guessed_from_the_question() -> None:
    """A question in Italian belongs on it.wikipedia. Sending it to the English
    one and getting nothing is how a real town became "never heard of it"."""
    assert searchd.language_of("mi dici quale è il cap di latiano?") == "it"
    assert searchd.language_of("what is the postal code of Latiano?") == "en"
    assert searchd.language_of("cual es el codigo postal de una ciudad") == "es"
    # Too little to go on stays with the default rather than guessing wildly.
    assert searchd.language_of("latiano") == "en"


def test_the_asking_is_stripped_from_the_question() -> None:
    """These APIs match titles, so "mi dici quale è il" is noise that misses."""
    assert searchd.keywords("mi dici quale è il cap di latiano?") == "cap latiano"
    assert searchd.keywords("what is a circular buffer?") == "circular buffer"
    assert searchd.keywords("tell me about ring buffers") == "about ring buffers"
    # A question made only of noise still searches for something.
    assert searchd.keywords("what is it?") == "what is it?"


def test_wikipedia_looks_up_the_title_before_the_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question about a place wants that place, not articles mentioning it."""
    asked: list[str] = []

    def fake_json(url):
        asked.append(url)
        if "opensearch" in url:
            if "latiano" in url.lower():
                return ["latiano", ["Latiano", "Latina"], [], []]
            return ["x", [], [], []]
        return {"query": {"search": [{"title": "Bartolo Longo", "snippet": "a man"}]}}

    monkeypatch.setattr(searchd, "_json", fake_json)
    hits = searchd.wikipedia("mi dici quale è il cap di latiano?", limit=3)

    assert hits[0].title == "Latiano", "the town comes first"
    assert hits[0].url == "https://it.wikipedia.org/wiki/Latiano"
    assert any(hit.title == "Bartolo Longo" for hit in hits), "and the rest follows"
    assert all("it.wikipedia.org" in url for url in asked)


def test_a_subject_only_documented_in_english_is_still_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    languages: list[str] = []

    def fake_json(url):
        languages.append("it" if "//it." in url else "en")
        if "//it." in url:
            return ["x", [], [], []] if "opensearch" in url else {"query": {"search": []}}
        if "opensearch" in url:
            return ["x", ["Ring buffer"], [], []]
        return {"query": {"search": []}}

    monkeypatch.setattr(searchd, "_json", fake_json)
    hits = searchd.wikipedia("mi dici cosa è il ring buffer che uso", limit=3)
    assert "en" in languages, "it fell back to the English encyclopaedia"
    assert hits and hits[0].url.startswith("https://en.wikipedia.org/")


def test_nothing_found_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstreams(monkeypatch, wikipedia=lambda query, limit=3: [])
    with pytest.raises(searchd.SearchdError, match="nothing found"):
        searchd.search("a question nobody has written about")
