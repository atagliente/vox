"""The little search server VOX runs for itself.

Web mode needs somewhere to search. The choices were a container to install, an
API key to register for, or this: a few hundred lines that VOX starts on
localhost the moment you press F6, with nothing to set up at all.

It speaks the same JSON as SearXNG — ``/search?q=…&format=json`` — so the
client code does not care which is answering, and pointing ``web.endpoint`` at a
real SearXNG later changes nothing else.

Where the results come from, honestly. One general web index — DuckDuckGo's
HTML endpoint, scraped, which is the price of no key and no install, and which
rate-limits anyone who leans on it. Then three documented APIs that need no key
and do not break: Wikipedia, Stack Exchange and Hacker News.

They are asked in that order, and their failures are survivable. When the web
index answers with a captcha page — and it does — the search still returns the
encyclopaedia, the Q&A and the discussion instead of nothing, and says which of
them answered. Only when every upstream fails is the search an error. For
anything you depend on, run a real SearXNG or use a Brave key; both are still
supported and neither changes how the rest of VOX behaves.

The server binds to 127.0.0.1 and nothing else: it is this machine's, not the
network's.
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import http, threads, urls
from .logging_setup import get_logger

log = get_logger("searchd")

HOST = "127.0.0.1"
DEFAULT_PORT = 8888
UPSTREAM_TIMEOUT = 20.0

# A browser's user agent, because the HTML endpoint answers browsers. Saying
# "vox" here gets a redirect to a page that asks you to enable JavaScript.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
WIKIPEDIA = "https://{language}.wikipedia.org/w/api.php"

# Enough of a heuristic to pick an encyclopaedia, and no more. A question in
# Italian belongs on it.wikipedia: sending it to the English one and getting
# nothing is how "what is the postal code of Latiano" became "I have never
# heard of Latiano".
_LANGUAGE_MARKERS = {
    "it": {
        "il",
        "lo",
        "la",
        "gli",
        "che",
        "di",
        "quale",
        "come",
        "dove",
        "perche",
        "perché",
        "qual",
        "cosa",
        "mi",
        "sono",
        "una",
        "del",
    },
    "es": {"el", "los", "las", "que", "cual", "como", "donde", "por", "una"},
    "fr": {"le", "les", "des", "que", "quel", "comment", "pourquoi", "une"},
    "de": {"der", "die", "das", "und", "wie", "warum", "wo", "ist", "eine"},
}

# Words that say how a question was asked rather than what it was about. The
# APIs match titles, so leaving them in is what makes a real question miss.
_NOISE = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "please",
    "tell",
    "than",
    "that",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "che",
    "chi",
    "come",
    "cosa",
    "cos",
    "dammi",
    "del",
    "della",
    "dello",
    "di",
    "dice",
    "dici",
    "dove",
    "e",
    "il",
    "la",
    "le",
    "lo",
    "mi",
    "per",
    "perche",
    "perché",
    "qual",
    "quale",
    "quali",
    "quando",
    "sai",
    "sono",
    "un",
    "una",
    "uno",
    "vorrei",
    "è",
    "e'",
    "sai?",
}
STACKEXCHANGE = "https://api.stackexchange.com/2.3/search/advanced"
HACKERNEWS = "https://hn.algolia.com/api/v1/search"


def language_of(query: str, default: str = "en") -> str:
    """Which Wikipedia to ask. A guess, and only ever a guess."""
    words = {word.strip(".,;:!?").lower() for word in query.split()}
    best, score = default, 0
    for language, markers in _LANGUAGE_MARKERS.items():
        hits = len(words & markers)
        if hits > score:
            best, score = language, hits
    return best if score >= 2 else default


def keywords(query: str, keep: int = 8) -> str:
    """The question with the asking removed, for APIs that match titles."""
    words = [word.strip("¿?¡!.,;:\"'()[]") for word in query.split()]
    kept = [word for word in words if word and word.lower() not in _NOISE]
    return " ".join(kept[:keep]) or query


def port_of(endpoint: str) -> int:
    """The port an endpoint names, defaulting the way a browser would."""
    parsed = urls.split(endpoint)
    return parsed.port or (443 if parsed.scheme == "https" else 80)


class SearchdError(Exception):
    """The upstream did not answer, or answered with something unreadable."""


@dataclass
class Hit:
    title: str
    url: str
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "content": self.content}


# ------------------------------------------------------------------ upstreams


_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    """Tags out, entities in, whitespace collapsed."""
    text = " ".join(html.unescape(_TAGS.sub(" ", fragment)).split())
    # A tag between a word and its comma leaves a space in front of the comma.
    return re.sub(r"\s+([,.;:!?%)\]])", lambda match: match.group(1), text)


def _direct(url: str) -> str:
    """DuckDuckGo wraps every result in a redirect; this unwraps it."""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urls.split(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return url


_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_LITE = re.compile(
    r'<a[^>]+class="[^"]*result-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _post(url: str, query: str) -> str:
    try:
        return http.request(
            url,
            data=urllib.parse.urlencode({"q": query}).encode(),
            headers={
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=UPSTREAM_TIMEOUT,
            retry=http.OPEN_WEB,
            max_bytes=2_000_000,
        ).text()
    except http.HttpError as exc:
        if exc.kind == "http":
            raise SearchdError(
                f"the search endpoint returned HTTP {exc.status}"
            ) from exc
        raise SearchdError(f"cannot reach the search endpoint: {exc.message}") from exc


def duckduckgo(query: str, limit: int = 10) -> list[Hit]:
    """The general web index: the HTML endpoint, then the lite page."""
    blocked = False
    hits: list[Hit] = []
    for url in (DDG_HTML, DDG_LITE):
        try:
            page = _post(url, query)
        except SearchdError as exc:
            # A reset connection is one endpoint's problem, not the search's.
            log.debug("%s: %s", url, exc)
            continue
        snippets = [_clean(fragment) for fragment in _SNIPPET.findall(page)]
        found = [
            Hit(
                title=_clean(title),
                url=_direct(href),
                content=snippets[index] if index < len(snippets) else "",
            )
            for index, (href, title) in enumerate(_RESULT.findall(page))
        ]
        found += [
            Hit(title=_clean(title), url=_direct(href))
            for href, title in _LITE.findall(page)
        ]
        hits.extend(found)
        if found:
            break
        blocked = blocked or "anomaly" in page.lower() or "captcha" in page.lower()

    if not hits:
        raise SearchdError(
            "asked for a captcha; it rate-limits heavy use"
            if blocked
            else "returned nothing readable"
        )
    return [hit for hit in hits if hit.url.startswith("http")][:limit]


def _json(url: str) -> dict:
    """A GET that must return JSON, or say why it did not."""
    try:
        reply = http.request(
            url,
            timeout=UPSTREAM_TIMEOUT,
            retry=http.OPEN_WEB,
            max_bytes=2_000_000,
        )
    except http.HttpError as exc:
        if exc.kind == "http":
            raise SearchdError(f"HTTP {exc.status}") from exc
        raise SearchdError(exc.message) from exc
    try:
        return json.loads(reply.body)
    except ValueError as exc:
        raise SearchdError("the answer was not JSON") from exc


def _wiki_url(language: str, title: str) -> str:
    return f"https://{language}.wikipedia.org/wiki/" + urllib.parse.quote(
        title.replace(" ", "_")
    )


def _wiki_titles(language: str, term: str, limit: int = 3) -> list[str]:
    """Titles that start with ``term``: how you find a place or a person."""
    url = (
        WIKIPEDIA.format(language=language)
        + "?"
        + urllib.parse.urlencode(
            {
                "action": "opensearch",
                "search": term,
                "limit": limit,
                "format": "json",
            }
        )
    )
    payload = _json(url)
    return [str(title) for title in (payload[1] if len(payload) > 1 else [])]


def wikipedia(query: str, limit: int = 3, language: str = "") -> list[Hit]:
    """A documented API with no key, in the language the question was asked in.

    Two lookups, because they fail differently: a title match finds the thing
    being asked about ("latiano" -> Latiano), and a full-text search finds
    articles that discuss it. The title match goes first, since a question
    about a place usually wants that place.
    """
    language = language or language_of(query)
    terms = keywords(query)
    hits: list[Hit] = []
    seen: set[str] = set()

    # A capitalised word is a name, and a name is what a question is about;
    # length is only the fallback. "codice di avviamento postale di Latiano"
    # is about Latiano, not about avviamento.
    words = terms.split()
    candidates = [word for word in words if word[:1].isupper()]
    candidates += sorted(
        (word for word in words if word not in candidates), key=len, reverse=True
    )
    for word in candidates[:3]:
        if len(word) < 4:
            continue
        try:
            titles = _wiki_titles(language, word)
        except SearchdError:
            titles = []
        for title in titles[:2]:
            url = _wiki_url(language, title)
            if url not in seen:
                seen.add(url)
                hits.append(Hit(title=title, url=url, content="Wikipedia article"))
        if hits:
            break

    url = (
        WIKIPEDIA.format(language=language)
        + "?"
        + urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": terms,
                "format": "json",
                "srlimit": limit,
            }
        )
    )
    for item in _json(url).get("query", {}).get("search", []):
        title = str(item.get("title", ""))
        address = _wiki_url(language, title)
        if address not in seen:
            seen.add(address)
            hits.append(
                Hit(
                    title=title,
                    url=address,
                    content=_clean(str(item.get("snippet", ""))),
                )
            )

    if not hits and language != "en":
        # The subject may simply be documented in English instead.
        return wikipedia(terms, limit, "en")
    return hits[:limit]


def stackexchange(query: str, limit: int = 3) -> list[Hit]:
    """Stack Overflow, which is where the answer often actually is."""
    url = (
        STACKEXCHANGE
        + "?"
        + urllib.parse.urlencode(
            {
                "order": "desc",
                "sort": "relevance",
                "q": keywords(query),
                "site": "stackoverflow",
                "pagesize": limit,
            }
        )
    )
    payload = _json(url)
    hits = []
    for item in payload.get("items", []):
        if not item.get("link"):
            continue
        tags = ", ".join(item.get("tags", [])[:5])
        hits.append(
            Hit(
                title=_clean(str(item.get("title", ""))),
                url=str(item["link"]),
                content=(
                    f"{item.get('score', 0)} votes, "
                    f"{item.get('answer_count', 0)} answers"
                    + (", accepted" if item.get("is_answered") else "")
                    + (f" · {tags}" if tags else "")
                ),
            )
        )
    return hits


def hackernews(query: str, limit: int = 3) -> list[Hit]:
    """What was linked and argued about, which dates a thing usefully."""
    url = (
        HACKERNEWS
        + "?"
        + urllib.parse.urlencode(
            {
                "query": keywords(query),
                "hitsPerPage": limit,
                "tags": "story",
            }
        )
    )
    payload = _json(url)
    hits = []
    for item in payload.get("hits", []):
        link = item.get("url") or (
            f"https://news.ycombinator.com/item?id={item.get('objectID')}"
        )
        hits.append(
            Hit(
                title=_clean(str(item.get("title") or item.get("story_title") or link)),
                url=str(link),
                content=(
                    f"{item.get('points', 0)} points, "
                    f"{item.get('num_comments', 0)} comments on Hacker News"
                ),
            )
        )
    return hits


# The general index first, then the ones that always answer.
# Two more general indexes, both keyless and both run by people who are not
# in the surveillance business. They matter because the DuckDuckGo HTML
# endpoint is the one pillar here that is scraped rather than offered, and it
# answers with a captcha under load. Three general indexes means a rate limit
# is a slower search rather than no search.
MOJEEK = "https://www.mojeek.com/search"
MARGINALIA = "https://old-search.marginalia.nu/search"

_MOJEEK_RESULT = re.compile(
    r'<a[^>]+class="[^"]*ob[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_MOJEEK_ANCHOR = re.compile(
    r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
    re.IGNORECASE | re.DOTALL,
)


def _get_html(url: str, params: dict[str, str]) -> str:
    """A GET that returns markup, or says why it did not."""
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        return http.request(
            full,
            headers={"User-Agent": BROWSER_UA},
            timeout=UPSTREAM_TIMEOUT,
            retry=http.OPEN_WEB,
            max_bytes=2_000_000,
        ).text()
    except http.HttpError as exc:
        if exc.kind == "http":
            raise SearchdError(f"HTTP {exc.status}") from exc
        raise SearchdError(exc.message) from exc


def mojeek(query: str, limit: int = 10) -> list[Hit]:
    """An independent index with its own crawler. No key, no rate limit worth
    the name, and markup that has stayed the same for years."""
    page = _get_html(MOJEEK, {"q": query})
    found = _MOJEEK_ANCHOR.findall(page) or _MOJEEK_RESULT.findall(page)
    hits = [
        Hit(title=_clean(title), url=_direct(href))
        for href, title in found
        if href.startswith("http")
    ]
    if not hits:
        raise SearchdError("returned nothing readable")
    return hits[:limit]


def marginalia(query: str, limit: int = 5) -> list[Hit]:
    """A small index of the non-commercial web.

    Deliberately last and deliberately few: it finds things the big indexes
    bury, and finds nothing at all for most ordinary queries. Worth asking,
    not worth waiting on.
    """
    page = _get_html(MARGINALIA, {"query": query})
    hits = [
        Hit(title=_clean(title), url=_direct(href))
        for href, title in _MOJEEK_ANCHOR.findall(page)
        if href.startswith("http")
    ]
    if not hits:
        raise SearchdError("returned nothing readable")
    return hits[:limit]


# Asked in order, and the search stops once it has enough. DuckDuckGo first
# because it is the broadest; the other two general indexes are what make a
# captcha from it a slower answer rather than no answer.
UPSTREAMS = (
    ("web", duckduckgo),
    ("wikipedia", wikipedia),
    ("mojeek", mojeek),
    ("stackoverflow", stackexchange),
    ("hackernews", hackernews),
    ("marginalia", marginalia),
)


def search(query: str, limit: int = 10) -> tuple[list[Hit], list[str], list[str]]:
    """Ask every upstream and survive the ones that fail.

    Returns the hits, which upstreams answered, and what the others said. Only
    a search where nothing at all answered is an error: one blocked index must
    not cost the operator the encyclopaedia.
    """
    hits: list[Hit] = []
    seen: set[str] = set()
    answered: list[str] = []
    failures: list[str] = []

    for name, upstream in UPSTREAMS:
        if len(hits) >= limit:
            break
        try:
            found = upstream(query, limit if name == "web" else 3)
        except SearchdError as exc:
            failures.append(f"{name}: {exc}")
            continue
        except Exception as exc:
            # Wide on purpose: each upstream is a different site's HTML or
            # JSON, and one of them breaking in a new way is a reason to try
            # the next rather than to fail the search.
            failures.append(f"{name}: {exc.__class__.__name__}")
            continue
        new = [hit for hit in found if hit.url and hit.url not in seen]
        if not new:
            continue
        answered.append(name)
        for hit in new:
            seen.add(hit.url)
            hits.append(hit)

    if not hits:
        detail = "; ".join(failures)
        raise SearchdError(f"nothing found ({detail})" if detail else "nothing found")
    return hits[:limit], answered, failures


# --------------------------------------------------------------------- server


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR means "bind even if somebody already has this
    # port", which would let VOX quietly steal a port and answer half the
    # requests. Elsewhere it means "reuse a TIME_WAIT socket", which is what
    # makes a restart work, so it stays on there.
    allow_reuse_address = sys.platform != "win32"


class _Handler(BaseHTTPRequestHandler):
    server_version = "vox-searchd"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path.rstrip("/") not in ("/search", ""):
            self._json(404, {"error": "only /search"})
            return
        query = (params.get("q") or [""])[0].strip()
        if not query:
            self._json(400, {"error": "no q"})
            return
        try:
            hits, answered, failures = search(
                query, int((params.get("count") or ["10"])[0])
            )
        except SearchdError as exc:
            # 502: the upstreams failed, not this server. The client shows it.
            self._json(502, {"error": str(exc)})
            return
        self._json(
            200,
            {
                "query": query,
                "results": [hit.to_dict() for hit in hits],
                # Which answered, so a blocked web index is visible rather than
                # looking like a thin day on the internet.
                "sources": answered,
                "failures": failures,
            },
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        log.debug("searchd %s", format % args)


def answering(endpoint: str, timeout: float = 3.0) -> bool:
    """Is something already serving searches there?

    Another VOX, or a SearXNG of your own: either way there is no reason to
    bind the port a second time and every reason not to.
    """
    url = (
        endpoint.rstrip("/")
        + "/search?"
        + urllib.parse.urlencode({"q": "vox", "format": "json"})
    )
    try:
        json.loads(http.request(url, timeout=timeout, max_bytes=200_000).body)
        return True
    except (http.HttpError, ValueError):
        return False


class LocalSearch:
    """The server, owned by whoever started it."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        if self._server is not None:
            return f"already listening on {HOST}:{self.port}"
        try:
            self._server = _Server((HOST, self.port), _Handler)
        except OSError as exc:
            raise SearchdError(
                f"cannot listen on {HOST}:{self.port}: {exc}. Something else may "
                "be using that port; change web.endpoint"
            ) from exc
        self._thread = threads.serve(self._server, "vox-searchd")
        log.info("search server listening on %s:%d", HOST, self.port)
        return f"listening on {HOST}:{self.port}"

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        log.info("search server stopped")
