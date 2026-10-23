"""Torznab client (Jackett, Prowlarr, NZB-style APIs that speak Torznab): one provider per endpoint.

``GET {url}?t=search&q=…&apikey=…&offset=&limit=`` answers an RSS feed whose items carry
``torznab:attr`` elements (seeders, peers, size, infohash, magneturl) and an ``enclosure``.
Parsed with the standard library only; documents declaring DTDs/entities are refused.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import aiohttp

from .model import HEX40, Provider, ProviderError, Result, to_int

MAX_BODY = 5 * 1024 * 1024
CATEGORIES = {"games": "4000,1000"}   # Torznab: PC, Console


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean_url(url: str) -> tuple[str, str]:
    """Validate an http(s) endpoint; returns (url without apikey, apikey found in it or '')."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc or not u.hostname:
        raise ValueError("The URL must start with http:// or https://")
    q = parse_qsl(u.query, keep_blank_values=True)
    key = next((v for k, v in q if k.lower() == "apikey"), "")
    rest = [(k, v) for k, v in q if k.lower() not in ("apikey", "t", "q", "offset", "limit")]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(rest), "")), key


def magnet_from_hash(h: str, name: str) -> str:
    return "magnet:?xt=urn:btih:%s&dn=%s" % (h, quote_plus(name))


def parse(xml_text: str, name: str, secret: str = "") -> list[Result]:
    """Torznab RSS → Results (raises ProviderError for an error document or garbage)."""
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.I):
        raise ProviderError("malformed", "the answer is not a plain Torznab feed")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise ProviderError("malformed", "the answer is not valid XML") from err
    if local(root.tag) == "error":
        code, desc = root.get("code", ""), root.get("description", "") or "error"
        if code.startswith("1"):
            desc = "%s (check the API key)" % desc
        raise ProviderError("error", "Torznab %s: %s" % (code, desc) if code else desc)
    if local(root.tag) != "rss":
        raise ProviderError("malformed", "unexpected answer shape")
    out: list[Result] = []
    for item in root.iter("item"):
        attrs: dict[str, str] = {}
        enc = link = title = size_tag = None
        for ch in item:
            n = local(ch.tag)
            if n == "title":
                title = (ch.text or "").strip()
            elif n == "link":
                link = (ch.text or "").strip()
            elif n == "size":
                size_tag = ch.text
            elif n == "enclosure":
                enc = ch
            elif n == "attr" and ch.get("name"):
                attrs.setdefault(ch.get("name"), ch.get("value") or "")
        if not title:
            continue
        h = (attrs.get("infohash") or "").strip().lower()
        h = h if HEX40.match(h) else None
        magnet = attrs.get("magneturl") or (link if link and link.startswith("magnet:?") else None)
        if not magnet and h:
            magnet = magnet_from_hash(h, title)
        url = (enc.get("url") if enc is not None else None) or (link if link and link.startswith("http") else None)
        if secret and url and secret in url:   # a download link carrying the key: only if nothing else works
            url = None if magnet else url
        seeders = to_int(attrs.get("seeders"))
        leechers = to_int(attrs.get("leechers"))
        if leechers is None and seeders is not None and to_int(attrs.get("peers")) is not None:
            leechers = max(to_int(attrs["peers"]) - seeders, 0)
        size = to_int(attrs.get("size")) or to_int(size_tag) or (to_int(enc.get("length")) if enc is not None else None)
        out.append(Result(title=title, source=name, size=size or None, seeders=seeders, leechers=leechers,
                          magnet=magnet, url=url, hash=h, rank=len(out)))
    return out


class TorznabProvider(Provider):
    kind = "torznab"

    def __init__(self, id: str, name: str, url: str, key: str = ""):
        self.id, self.name, self.url, self.key = id, name, url, key

    def scrub(self, text: str) -> str:
        t = str(text)
        return t.replace(self.key, "***") if self.key else t

    async def search(self, session, query, page, page_size, timeout, category=""):
        params = {"t": "search", "q": query, "offset": max(page - 1, 0) * page_size, "limit": page_size}
        if CATEGORIES.get(category):
            params["cat"] = CATEGORIES[category]
        if self.key:
            params["apikey"] = self.key
        try:
            async with session.get(self.url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status in (429, 503):
                    raise ProviderError("rate_limited", "rate limited (HTTP %d)" % resp.status,
                                        min(to_int(resp.headers.get("Retry-After")) or 30, 300))
                if resp.status in (401, 403):
                    raise ProviderError("error", "HTTP %d (check the API key)" % resp.status)
                if resp.status >= 400:
                    raise ProviderError("unavailable", "HTTP %d" % resp.status)
                text = await resp.text(errors="replace")
                if len(text) > MAX_BODY:
                    raise ProviderError("malformed", "the answer is too large")
        except asyncio.TimeoutError as err:
            raise ProviderError("timeout", "no answer within %ds" % timeout) from err
        except aiohttp.ClientError as err:
            raise ProviderError("unavailable", self.scrub(str(err) or type(err).__name__)) from err
        return parse(text, self.name, self.key)
