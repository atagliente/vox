"""The one place VOX makes an HTTP request.

It used to be seventeen places across ``web.py``, ``searchd.py`` and
``ollama.py``, each with its own idea of timeouts, redirects, decoding, byte
caps and which exceptions were worth catching. That is the kind of spread
where one call quietly grows a behaviour the others do not have, and nobody
finds out until a provider misbehaves.

So: one function that performs a request, one exception with a typed ``kind``,
and one retry policy. Callers still raise their own errors — ``OllamaError``,
``SearchdError``, ``WebError`` say something the operator can act on, and a
bare HTTP failure does not — but they translate a single shape rather than
each catching its own set.

Nothing here decides *whether* a request may be made. Refusing private
addresses is ``web.check_url``'s job and stays there, because it is a policy
about the open web rather than a property of HTTP.
"""

from __future__ import annotations

import contextlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

from .logging_setup import get_logger

log = get_logger("http")

Kind = Literal["connection", "timeout", "http", "protocol"]

USER_AGENT = "vox/0.1 (+https://github.com/atagliente/vox)"

# Statuses worth trying again: the server said it is busy or briefly broken,
# not that the request was wrong. A 4xx other than 429 is answered by fixing
# the request, so repeating it only wastes the operator's time.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """A request that did not produce an answer, and what kind of failure it was.

    ``kind`` is what a caller switches on; ``status`` and ``body`` carry the
    parts worth putting in a message, because "HTTP 502" on its own throws
    away the only useful thing a JSON error body had to say.
    """

    def __init__(
        self,
        kind: Kind,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.kind: Kind = kind
        self.message = message
        self.status = status
        self.body = body
        self.url = url


@dataclass(frozen=True)
class Retry:
    """How many times to try, and how long to wait between attempts.

    One policy, written down once. The default is a single attempt, because
    that is what every call site did before this module existed and changing
    the number of requests VOX makes is a decision to take deliberately, per
    caller, not to acquire by refactoring.
    """

    tries: int = 1
    backoff: float = 0.0
    on_status: frozenset[int] = field(default=TRANSIENT_STATUS)
    # A server that said "busy" will likely be free in a moment. A refused
    # connection to a fixed address will not, and repeating it only makes the
    # operator wait longer to be told the same thing, so it is off by default.
    on_connection: bool = False

    def sleep_before(self, attempt: int) -> float:
        """Seconds to wait before attempt number ``attempt`` (1-based)."""
        return self.backoff * (2 ** (attempt - 2)) if attempt > 1 else 0.0


NO_RETRY = Retry()
# For the open web, where a rate limit or a tired edge node is ordinary. The
# search backends fall through to one another, so a refusal is answered by
# trying the next endpoint rather than the same one again.
OPEN_WEB = Retry(tries=3, backoff=0.5)


@dataclass
class Reply:
    """What came back, already read."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    content_type: str = ""
    charset: str = "utf-8"

    def text(self, errors: str = "replace") -> str:
        return self.body.decode(self.charset, errors)


# The only schemes this will open. urlopen speaks file:, ftp: and data: too,
# and a URL that reaches here from a model, a search result or an MCP server
# is not one to hand those to: file:///etc/passwd is a perfectly good URL.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    retry: Retry = NO_RETRY,
    max_bytes: int | None = None,
    method: str | None = None,
) -> Reply:
    """Make one request and read the answer, or raise :class:`HttpError`.

    ``max_bytes`` is a cap on what is read, not a promise about what the
    server sends: one extra byte is read past it so the caller can tell a
    body that fitted from one that was cut off.
    """
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise HttpError(
            "protocol",
            f"{scheme or 'that'} is not a scheme VOX will open; only http and https",
            url=url,
        )
    sent = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            # No decompression to write, or to get wrong.
            "Accept-Encoding": "identity",
            **(headers or {}),
        },
    )
    last: HttpError | None = None
    for attempt in range(1, max(1, retry.tries) + 1):
        pause = retry.sleep_before(attempt)
        if pause:
            time.sleep(pause)
        try:
            return _once(sent, url, timeout, max_bytes)
        except HttpError as exc:
            last = exc
            retryable = (exc.status is not None and exc.status in retry.on_status) or (
                retry.on_connection and exc.kind in ("connection", "timeout")
            )
            if not retryable or attempt == retry.tries:
                raise
            log.debug("retrying %s after %s (attempt %d)", url, exc.message, attempt)
    raise last if last else HttpError("connection", f"cannot reach {url}", url=url)


def _once(
    sent: urllib.request.Request,
    url: str,
    timeout: float,
    max_bytes: int | None,
) -> Reply:
    try:
        # nosec B310 - request() refuses every scheme but http and https
        # before anything reaches here; that check is the answer to this.
        with urllib.request.urlopen(sent, timeout=timeout) as response:  # nosec B310
            raw = response.read() if max_bytes is None else response.read(max_bytes + 1)
            headers = {k.lower(): v for k, v in response.headers.items()}
            content_type = (
                (response.headers.get("Content-Type") or "").split(";")[0].strip()
            )
            return Reply(
                url=response.geturl(),
                status=getattr(response, "status", 200) or 200,
                headers=headers,
                body=raw,
                content_type=content_type,
                charset=response.headers.get_content_charset() or "utf-8",
            )
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(OSError):  # the body is optional
            body = exc.read(10_000).decode("utf-8", "replace")
        raise HttpError(
            "http",
            f"{url} returned HTTP {exc.code}",
            status=exc.code,
            body=body,
            url=url,
        ) from exc
    except TimeoutError as exc:
        raise HttpError("timeout", f"{url} timed out", url=url) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise HttpError("timeout", f"{url} timed out", url=url) from exc
        raise HttpError("connection", f"cannot reach {url}: {reason}", url=url) from exc
    except OSError as exc:  # pragma: no cover - platform specific
        raise HttpError("connection", f"cannot reach {url}: {exc}", url=url) from exc
