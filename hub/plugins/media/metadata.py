"""Movie / series metadata: TMDB (Italian, needs a free key) or Cinemeta (no key).

Both are normalized to one shape:
    {id, type: movie|series, title, original_title, year, overview, poster,
     backdrop, rating, genres, imdb_id, release_date, original_language}
Details add titles (all names to search trackers with), trailer, runtime,
seasons [{season, name, episodes, air_date, poster}], cast, status.
Episodes: [{season, episode, title, overview, air_date, still}].

Ids: "tmdb:<n>" for TMDB, "tt<n>" (IMDb) for Cinemeta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from urllib.parse import quote
from ...i18n import _

log = logging.getLogger("hub.media.metadata")

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"
CINEMETA = "https://v3-cinemeta.strem.io"
CINEMETA_PAGE = 50  # items per genre catalog page (skip= steps)


# TMDB's TV genre list has no Horror or Thriller (and no Anime for either kind): browse them through
# keywords / language. Keyword ids are looked up by exact name at run time, never hard-coded.
VIRTUAL = {
    "v:horror": {"name": "Horror", "types": ("series",),
                 "keywords": ["horror", "slasher", "supernatural horror", "zombie"]},
    "v:thriller": {"name": "Thriller", "types": ("series",),
                   "keywords": ["thriller", "psychological thriller", "suspense"]},
    "v:anime": {"name": "Anime", "types": ("movie", "series"), "genres": ["16"], "language": "ja"},
}
# sort -> (TMDB sort_by, minimum votes per kind: stops one-vote wonders topping the lists)
SORTS = {"popular": ("popularity.desc", {"movie": 20, "tv": 10}),
         "rated": ("vote_average.desc", {"movie": 500, "tv": 200}),
         "recent": ("{date}.desc", {"movie": 10, "tv": 5})}


def year_range(year: str) -> tuple[str, str]:
    """'2019' -> ('2019-01-01', '2019-12-31'); '2010s' -> ('2010-01-01', '2019-12-31'); '' -> ('', '')."""
    y = str(year or "").strip().lower()
    if len(y) == 4 and y.isdigit():
        return f"{y}-01-01", f"{y}-12-31"
    if len(y) == 5 and y[:4].isdigit() and y.endswith("s") and y[3] == "0":
        return f"{y[:4]}-01-01", f"{int(y[:4]) + 9}-12-31"
    return "", ""


def parse_tmdb_genres(data: dict) -> list[dict]:
    return sorted(({"id": str(g["id"]), "name": g["name"]} for g in data.get("genres", [])
                   if g.get("id") is not None and g.get("name")), key=lambda g: g["name"])


def parse_cinemeta_genres(manifest: dict, type_: str) -> list[dict]:
    """Genre options of the 'top' catalog for a type in the Cinemeta manifest."""
    for c in manifest.get("catalogs", []):
        if c.get("type") != type_ or c.get("id") != "top":
            continue
        for e in c.get("extra", []):
            if e.get("name") == "genre":
                return [{"id": o, "name": o} for o in e.get("options", []) if o]
    return []


class Metadata:
    def __init__(self, plugin, cfg: dict):
        self.plugin = plugin
        self.key = (cfg.get("tmdb_api_key") or "").strip()
        self.language = cfg.get("language", "it-IT")
        self.region = self.language.split("-")[-1] if "-" in self.language else "IT"
        self.mem: dict[str, tuple[float, object]] = {}
        plugin.hub.store.execute(
            "CREATE TABLE IF NOT EXISTS media_cache (key TEXT PRIMARY KEY, ts REAL, data TEXT)")

    @property
    def provider(self) -> str:
        return "tmdb" if self.key else "cinemeta"

    # -- http + cache -------------------------------------------------------------

    async def _get(self, url: str, params: dict | None = None, ttl: float = 6 * 3600,
                   persist: bool = False):
        params = dict(params or {})
        headers = {}
        if url.startswith(TMDB):
            if self.key.startswith("eyJ"):  # v4 read access token
                headers["Authorization"] = f"Bearer {self.key}"
            else:
                params["api_key"] = self.key
        ck = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")
        now = time.time()
        hit = self.mem.get(ck)
        if hit and hit[0] > now:
            return hit[1]
        if persist:
            row = self.plugin.hub.store.query("SELECT ts, data FROM media_cache WHERE key=?", (ck,))
            if row and row[0]["ts"] + ttl > now:
                data = json.loads(row[0]["data"])
                self.mem[ck] = (now + ttl, data)
                return data
        ix = self.plugin.indexers
        async with ix._get(url, params=params, headers=headers) as r:
            if r.status == 401:
                raise PermissionError(_("TMDB rejected the API key"))
            r.raise_for_status()
            data = await r.json(content_type=None)
        if len(self.mem) > 600:
            self.mem = {k: v for k, v in self.mem.items() if v[0] > now}
        self.mem[ck] = (now + ttl, data)
        if persist:
            self.plugin.hub.store.execute(
                "INSERT OR REPLACE INTO media_cache(key, ts, data) VALUES(?,?,?)",
                (ck, now, json.dumps(data)))
        return data

    async def _tmdb(self, path: str, ttl: float = 6 * 3600, persist=False, **params):
        params.setdefault("language", self.language)
        return await self._get(TMDB + path, params, ttl, persist)

    # -- normalizers ----------------------------------------------------------------

    def _norm_tmdb(self, it: dict, type_: str | None = None) -> dict | None:
        t = type_ or {"movie": "movie", "tv": "series"}.get(it.get("media_type"))
        if t not in ("movie", "series"):
            return None
        d = it.get("release_date") if t == "movie" else it.get("first_air_date")
        return {
            "id": f"tmdb:{it['id']}", "type": t,
            "title": it.get("title") or it.get("name") or "",
            "original_title": it.get("original_title") or it.get("original_name") or "",
            "year": int(d[:4]) if d else None, "release_date": d or None,
            "overview": it.get("overview") or "",
            "poster": IMG + "w342" + it["poster_path"] if it.get("poster_path") else None,
            "backdrop": IMG + "w1280" + it["backdrop_path"] if it.get("backdrop_path") else None,
            "rating": round(it.get("vote_average") or 0, 1) or None,
            "votes": it.get("vote_count"),
            "genres": it.get("genre_ids") or [g["name"] for g in it.get("genres", [])],
            "original_language": it.get("original_language") or "en",
            "imdb_id": (it.get("external_ids") or {}).get("imdb_id") or it.get("imdb_id"),
        }

    @staticmethod
    def _norm_cinemeta(m: dict, type_: str) -> dict:
        rel = m.get("releaseInfo") or m.get("year") or ""
        y = str(rel)[:4]
        return {
            "id": m.get("imdb_id") or m.get("id"), "type": type_,
            "title": m.get("name") or "", "original_title": m.get("name") or "",
            "year": int(y) if y.isdigit() else None,
            "release_date": (m.get("released") or "")[:10] or None,
            "overview": m.get("description") or "",
            "poster": m.get("poster"), "backdrop": m.get("background"),
            "rating": float(m["imdbRating"]) if m.get("imdbRating") else None,
            "genres": m.get("genres") or m.get("genre") or [],
            "original_language": "en" if (m.get("country") or "").startswith(("USA", "UK", "United")) else
                                 ("it" if (m.get("country") or "").startswith("Italy") else "en"),
            "imdb_id": m.get("imdb_id") or (m.get("id") if str(m.get("id", "")).startswith("tt") else None),
        }

    # -- discovery ------------------------------------------------------------------

    async def sections(self) -> list[dict]:
        """Rows for the Discover page."""
        if self.provider == "tmdb":
            specs = [
                ("trending", _("Trending this week"), "/trending/all/week", None, {}),
                ("now_playing", _("In cinemas"), "/movie/now_playing", "movie", {"region": self.region}),
                ("upcoming", _("Coming soon"), "/movie/upcoming", "movie", {"region": self.region}),
                ("tv_popular", _("Popular series"), "/tv/popular", "series", {}),
                ("on_the_air", _("New episodes"), "/tv/on_the_air", "series", {}),
                ("movie_popular", _("Popular movies"), "/movie/popular", "movie", {}),
                ("top_movies", _("Top rated movies"), "/movie/top_rated", "movie", {}),
                ("top_tv", _("Top rated series"), "/tv/top_rated", "series", {}),
            ]
            out = []
            for key, title, path, t, params in specs:
                try:
                    data = await self._tmdb(path, ttl=3 * 3600, **params)
                except Exception as err:
                    log.warning("TMDB %s: %s", path, err)
                    continue
                items = [x for x in (self._norm_tmdb(i, t) for i in data.get("results", [])) if x]
                if key == "upcoming":
                    today = date.today().isoformat()
                    items = [i for i in items if (i["release_date"] or "") >= today] or items
                out.append({"key": key, "title": title, "items": items})
            return out
        specs = [
            ("movie_popular", _("Popular movies"), "movie", "/catalog/movie/top.json"),
            ("tv_popular", _("Popular series"), "series", "/catalog/series/top.json"),
            ("new_movies", _("Movies of {year}", year=date.today().year), "movie",
             f"/catalog/movie/year/genre={date.today().year}.json"),
            ("new_series", _("Series of {year}", year=date.today().year), "series",
             f"/catalog/series/year/genre={date.today().year}.json"),
            ("top_movies", _("Top rated movies"), "movie", "/catalog/movie/imdbRating.json"),
            ("top_tv", _("Top rated series"), "series", "/catalog/series/imdbRating.json"),
        ]
        out = []
        for key, title, t, path in specs:
            try:
                data = await self._get(CINEMETA + path, ttl=6 * 3600)
            except Exception as err:
                log.warning("Cinemeta %s: %s", path, err)
                continue
            out.append({"key": key, "title": title,
                        "items": [self._norm_cinemeta(m, t) for m in data.get("metas", [])[:40]]})
        return out

    # -- genres ---------------------------------------------------------------------

    async def genres(self, type_: str) -> list[dict]:
        """[{id, name}] for 'movie' or 'series'. TMDB: localized names (plus the virtual genres TMDB
        lacks, see VIRTUAL); Cinemeta: English names."""
        if self.provider == "tmdb":
            data = await self._tmdb(f"/genre/{'movie' if type_ == 'movie' else 'tv'}/list",
                                    ttl=86400, persist=True)
            out = parse_tmdb_genres(data)
            names = {g["name"].lower() for g in out}
            for vid, v in VIRTUAL.items():
                if type_ in v["types"] and v["name"].lower() not in names:
                    out.append({"id": vid, "name": _(v["name"])})
            return sorted(out, key=lambda g: g["name"])
        manifest = await self._get(CINEMETA + "/manifest.json", ttl=86400, persist=True)
        return parse_cinemeta_genres(manifest, type_)

    async def _keyword_ids(self, names: list[str]) -> list[str]:
        """TMDB keyword ids for exact keyword names (looked up once, cached for a month)."""
        ids = []
        for n in names:
            try:
                res = (await self._tmdb("/search/keyword", ttl=30 * 86400, persist=True,
                                        query=n)).get("results", [])
            except Exception as err:
                log.warning("TMDB keyword %r: %s", n, err)
                continue
            hit = next((k for k in res if (k.get("name") or "").lower() == n.lower()), None)
            if hit:
                ids.append(str(hit["id"]))
        return ids

    async def _genre_params(self, genre: str) -> dict:
        """TMDB discover filters for a genre id: a real genre, or one of the virtual ones."""
        if genre.isdigit():
            return {"with_genres": genre}
        v = VIRTUAL.get(genre)
        if not v:
            raise ValueError(_("unknown genre"))
        p = {}
        if v.get("genres"):
            p["with_genres"] = ",".join(v["genres"])
        if v.get("language"):
            p["with_original_language"] = v["language"]
        if v.get("keywords"):
            ids = await self._keyword_ids(v["keywords"])
            p["with_keywords"] = "|".join(ids) if ids else "0"  # none found: match nothing
        return p

    async def discover(self, type_: str, genre: str, page: int = 1, sort: str = "popular",
                       year: str = "", ttl: float = 3 * 3600, persist: bool = False) -> dict:
        """Titles of one genre: {items, page, has_more}. sort: popular | rated | recent;
        year: '2019' or a decade like '2010s'."""
        page = max(1, page)
        sort = sort if sort in SORTS else "popular"
        gte, lte = year_range(year)
        if self.provider == "tmdb":
            kind = "movie" if type_ == "movie" else "tv"
            params = await self._genre_params(str(genre))
            datef = "primary_release_date" if kind == "movie" else "first_air_date"
            if sort == "recent":
                lte = min(lte or "9999", date.today().isoformat())
            params.update({"sort_by": SORTS[sort][0].format(date=datef), "page": page,
                           "include_adult": "false", "vote_count.gte": SORTS[sort][1][kind]})
            if gte:
                params[f"{datef}.gte"] = gte
            if lte and (sort == "recent" or year):
                params[f"{datef}.lte"] = lte
            data = await self._tmdb(f"/discover/{kind}", ttl=ttl, persist=persist, **params)
            items = [x for x in (self._norm_tmdb(i, type_) for i in data.get("results", [])) if x]
            return {"items": items, "page": page, "has_more": page < min(data.get("total_pages", 1), 500)}
        skip = (page - 1) * CINEMETA_PAGE
        cat = "imdbRating" if sort == "rated" else "top"
        data = await self._get(f"{CINEMETA}/catalog/{type_}/{cat}/genre={quote(str(genre))}&skip={skip}.json",
                               ttl=ttl, persist=persist)
        metas = data.get("metas", [])
        items = [self._norm_cinemeta(m, type_) for m in metas]
        if gte or lte:  # Cinemeta can't filter by date: trim the page (the list may come out short)
            items = [i for i in items if i["year"] and gte[:4] <= str(i["year"]) <= (lte or "9999")[:4]]
        if sort == "recent":
            items.sort(key=lambda i: i["release_date"] or str(i["year"] or ""), reverse=True)
        return {"items": items, "page": page, "has_more": len(metas) >= CINEMETA_PAGE * 0.9}

    async def genre_images(self, type_: str) -> dict:
        """{genre id: backdrop url} - the most popular title of each genre, distinct across genres.
        Cached for a week (one TMDB call per genre the first time)."""
        genres = await self.genres(type_)
        sem = asyncio.Semaphore(4)

        async def one(g):
            async with sem:
                try:
                    r = await self.discover(type_, g["id"], 1, ttl=7 * 86400, persist=True)
                except Exception as err:
                    log.debug("genre image %s: %s", g["id"], err)
                    return []
                return [i["backdrop"].replace("/w1280/", "/w780/") for i in r["items"] if i.get("backdrop")]

        pools = await asyncio.gather(*(one(g) for g in genres))
        used, out = set(), {}
        for g, pool in zip(genres, pools):
            pick = next((u for u in pool if u not in used), pool[0] if pool else None)
            if pick:
                used.add(pick)
                out[g["id"]] = pick
        return out

    async def search(self, query: str) -> list[dict]:
        if self.provider == "tmdb":
            data = await self._tmdb("/search/multi", ttl=3600, query=query, include_adult="false")
            items = [x for x in (self._norm_tmdb(i) for i in data.get("results", [])) if x]
            return sorted(items, key=lambda i: -(i.get("votes") or 0) * (2 if i["poster"] else 1))
        lists = []
        for t in ("movie", "series"):
            data = await self._get(f"{CINEMETA}/catalog/{t}/top/search={query}.json", ttl=3600)
            lists.append([self._norm_cinemeta(m, t) for m in data.get("metas", [])[:20]])
        # interleave so a series isn't buried under 20 movies (and vice versa)
        return [i for pair in zip(*lists) for i in pair] + lists[0][len(lists[1]):] + lists[1][len(lists[0]):]

    # -- details ----------------------------------------------------------------------

    async def details(self, type_: str, id_: str) -> dict:
        if id_.startswith("tmdb:"):
            return await self._tmdb_details(type_, id_.split(":", 1)[1])
        if self.provider == "tmdb":
            # IMDb id while TMDB is configured (e.g. old watchlist entry): look it up
            found = await self._tmdb(f"/find/{id_}", external_source="imdb_id", persist=True,
                                     ttl=30 * 86400)
            res = found.get("movie_results" if type_ == "movie" else "tv_results") or []
            if res:
                return await self._tmdb_details(type_, str(res[0]["id"]))
        return await self._cinemeta_details(type_, id_)

    async def _tmdb_details(self, type_: str, tid: str) -> dict:
        kind = "movie" if type_ == "movie" else "tv"
        d = await self._tmdb(f"/{kind}/{tid}", ttl=12 * 3600, persist=True,
                             append_to_response="external_ids,videos,credits,translations,release_dates",
                             include_video_language="it,en,null")
        item = self._norm_tmdb(d, type_)
        item["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
        en = next((t["data"] for t in (d.get("translations") or {}).get("translations", [])
                   if t.get("iso_639_1") == "en"), {})
        if not item["overview"]:
            item["overview"] = en.get("overview") or ""
        en_title = en.get("title") or en.get("name") or ""
        item["titles"] = _uniq([item["title"], en_title, item["original_title"]])
        vids = sorted((d.get("videos") or {}).get("results", []),
                      key=lambda v: (v.get("type") != "Trailer", v.get("iso_639_1") != "it"))
        yt = next((v for v in vids if v.get("site") == "YouTube"), None)
        item["trailer"] = f"https://www.youtube.com/watch?v={yt['key']}" if yt else None
        item["cast"] = [c["name"] for c in (d.get("credits") or {}).get("cast", [])[:8]]
        item["status"] = d.get("status")
        item["genres"] = [g["name"] for g in d.get("genres", [])]
        if type_ == "movie":
            item["runtime"] = d.get("runtime")
            item["digital_release"] = _digital_release(d, self.region)
        else:
            item["runtime"] = (d.get("episode_run_time") or [None])[0]
            item["seasons"] = [
                {"season": s["season_number"], "name": s.get("name"),
                 "episodes": s.get("episode_count"), "air_date": s.get("air_date"),
                 "poster": IMG + "w185" + s["poster_path"] if s.get("poster_path") else None}
                for s in d.get("seasons", []) if s.get("season_number", 0) > 0]
            nxt = d.get("next_episode_to_air")
            item["next_episode"] = ({"season": nxt["season_number"], "episode": nxt["episode_number"],
                                     "air_date": nxt.get("air_date"), "title": nxt.get("name")}
                                    if nxt else None)
            item["in_production"] = d.get("in_production")
        return item

    async def _cinemeta_details(self, type_: str, imdb: str) -> dict:
        d = (await self._get(f"{CINEMETA}/meta/{type_}/{imdb}.json", ttl=12 * 3600,
                             persist=True)).get("meta") or {}
        item = self._norm_cinemeta(d, type_)
        item["titles"] = _uniq([item["title"]])
        tr = (d.get("trailers") or [{}])[0].get("source")
        item["trailer"] = f"https://www.youtube.com/watch?v={tr}" if tr else None
        item["cast"] = (d.get("cast") or [])[:8]
        item["runtime"] = d.get("runtime")
        item["status"] = d.get("status")
        if type_ == "series":
            seasons: dict[int, list] = {}
            for v in d.get("videos") or []:
                if v.get("season"):
                    seasons.setdefault(v["season"], []).append(v)
            item["seasons"] = [{"season": n, "name": f"Season {n}", "episodes": len(eps),
                                "air_date": min((e.get("released") or "")[:10] for e in eps) or None,
                                "poster": None} for n, eps in sorted(seasons.items())]
            today = date.today().isoformat()
            upcoming = sorted((v for v in d.get("videos") or []
                               if v.get("season") and (v.get("released") or "")[:10] > today),
                              key=lambda v: v["released"])
            item["next_episode"] = ({"season": upcoming[0]["season"],
                                     "episode": upcoming[0].get("episode") or upcoming[0].get("number"),
                                     "air_date": upcoming[0]["released"][:10],
                                     "title": upcoming[0].get("name")} if upcoming else None)
        return item

    async def episodes(self, type_: str, id_: str, season: int) -> list[dict]:
        if id_.startswith("tmdb:"):
            d = await self._tmdb(f"/tv/{id_.split(':', 1)[1]}/season/{season}", ttl=6 * 3600,
                                 persist=True)
            return [{"season": season, "episode": e["episode_number"], "title": e.get("name") or "",
                     "overview": e.get("overview") or "", "air_date": e.get("air_date"),
                     "still": IMG + "w300" + e["still_path"] if e.get("still_path") else None}
                    for e in d.get("episodes", [])]
        d = (await self._get(f"{CINEMETA}/meta/series/{id_}.json", ttl=6 * 3600,
                             persist=True)).get("meta") or {}
        eps = [v for v in d.get("videos") or [] if v.get("season") == season]
        return sorted(({"season": season, "episode": v.get("episode") or v.get("number"),
                        "title": v.get("name") or v.get("title") or "",
                        "overview": v.get("overview") or v.get("description") or "",
                        "air_date": (v.get("released") or v.get("firstAired") or "")[:10] or None,
                        "still": v.get("thumbnail")} for v in eps), key=lambda e: e["episode"])


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _digital_release(d: dict, region: str) -> str | None:
    """Earliest digital (4) or physical (5) release date, local region first."""
    best = None
    for c in (d.get("release_dates") or {}).get("results", []):
        for rd in c.get("release_dates", []):
            if rd.get("type") in (4, 5) and rd.get("release_date"):
                day = rd["release_date"][:10]
                if c.get("iso_3166_1") == region:
                    return day
                best = min(best or day, day)
    return best
