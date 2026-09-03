"""Asking a site whether it wants to be read, before reading it.

`robots.txt` is a request, not a fence — nothing stops a client ignoring it,
and plenty do. That is exactly why honouring it is worth doing: a tool that
fetches pages on somebody's behalf, from servers that are paying for the
bandwidth, should behave the way it would want a tool fetching from it to
behave.

It applies to `fetch` and not to search. A search endpoint is being used the
way it is meant to be; a page fetch is this program reading somebody's site.

Overridable, because there are honest reasons: your own site, a staging
server, a robots.txt that is broken in a way its owner does not know about.
`web.respect_robots: false` turns it off, and the setting says what it means.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field

from . import http
from .logging_setup import get_logger

log = get_logger("robots")

USER_AGENT = "vox"

# A robots.txt is small and changes rarely; asking once an hour per host is
# already generous, and asking per fetch would double every request VOX makes.
TTL = 3600.0

# A file this large is not a robots.txt, and reading more of it would be a
# denial of service somebody could point at VOX.
MAX_BYTES = 512 * 1024


@dataclass
class _Cached:
    parser: urllib.robotparser.RobotFileParser | None
    taken_at: float = field(default_factory=time.time)

    @property
    def stale(self) -> bool:
        return time.time() - self.taken_at > TTL


class RobotsCache:
    """One robots.txt per host, remembered for an hour."""

    def __init__(self) -> None:
        self._hosts: dict[str, _Cached] = {}

    def allows(self, url: str, timeout: float = 10.0) -> bool:
        """May this URL be fetched?

        Anything that goes wrong answers **yes**. A host that cannot serve a
        robots.txt has not refused anything, and treating an unreachable file
        as a refusal would make an outage look like a policy.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return True
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._hosts.get(origin)
        if cached is None or cached.stale:
            cached = _Cached(self._fetch(origin, timeout))
            self._hosts[origin] = cached
        if cached.parser is None:
            return True
        return bool(cached.parser.can_fetch(USER_AGENT, url))

    def _fetch(self, origin: str, timeout: float):
        try:
            reply = http.request(
                f"{origin}/robots.txt", timeout=timeout, max_bytes=MAX_BYTES
            )
        except http.HttpError as exc:
            # 404 is the ordinary case and means everything is allowed. Any
            # other failure is the host's problem, not a refusal.
            log.debug("no robots.txt for %s: %s", origin, exc.message)
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            parser.parse(reply.text().splitlines())
        except Exception:  # pragma: no cover - the stdlib parser is forgiving
            # Wide on purpose: a malformed robots.txt is the host's mistake,
            # and refusing to fetch anything from them over it would punish
            # the wrong party.
            return None
        return parser

    def forget(self) -> None:
        self._hosts.clear()
