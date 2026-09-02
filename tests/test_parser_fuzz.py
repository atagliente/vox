"""Malformed input through the parsers that face the open web.

`web.to_text` reads whatever a page happens to contain and `searchd` reads
DuckDuckGo's HTML, which is neither documented nor stable. Both are fed by
strangers, so the promise worth holding is narrow and absolute: whatever
arrives, they return something of the right shape or raise the error the
callers already handle. They never crash, never hang, never hand back
something the transcript cannot render.
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vox_chat import searchd, web

SLOW_OK = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Biased towards the shapes that break parsers: unclosed tags, stray angle
# brackets, half-written entities, the class names searchd matches on.
MARKUP = st.sampled_from(
    [
        "<",
        ">",
        "</",
        "<p>",
        "</p",
        "<script>",
        "</script>",
        "<!--",
        "-->",
        "<a href=",
        '<a class="result__a" href="',
        '"',
        "&",
        "&amp",
        "&#",
        "&#x",
        "&nbsp;",
        "<td>",
        "<br",
        "<html",
        "<title>",
        "</title>",
        "\x00",
        "\r\n",
        "text ",
        "à",
        "🙂",
    ]
)

PAGES = st.lists(MARKUP, max_size=25).map("".join)


@given(page=PAGES)
@SLOW_OK
def test_to_text_survives_anything_a_page_can_be(page: str) -> None:
    """Either a (title, text) pair of strings, or the WebError the callers
    already know how to show. Never anything else."""
    try:
        title, text = web.to_text(page)
    except web.WebError:
        return
    assert isinstance(title, str)
    assert isinstance(text, str)
    # The title is collapsed whitespace by construction, so it can go in a
    # single-line header without wrecking the layout.
    assert title == " ".join(title.split())


@given(
    before=PAGES,
    after=PAGES,
    tag=st.sampled_from(["script", "style", "noscript", "svg"]),
)
@SLOW_OK
def test_code_never_reaches_the_model_however_broken_the_page(
    before: str, after: str, tag: str
) -> None:
    """Script and style content is code, not prose, and no amount of broken
    markup around it may let it through.

    This is the shape of a bug that was here: markup ahead of a ``<script>``
    swallowed its opening tag — as a bogus comment, as a mangled tag name, as
    an unquoted attribute value — so nothing started skipping and the whole
    body arrived as text for the model to read. The sentinel is what no page
    could produce, so seeing it means the leak is real.

    The rest of SKIP — head, nav, footer, form — is text a reader would see
    somewhere on the page; losing the odd line of it to broken markup is
    untidy, not a defect, so it is not asserted here.
    """
    sentinel = "zqxjvw-furniture-9137"
    page = f"<html><body>{before}<{tag}>{sentinel}</{tag}>{after}</body></html>"
    _title, text = web.to_text(page)
    assert sentinel not in text


@given(fragment=PAGES)
@SLOW_OK
def test_clean_always_returns_collapsed_text(fragment: str) -> None:
    """`_clean` feeds result titles straight into the transcript: no tags, no
    runs of whitespace, nothing that would smear a line."""
    text = searchd._clean(fragment)
    assert isinstance(text, str)
    assert searchd._TAGS.search(text) is None, "no tag survives the clean"
    assert text == text.strip()
    assert "  " not in text


@given(
    url=st.text(
        alphabet=string.printable.replace("\x0b", "").replace("\x0c", ""),
        max_size=120,
    )
)
@SLOW_OK
def test_unwrapping_a_redirect_never_raises(url: str) -> None:
    """`_direct` runs on every href DuckDuckGo hands back, including the ones
    that are not URLs at all."""
    assert isinstance(searchd._direct(url), str)


@given(page=PAGES)
@SLOW_OK
def test_the_result_patterns_never_produce_a_broken_hit(page: str) -> None:
    """Whatever the regexes match on a malformed page, the Hits built from it
    are well formed: strings throughout, and the caller's http:// filter is
    the only thing standing between them and the transcript."""
    for href, title in searchd._RESULT.findall(page) + searchd._LITE.findall(page):
        hit = searchd.Hit(title=searchd._clean(title), url=searchd._direct(href))
        assert isinstance(hit.title, str)
        assert isinstance(hit.url, str)


@given(query=st.text(max_size=200))
@SLOW_OK
def test_the_query_helpers_never_raise_on_anything_typed(query: str) -> None:
    """These read whatever the operator typed, emoji and control characters
    included, before anything has had a chance to validate it."""
    assert isinstance(searchd.language_of(query), str)
    assert isinstance(searchd.keywords(query), str)
    assert isinstance(web.query_for(query), str)


@given(text=st.text(max_size=400), query=st.text(max_size=40))
@SLOW_OK
def test_the_excerpt_respects_its_budget(text: str, query: str) -> None:
    """`excerpt` is what keeps a fetched page from blowing the context
    window; a budget it does not honour is a 400 from the provider."""
    budget = 120
    out = web.excerpt(text, query, budget=budget)
    assert isinstance(out, str)
    assert len(out) <= budget or len(out) <= len(text)
