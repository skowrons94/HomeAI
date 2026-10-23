"""The Media plugin's torrent sites as torrent-search providers (one per site).

Only sites that answer a free-text query are exposed (torrentio and eztv need an IMDb id).
The mirrors and the proxy come from ``[plugins.media]`` so the same settings apply. Nothing is
opened until the first search; ``hub/plugins/media`` itself is not touched.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .model import Provider, ProviderError, Result, to_int

log = logging.getLogger("hub.torrents.media")

# indexer name -> display title, for the sites whose _s_ method works on plain text
SITES = {"knaben": "Knaben", "apibay": "The Pirate Bay", "yts": "YTS",
         "ilcorsaronero": "ilCorSaRoNeRo", "1337x": "1337x"}
RECHECK = 300   # seconds a failed mirror probe is trusted


class MediaBackend:
    """One shared Indexers client (proxy, mirrors, health) behind all the media providers."""

    def __init__(self, media_cfg: dict | None, indexers_cls=None):
        from ..media import indexers as ix   # imported lazily: keeps start() light
        self.ix = ix
        cfg = dict(media_cfg or {})
        cfg["indexers"] = list(SITES)
        self.idx = (indexers_cls or ix.Indexers)(cfg)
        self._opened = False
        self._lock = asyncio.Lock()

    async def ensure(self):
        async with self._lock:
            if not self._opened:
                await self.idx.open()
                self._opened = True

    async def close(self):
        if self._opened:
            await self.idx.close()
            self._opened = False

    async def base(self, name: str) -> str:
        h = self.idx.health.get(name)
        if h is None or (not h.get("ok") and time.time() - h.get("checked", 0) > RECHECK):
            await self.idx._probe_one(name)
            h = self.idx.health.get(name)
        if not h or not h.get("ok"):
            raise ProviderError("unavailable", (h or {}).get("error") or "no reachable mirror")
        return h["mirror"]


class MediaProvider(Provider):
    kind = "media"

    def __init__(self, backend: MediaBackend, site: str):
        self.backend, self.site = backend, site
        self.id, self.name = site, SITES[site]

    async def search(self, session, query, page, page_size, timeout):
        if page > 1:
            return []   # these sites answer one fixed page
        b = self.backend
        try:
            await b.ensure()
            base = await b.base(self.site)
            fn = getattr(b.idx, "_s_" + self.site.replace("1337x", "x1337"))
            rows = await fn(base, b.ix.Query(titles=[query], type="movie"))
        except ProviderError:
            raise
        except asyncio.TimeoutError as err:
            raise ProviderError("timeout", "no answer within %ds" % timeout) from err
        except Exception as err:
            msg = b.ix._err(err)
            if "429" in msg:
                raise ProviderError("rate_limited", msg, 60) from err
            raise ProviderError("unavailable", msg) from err
        return map_rows(rows, self.name, b.ix.magnet)


def map_rows(rows, source: str, make_magnet) -> list[Result]:
    rows = [r for r in rows if isinstance(r, dict) and r.get("name")]
    rows.sort(key=lambda r: -(to_int(r.get("seeders")) or 0))
    out = []
    for n, r in enumerate(rows):
        h = (r.get("hash") or "").lower() or None
        out.append(Result(
            title=r["name"], source=source, size=to_int(r.get("size")) or None,
            seeders=to_int(r.get("seeders")), leechers=to_int(r.get("leechers")),
            magnet=make_magnet(h, r["name"]) if h else None, url=r.get("url"), hash=h, rank=n))
    return out
