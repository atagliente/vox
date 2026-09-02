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
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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
WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
STACKEXCHANGE = "https://api.stackexchange.com/2.3/search/advanced"
HACKERNEWS = "https://hn.algolia.com/api/v1/search"


def port_of(endpoint: str) -> int:
    """The port an endpoint names, defaulting the way a browser would."""
    parsed = urllib.parse.urlparse(endpoint)
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
    parsed = urllib.parse.urlparse(url)
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
    data = urllib.parse.urlencode({"q": query}).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": BROWSER_UA,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
            return response.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise SearchdError(f"the search endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SearchdError(f"cannot reach the search endpoint: {exc}") from exc


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
            Hit(title=_clean(title), url=_direct(href),
                content=snippets[index] if index < len(snippets) else "")
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
            "asked for a captcha; it rate-limits heavy use" if blocked
            else "returned nothing readable"
        )
    return [hit for hit in hits if hit.url.startswith("http")][:limit]


def _json(url: str) -> dict:
    """A GET that must return JSON, or say why it did not."""
    request = urllib.request.Request(url, headers={"User-Agent": "vox/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
            return json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        raise SearchdError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SearchdError(str(getattr(exc, "reason", exc))) from exc
    except ValueError as exc:
        raise SearchdError("the answer was not JSON") from exc


def wikipedia(query: str, limit: int = 3) -> list[Hit]:
    """A documented API with no key, good at exactly what it is good at."""
    url = WIKIPEDIA + "?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": limit,
    })
    payload = _json(url)
    return [
        Hit(
            title=str(item.get("title", "")),
            url="https://en.wikipedia.org/wiki/"
                + urllib.parse.quote(str(item.get("title", "")).replace(" ", "_")),
            content=_clean(str(item.get("snippet", ""))),
        )
        for item in payload.get("query", {}).get("search", [])
    ]


def stackexchange(query: str, limit: int = 3) -> list[Hit]:
    """Stack Overflow, which is where the answer often actually is."""
    url = STACKEXCHANGE + "?" + urllib.parse.urlencode({
        "order": "desc", "sort": "relevance", "q": query,
        "site": "stackoverflow", "pagesize": limit,
    })
    payload = _json(url)
    hits = []
    for item in payload.get("items", []):
        if not item.get("link"):
            continue
        tags = ", ".join(item.get("tags", [])[:5])
        hits.append(Hit(
            title=_clean(str(item.get("title", ""))),
            url=str(item["link"]),
            content=(f"{item.get('score', 0)} votes, "
                     f"{item.get('answer_count', 0)} answers"
                     + (", accepted" if item.get("is_answered") else "")
                     + (f" · {tags}" if tags else "")),
        ))
    return hits


def hackernews(query: str, limit: int = 3) -> list[Hit]:
    """What was linked and argued about, which dates a thing usefully."""
    url = HACKERNEWS + "?" + urllib.parse.urlencode({
        "query": query, "hitsPerPage": limit, "tags": "story",
    })
    payload = _json(url)
    hits = []
    for item in payload.get("hits", []):
        link = item.get("url") or (
            f"https://news.ycombinator.com/item?id={item.get('objectID')}"
        )
        hits.append(Hit(
            title=_clean(str(item.get("title") or item.get("story_title") or link)),
            url=str(link),
            content=(f"{item.get('points', 0)} points, "
                     f"{item.get('num_comments', 0)} comments on Hacker News"),
        ))
    return hits


# The general index first, then the ones that always answer.
UPSTREAMS = (
    ("web", duckduckgo),
    ("wikipedia", wikipedia),
    ("stackoverflow", stackexchange),
    ("hackernews", hackernews),
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
        except Exception as exc:  # noqa: BLE001 - an upstream is not our code
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
        raise SearchdError("; ".join(failures) or "no upstream returned anything")
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

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
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
        self._json(200, {
            "query": query,
            "results": [hit.to_dict() for hit in hits],
            # Which answered, so a blocked web index is visible rather than
            # looking like a thin day on the internet.
            "sources": answered,
            "failures": failures,
        })

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("searchd %s", format % args)


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
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="vox-searchd", daemon=True
        )
        self._thread.start()
        log.info("search server listening on %s:%d", HOST, self.port)
        return f"listening on {HOST}:{self.port}"

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        log.info("search server stopped")
