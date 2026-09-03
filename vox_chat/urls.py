"""Taking a URL apart without trusting it to be one.

``urllib.parse.urlparse`` raises. That is easy to forget, because on Python
3.12 it mostly does not: give it ``//[`` and 3.12 hands back a result while
3.11 raises ``ValueError("Invalid IPv6 URL")``. Every URL VOX parses arrives
from somewhere it does not control — a model's tool call, an href in scraped
search results, an MCP server's reply — so "mostly does not" is the wrong
guarantee to build on, and the interpreter deciding it is worse.

So: one parse, one behaviour, on every version. A string that is not a URL
comes back as an empty result rather than an exception, and the caller
decides what to do about a URL with no scheme and no host — which it had to
decide anyway.
"""

from __future__ import annotations

import urllib.parse

EMPTY = urllib.parse.SplitResult("", "", "", "", "")


def split(url: str) -> urllib.parse.SplitResult:
    """Parse ``url``, or return an empty result if it cannot be parsed."""
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        # Malformed beyond parsing — an unclosed IPv6 bracket, a port that is
        # not a number. Not a URL, and saying so is the caller's job.
        return EMPTY


def host_of(url: str) -> str:
    """The hostname, lowercased, or "" when there is not one."""
    try:
        return (split(url).hostname or "").lower()
    except ValueError:  # pragma: no cover - hostname re-parses the netloc
        return ""


def netloc_of(url: str) -> str:
    """The host and port as written, or "" when there is not one."""
    return split(url).netloc
