"""Ranking, deduplication, caching, robots.txt and main-content extraction.

Nothing here touches the network. What is tested is the arithmetic that
decides which two of ten results get read, and the three refusals that stop
VOX asking the same question twice or reading a page it was asked not to.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest

from vox_chat import ranking, robots, web, webcache
from vox_chat.web import Result

# ------------------------------------------------------------------ ranking


def a_result(
    title: str, url: str = "https://example.com/x", snippet: str = ""
) -> Result:
    return Result(title=title, url=url, snippet=snippet)


def test_the_title_counts_for_more_than_the_snippet() -> None:
    """The title is what the page says it is about; the snippet is the
    index's guess at the relevant part."""
    question = "how do python generators work"
    in_title = ranking.score(a_result("Python generators explained"), question)
    in_snippet = ranking.score(
        a_result("An article", snippet="python generators explained"), question
    )
    assert in_title.score > in_snippet.score


def test_a_result_with_nothing_in_common_scores_nothing() -> None:
    scored = ranking.score(a_result("Cheap flights to Rome"), "python generators")
    assert scored.score == 0.0
    assert "no overlap" in scored.why


def test_a_known_source_gets_a_nudge_not_an_override() -> None:
    """Two places, both earned. A nudge that outranked a real match would be
    a bookmark list, not a ranking."""
    question = "python generators"
    known = ranking.score(
        a_result("Something else", "https://stackoverflow.com/q/1"), question
    )
    matching = ranking.score(
        a_result("Python generators", "https://nowhere.test/x"), question
    )
    assert known.score > 0
    assert matching.score > known.score


def test_ties_keep_the_order_the_index_gave() -> None:
    """Where the arithmetic has nothing to say, the index's own judgement is
    better than none."""
    results = [a_result("unrelated one"), a_result("unrelated two")]
    assert ranking.rank(results, "python") == results


def test_the_best_match_comes_first() -> None:
    results = [
        a_result("Cheap flights", "https://a.test/1"),
        a_result("How python generators work", "https://b.test/2"),
    ]
    assert ranking.rank(results, "python generators")[0].url == "https://b.test/2"


def test_ranking_is_not_asked_to_read_anything() -> None:
    """It runs before a single page has been fetched, on titles and snippets."""
    results = [a_result(f"title {n}", f"https://x.test/{n}") for n in range(200)]
    started = time.perf_counter()
    ranking.rank(results, "a question with several words in it")
    assert time.perf_counter() - started < 0.5


# ------------------------------------------------------------- duplicates


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/page", "https://example.com/page/"),
        ("https://example.com/page", "https://www.example.com/page"),
        ("https://example.com/page", "http://example.com/page#section"),
        ("https://example.com/p?a=1", "https://example.com/p?a=1&utm_source=x"),
    ],
)
def test_the_same_page_written_differently_is_one_page(a: str, b: str) -> None:
    assert ranking.normalise(a) == ranking.normalise(b)


def test_two_links_to_the_same_page_are_read_once() -> None:
    results = [
        a_result("A", "https://example.com/page"),
        a_result("A", "https://www.example.com/page/"),
    ]
    assert len(ranking.deduplicate(results)) == 1


def test_a_mirror_is_caught_by_what_it_says() -> None:
    """A Stack Overflow answer, its mirror, and a site that scraped both are
    three URLs and one answer. This is why it is not a set of URLs."""
    body = (
        "the way to do this is to call the function with a keyword argument "
        "instead of positional ones, which keeps the signature readable"
    )
    results = [
        a_result("Question", "https://stackoverflow.com/q/1", snippet=body),
        a_result("Question", "https://scraped.test/q/1", snippet=body),
    ]
    assert len(ranking.deduplicate(results)) == 1


def test_two_different_pages_both_survive() -> None:
    results = [
        a_result("One", "https://a.test/1", snippet="something about cats"),
        a_result("Two", "https://b.test/2", snippet="something about bridges"),
    ]
    assert len(ranking.deduplicate(results)) == 2


# ---------------------------------------------------------- second attempt


def test_a_search_that_found_nothing_is_noticed() -> None:
    assert ranking.thin([], "anything") is True
    assert ranking.thin([a_result("Cheap flights")], "python generators") is True
    assert ranking.thin([a_result("Python generators")], "python generators") is False


def test_a_synonym_is_not_a_failed_search() -> None:
    """Ask about ring buffers and the right answer is titled Circular buffer,
    which shares not one word with the question. Retrying there would throw
    away the correct result and pay a round trip for it."""
    results = [
        a_result("Circular buffer - Wikipedia", "https://en.wikipedia.org/x"),
        a_result("Circular buffers in C", "https://a.test/1"),
        a_result("Bounded queues", "https://b.test/2"),
    ]
    assert ranking.thin(results, "how do ring buffers work") is False


def test_the_second_query_is_arithmetic_not_a_model_call() -> None:
    """A second search that needs the model to write it costs a whole turn of
    latency for a case that is usually the index having a bad minute."""
    question = "what is the postcode of the town of Latiano in Puglia"
    second = ranking.refine(question, [question])
    assert second and second != question
    assert len(second.split()) <= 4

    third = ranking.refine(question, [question, second])
    assert '"' in third, "the distinctive words, quoted"

    assert ranking.refine(question, [question, second, third]) == ""


def test_a_question_too_short_to_refine_says_so() -> None:
    assert ranking.refine("why", []) == ""


# -------------------------------------------------------------- the cache


def test_a_search_is_not_repeated_within_its_ttl(tmp_path: Path) -> None:
    cache = webcache.WebCache(tmp_path)
    cache.put(
        "search", "python", [{"title": "A", "url": "https://a.test", "snippet": ""}]
    )
    entry = cache.get("search", "python", ttl=900)
    assert entry is not None
    assert entry.value[0]["title"] == "A"
    assert entry.describe_age().endswith("old")


def test_an_expired_entry_is_a_miss_and_is_removed(tmp_path: Path) -> None:
    """An answer built on yesterday's cache while claiming to be current is
    worse than a slow one."""
    cache = webcache.WebCache(tmp_path)
    cache.put("search", "python", ["x"])
    assert cache.get("search", "python", ttl=0) is None
    assert list(tmp_path.glob("*.json")) == []


def test_a_different_question_is_a_different_entry(tmp_path: Path) -> None:
    cache = webcache.WebCache(tmp_path)
    cache.put("search", "python", ["a"])
    assert cache.get("search", "rust", ttl=900) is None


def test_a_cache_that_cannot_be_written_is_simply_not_used(tmp_path: Path) -> None:
    """A cache that can fail a search is worse than no cache."""
    cache = webcache.WebCache(tmp_path / "nested")
    cache.put("search", "x", {"not": {"json", "serialisable"}})
    assert cache.get("search", "x", ttl=900) is None


def test_unreadable_contents_are_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = webcache.WebCache(tmp_path)
    cache.put("search", "x", ["a"])
    next(tmp_path.glob("*.json")).write_text("{ not json", encoding="utf-8")
    assert cache.get("search", "x", ttl=900) is None


def test_the_cache_says_how_much_it_saved(tmp_path: Path) -> None:
    cache = webcache.WebCache(tmp_path)
    cache.put("search", "x", ["a"])
    cache.get("search", "x", ttl=900)
    cache.get("search", "y", ttl=900)
    assert "50% reused" in cache.describe()


def test_clearing_it_says_how_many_went(tmp_path: Path) -> None:
    cache = webcache.WebCache(tmp_path)
    for n in range(3):
        cache.put("search", str(n), [n])
    assert cache.clear() == 3


# ------------------------------------------------------------- robots.txt


class FakeRobots:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body

    def read(self, n: int = -1) -> bytes:
        return self.body

    def geturl(self) -> str:
        return "https://example.com/robots.txt"

    @property
    def headers(self):
        from email.message import EmailMessage

        headers = EmailMessage()
        headers["Content-Type"] = "text/plain"
        return headers

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_page_the_site_asks_crawlers_to_leave_is_left(monkeypatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeRobots(b"User-agent: *\nDisallow: /private/"),
    )
    cache = robots.RobotsCache()
    assert cache.allows("https://example.com/public/x") is True
    assert cache.allows("https://example.com/private/x") is False


def test_a_host_with_no_robots_txt_has_refused_nothing(monkeypatch) -> None:
    """Treating an unreachable file as a refusal would make an outage look
    like a policy."""

    def fail(request, timeout=None):
        raise OSError("nothing there")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert robots.RobotsCache().allows("https://example.com/x") is True


def test_it_is_asked_once_per_host(monkeypatch) -> None:
    calls: list[str] = []

    def urlopen(request, timeout=None):
        calls.append(request.full_url if hasattr(request, "full_url") else str(request))
        return FakeRobots(b"User-agent: *\nDisallow:")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    cache = robots.RobotsCache()
    for _ in range(5):
        cache.allows("https://example.com/page")
    assert len(calls) == 1


def test_something_that_is_not_a_web_url_is_nobody_else_business() -> None:
    assert robots.RobotsCache().allows("file:///etc/passwd") is True


# ------------------------------------------------------- main content


def test_the_article_is_taken_over_the_furniture_around_it() -> None:
    """to_text drops what it can name; a modern page is mostly div, and its
    sidebar survives that."""
    article = "The content of the article, at some length. " * 20
    page = (
        "<html><head><title>An Article</title></head><body>"
        '<div class="sidebar">related links you did not ask for</div>'
        f"<main><p>{article}</p></main>"
        "</body></html>"
    )
    title, text = web.to_text(page)
    assert title == "An Article"
    assert "related links" not in text
    assert "content of the article" in text


def test_a_page_that_does_not_say_where_its_content_is_keeps_everything() -> None:
    page = "<html><body><p>Just a paragraph, with no main element.</p></body></html>"
    _title, text = web.to_text(page)
    assert "Just a paragraph" in text


def test_a_main_element_holding_nothing_is_not_the_article() -> None:
    """A navigation stub that happens to be named main would lose the page."""
    page = (
        "<html><body><main><nav>x</nav></main>"
        f"<p>{'The real body of the page. ' * 30}</p></body></html>"
    )
    _title, text = web.to_text(page)
    assert "real body of the page" in text


def test_wikipedias_own_wrapper_is_recognised() -> None:
    body = "The subject of the article is described here at length. " * 15
    page = (
        f'<html><body><div class="mw-parser-output"><p>{body}</p></div>'
        "<div>footer junk everywhere</div></body></html>"
    )
    _title, text = web.to_text(page)
    assert "subject of the article" in text
    assert "footer junk" not in text


# ----------------------------------------------------------- more backends


def test_three_general_indexes_not_one() -> None:
    """DuckDuckGo's HTML endpoint is the one pillar that is scraped rather
    than offered, and it answers with a captcha under load."""
    from vox_chat import searchd

    names = [name for name, _ in searchd.UPSTREAMS]
    assert "web" in names and "mojeek" in names and "marginalia" in names


def test_the_settings_are_on_by_default_and_checked() -> None:
    from vox_chat.config import default_config, validate_config

    config = default_config()
    assert config["web"]["respect_robots"] is True
    assert config["web"]["rerank"] is True
    assert config["web"]["cache_seconds"] > 0
    assert validate_config(config) == []


def test_a_cached_page_says_it_was_cached() -> None:
    """An answer built on a cached page while claiming to be current is worse
    than a slow one."""
    page = web.Page(url="https://x.test", title="T", text="body", from_cache="3m old")
    assert page.from_cache == "3m old"
    assert json.dumps(page.to_dict())
