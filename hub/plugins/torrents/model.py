"""Common result schema, error type and the HTTP helper every provider uses."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urlparse

import aiohttp

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


class ProviderError(Exception):
    """A provider failed. ``kind``: timeout | rate_limited | unavailable | malformed | error."""

    def __init__(self, kind: str, message: str, retry_after: float = 0):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


@dataclass
class Result:
    title: str
    source: str = ""
    size: int | None = None        # bytes
    seeders: int | None = None     # None = the source doesn't say
    leechers: int | None = None
    magnet: str | None = None
    url: str | None = None         # torrent file or details page
    hash: str | None = None        # lowercase hex infohash, when known
    sources: list = field(default_factory=list)
    rank: int = 0                  # position inside its provider's answer (relevance)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.key()
        return d

    def key(self) -> str:
        if self.hash:
            return "h:" + self.hash
        if self.size:
            return "t:%s:%d" % (norm_title(self.title), self.size)
        return "u:" + (self.url or self.magnet or norm_title(self.title))


def norm_title(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def to_int(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def clean(r: Result) -> Result | None:
    """Validate and coerce a provider's result; None if it is unusable."""
    if not isinstance(r.title, str) or not r.title.strip():
        return None
    r.title = " ".join(r.title.split())[:300]
    r.size, r.seeders, r.leechers = to_int(r.size), to_int(r.seeders), to_int(r.leechers)
    if r.magnet and not (isinstance(r.magnet, str) and r.magnet.startswith("magnet:?")):
        r.magnet = None
    if r.url and not (isinstance(r.url, str) and urlparse(r.url).scheme in ("http", "https")):
        r.url = None
    if r.magnet and not r.hash:
        for xt in parse_qs(urlparse(r.magnet).query).get("xt", []):
            if xt.lower().startswith("urn:btih:") and HEX40.match(xt[9:]):
                r.hash = xt[9:].lower()
    if r.hash and not HEX40.match(r.hash):
        r.hash = None
    elif r.hash:
        r.hash = r.hash.lower()
    if not (r.magnet or r.url):
        return None
    r.sources = [r.source] if r.source else []
    return r


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None,
                     timeout: float = 10, headers: dict | None = None):
    """GET → parsed JSON, or a ProviderError with a precise ``kind``."""
    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status in (429, 503):
                ra = to_int(resp.headers.get("Retry-After")) or 30
                raise ProviderError("rate_limited", "rate limited (HTTP %d)" % resp.status, min(ra, 300))
            if resp.status >= 400:
                raise ProviderError("unavailable", "HTTP %d" % resp.status)
            try:
                return await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise ProviderError("malformed", "the answer is not valid JSON") from err
    except asyncio.TimeoutError as err:
        raise ProviderError("timeout", "no answer within %ds" % timeout) from err
    except aiohttp.ClientError as err:
        raise ProviderError("unavailable", str(err) or type(err).__name__) from err


class Provider:
    """Base class for a search source. Subclass it, set ``id``/``name``, implement ``search``.

    ``search`` returns raw ``Result`` objects (``clean`` is applied by the engine), best match
    first, for page ``page`` (1-based) of ``page_size`` items, and raises ``ProviderError``.
    A module exports its class as ``SOURCE``. See docs/torrents.md.
    """

    id = ""
    name = ""
    kind = ""   # builtin | file | media | torznab (set by the loader)

    async def search(self, session, query: str, page: int, page_size: int,
                     timeout: float) -> list[Result]:
        raise NotImplementedError
