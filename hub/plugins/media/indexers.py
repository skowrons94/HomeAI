"""Torrent indexers, searched concurrently through one (optionally proxied) session.

Every indexer returns plain release dicts:
    name, hash (lowercase hex), size (bytes), seeders, leechers, source,
    flags (Torrentio language flags), trusted (matched by IMDb id, skip title
    check), file / file_idx (episode inside a pack), match_name, url

Sources that are DNS-blocked in Italy (YTS mirrors, ilCorSaRoNeRo, 1337x)
become usable through ``[plugins.media.proxy] url = "socks5h://..."`` or a VPN.
Unreachable sources are probed at start and hourly and skipped silently.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

import aiohttp
from ...i18n import _

log = logging.getLogger("hub.media.indexers")

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "https://tracker.tamersunion.org:443/announce",
]

DEFAULT_INDEXERS = ["torrentio", "knaben", "apibay", "eztv", "yts", "ilcorsaronero", "1337x"]
DEFAULT_MIRRORS = {
    "torrentio": ["https://torrentio.strem.fun"],
    "knaben": ["https://api.knaben.org"],
    "apibay": ["https://apibay.org"],
    "eztv": ["https://eztvx.to", "https://eztv.re", "https://eztv.wf"],
    "yts": ["https://movies-api.accel.li", "https://yts.gg", "https://yts.mx", "https://yts.lt"],
    "ilcorsaronero": ["https://ilcorsaronero.link"],
    "1337x": ["https://1337x.to", "https://1337x.st", "https://x1337x.ws", "https://1337x.gd"],
}
PROBE_PATH = {
    "torrentio": "/manifest.json",
    "knaben": None,  # POST, see _probe_one
    "apibay": "/q.php?q=ubuntu&cat=0",
    "eztv": "/api/get-torrents?limit=1&page=1",
    "yts": "/api/v2/list_movies.json?limit=1",
    "ilcorsaronero": "/",
    "1337x": "/",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)


def magnet(info_hash: str, name: str = "") -> str:
    tr = "".join(f"&tr={quote(t, safe='')}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(name)}{tr}"


def parse_size(text: str) -> int:
    m = re.search(r"([\d.,]+)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b", text or "", re.I)
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    exp = {"B": 0, "K": 1, "M": 2, "G": 3, "T": 4}[unit[0].upper()]
    return int(num * (1024 if "i" in unit else 1000) ** exp)


@dataclass
class Query:
    titles: list[str]
    type: str = "movie"          # movie | series
    year: int | None = None
    imdb_id: str | None = None   # "tt1234567"
    season: int | None = None
    episode: int | None = None   # None + season = whole season

    def text(self, title: str) -> str:
        if self.type == "series" and self.season is not None:
            se = f"S{self.season:02d}" + (f"E{self.episode:02d}" if self.episode else "")
            return f"{title} {se}"
        return f"{title} {self.year}" if self.year else title


class Indexers:
    def __init__(self, cfg: dict):
        self.enabled = list(cfg.get("indexers") or DEFAULT_INDEXERS)
        self.mirrors = {**DEFAULT_MIRRORS, **(cfg.get("mirrors") or {})}
        self.proxy = ((cfg.get("proxy") or {}).get("url") or "").strip()
        self.session: aiohttp.ClientSession | None = None
        self.error: str | None = None
        self._req_kw: dict = {}
        # name -> {ok, mirror, latency_ms, error, checked}
        self.health: dict[str, dict] = {}

    # -- session ----------------------------------------------------------------

    async def open(self) -> None:
        connector = None
        if self.proxy.startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError:
                self.error = _("proxy is socks but 'aiohttp-socks' isn't installed (pip install aiohttp-socks)")
                raise RuntimeError(self.error)
            # rdns: hostnames are resolved by the proxy -> no ISP DNS blocks
            # socks5h/socks4a = "resolve on the proxy", which rdns=True does anyway
            url = re.sub(r"^socks5h://", "socks5://", re.sub(r"^socks4a://", "socks4://", self.proxy))
            try:
                connector = ProxyConnector.from_url(url, rdns=True)
            except ValueError as err:
                self.error = _("bad proxy url: {err}", err=err)
                raise RuntimeError(self.error) from err
        elif self.proxy:
            self._req_kw = {"proxy": self.proxy}
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=SEARCH_TIMEOUT,
            headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    def _get(self, url, **kw):
        return self.session.get(url, **self._req_kw, **kw)

    def _post(self, url, **kw):
        return self.session.post(url, **self._req_kw, **kw)

    async def get_json(self, url: str, timeout=SEARCH_TIMEOUT, **kw):
        async with self._get(url, timeout=timeout, **kw) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def get_text(self, url: str, timeout=SEARCH_TIMEOUT) -> str:
        async with self._get(url, timeout=timeout) as r:
            text = await r.text(errors="replace")
            if r.status in (403, 503) and ("Just a moment" in text or "cf-chl" in text):
                raise PermissionError("Cloudflare challenge")
            r.raise_for_status()
            return text

    # -- health -----------------------------------------------------------------

    async def probe(self) -> dict:
        await asyncio.gather(*(self._probe_one(n) for n in self.enabled))
        return self.health

    async def _probe_one(self, name: str) -> None:
        last_err = "no mirrors"
        for base in self.mirrors.get(name, []):
            t0 = time.monotonic()
            try:
                if name == "knaben":
                    async with self._post(f"{base}/v1", json={"query": "ubuntu", "size": 1},
                                          timeout=PROBE_TIMEOUT) as r:
                        r.raise_for_status()
                        await r.read()
                else:
                    await self.get_text(base + PROBE_PATH[name], timeout=PROBE_TIMEOUT)
                self.health[name] = {"ok": True, "mirror": base, "error": None,
                                     "latency_ms": int((time.monotonic() - t0) * 1000),
                                     "checked": time.time()}
                return
            except Exception as err:
                last_err = _err(err)
        self.health[name] = {"ok": False, "mirror": None, "error": last_err,
                             "latency_ms": None, "checked": time.time()}

    def _base(self, name: str) -> str | None:
        h = self.health.get(name)
        if h is None:  # not probed yet: optimistic first mirror
            return self.mirrors.get(name, [None])[0]
        return h["mirror"] if h["ok"] else None

    # -- search ---------------------------------------------------------------------

    async def search(self, q: Query) -> tuple[list[dict], dict]:
        """All enabled & healthy indexers concurrently -> (releases deduped, per-source stats)."""
        jobs = {}
        for name in self.enabled:
            fn = getattr(self, "_s_" + name.replace("1337x", "x1337"), None)
            base = self._base(name)
            if fn is None or base is None:
                continue
            if name in ("torrentio", "eztv") and not q.imdb_id:
                continue
            if name == "eztv" and q.type != "series":
                continue
            if name == "yts" and q.type != "movie":
                continue
            jobs[name] = asyncio.create_task(asyncio.wait_for(fn(base, q), 25))
        stats, seen = {}, {}
        for name, task in jobs.items():
            try:
                rels = await task
                stats[name] = {"count": len(rels)}
            except Exception as err:
                stats[name] = {"error": _err(err)}
                log.info("%s search failed: %s", name, _err(err))
                if isinstance(err, (aiohttp.ClientConnectorError, PermissionError)):
                    self.health[name] = {**self.health.get(name, {}), "ok": False,
                                         "error": _err(err), "checked": time.time(),
                                         "mirror": None}
                continue
            for r in rels:
                h = (r.get("hash") or "").lower()
                if not h:
                    continue
                r["hash"] = h
                old = seen.get(h)
                if old is None:
                    seen[h] = r
                else:  # keep the richer record, the best seeder count
                    old["seeders"] = max(old.get("seeders") or 0, r.get("seeders") or 0)
                    old["trusted"] = old.get("trusted") or r.get("trusted")
                    old.setdefault("flags", r.get("flags", ""))
                    if not old.get("size"):
                        old["size"] = r.get("size")
        return list(seen.values()), stats

    # -- individual indexers -------------------------------------------------------

    async def _s_torrentio(self, base: str, q: Query) -> list[dict]:
        if q.type == "series":
            path = f"/stream/series/{q.imdb_id}:{q.season or 1}:{q.episode or 1}.json"
        else:
            path = f"/stream/movie/{q.imdb_id}.json"
        data = await self.get_json(base + path)
        out = []
        for s in data.get("streams", []):
            lines = (s.get("title") or "").split("\n")
            info_i = next((i for i, l in enumerate(lines) if "👤" in l or "💾" in l), len(lines))
            name = lines[0].strip()
            file = lines[1].strip() if info_i >= 2 else ""
            info = lines[info_i] if info_i < len(lines) else ""
            flags = " ".join(lines[info_i + 1:])
            m_seed = re.search(r"👤\s*(\d+)", info)
            m_src = re.search(r"⚙️\s*(.+)$", info)
            out.append({
                "name": name, "hash": s.get("infoHash"), "size": parse_size(info.split("💾")[-1]),
                "seeders": int(m_seed.group(1)) if m_seed else 0, "leechers": None,
                "source": m_src.group(1).strip() if m_src else "Torrentio",
                "flags": flags, "trusted": True,
                "file": file if file and file != name else "",
                "file_idx": s.get("fileIdx") if file and file != name else None,
                "match_name": f"{name} {file}" if file and file != name else name,
            })
        return out

    async def _s_knaben(self, base: str, q: Query) -> list[dict]:
        cat = [2000000] if q.type == "series" else [3000000]
        out = []
        for title in _distinct(q.titles)[:2]:
            body = {"search_type": "100%", "search_field": "title", "query": q.text(title),
                    "order_by": "seeders", "order_direction": "desc", "size": 50,
                    "categories": cat, "hide_unsafe": True, "hide_xxx": True}
            async with self._post(f"{base}/v1", json=body) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            for h in data.get("hits", []):
                out.append({
                    "name": h.get("title") or "", "hash": h.get("hash"),
                    "size": int(h.get("bytes") or 0),
                    "seeders": int(h.get("liveSeeders") or h.get("seeders") or 0),
                    "leechers": int(h.get("livePeers") or h.get("peers") or 0),
                    "source": h.get("tracker") or h.get("cachedOrigin") or "Knaben",
                    "url": h.get("details"),
                })
        return out

    async def _s_apibay(self, base: str, q: Query) -> list[dict]:
        out = []
        for title in _distinct(q.titles)[:2]:
            data = await self.get_json(f"{base}/q.php?q={quote_plus(q.text(title))}&cat=200")
            for t in data:
                if t.get("id") == "0":
                    continue
                out.append({
                    "name": t["name"], "hash": t["info_hash"], "size": int(t.get("size") or 0),
                    "seeders": int(t.get("seeders") or 0), "leechers": int(t.get("leechers") or 0),
                    "source": "ThePirateBay",
                    "trusted": bool(q.imdb_id and t.get("imdb") == q.imdb_id),
                })
        return out

    async def _s_eztv(self, base: str, q: Query) -> list[dict]:
        out = []
        for page in (1, 2):
            data = await self.get_json(
                f"{base}/api/get-torrents?imdb_id={q.imdb_id.lstrip('t')}&limit=100&page={page}")
            items = data.get("torrents") or []
            for t in items:
                if q.season is not None and str(t.get("season")) not in (str(q.season), "0"):
                    continue
                out.append({
                    "name": t.get("title") or t.get("filename") or "", "hash": t.get("hash"),
                    "size": int(t.get("size_bytes") or 0), "seeders": int(t.get("seeds") or 0),
                    "leechers": int(t.get("peers") or 0), "source": "EZTV", "trusted": True,
                })
            if len(items) < 100:
                break
        return out

    async def _s_yts(self, base: str, q: Query) -> list[dict]:
        term = q.imdb_id or q.text(q.titles[0])
        data = await self.get_json(f"{base}/api/v2/list_movies.json?query_term={quote_plus(term)}&limit=5")
        out = []
        for m in (data.get("data") or {}).get("movies") or []:
            if q.imdb_id and m.get("imdb_code") != q.imdb_id:
                continue
            for t in m.get("torrents") or []:
                name = (f"{m.get('title_english') or m['title']} ({m.get('year')}) "
                        f"[{t.get('quality')}] [{(t.get('type') or '').upper()}] "
                        f"[{t.get('video_codec') or ''}] [YTS]")
                out.append({
                    "name": name, "hash": t.get("hash"), "size": int(t.get("size_bytes") or 0),
                    "seeders": int(t.get("seeds") or 0), "leechers": int(t.get("peers") or 0),
                    "source": "YTS", "trusted": bool(q.imdb_id), "flags": "",
                })
        return out

    async def _s_ilcorsaronero(self, base: str, q: Query) -> list[dict]:
        rows = []
        for title in _distinct(q.titles)[:2]:
            page = await self.get_text(f"{base}/search?q={quote_plus(q.text(title))}")
            rows += _html_rows(page, r'href="(/torrent/\d+[^"]*)"')
        return await self._with_magnets(base, _dedupe_rows(rows)[:12], "ilCorSaRoNeRo")

    async def _s_x1337(self, base: str, q: Query) -> list[dict]:
        cat = "TV" if q.type == "series" else "Movies"
        page = await self.get_text(
            f"{base}/category-search/{quote(q.text(q.titles[0]))}/{cat}/1/")
        rows = _html_rows(page, r'href="(/torrent/\d+/[^"]*)"')
        return await self._with_magnets(base, _dedupe_rows(rows)[:10], "1337x")

    async def _with_magnets(self, base: str, rows: list[dict], source: str) -> list[dict]:
        """HTML indexers list names only; the info hash is on each detail page."""

        async def one(row):
            try:
                text = await self.get_text(base + row["path"])
            except Exception:
                return None
            m = re.search(r"magnet:\?xt=urn:btih:([0-9a-zA-Z]{32,40})", text)
            if not m:
                return None
            h = m.group(1)
            if len(h) == 32:  # base32 hash
                import base64
                h = base64.b32decode(h.upper()).hex()
            return {"name": row["name"], "hash": h, "size": row["size"], "seeders": row["seeders"],
                    "leechers": row["leechers"], "source": source, "url": base + row["path"]}

        return [r for r in await asyncio.gather(*(one(r) for r in rows)) if r]


# -- helpers --------------------------------------------------------------------------

def _distinct(titles: list[str]) -> list[str]:
    seen, out = set(), []
    for t in titles:
        k = (t or "").lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _html_rows(page: str, link_re: str) -> list[dict]:
    """Generic table scraper: every <tr> holding a torrent link -> name, numbers, size."""
    out = []
    for tr in re.findall(r"<tr\b.*?</tr>", page, re.S | re.I):
        m = re.search(link_re + r"[^>]*>(.*?)</a>", tr, re.S | re.I)
        if not m:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        nums = [int(c) for c in cells if re.fullmatch(r"\d+", c)]
        size = next((parse_size(c) for c in cells if parse_size(c)), 0)
        if name:
            out.append({"path": m.group(1), "name": name, "size": size,
                        "seeders": nums[0] if nums else 0,
                        "leechers": nums[1] if len(nums) > 1 else 0})
    return out


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -r["seeders"]):
        if r["path"] not in seen:
            seen.add(r["path"])
            out.append(r)
    return out


def _err(err: Exception) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return _("timeout")
    if isinstance(err, aiohttp.ClientConnectorError):
        return _("unreachable (blocked or offline)")
    if isinstance(err, aiohttp.ClientResponseError):
        return f"HTTP {err.status}"
    return str(err) or type(err).__name__
