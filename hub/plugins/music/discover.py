"""Music discovery: what is popular right now, new releases, album and artist details.

Sources (no API keys): the public Deezer API (charts, genres, albums, artists, 30 s previews) and the Apple
Music RSS feed for Italy ("Top in Italia"). Answers are cached in memory with a TTL and a size cap, a failing
source never breaks the others (the caller gets the rails that worked plus a message per failed rail), and an
older cached answer is served when a source is down. Albums are marked ``owned`` when Navidrome has them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, timedelta

import aiohttp

from ...i18n import _
from .musicbrainz import norm, norm_title, same_artist, same_title

log = logging.getLogger("hub.music.discover")

DEEZER = "https://api.deezer.com"
APPLE = "https://rss.marketingtools.apple.com/api/v2/it/music/most-played/{n}/{kind}.json"
CHART_TTL = 3 * 3600
DETAIL_TTL = 24 * 3600
PREVIEW_TTL = 600            # Deezer preview URLs are signed and expire after ~15 minutes
MAX_CACHE = 300
NEW_DAYS = 75                # "New releases": Italian top albums released in the last N days
ID = re.compile(r"^\d{1,15}$")
RAILS = ("top_it", "chart_albums", "chart_artists", "chart_tracks", "new")


class DiscoverError(ValueError):
    """User-facing (already translated) message."""


def valid_id(v) -> bool:
    return isinstance(v, str) and bool(ID.match(v))


def _check_id(v) -> str:
    v = str(v or "")
    if not valid_id(v):
        raise DiscoverError(_("Invalid request"))
    return v


# -- mapping ----------------------------------------------------------------------------------------

def _year(s) -> int | None:
    m = re.match(r"^(\d{4})", str(s or ""))
    return int(m.group(1)) if m else None


def _pic(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.startswith("https://") and "/images/" in v:
            return v
    return ""


def map_album(a: dict) -> dict:
    art = a.get("artist") if isinstance(a.get("artist"), dict) else {}
    return {"id": str(a.get("id") or ""), "title": a.get("title") or "", "artist": art.get("name") or "",
            "artist_id": str(art.get("id") or ""), "cover": _pic(a, "cover_big", "cover_medium", "cover_xl"),
            "year": _year(a.get("release_date")), "type": a.get("record_type") or "",
            "explicit": bool(a.get("explicit_lyrics")), "owned": None}


def map_artist(a: dict) -> dict:
    return {"id": str(a.get("id") or ""), "name": a.get("name") or "",
            "picture": _pic(a, "picture_big", "picture_medium", "picture_xl"), "fans": a.get("nb_fan")}


def map_track(t: dict) -> dict:
    art = t.get("artist") if isinstance(t.get("artist"), dict) else {}
    alb = t.get("album") if isinstance(t.get("album"), dict) else {}
    prev = t.get("preview")
    return {"id": str(t.get("id") or ""), "title": t.get("title") or "", "duration": t.get("duration") or 0,
            "artist": art.get("name") or "", "artist_id": str(art.get("id") or ""),
            "album": alb.get("title") or "", "album_id": str(alb.get("id") or ""),
            "cover": _pic(alb, "cover_medium", "cover_big"),
            "preview": prev if isinstance(prev, str) and prev.startswith("https://") else "",
            "explicit": bool(t.get("explicit_lyrics"))}


def map_apple(a: dict) -> dict:
    art = str(a.get("artworkUrl100") or "")
    if art.startswith("https://"):
        art = re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", r"/500x500bb.\2", art)
    else:
        art = ""
    return {"id": "", "apple": str(a.get("id") or ""), "title": a.get("name") or "", "artist": a.get("artistName") or "",
            "artist_id": "", "cover": art, "year": _year(a.get("releaseDate")), "released": a.get("releaseDate") or "",
            "type": "album", "explicit": "xplicit" in str(a.get("contentAdvisoryRating") or ""), "owned": None}


# -- library matching -------------------------------------------------------------------------------

class LibIndex:
    """Navidrome albums by normalised title and artist, so each lookup is a dict hit, not a scan."""

    def __init__(self, albums: list[dict]):
        self.by_title: dict[str, list[dict]] = {}
        self.by_artist: dict[str, list[dict]] = {}
        for a in albums or []:
            self.by_title.setdefault(norm_title(a.get("name", "")), []).append(a)
            self.by_artist.setdefault(norm(a.get("artist", "")), []).append(a)

    def match(self, artist: str, title: str) -> str | None:
        """Navidrome album id, or None."""
        x = norm(artist)
        for o in self.by_title.get(norm_title(title), []):
            y = norm(o.get("artist", ""))
            if same_artist(o.get("artist", ""), artist) or (x and y and (x in y or y in x)):
                return o.get("id") or None
        for o in self.by_artist.get(x, []):          # same artist, slightly different title
            if same_title(o.get("name", ""), title):
                return o.get("id") or None
        return None


class Discover:
    """``session``: a callable returning the shared aiohttp session. ``owned``: async callable giving the
    Navidrome album list (the plugin caches it for a few minutes); errors there only mean 'not in library'."""

    def __init__(self, session, owned=None):
        self._session = session
        self._owned = owned
        self._cache: dict[str, tuple[float, float, object]] = {}
        self._flight: dict[str, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(4)           # Deezer allows 50 requests / 5 s
        self._idx: tuple[int, LibIndex] | None = None
        self._down_until = 0.0

    # -- http + cache -------------------------------------------------------------------------------
    def _put(self, key, ttl, val):
        if len(self._cache) >= MAX_CACHE:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:MAX_CACHE // 5]:
                self._cache.pop(k, None)
        self._cache[key] = (time.time(), ttl, val)

    async def cached(self, key: str, ttl: float, fetch):
        """Fresh cache hit, else ``fetch()``; on failure the stale value (if any) is better than an error.
        Concurrent callers of the same key share one request."""
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < hit[1]:
            return hit[2]
        if key in self._flight:
            return await asyncio.shield(self._flight[key])
        fut = asyncio.get_running_loop().create_future()
        self._flight[key] = fut
        try:
            try:
                val = await fetch()
            except (DiscoverError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                if hit:
                    log.info("serving stale %s after: %s", key, err)
                    val = hit[2]
                else:
                    raise
            else:
                self._put(key, ttl, val)
            fut.set_result(val)
            return val
        except BaseException as err:
            fut.set_exception(err if isinstance(err, Exception) else RuntimeError("cancelled"))
            fut.exception()
            raise
        finally:
            self._flight.pop(key, None)

    async def _json(self, url: str, params: dict | None = None, retry: bool = False) -> dict:
        async with self._sem:
            for attempt in (0, 1):
                try:
                    async with self._session().get(url, params=params, headers={"User-Agent": "Casa/1.0"},
                                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status >= 500 and retry and attempt == 0:
                            await asyncio.sleep(0.6)
                            continue
                        if r.status >= 400:
                            raise DiscoverError(_("The music service answered HTTP {code}", code=r.status))
                        data = await r.json(content_type=None)
                except asyncio.TimeoutError as err:
                    raise DiscoverError(_("The music service is not answering")) from err
                except aiohttp.ClientError as err:
                    log.info("fetch %s failed: %s", url, err)
                    raise DiscoverError(_("The music service is not reachable")) from err
                except ValueError as err:
                    raise DiscoverError(_("The music service sent an unexpected answer")) from err
                break
        if not isinstance(data, dict):
            raise DiscoverError(_("The music service sent an unexpected answer"))
        err = data.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            if code == 4:
                raise DiscoverError(_("The music service is busy, try again in a moment"))
            if code == 800:
                raise DiscoverError(_("Not found"))
            raise DiscoverError(_("The music service sent an unexpected answer"))
        return data

    async def _dz(self, path: str, **params) -> dict:
        return await self._json(f"{DEEZER}{path}", params or None)

    # -- library ------------------------------------------------------------------------------------
    async def index(self) -> LibIndex | None:
        if not self._owned or time.time() < self._down_until:
            return None
        try:
            albums = await self._owned()
        except Exception as err:                     # Navidrome down or not set up: nothing is "owned"
            log.debug("library index unavailable: %s", err)
            self._down_until = time.time() + 60      # do not make every rail wait for a dead server
            return None
        if self._idx and self._idx[0] == id(albums):
            return self._idx[1]
        idx = LibIndex(albums)
        self._idx = (id(albums), idx)
        return idx

    async def mark(self, items: list[dict]) -> list[dict]:
        idx = await self.index()
        for it in items:
            if idx and it.get("title") is not None and "owned" in it:
                it["owned"] = idx.match(it.get("artist", ""), it["title"])
        return items

    # -- rails --------------------------------------------------------------------------------------
    async def _apple(self, kind: str = "albums", n: int = 25) -> list[dict]:
        async def fetch():
            d = await self._json(APPLE.format(n=n, kind=kind), retry=True)
            res = (d.get("feed") or {}).get("results")
            if not isinstance(res, list):
                raise DiscoverError(_("The music service sent an unexpected answer"))
            return [map_apple(x) for x in res if isinstance(x, dict) and x.get("name")]
        return await self.cached(f"apple:{kind}:{n}", CHART_TTL, fetch)

    async def rail(self, key: str) -> dict:
        """{key, kind, items}. Raises DiscoverError when this rail's source fails."""
        if key == "top_it":
            return {"key": key, "kind": "album", "items": await self.mark(list(map(dict, await self._apple())))}
        if key == "new":
            cut = (date.today() - timedelta(days=NEW_DAYS)).isoformat()
            fresh = [dict(a) for a in await self._apple() if a["released"] >= cut]
            fresh.sort(key=lambda a: a["released"], reverse=True)
            return {"key": key, "kind": "album", "items": await self.mark(fresh)}
        if key == "chart_albums":
            return {"key": key, "kind": "album", "items": await self.mark(await self.chart("albums", 0))}
        if key == "chart_artists":
            return {"key": key, "kind": "artist", "items": await self.chart("artists", 0)}
        if key == "chart_tracks":
            return {"key": key, "kind": "track", "items": await self.chart("tracks", 0)}
        raise DiscoverError(_("Invalid request"))

    async def rails(self, keys: list[str]) -> dict:
        """Several rails at once; one failing does not affect the others."""
        keys = [k for k in keys if k in RAILS] or list(RAILS)
        results = await asyncio.gather(*(self.rail(k) for k in keys), return_exceptions=True)
        rails, errors = [], {}
        for k, r in zip(keys, results):
            if isinstance(r, DiscoverError):
                errors[k] = str(r)
            elif isinstance(r, Exception):
                log.warning("rail %s failed: %s", k, r)
                errors[k] = _("The music service sent an unexpected answer")
            else:
                rails.append(r)
        return {"rails": rails, "errors": errors}

    async def chart(self, kind: str, genre: int | str, limit: int = 24) -> list[dict]:
        gid = _check_id(str(genre))
        mapper = {"albums": map_album, "artists": map_artist, "tracks": map_track}[kind]

        async def fetch():
            d = await self._dz(f"/chart/{gid}/{kind}", limit=limit)
            return [mapper(x) for x in d.get("data") or [] if isinstance(x, dict) and x.get("id")]
        items = await self.cached(f"chart:{gid}:{kind}", PREVIEW_TTL if kind == "tracks" else CHART_TTL, fetch)
        return [dict(i) for i in items]

    async def genres(self) -> list[dict]:
        async def fetch():
            d = await self._dz("/genre")
            return [{"id": str(g["id"]), "name": g.get("name") or ""} for g in d.get("data") or []
                    if isinstance(g, dict) and g.get("name") and g.get("id") not in (0, "0")]
        return await self.cached("genres", DETAIL_TTL, fetch)

    async def genre_albums(self, gid: str) -> list[dict]:
        return await self.mark(await self.chart("albums", _check_id(gid), 30))

    # -- details ------------------------------------------------------------------------------------
    async def album(self, aid: str) -> dict:
        aid = _check_id(aid)

        async def fetch():
            d = await self._dz(f"/album/{aid}")
            art = d.get("artist") if isinstance(d.get("artist"), dict) else {}
            tracks = [map_track(x) for x in (d.get("tracks") or {}).get("data") or [] if isinstance(x, dict)]
            return {"id": aid, "title": d.get("title") or "", "artist": art.get("name") or "",
                    "artist_id": str(art.get("id") or ""), "cover": _pic(d, "cover_xl", "cover_big", "cover_medium"),
                    "year": _year(d.get("release_date")), "date": d.get("release_date") or "",
                    "label": d.get("label") or "", "type": d.get("record_type") or "",
                    "genres": [g.get("name") for g in (d.get("genres") or {}).get("data") or [] if g.get("name")],
                    "fans": d.get("fans"), "duration": d.get("duration") or 0, "link": d.get("link") or "",
                    "explicit": bool(d.get("explicit_lyrics")), "tracks": tracks, "owned": None}
        a = dict(await self.cached(f"album:{aid}", PREVIEW_TTL, fetch))
        idx = await self.index()
        a["owned"] = idx.match(a["artist"], a["title"]) if idx else None
        return a

    async def artist(self, aid: str) -> dict:
        aid = _check_id(aid)

        async def part(path, mapper, ttl, **params):
            async def fetch():
                d = await self._dz(path, **params)
                return [mapper(x) for x in d.get("data") or [] if isinstance(x, dict) and x.get("id")]
            return await self.cached(f"{path}:{params}", ttl, fetch)

        async def base():
            async def fetch():
                d = await self._dz(f"/artist/{aid}")
                return {**map_artist(d), "picture": _pic(d, "picture_xl", "picture_big"), "albums_count": d.get("nb_album"),
                        "link": d.get("link") or ""}
            return await self.cached(f"artist:{aid}", DETAIL_TTL, fetch)

        main, top, albums, related = await asyncio.gather(
            base(), part(f"/artist/{aid}/top", map_track, PREVIEW_TTL, limit=10),
            part(f"/artist/{aid}/albums", map_album, DETAIL_TTL, limit=100),
            part(f"/artist/{aid}/related", map_artist, DETAIL_TTL, limit=14), return_exceptions=True)
        if isinstance(main, Exception):
            raise main if isinstance(main, DiscoverError) else DiscoverError(_("The music service sent an unexpected answer"))
        out, errors = dict(main), {}
        for name, val in (("top", top), ("albums", albums), ("related", related)):
            if isinstance(val, Exception):
                errors[name] = str(val) if isinstance(val, DiscoverError) else _("The music service sent an unexpected answer")
                val = []
            out[name] = [dict(x) for x in val]
        out["albums"] = await self.mark(self._dedupe(out["albums"], out["name"]))
        out["errors"] = errors
        return out

    @staticmethod
    def _dedupe(albums: list[dict], artist: str) -> list[dict]:
        """One entry per title (Deezer lists every regional edition), albums and EPs first, newest first."""
        seen, out = set(), []
        for a in sorted(albums, key=lambda a: (a.get("type") not in ("album", "ep"), -(a.get("year") or 0))):
            k = norm_title(a["title"])
            if k in seen:
                continue
            seen.add(k)
            a.setdefault("artist", artist)
            if not a.get("artist"):
                a["artist"] = artist
            out.append(a)
        return out

    # -- search -------------------------------------------------------------------------------------
    async def search(self, q: str) -> dict:
        q = " ".join(str(q or "").split())[:100]
        if len(q) < 2:
            raise DiscoverError(_("Type at least 2 characters"))

        async def one(kind, mapper):
            async def fetch():
                d = await self._dz(f"/search/{kind}", q=q, limit=24)
                return [mapper(x) for x in d.get("data") or [] if isinstance(x, dict) and x.get("id")]
            return await self.cached(f"search:{kind}:{q.lower()}", CHART_TTL, fetch)
        arts, albs = await asyncio.gather(one("artist", map_artist), one("album", map_album), return_exceptions=True)
        if isinstance(arts, Exception) and isinstance(albs, Exception):
            raise albs if isinstance(albs, DiscoverError) else DiscoverError(_("The music service sent an unexpected answer"))
        arts = [] if isinstance(arts, Exception) else [dict(x) for x in arts]
        albs = [] if isinstance(albs, Exception) else [dict(x) for x in albs]
        return {"artists": arts, "albums": await self.mark(albs)}

    async def resolve(self, artist: str, title: str) -> str:
        """Deezer album id for an artist + title (the Apple chart has no Deezer ids)."""
        artist, title = " ".join(str(artist or "").split())[:100], " ".join(str(title or "").split())[:150]
        if not title:
            raise DiscoverError(_("Invalid request"))

        async def fetch():
            d = await self._dz("/search/album", q=f'artist:"{artist}" album:"{title}"', limit=10)
            found = [map_album(x) for x in d.get("data") or [] if isinstance(x, dict) and x.get("id")]
            if not found:
                d = await self._dz("/search/album", q=f"{artist} {title}", limit=10)
                found = [map_album(x) for x in d.get("data") or [] if isinstance(x, dict) and x.get("id")]
            for a in found:
                if same_title(a["title"], title) and same_artist(a["artist"], artist):
                    return a["id"]
            for a in found:
                if same_title(a["title"], title):
                    return a["id"]
            return ""
        got = await self.cached(f"resolve:{norm(artist)}:{norm_title(title)}", DETAIL_TTL, fetch)
        if not got:
            raise DiscoverError(_("This album was not found"))
        return got
