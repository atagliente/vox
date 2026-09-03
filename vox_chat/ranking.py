"""Deciding which results are worth reading, and noticing when two are the same.

A search comes back with ten links in whatever order the index felt like, and
web mode reads the first two. Which means the answer depends on an ordering
nobody chose for this question — and on two of the ten being different pages,
which they often are not: a Stack Overflow answer, its mirror, and a site that
scraped both.

Two small pieces of arithmetic, both deliberately simple:

- **Ranking.** Term overlap with the question, weighted towards the title,
  with a nudge for a source that has earned one. No embeddings: this runs
  before anything has been fetched, on ten titles and snippets, and it has to
  cost nothing.
- **Deduplication.** Same URL after normalising, same host and title, or
  snippets that are largely the same words. The third is what catches a
  mirror, and it is why this is not just a set of URLs.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from . import urls

WORD = re.compile(r"[\w']+", re.UNICODE)

# Words that appear in every question and rank nothing.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "che",
        "cosa",
        "come",
        "dove",
        "di",
        "da",
        "e",
        "il",
        "la",
        "le",
        "lo",
        "per",
        "un",
        "una",
        "che",
        "quando",
        "quale",
    ]
)

# Hosts whose pages are worth reading for a technical question more often than
# the rank alone suggests. A nudge, not an override: two places, both earned.
TRUSTED = (
    "wikipedia.org",
    "stackoverflow.com",
    "docs.python.org",
    "developer.mozilla.org",
    "github.com",
)

# Query parameters that identify a campaign rather than a page.
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$|_hs)", re.IGNORECASE)


def words(text: str) -> set[str]:
    """The words worth matching on."""
    return {
        word.lower()
        for word in WORD.findall(text or "")
        if len(word) > 2 and word.lower() not in STOPWORDS
    }


def normalise(url: str) -> str:
    """The same page, written the same way.

    Scheme and fragment dropped, tracking parameters dropped, trailing slash
    dropped, host lowercased and stripped of ``www.``. Two links that differ
    only in those are one page, and reading both spends a fetch on nothing.
    """
    parsed = urls.split(url)
    host = parsed.netloc.lower().removeprefix("www.")
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not TRACKING.match(key)
    ]
    path = parsed.path.rstrip("/") or "/"
    rebuilt = host + path
    if query:
        rebuilt += "?" + urllib.parse.urlencode(sorted(query))
    return rebuilt


def overlap(a: set[str], b: set[str]) -> float:
    """How much two bags of words have in common, 0 to 1."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class Scored:
    """One result and what it scored, so the reasoning can be shown."""

    result: object
    score: float
    why: str


def score(result: object, question: str) -> Scored:
    """How promising this result looks for this question."""
    wanted = words(question)
    title = words(getattr(result, "title", ""))
    snippet = words(getattr(result, "snippet", ""))
    url = str(getattr(result, "url", ""))

    # The title is what the page says it is about; the snippet is the index's
    # guess at the relevant part. Both matter, the title more.
    value = overlap(wanted, title) * 2.0 + overlap(wanted, snippet)
    reasons = []
    if title & wanted:
        reasons.append("title matches")
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(host.endswith(name) for name in TRUSTED):
        value += 0.25
        reasons.append("known source")
    if not reasons:
        reasons.append("no overlap")
    return Scored(result, round(value, 3), ", ".join(reasons))


def rank(results: list, question: str) -> list:
    """Best first, ties in the order the index gave them.

    A stable sort, so a result the index thought was first stays ahead of an
    equally-scoring one it thought was eighth: where the arithmetic has
    nothing to say, the index's own judgement is better than none.
    """
    scored = [score(result, question) for result in results]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i].score, i))
    return [scored[i].result for i in order]


# A snippet shorter than this says too little to be evidence of anything. Two
# results reading "snippet 1" and "snippet 2" share every word they have and
# are not the same page; a mirror has a paragraph, not a label.
MIN_SNIPPET_WORDS = 6


def deduplicate(results: list, threshold: float = 0.8) -> list:
    """Drop the ones that are the same page said twice.

    Three ways of being the same: the same URL once normalised, the same host
    and title, or snippets made of largely the same words. The last catches a
    mirror, which is why this is not simply a set of URLs.
    """
    kept: list = []
    seen_urls: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    for result in results:
        url = normalise(str(getattr(result, "url", "")))
        if not url or url in seen_urls:
            continue
        host = urllib.parse.urlparse(str(getattr(result, "url", ""))).netloc.lower()
        title = " ".join(sorted(words(getattr(result, "title", ""))))
        if title and (host, title) in seen_titles:
            continue
        snippet = words(getattr(result, "snippet", ""))
        if len(snippet) >= MIN_SNIPPET_WORDS and any(
            overlap(snippet, words(getattr(other, "snippet", ""))) >= threshold
            for other in kept
            if len(words(getattr(other, "snippet", ""))) >= MIN_SNIPPET_WORDS
        ):
            continue
        seen_urls.add(url)
        if title:
            seen_titles.add((host, title))
        kept.append(result)
    return kept


# ------------------------------------------------------- was that enough?


# Below this the index found almost nothing, whatever the words say.
FEW_RESULTS = 3


def thin(results: list, question: str, floor: float = 0.15) -> bool:
    """Did the search come back with nothing worth reading?

    Deliberately hard to satisfy. The obvious rule — "nothing scores against
    the question" — is wrong, and wrong in the case that matters: ask about
    *ring buffers* and the right answer is titled *Circular buffer*, which
    shares not one word with the question. Retrying there would throw away
    the correct result and pay a round trip for the privilege.

    So the count comes first. No results is a failed search; a handful of
    results none of which mentions anything asked about is a failed search;
    ten results that happen to use different words is a search that worked.
    """
    if not results:
        return True
    if len(results) >= FEW_RESULTS:
        return False
    return max(score(result, question).score for result in results) < floor


def refine(question: str, tried: list[str]) -> str:
    """A different query for the same question, or "" when there is none.

    Not a model call. A second search that needs the model to write it costs
    a whole turn of latency for a case that is usually the index having a bad
    minute, so this is arithmetic: drop the least useful words, and if that
    has been tried, quote the most distinctive pair.
    """
    terms = [word for word in WORD.findall(question) if word.lower() not in STOPWORDS]
    if len(terms) < 2:
        return ""
    shorter = " ".join(terms[:4])
    if shorter and shorter not in tried:
        return shorter
    longest = sorted(terms, key=len, reverse=True)[:2]
    quoted = " ".join(f'"{term}"' for term in longest)
    return quoted if quoted not in tried else ""
