"""Media center: discover (TMDB / Cinemeta), Italian-first torrent search,
Transmission downloads into the NAS library, favourites with notifications.

    [plugins.media]
    tmdb_api_key = ""                 # free at themoviedb.org; empty = Cinemeta
    language = "it-IT"
    languages = ["it", "en"]          # audio priority
    quality = "1080p"
    allow_4k = true
    max_movie_gb = 20
    max_episode_gb = 4
    movies_dir = "/mnt/nas/movies"
    series_dir = "/mnt/nas/series"
    download_dir = "/mnt/nas/downloads/incomplete"
    post_process = "move"             # move | copy | none
    seed_ratio = 0                    # with "copy": seed up to this ratio
    auto_download = false             # default for new favourites
    italian_grace_days = 3
    indexers = ["torrentio", "knaben", "apibay", "eztv", "yts", "ilcorsaronero", "1337x"]
    [plugins.media.transmission]
    url = "http://127.0.0.1:9091/transmission/rpc"
    [plugins.media.proxy]
    url = ""                          # socks5h://user:pass@host:1080 or http://host:3128
"""

from __future__ import annotations

import json
import time
from datetime import date

from aiohttp import web

from ...core import Plugin
from .downloads import Downloads
from .indexers import Indexers, Query, magnet
from .library import Library
from .metadata import Metadata
from .ranking import Profile, Want, rank
from .transmission import Transmission
from .watcher import Watcher


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


class MediaPlugin(Plugin):
    name = "media"
    title = "Media"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        c = config
        self.profile = Profile(
            languages=list(c.get("languages", ["it", "en"])),
            quality=int(str(c.get("quality", "1080p")).rstrip("pP") or 1080),
            allow_4k=bool(c.get("allow_4k", True)),
            max_movie_gb=float(c.get("max_movie_gb", 20)),
            max_episode_gb=float(c.get("max_episode_gb", 4)),
        )
        self.indexers = Indexers(c)
        self.metadata = Metadata(self, c)
        self.transmission = Transmission(c.get("transmission", {}))
        self.library = Library(self, c)
        self.downloads = Downloads(self, c)
        self.watcher = Watcher(self, c)
        self._releases: dict[str, tuple[float, dict]] = {}  # hash -> release, for one-click

    async def start(self):
        await self.indexers.open()  # raises with a clear message if the proxy can't work
        await self.transmission.open()
        hub = self.hub
        hub.spawn(self.indexers.probe(), "indexer probe")
        hub.every(3600, self.indexers.probe, "indexer probe")
        hub.every(10, self.downloads.poll, "transmission poll", initial_delay=3)
        hub.every(6 * 3600, self._scan, "library scan", initial_delay=5)
        hub.every(15 * 60, self.watcher.tick, "watchlist", initial_delay=90)

    async def stop(self):
        await self.indexers.close()
        await self.transmission.close()

    async def _scan(self):
        await self.library.scan()
        await self.library.resolve_posters()

    def status(self):
        return {
            "metadata": self.metadata.provider,
            "tmdb": bool(self.metadata.key),
            "transmission": {"ok": self.downloads.ok, "error": self.downloads.error,
                             "url": self.transmission.url},
            "proxy": _mask(self.indexers.proxy),
            "indexers": self.indexers.health,
            "library": self.library.stats(),
            "watchlist": len(self.watcher.keys()),
            "active_downloads": len(self.downloads.rows(True)),
            "profile": {"languages": self.profile.languages, "quality": self.profile.quality,
                        "allow_4k": self.profile.allow_4k},
        }

    # -- core operations ---------------------------------------------------------------

    def _flag(self, items: list[dict]) -> list[dict]:
        wl = self.watcher.keys()
        for i in items:
            self.library.flag(i)
            i["favourite"] = f"{i['type']}:{i['id']}" in wl
        return items

    async def find_releases(self, item: dict, season=None, episode=None, show_all=False) -> dict:
        titles = item.get("titles") or [item["title"], item.get("original_title")]
        q = Query(titles=[t for t in titles if t], type=item["type"], year=item.get("year"),
                  imdb_id=item.get("imdb_id"), season=season, episode=episode)
        want = Want(titles=q.titles, year=item.get("year") if item["type"] == "movie" else None,
                    type=item["type"], season=season, episode=episode,
                    original_language=item.get("original_language") or "en")
        releases, stats = await self.indexers.search(q)
        profile = self.profile
        if show_all:
            profile = Profile(**{**profile.__dict__, "show_all": True})
        ranked = rank(releases, want, profile)
        now = time.time()
        self._releases = {h: v for h, v in self._releases.items() if v[0] > now - 3600}
        for r in ranked:
            r["magnet"] = magnet(r["hash"], r["name"])
            if season is not None and episode is None:
                r["file_idx"] = None  # whole season: take the full pack
            self._releases[r["hash"]] = (now, r)
        best = next((r for r in ranked if not r["rejected"]), None)
        return {"releases": ranked[:120], "stats": stats, "best": best}

    # -- routes ----------------------------------------------------------------------------

    def routes(self):
        return [
            web.get("/status", lambda r: _json(self.status())),
            web.get("/discover", self.r_discover),
            web.get("/search", self.r_search),
            web.get("/details/{type}/{id}", self.r_details),
            web.get("/episodes/{type}/{id}/{season}", self.r_episodes),
            web.get("/releases/{type}/{id}", self.r_releases),
            web.post("/download", self.r_download),
            web.get("/watchlist", self.r_watchlist),
            web.post("/watchlist", self.r_watch_add),
            web.patch("/watchlist/{key}", self.r_watch_patch),
            web.delete("/watchlist/{key}", self.r_watch_del),
            web.post("/watchlist/{key}/check", self.r_watch_check),
            web.get("/downloads", lambda r: _json(self.downloads.rows())),
            web.post("/downloads/{hash}/{action}", self.r_dl_action),
            web.get("/library", self.r_library),
            web.post("/library/scan", self.r_scan),
            web.get("/indexers", lambda r: _json(self._indexer_info())),
            web.post("/indexers/test", self.r_indexers_test),
        ]

    async def r_discover(self, request):
        sections = await self.metadata.sections()
        for s in sections:
            self._flag(s["items"])
        return _json({"provider": self.metadata.provider, "sections": sections})

    async def r_search(self, request):
        q = request.query.get("q", "").strip()
        return _json(self._flag(await self.metadata.search(q)) if q else [])

    async def r_details(self, request):
        t, id_ = request.match_info["type"], request.match_info["id"]
        item = await self.metadata.details(t, id_)
        self._flag([item])
        key = f"{t}:{item['id']}"
        item["watch"] = self.watcher.get(key)
        item["downloads"] = self.downloads.active_for(key)
        if t == "series":
            show = self.library.find_show(item.get("titles") or [item["title"]])
            have = self.library.episodes(show)
            for s in item.get("seasons", []):
                s["in_library"] = sum(1 for (sn, _) in have if sn == s["season"])
        return _json(item)

    async def r_episodes(self, request):
        t, id_ = request.match_info["type"], request.match_info["id"]
        season = int(request.match_info["season"])
        item = await self.metadata.details(t, id_)
        eps = await self.metadata.episodes(t, item["id"], season)
        have = self.library.episodes(self.library.find_show(item.get("titles") or [item["title"]]))
        busy = {(d["season"], d["episode"]): d for d in self.downloads.active_for(f"{t}:{item['id']}")}
        today = date.today().isoformat()
        for e in eps:
            e["aired"] = bool(e.get("air_date") and e["air_date"] <= today)
            e["in_library"] = (season, e["episode"]) in have
            d = busy.get((season, e["episode"])) or busy.get((season, None))
            e["downloading"] = {"progress": d["progress"], "state": d["state"]} if d else None
        return _json(eps)

    async def r_releases(self, request):
        t, id_ = request.match_info["type"], request.match_info["id"]
        q = request.query
        item = await self.metadata.details(t, id_)
        season = int(q["season"]) if q.get("season") else None
        episode = int(q["episode"]) if q.get("episode") else None
        res = await self.find_releases(item, season, episode, show_all=q.get("all") == "1")
        return _json(res)

    async def r_download(self, request):
        body = await request.json()
        item = await self.metadata.details(body["type"], body["id"])
        season, episode = body.get("season"), body.get("episode")
        rel = self._releases.get((body.get("hash") or "").lower(), (0, None))[1]
        if rel is None:
            if body.get("hash"):
                raise ValueError("release expired - search again")
            res = await self.find_releases(item, season, episode)
            rel = res["best"]
            if rel is None:
                return _json({"error": "no acceptable release found"}, 404)
        result = await self.downloads.add(item, rel, season, episode)
        return _json({"ok": True, **result, "release": rel})

    async def r_watchlist(self, request):
        return _json(self.watcher.items())

    async def r_watch_add(self, request):
        b = await request.json()
        return _json(await self.watcher.add(b["type"], b["id"], b.get("auto_download")))

    async def r_watch_patch(self, request):
        b = await request.json()
        return _json(self.watcher.update(request.match_info["key"], **b))

    async def r_watch_del(self, request):
        self.watcher.remove(request.match_info["key"])
        return _json({"ok": True})

    async def r_watch_check(self, request):
        await self.watcher.check(request.match_info["key"])
        return _json(self.watcher.get(request.match_info["key"]))

    async def r_dl_action(self, request):
        await self.downloads.action(request.match_info["hash"], request.match_info["action"])
        return _json({"ok": True})

    async def r_library(self, request):
        kind = request.query.get("kind", "movies")
        return _json({"stats": self.library.stats(), "items": self.library.browse(kind)})

    async def r_scan(self, request):
        stats = await self.library.scan()
        self.hub.spawn(self.library.resolve_posters(), "posters")
        return _json(stats)

    def _indexer_info(self):
        return {"proxy": _mask(self.indexers.proxy), "enabled": self.indexers.enabled,
                "health": self.indexers.health, "error": self.indexers.error}

    async def r_indexers_test(self, request):
        await self.indexers.probe()
        return _json(self._indexer_info())


def _mask(url: str) -> str:
    """socks5h://user:secret@host:1080 -> socks5h://user:•••@host:1080"""
    if "@" in url and ":" in url.split("@")[0].split("//")[-1]:
        scheme, rest = url.split("//", 1)
        cred, host = rest.split("@", 1)
        return f"{scheme}//{cred.split(':')[0]}:•••@{host}"
    return url


PLUGIN = MediaPlugin
