"""The little search server VOX runs for itself.

Web mode needs somewhere to search. The choices were a container to install, an
API key to register for, or this: a few hundred lines that VOX starts on
localhost the moment you press F6, with nothing to set up at all.

It speaks the same JSON as SearXNG — ``/search?q=…&format=json`` — so the
client code does not care which is answering, and pointing ``web.endpoint`` at a
real SearXNG later changes nothing else.

Where the results come from, honestly: DuckDuckGo's HTML endpoint, parsed, plus
Wikipedia's documented API. That is scraping, and it is the price of no key and
no container. It can break when their markup changes, it is rate-limited, and
when it does break VOX says so rather than returning nothing and looking
confident. For anything you depend on, run a real SearXNG or use a Brave key —
both are still supported and neither changes how the rest of VOX behaves.

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
    """Results from the HTML endpoint, with the lite page as a second try."""
    page = _post(DDG_HTML, query)
    snippets = [_clean(s) for s in _SNIPPET.findall(page)]
    hits = [
        Hit(title=_clean(title), url=_direct(href),
            content=snippets[index] if index < len(snippets) else "")
        for index, (href, title) in enumerate(_RESULT.findall(page))
    ]
    if not hits:
        page = _post(DDG_LITE, query)
        hits = [
            Hit(title=_clean(title), url=_direct(href))
            for href, title in _LITE.findall(page)
        ]
    if not hits and ("anomaly" in page.lower() or "captcha" in page.lower()):
        raise SearchdError(
            "the search endpoint asked for a captcha; it rate-limits heavy use. "
            "Wait a little, or configure a Brave key or your own SearXNG"
        )
    return [hit for hit in hits if hit.url.startswith("http")][:limit]


def wikipedia(query: str, limit: int = 3) -> list[Hit]:
    """A documented API with no key, good at exactly what it is good at."""
    url = WIKIPEDIA + "?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": limit,
    })
    request = urllib.request.Request(url, headers={"User-Agent": "vox/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
            payload = json.loads(response.read(1_000_000))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        log.debug("wikipedia: %s", exc)
        return []
    return [
        Hit(
            title=str(item.get("title", "")),
            url="https://en.wikipedia.org/wiki/"
                + urllib.parse.quote(str(item.get("title", "")).replace(" ", "_")),
            content=_clean(str(item.get("snippet", ""))),
        )
        for item in payload.get("query", {}).get("search", [])
    ]


def search(query: str, limit: int = 10) -> list[Hit]:
    """Everything the local server knows how to ask, in one list."""
    hits = duckduckgo(query, limit)
    seen = {hit.url for hit in hits}
    for hit in wikipedia(query):
        if hit.url not in seen and len(hits) < limit:
            hits.append(hit)
    return hits


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
            hits = search(query, int((params.get("count") or ["10"])[0]))
        except SearchdError as exc:
            # 502: the upstream failed, not this server. The client shows it.
            self._json(502, {"error": str(exc)})
            return
        self._json(200, {"query": query, "results": [hit.to_dict() for hit in hits]})

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
