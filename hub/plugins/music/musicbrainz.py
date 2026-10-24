"""MusicBrainz (metadata) + Cover Art Archive (covers): open, keyless. One request per second, cached."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import os
import re
import time
import unicodedata
from pathlib import Path

import aiohttp

from ...i18n import _

log = logging.getLogger("hub.music")

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org/release-group/%s/front-250"
USER_AGENT = "Casa/1.0 ( https://github.com/skowrons94/HomeAI )"
GAP = 1.05                       # seconds between MusicBrainz requests, process-wide
TTL = 7 * 86400
TTL_SEARCH = 86400
MAX_IMAGE = 1024 * 1024
CACHE_CAP = 120 * 1024 * 1024
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")



async def read_capped(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    """The whole body, up to ``limit`` bytes. (``content.read(n)`` returns only what has arrived so far:
    images came back cut off, a grey block under the first rows.)"""
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(1 << 16):
        buf += chunk
        if len(buf) >= limit:
            break
    return bytes(buf[:limit])

class MetaError(Exception):
    pass


class Limiter:
    """Serialises requests and keeps ``gap`` seconds between the starts of two of them."""

    def __init__(self, gap: float = GAP, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self._lock: asyncio.Lock | None = None
        self._last = None

    async def wait(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._last is not None:
                d = self._last + self.gap - self.clock()
                if d > 0:
                    await self.sleep(d)
            self._last = self.clock()


LIMITER = Limiter()


# -- normalising and matching ---------------------------------------------------------------------

_BR = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_EDITION = re.compile(r"\b(deluxe|remaster(ed)?|expanded|edition|anniversary|bonus|special|version|reissue)\b.*$", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_title(s: str) -> str:
    """Album title without '(Deluxe Edition)', '[Remastered]' and the like."""
    base = _BR.sub("", s or "") or s or ""
    base = _EDITION.sub("", base)
    return norm(base) or norm(s)


def same_title(a: str, b: str) -> bool:
    x, y = norm_title(a), norm_title(b)
    if not x or not y:
        return False
    return x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.93


def same_artist(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.88)


def find_owned(owned: list[dict], artist: str, title: str, mbid: str = "") -> dict | None:
    """The Navidrome album (from ``owned``) that is this MusicBrainz album, or None."""
    for o in owned:
        if mbid and o.get("mbid") == mbid:
            return o
    for o in owned:
        if same_artist(o.get("artist", ""), artist) and same_title(o.get("name", ""), title):
            return o
    return None


# -- mapping --------------------------------------------------------------------------------------

def _year(date: str) -> int | None:
    m = re.match(r"^(\d{4})", date or "")
    return int(m.group(1)) if m else None


def map_artist(a: dict) -> dict:
    return {"mbid": a.get("id", ""), "name": a.get("name") or "", "sort": a.get("sort-name") or "",
            "disambiguation": a.get("disambiguation") or "", "country": a.get("country") or "",
            "type": a.get("type") or "", "score": a.get("score"),
            "begin": _year((a.get("life-span") or {}).get("begin") or "")}


def map_group(g: dict) -> dict:
    credit = g.get("artist-credit") or []
    return {"mbid": g.get("id", ""), "title": g.get("title") or "", "type": g.get("primary-type") or "",
            "secondary": g.get("secondary-types") or [], "date": g.get("first-release-date") or "",
            "year": _year(g.get("first-release-date") or ""),
            "artist": "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in credit if isinstance(c, dict)),
            "artist_mbid": next((c["artist"]["id"] for c in credit
                                 if isinstance(c, dict) and isinstance(c.get("artist"), dict)), "")}


def sort_groups(groups: list[dict]) -> list[dict]:
    """Studio albums first (by date), then EPs, then albums with a secondary type (live, compilation…)."""
    def rank(g):
        if g["type"] == "Album" and not g["secondary"]:
            return 0
        if g["type"] == "EP" and not g["secondary"]:
            return 1
        return 2 if g["type"] == "Album" else 3
    return sorted(groups, key=lambda g: (rank(g), g["date"] or "9999", g["title"].lower()))


def map_tracks(releases: list[dict]) -> list[dict]:
    """First release that has a tracklist → [{disc, number, title, length}]."""
    for rel in releases or []:
        out = []
        for mi, medium in enumerate(rel.get("media") or [], 1):
            for t in medium.get("tracks") or []:
                out.append({"disc": medium.get("position") or mi, "number": t.get("position") or 0,
                            "title": t.get("title") or "", "length": int((t.get("length") or 0) / 1000)})
        if out:
            return out
    return []


# -- the service ----------------------------------------------------------------------------------

class MusicBrainz:
    def __init__(self, store, session: aiohttp.ClientSession, cache_dir: Path, limiter: Limiter | None = None):
        self.store, self.session, self.dir = store, session, Path(cache_dir)
        self.limiter = limiter or LIMITER

    def prune(self) -> None:
        try:
            self.store.execute("DELETE FROM kv WHERE key LIKE 'music.mb.%' AND "
                               "COALESCE(json_extract(value, '$.ts'), 0) < ?", (time.time() - TTL,))
        except Exception:    # no JSON1: the cache just keeps growing slowly
            pass

    async def _get(self, path: str, params: dict, ttl: float = TTL) -> dict:
        params = {**params, "fmt": "json"}
        key = "music.mb." + hashlib.sha1((path + repr(sorted(params.items()))).encode()).hexdigest()
        hit = self.store.get(key)
        if isinstance(hit, dict) and time.time() - hit.get("ts", 0) < ttl:
            return hit["data"]
        await self.limiter.wait()
        try:
            async with self.session.get(MB + path, params=params, headers={"User-Agent": USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status in (429, 503):
                    raise MetaError(_("MusicBrainz is busy, try again in a minute"))
                if r.status >= 400:
                    raise MetaError(_("MusicBrainz answered HTTP {code}", code=r.status))
                data = await r.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise MetaError(_("MusicBrainz did not answer")) from err
        except aiohttp.ClientError as err:
            raise MetaError(_("MusicBrainz is not reachable")) from err
        self.store.set(key, {"ts": time.time(), "data": data})
        return data

    async def search_artists(self, q: str) -> list[dict]:
        d = await self._get("/artist", {"query": q, "limit": 10}, TTL_SEARCH)
        return [map_artist(a) for a in d.get("artists") or []]

    async def search_albums(self, q: str) -> list[dict]:
        d = await self._get("/release-group", {"query": q, "limit": 12}, TTL_SEARCH)
        return [map_group(g) for g in d.get("release-groups") or []
                if g.get("primary-type") in ("Album", "EP")]

    async def discography(self, artist_mbid: str) -> list[dict]:
        if not MBID.match(artist_mbid or ""):
            raise ValueError(_("Invalid artist"))
        groups, offset = [], 0
        while offset < 300:
            d = await self._get("/release-group", {"artist": artist_mbid, "type": "album|ep",
                                                   "limit": 100, "offset": offset})
            part = d.get("release-groups") or []
            groups += [map_group(g) for g in part]
            offset += 100
            if len(part) < 100 or offset >= d.get("release-group-count", 0):
                break
        return sort_groups(groups)

    async def tracklist(self, group_mbid: str) -> list[dict]:
        if not MBID.match(group_mbid or ""):
            raise ValueError(_("Invalid album"))
        d = await self._get("/release", {"release-group": group_mbid, "inc": "recordings",
                                         "status": "official", "limit": 5})
        return map_tracks(d.get("releases") or [])

    # -- covers (Cover Art Archive), proxied and cached on disk
    def cover_path(self, mbid: str) -> Path | None:
        if not MBID.match(mbid or ""):
            return None
        p = self.dir / (mbid + ".jpg")
        return p if p.is_file() else None

    async def cover(self, mbid: str) -> Path | None:
        if not MBID.match(mbid or ""):
            return None
        p = self.cover_path(mbid)
        if p:
            return p
        miss = "music.mb.nocover." + mbid
        ts = self.store.get(miss)
        if ts and time.time() - ts < TTL:
            return None
        try:
            async with self.session.get(CAA % mbid, headers={"User-Agent": USER_AGENT}, allow_redirects=True,
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    self.store.set(miss, time.time())
                    return None
                data = await read_capped(r, MAX_IMAGE + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        if len(data) > MAX_IMAGE or not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG")):
            self.store.set(miss, time.time())
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / (mbid + ".jpg")
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        self.evict()
        return p

    def evict(self) -> None:
        try:
            files = sorted((f for f in self.dir.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
            total = sum(f.stat().st_size for f in files)
            for f in files:
                if total <= CACHE_CAP:
                    break
                total -= f.stat().st_size
                f.unlink()
        except OSError:
            pass
