"""Games discovery: what is popular, new, on sale or well rated right now, with no API key needed.

Sources (all public, JSON):
  * Steam store ``featuredcategories``  → top sellers, new releases, specials, coming soon
  * Steam charts ``GetMostPlayedGames`` → most played right now (names/reviews from ``GetItems``,
    SteamSpy ``top100in2weeks`` as a fallback for the names)
  * Steam ``appdetails``                → one game in detail
  * IGDB (only when the keys are set)   → popular now, top rated, best per emulated platform, coming soon
  * Genres: Steam ``IStoreQueryService`` by store tag (top sellers of Horror, Indie, RPG…; no key), SteamSpy
    ``request=genre`` as a fallback for the broad genres, IGDB genres/themes/modes per platform (keys)

Everything is cached in memory (bounded, with TTLs) and one failing source never breaks the answer:
the rails that worked come back together with a list of per-rail errors.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

import aiohttp

from ...i18n import _
from . import find
from .metadata import IMG_URL, MetaError, _norm, clean_title, igdb_platforms, read_capped

log = logging.getLogger("hub.games")

RAILS_TTL = 3 * 3600
DETAIL_TTL = 24 * 3600
INFO_TTL = 6 * 3600
TIMEOUT = aiohttp.ClientTimeout(total=12)
RAIL_LEN = 20

FEATURED_URL = "https://store.steampowered.com/api/featuredcategories"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
CHARTS_URL = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
SPY_URL = "https://steamspy.com/api.php"
QUERY_URL = "https://api.steampowered.com/IStoreQueryService/Query/v1/"
ART_URL = "https://shared.steamstatic.com/store_item_assets/steam/apps/%s/%s"
STORE_URL = "https://store.steampowered.com/app/%s"

STEAM_RAILS = ("top_sellers", "most_played", "new_releases", "specials", "coming_soon")
FEATURED_KEYS = ("top_sellers", "new_releases", "specials", "coming_soon")
IGDB_GENERAL = ("popular", "top_rated", "coming_soon")
IGDB_DEFAULT_PLATFORMS = ("switch", "ps2", "gba", "snes")
MAX_PLATFORM_RAILS = 6

# find.py platform keys → IGDB platform ids
IGDB_PLATFORM_IDS = {
    "nes": 18, "snes": 19, "n64": 4, "gb": 33, "gbc": 22, "gba": 24, "nds": 20, "n3ds": 37, "gc": 21, "wii": 5,
    "wiiu": 41, "switch": 130, "psx": 7, "ps2": 8, "psp": 38, "psvita": 46, "genesis": 29, "mastersystem": 64,
    "gamegear": 35, "saturn": 32, "dreamcast": 23, "arcade": 52, "neogeo": 80, "atari2600": 59, "pcengine": 86,
}
IGDB_ITEM_FIELDS = ("name,slug,cover.image_id,first_release_date,total_rating,total_rating_count,"
                    "platforms.abbreviation,platforms.name")
IGDB_DETAIL_FIELDS = ("name,slug,summary,storyline,cover.image_id,first_release_date,total_rating,total_rating_count,"
                      "genres.name,platforms.abbreviation,platforms.name,screenshots.image_id,artworks.image_id,"
                      "involved_companies.developer,involved_companies.company.name,videos.video_id,videos.name")

GENRE_TTL = 6 * 3600
GENRE_PAGE = 24
GENRE_MAX_PAGES = 5
SPY_GAP = 1.2                  # SteamSpy asks for about one request a second
SPY_MAX = 14 * 1024 * 1024     # a broad genre is several MB of JSON: refuse anything absurd
STEAM_SORT_TOP_SELLERS = 10    # IStoreQueryService sort: the store's "top sellers" order
IGDB_PC = 6

# key, Steam store tag id (verified with IStoreService/GetTagList), IGDB filter (field, id) or None,
# SteamSpy genre for the fallback or ""
GENRES = [
    ("action", 19, ("themes", 1), "Action"), ("adventure", 21, ("genres", 31), "Adventure"),
    ("rpg", 122, ("genres", 12), "RPG"), ("strategy", 9, ("genres", 15), "Strategy"),
    ("horror", 1667, ("themes", 19), ""), ("indie", 492, ("genres", 32), "Indie"),
    ("simulation", 599, ("genres", 13), "Simulation"), ("racing", 699, ("genres", 10), "Racing"),
    ("sports", 701, ("genres", 14), "Sports"), ("puzzle", 1664, ("genres", 9), ""),
    ("platformer", 1625, ("genres", 8), ""), ("roguelike", 1716, None, ""),
    ("survival", 1662, ("themes", 21), ""), ("coop", 1685, ("game_modes", 3), ""),
    ("shooter", 1774, ("genres", 5), ""), ("fighting", 1743, ("genres", 4), ""),
    ("open_world", 1695, ("themes", 38), ""), ("stealth", 1687, ("themes", 23), ""),
    ("metroidvania", 1628, None, ""),
]
GENRE_BY_KEY = {g[0]: g for g in GENRES}

IMAGE_HOSTS = ("steamstatic.com", "steampowered.com", "akamaihd.net", "igdb.com", "youtube.com", "ytimg.com")


class DiscoverError(Exception):
    pass


# -- small helpers -------------------------------------------------------------------------------

def valid_id(value) -> bool:
    """App ids are plain decimal digits: nothing else ever reaches a URL."""
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) <= 10


def safe_url(url) -> str | None:
    """An https URL on a host we expect (images, videos); anything else is dropped."""
    if not isinstance(url, str) or len(url) > 600:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == d or host.endswith("." + d) for d in IMAGE_HOSTS):
        return None
    return url


class _Text(HTMLParser):
    BREAK = {"br", "p", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def strip_html(text, limit: int = 1200) -> str:
    """HTML (Steam descriptions) → plain text with paragraph breaks. The client only uses textContent."""
    if not isinstance(text, str) or not text:
        return ""
    p = _Text()
    try:
        p.feed(text[:20000])
        p.close()
        raw = "".join(p.out)
    except Exception:  # noqa: BLE001 - malformed markup: fall back to a blunt strip
        raw = html.unescape(re.sub(r"<[^>]*>", " ", text))
    lines = [" ".join(x.split()) for x in raw.replace("\r", "").split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return out


def norm_title(title: str) -> str:
    """Comparable form of a game title: no ™/®, punctuation, case or release-tag noise."""
    t = (title or "").replace("™", " ").replace("®", " ")
    return _norm(clean_title(t))


def money(cents, currency: str = "EUR") -> str | None:
    if not isinstance(cents, (int, float)) or isinstance(cents, bool) or cents < 0:
        return None
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency or "", "")
    txt = f"{cents / 100:.2f}"
    return f"{txt.replace('.', ',')}{sym}" if sym == "€" else f"{sym}{txt}" if sym else f"{txt} {currency}"


def steam_cover(appid) -> str:
    return ART_URL % (appid, "library_600x900.jpg")


def steam_header(appid) -> str:
    return ART_URL % (appid, "header.jpg")


def _year_from_ts(ts) -> str | None:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return time.strftime("%Y", time.gmtime(ts))
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _year_from_text(text) -> str | None:
    m = re.search(r"\b(19[7-9]\d|20\d\d)\b", text) if isinstance(text, str) else None
    return m.group(1) if m else None


class Cache:
    """Small TTL cache; the oldest-to-expire entries go first when it is full. ``stale`` reads ignore expiry."""

    def __init__(self, cap: int, clock: Callable[[], float] = time.monotonic):
        self.cap, self.clock, self.data = cap, clock, {}

    def get(self, key, stale: bool = False):
        hit = self.data.get(key)
        if hit is None:
            return None
        if hit[0] < self.clock() and not stale:
            return None
        return hit[1]

    def put(self, key, value, ttl: float):
        self.data[key] = (self.clock() + ttl, value)
        if len(self.data) > self.cap:
            now = self.clock()
            for k in [k for k, (exp, _v) in self.data.items() if exp < now]:
                del self.data[k]
            while len(self.data) > self.cap:
                del self.data[min(self.data, key=lambda k: self.data[k][0])]

    def clear(self):
        self.data.clear()


# -- the engine ----------------------------------------------------------------------------------

class Discover:
    """``library()`` returns {normalised title: {platform keys}} for the "in library" flag;
    ``language()`` the UI language code ("it", "en"); ``meta`` is the IGDB client (may be unconfigured)."""

    def __init__(self, session: aiohttp.ClientSession, meta=None, library: Callable[[], dict] | None = None,
                 language: Callable[[], str] | None = None, clock: Callable[[], float] = time.monotonic):
        self.session, self.meta = session, meta
        self._library, self._language = library or (lambda: {}), language or (lambda: "it")
        self.rails_cache, self.detail_cache, self.info_cache = Cache(60, clock), Cache(120, clock), Cache(800, clock)
        self.genre_cache = Cache(120, clock)
        self._locks: dict[str, asyncio.Lock] = {}
        self._spy_lock = asyncio.Lock()
        self._spy_last = 0.0
        self._clock = clock

    # -- plumbing --
    @property
    def igdb(self) -> bool:
        return bool(self.meta and getattr(self.meta, "igdb", False))

    def _loc(self) -> tuple[str, str]:
        it = (self._language() or "").lower().startswith("it")
        return ("italian", "it") if it else ("english", "us")

    def _lock(self, key: str) -> asyncio.Lock:
        if len(self._locks) > 200:
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        return self._locks.setdefault(key, asyncio.Lock())

    async def _json(self, url: str, params: dict | None = None):
        try:
            async with self.session.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"}) as r:
                if r.status == 429:
                    raise DiscoverError(_("Steam is busy, try again in a minute"))
                if r.status >= 400:
                    raise DiscoverError(_("Steam answered HTTP {code}", code=r.status))
                return await r.json(content_type=None)
        except DiscoverError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            log.debug("discover fetch %s failed: %s", url, err)
            raise DiscoverError(_("Steam is unreachable: {err}", err=str(err) or type(err).__name__)) from err

    async def _cached(self, cache: Cache, key, ttl: float, fetch, refresh: bool = False):
        """Cached value, or ``fetch()`` once (concurrent callers share it). A stale value beats an error."""
        if not refresh:
            hit = cache.get(key)
            if hit is not None:
                return hit
        async with self._lock(str(key)):
            if not refresh:
                hit = cache.get(key)
                if hit is not None:
                    return hit
            try:
                value = await fetch()
            except (DiscoverError, MetaError):
                stale = cache.get(key, stale=True)
                if stale is not None:
                    return stale
                raise
            cache.put(key, value, ttl)
            return value

    # -- library flag --
    def _flag(self, items: list[dict]) -> list[dict]:
        lib = self._library() or {}
        out = []
        for it in items:
            plats = lib.get(norm_title(it["title"])) or set()
            want = it.get("platform") or ""
            have = bool(plats) and (not want or want in plats or (want == "pc" and "linux" in plats))
            out.append({**it, "in_library": have})
        return out

    # -- Steam: featured categories --
    @staticmethod
    def _steam_item(raw: dict) -> dict | None:
        if not isinstance(raw, dict) or not raw.get("name") or not str(raw.get("id", "")).isdigit():
            return None
        if raw.get("type", 0) not in (0, None):
            return None
        appid = str(raw["id"])
        disc = raw.get("discount_percent")
        final = raw.get("final_price")
        cur = raw.get("currency") or "EUR"
        return {
            "id": appid, "source": "steam", "title": str(raw["name"]), "cover": steam_cover(appid),
            "image": safe_url(raw.get("header_image")) or steam_header(appid), "platform": "pc",
            "discount": int(disc) if isinstance(disc, (int, float)) and disc > 0 else None,
            "price": money(final, cur) if final else None, "was": money(raw.get("original_price"), cur) if disc else None,
            "free": False, "rating": None, "rating_kind": None, "year": None, "url": STORE_URL % appid,
        }

    async def _featured(self) -> dict:
        lang, cc = self._loc()
        d = await self._json(FEATURED_URL, {"cc": cc, "l": lang})
        if not isinstance(d, dict):
            raise DiscoverError(_("Steam sent an unexpected answer"))
        out = {}
        for key in FEATURED_KEYS:
            seen, items = set(), []
            for raw in ((d.get(key) or {}).get("items") or []) if isinstance(d.get(key), dict) else []:
                it = self._steam_item(raw)
                if it and it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
            out[key] = items[:RAIL_LEN]
        if not any(out.values()):
            raise DiscoverError(_("Steam sent an unexpected answer"))
        return out

    async def _ranks(self) -> list[str]:
        d = await self._json(CHARTS_URL)
        ranks = ((d or {}).get("response") or {}).get("ranks") if isinstance(d, dict) else None
        ids = [str(r["appid"]) for r in ranks or [] if isinstance(r, dict) and str(r.get("appid", "")).isdigit()]
        if not ids:
            raise DiscoverError(_("Steam sent an unexpected answer"))
        return ids[:60]

    async def _spy(self) -> list[dict]:
        """SteamSpy's most played in two weeks: name + id only (fallback when GetItems fails)."""
        d = await self._json(SPY_URL, {"request": "top100in2weeks"})
        if not isinstance(d, dict):
            raise DiscoverError(_("Steam sent an unexpected answer"))
        items = []
        for k, v in d.items():
            if str(k).isdigit() and isinstance(v, dict) and v.get("name"):
                items.append({"id": str(k), "source": "steam", "title": str(v["name"]), "cover": steam_cover(k),
                              "image": steam_header(k), "platform": "pc", "discount": None, "price": None, "was": None,
                              "free": str(v.get("price", "")) == "0", "rating": None, "rating_kind": None, "year": None,
                              "url": STORE_URL % k})
        if not items:
            raise DiscoverError(_("Steam sent an unexpected answer"))
        return items

    async def _fetch_info(self, ids: list[str]) -> None:
        """Names, review % and release year for app ids → info cache (one batched call)."""
        import json
        lang, cc = self._loc()
        body = {"ids": [{"appid": int(i)} for i in ids], "context": {"language": lang, "country_code": cc.upper()},
                "data_request": {"include_basic_info": True, "include_reviews": True, "include_release": True}}
        d = await self._json(ITEMS_URL, {"input_json": json.dumps(body, separators=(",", ":"))})
        got = ((d or {}).get("response") or {}).get("store_items") if isinstance(d, dict) else None
        if not isinstance(got, list):
            raise DiscoverError(_("Steam sent an unexpected answer"))
        for it in got:
            if not isinstance(it, dict) or not str(it.get("appid", "")).isdigit():
                continue
            rv = ((it.get("reviews") or {}).get("summary_filtered") or {}) if isinstance(it.get("reviews"), dict) else {}
            pct, cnt = rv.get("percent_positive"), rv.get("review_count")
            rel = (it.get("release") or {}) if isinstance(it.get("release"), dict) else {}
            self.info_cache.put(str(it["appid"]), {
                "name": it.get("name") or "", "type": it.get("type"), "free": bool(it.get("is_free")),
                "rating": int(pct) if isinstance(pct, (int, float)) and isinstance(cnt, (int, float)) and cnt >= 10 else None,
                "year": _year_from_ts(rel.get("steam_release_date") or rel.get("original_release_date")),
                "coming": bool(rel.get("is_coming_soon")),
            }, INFO_TTL)
        for i in ids:           # unknown ids: remember the miss so they are not asked again every time
            if self.info_cache.get(i) is None:
                self.info_cache.put(i, {"name": "", "type": None}, INFO_TTL)

    async def _ensure_info(self, ids: list[str]) -> None:
        missing = [i for i in dict.fromkeys(ids) if self.info_cache.get(i) is None][:100]
        if missing:
            await self._fetch_info(missing)

    def _enrich(self, items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            info = self.info_cache.get(it["id"]) or {}
            e = dict(it)
            if info.get("rating") is not None:
                e["rating"], e["rating_kind"] = info["rating"], "users"
            e["year"] = it.get("year") or info.get("year")
            e["free"] = bool(it.get("free") or info.get("free"))
            out.append(e)
        return out

    async def steam_rails(self, refresh: bool = False) -> tuple[list[dict], list[dict]]:
        rails: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        lang = self._loc()[0]

        async def feat():
            return await self._cached(self.rails_cache, ("featured", lang), RAILS_TTL, self._featured, refresh)

        async def ranks():
            return await self._cached(self.rails_cache, ("ranks",), RAILS_TTL, self._ranks, refresh)

        (feat_r, ranks_r) = await asyncio.gather(feat(), ranks(), return_exceptions=True)
        for r in (feat_r, ranks_r):
            if isinstance(r, BaseException) and not isinstance(r, DiscoverError):
                log.warning("games discover: %r", r)
        if isinstance(feat_r, dict):
            rails.update(feat_r)
        else:
            for k in FEATURED_KEYS:
                errors[k] = str(feat_r) if isinstance(feat_r, DiscoverError) else _("Steam is unreachable: {err}", err=type(feat_r).__name__)
        ids = list(dict.fromkeys(
            [i for k in FEATURED_KEYS for it in rails.get(k, []) for i in [it["id"]]] + (ranks_r if isinstance(ranks_r, list) else [])))
        info_ok = True
        if ids:
            try:
                await self._ensure_info(ids)
            except DiscoverError as err:
                info_ok = False
                log.debug("games discover: store info unavailable: %s", err)
        if isinstance(ranks_r, list):
            items = []
            if info_ok:
                for i in ranks_r:
                    info = self.info_cache.get(i) or {}
                    if info.get("name") and info.get("type") in (0, None):
                        items.append({"id": i, "source": "steam", "title": info["name"], "cover": steam_cover(i),
                                      "image": steam_header(i), "platform": "pc", "discount": None, "price": None, "was": None,
                                      "free": False, "rating": None, "rating_kind": None, "year": None, "url": STORE_URL % i})
            if not items:
                try:
                    spy = await self._cached(self.rails_cache, ("spy",), RAILS_TTL, self._spy, refresh)
                    order = {i: n for n, i in enumerate(ranks_r)}
                    items = sorted(spy, key=lambda x: order.get(x["id"], 999))[:RAIL_LEN] if any(x["id"] in order for x in spy) else spy
                except DiscoverError as err:
                    errors["most_played"] = str(err)
            if items:
                rails["most_played"] = items[:RAIL_LEN]
        else:
            errors["most_played"] = str(ranks_r) if isinstance(ranks_r, DiscoverError) else _("Steam is unreachable: {err}", err=type(ranks_r).__name__)
        out, errs = [], []
        for k in STEAM_RAILS:
            if k in rails and rails[k]:
                out.append({"key": k, "source": "steam", "items": self._flag(self._enrich(rails[k]))})
            elif k in errors:
                errs.append({"rail": k, "error": errors[k]})
        return out, errs

    # -- IGDB --
    @staticmethod
    def _igdb_item(raw: dict, platform: str = "") -> dict | None:
        if not isinstance(raw, dict) or not raw.get("name") or not isinstance(raw.get("id"), int):
            return None
        cover = (raw.get("cover") or {}).get("image_id") if isinstance(raw.get("cover"), dict) else None
        rating = raw.get("total_rating")
        plats = [p.get("abbreviation") or p.get("name") for p in raw.get("platforms") or [] if isinstance(p, dict)]
        slug = raw.get("slug") if isinstance(raw.get("slug"), str) and re.fullmatch(r"[a-z0-9-]{1,150}", raw["slug"]) else None
        return {
            "id": str(raw["id"]), "source": "igdb", "title": str(raw["name"]),
            "cover": IMG_URL % ("t_cover_big", cover) if isinstance(cover, str) and re.fullmatch(r"[a-z0-9]+", cover) else None,
            "image": None, "platform": platform, "discount": None, "price": None, "was": None, "free": False,
            "rating": int(round(rating)) if isinstance(rating, (int, float)) and raw.get("total_rating_count", 0) >= 3 else None,
            "rating_kind": "igdb", "year": _year_from_ts(raw.get("first_release_date")),
            "platforms": [p for p in plats if p][:4], "url": f"https://www.igdb.com/games/{slug}" if slug else None,
        }

    async def _igdb_games(self, where: str, sort: str, platform: str = "", limit: int = RAIL_LEN, offset: int = 0) -> list[dict]:
        rows = await self.meta._igdb(f"fields {IGDB_ITEM_FIELDS}; where {where}; sort {sort}; limit {limit};"
                                     + (f" offset {int(offset)};" if offset else ""))
        return [x for x in (self._igdb_item(r, platform) for r in rows) if x]

    async def _igdb_popular(self) -> list[dict]:
        prim = await self.meta._igdb("fields game_id,value; where popularity_type = 1; sort value desc; limit 30;",
                                     "popularity_primitives")
        ids = [int(p["game_id"]) for p in prim if isinstance(p, dict) and isinstance(p.get("game_id"), int)]
        if not ids:
            raise DiscoverError(_("IGDB sent an unexpected answer"))
        rows = await self._igdb_games("id = (%s) & cover != null" % ",".join(map(str, ids)), "total_rating_count desc", limit=30)
        order = {str(i): n for n, i in enumerate(ids)}
        return sorted(rows, key=lambda x: order.get(x["id"], 999))[:RAIL_LEN]

    def platform_rails(self) -> list[str]:
        have = []
        for plats in (self._library() or {}).values():
            for p in plats:
                if p in IGDB_PLATFORM_IDS and p not in have:
                    have.append(p)
        keys = list(IGDB_DEFAULT_PLATFORMS) + [p for p in have if p not in IGDB_DEFAULT_PLATFORMS]
        return keys[:MAX_PLATFORM_RAILS]

    async def igdb_rails(self, refresh: bool = False) -> tuple[list[dict], list[dict]]:
        if not self.igdb:
            return [], []
        now = int(time.time())
        jobs: list[tuple[str, str, Callable]] = [
            ("popular", "", self._igdb_popular),
            ("top_rated", "", lambda: self._igdb_games(
                "total_rating_count > 200 & cover != null & version_parent = null", "total_rating desc")),
            ("coming_soon", "", lambda: self._igdb_games(
                f"first_release_date > {now} & hypes > 3 & cover != null", "hypes desc")),
        ]
        for p in self.platform_rails():
            pid = IGDB_PLATFORM_IDS[p]
            jobs.append((f"platform:{p}", p, lambda pid=pid, p=p: self._igdb_games(
                f"platforms = ({pid}) & total_rating_count > 20 & cover != null & version_parent = null",
                "total_rating desc", p)))
        out, errs = [], []
        for key, plat, fetch in jobs:
            try:
                items = await self._cached(self.rails_cache, ("igdb", key), RAILS_TTL, fetch, refresh)
            except (DiscoverError, MetaError) as err:
                errs.append({"rail": key, "error": str(err)})
                continue
            except Exception as err:  # noqa: BLE001 - one broken rail must not hide the others
                log.warning("games discover: IGDB rail %s: %r", key, err)
                errs.append({"rail": key, "error": _("IGDB sent an unexpected answer")})
                continue
            if items:
                rail = {"key": key, "source": "igdb", "items": self._flag(items)}
                if plat:
                    rail["platform"] = plat
                    rail["label"] = dict(find.PLATFORMS).get(plat, plat.upper())
                out.append(rail)
        return out, errs

    async def rails(self, which: str = "all", refresh: bool = False) -> dict:
        parts = []
        if which in ("all", "steam"):
            parts.append(self.steam_rails(refresh))
        if which in ("all", "igdb"):
            parts.append(self.igdb_rails(refresh))
        rails, errors = [], []
        for res in await asyncio.gather(*parts, return_exceptions=True):
            if isinstance(res, BaseException):
                log.warning("games discover failed: %r", res)
                errors.append({"rail": which, "error": _("Discovery is unavailable right now")})
                continue
            rails += res[0]
            errors += res[1]
        return {"rails": rails, "errors": errors, "igdb": self.igdb}

    # -- genres --
    def genre_list(self) -> dict:
        plats = [{"key": "pc", "label": "PC"}] + ([{"key": k, "label": dict(find.PLATFORMS).get(k, k.upper())}
                                                   for k in IGDB_PLATFORM_IDS] if self.igdb else [])
        return {"genres": [{"key": k, "igdb": f is not None} for k, _t, f, _s in GENRES], "igdb": self.igdb,
                "platforms": plats}

    def _plain_item(self, appid: str, name: str) -> dict:
        return {"id": appid, "source": "steam", "title": name, "cover": steam_cover(appid), "image": steam_header(appid),
                "platform": "pc", "discount": None, "price": None, "was": None, "free": False, "rating": None,
                "rating_kind": None, "year": None, "url": STORE_URL % appid}

    async def _steam_genre(self, tag: int, page: int) -> tuple[list[dict], bool]:
        """The store's top sellers with a tag: ids from IStoreQueryService, names/reviews from GetItems."""
        import json
        lang, cc = self._loc()
        body = {"query": {"start": page * GENRE_PAGE, "count": GENRE_PAGE, "sort": STEAM_SORT_TOP_SELLERS,
                          "filters": {"type_filters": {"include_games": True}, "tagids_must_match": [{"tagids": [tag]}]}},
                "context": {"language": lang, "country_code": cc.upper()}, "data_request": {}}
        d = await self._json(QUERY_URL, {"input_json": json.dumps(body, separators=(",", ":"))})
        resp = d.get("response") if isinstance(d, dict) else None
        ids = [str(x["appid"]) for x in (resp or {}).get("ids") or [] if isinstance(x, dict) and str(x.get("appid", "")).isdigit()]
        if not isinstance(resp, dict) or (not ids and page == 0):
            raise DiscoverError(_("Steam sent an unexpected answer"))
        total = ((resp.get("metadata") or {}).get("total_matching_records") or 0)
        try:
            await self._ensure_info(ids)
        except DiscoverError as err:
            log.debug("games discover: genre names unavailable: %s", err)
        items = []
        for i in ids:
            info = self.info_cache.get(i) or {}
            if info.get("name") and info.get("type") in (0, None):
                items.append(self._plain_item(i, info["name"]))
        more = isinstance(total, int) and (page + 1) * GENRE_PAGE < total and page + 1 < GENRE_MAX_PAGES
        return items, more

    async def _spy_genre(self, genre: str) -> list[dict]:
        """SteamSpy ``request=genre`` (broad genres only: its tag lists are unreliable). One request at a time,
        spaced out, size-capped; only the 24 best-reviewed games are kept."""
        import json
        async with self._spy_lock:
            wait = self._spy_last + SPY_GAP - self._clock()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.session.get(SPY_URL, params={"request": "genre", "genre": genre}, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status >= 400:
                        raise DiscoverError(_("Steam answered HTTP {code}", code=r.status))
                    raw = await read_capped(r, SPY_MAX)
            except DiscoverError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise DiscoverError(_("Steam is unreachable: {err}", err=str(err) or type(err).__name__)) from err
            finally:
                self._spy_last = self._clock()
        if len(raw) > SPY_MAX:
            raise DiscoverError(_("Steam sent an unexpected answer"))
        try:
            d = json.loads(raw)
        except ValueError:
            raise DiscoverError(_("Steam sent an unexpected answer"))
        del raw
        rows = [v for k, v in d.items() if str(k).isdigit() and isinstance(v, dict) and v.get("name")] if isinstance(d, dict) else []
        rows.sort(key=lambda v: -((v.get("positive") or 0) + (v.get("negative") or 0)))
        out = []
        for v in rows[:GENRE_PAGE]:
            it = self._plain_item(str(v["appid"]), str(v["name"]))
            it["free"] = str(v.get("price", "")) == "0"
            out.append(it)
        if not out:
            raise DiscoverError(_("Steam sent an unexpected answer"))
        return out

    async def _igdb_genre(self, key: str, platform: str, page: int) -> tuple[list[dict], bool]:
        flt = GENRE_BY_KEY[key][2]
        if flt is None:
            raise DiscoverError(_("This genre is not available for that platform"))
        pid = IGDB_PC if platform == "pc" else IGDB_PLATFORM_IDS[platform]
        where = f"platforms = ({pid}) & {flt[0]} = ({flt[1]}) & total_rating_count > 5 & cover != null & version_parent = null"
        rows = await self._igdb_games(where, "total_rating_count desc", "" if platform == "pc" else platform,
                                      GENRE_PAGE + 1, page * GENRE_PAGE)
        return rows[:GENRE_PAGE], len(rows) > GENRE_PAGE and page + 1 < GENRE_MAX_PAGES

    async def genre(self, key: str, platform: str = "pc", page: int = 0, refresh: bool = False) -> dict:
        if key not in GENRE_BY_KEY:
            raise ValueError(_("Unknown genre"))
        if platform != "pc" and not (self.igdb and platform in IGDB_PLATFORM_IDS):
            raise ValueError(_("Unknown platform"))
        page = min(max(int(page), 0), GENRE_MAX_PAGES - 1)
        _k, tag, flt, spy = GENRE_BY_KEY[key]
        lang = self._loc()[0]

        async def fetch():
            if platform == "pc":
                try:
                    items, more = await self._steam_genre(tag, page)
                    return {"items": items, "more": more, "source": "steam"}
                except DiscoverError as err:
                    if spy and page == 0:
                        try:
                            return {"items": await self._spy_genre(spy), "more": False, "source": "steam"}
                        except DiscoverError:
                            pass
                    if self.igdb and flt is not None:
                        items, more = await self._igdb_genre(key, "pc", page)
                        return {"items": items, "more": more, "source": "igdb"}
                    raise err
            items, more = await self._igdb_genre(key, platform, page)
            return {"items": items, "more": more, "source": "igdb"}

        res = await self._cached(self.genre_cache, (key, platform, page, lang), GENRE_TTL, fetch, refresh)
        items = self._enrich(res["items"]) if res["source"] == "steam" else res["items"]
        return {"genre": key, "platform": platform, "page": page, "more": res["more"], "source": res["source"],
                "items": self._flag(items)}

    # -- details --
    async def detail(self, source: str, ident: str, platform: str = "", refresh: bool = False) -> dict:
        if source not in ("steam", "igdb") or not valid_id(ident):
            raise ValueError(_("Invalid game id"))
        if source == "igdb" and not self.igdb:
            raise ValueError(_("IGDB keys not set"))
        lang = self._loc()[0]
        fetch = (lambda: self._steam_detail(ident)) if source == "steam" else (lambda: self._igdb_detail(ident))
        d = await self._cached(self.detail_cache, (source, ident, lang), DETAIL_TTL, fetch, refresh)
        d = dict(d)
        if source == "igdb":
            keys = d.get("search_platforms") or []
            d["platform"] = platform if platform in keys else (keys[0] if keys else "pc")
        return self._flag([d])[0]

    async def _steam_detail(self, appid: str) -> dict:
        lang, cc = self._loc()
        d = await self._json(APPDETAILS_URL, {"appids": appid, "l": lang, "cc": cc})
        rec = d.get(appid) if isinstance(d, dict) else None
        if not isinstance(rec, dict) or not rec.get("success") or not isinstance(rec.get("data"), dict):
            raise KeyError(_("Game not found"))
        x = rec["data"]
        try:
            await self._ensure_info([appid])
        except DiscoverError:
            pass
        info = self.info_cache.get(appid) or {}
        price = x.get("price_overview") if isinstance(x.get("price_overview"), dict) else {}
        meta = x.get("metacritic") if isinstance(x.get("metacritic"), dict) else {}
        shots = []
        for s in x.get("screenshots") or []:
            if isinstance(s, dict):
                thumb, full = safe_url(s.get("path_thumbnail")), safe_url(s.get("path_full"))
                if thumb or full:
                    shots.append({"thumb": thumb or full, "full": full or thumb})
        movie = None
        for m in x.get("movies") or []:
            if not isinstance(m, dict):
                continue
            mp4 = m.get("mp4") if isinstance(m.get("mp4"), dict) else {}
            web = m.get("webm") if isinstance(m.get("webm"), dict) else {}
            movie = {"thumb": safe_url(m.get("thumbnail")), "name": str(m.get("name") or "")[:120],
                     "mp4": safe_url(mp4.get("480") or mp4.get("max")), "webm": safe_url(web.get("480") or web.get("max")),
                     "hls": safe_url(m.get("hls_h264"))}
            if movie["mp4"] or movie["webm"] or movie["hls"]:
                break
            movie = None
        rel = x.get("release_date") if isinstance(x.get("release_date"), dict) else {}
        summary = strip_html(x.get("short_description")) or strip_html(x.get("about_the_game"), 600)
        sc = meta.get("score")
        fin = price.get("final")
        return {
            "id": appid, "source": "steam", "title": str(x.get("name") or ""), "cover": steam_cover(appid),
            "image": safe_url(x.get("header_image")) or steam_header(appid),
            "hero": safe_url(x.get("background_raw")) or safe_url(x.get("background")),
            "platform": "pc", "search_platforms": ["pc"], "year": _year_from_text(rel.get("date")),
            "released": str(rel.get("date") or "")[:40], "coming_soon": bool(rel.get("coming_soon")),
            "genres": [str(g["description"]) for g in x.get("genres") or [] if isinstance(g, dict) and g.get("description")][:6],
            "developers": [str(v) for v in x.get("developers") or [] if isinstance(v, str)][:3],
            "publishers": [str(v) for v in x.get("publishers") or [] if isinstance(v, str)][:3],
            "summary": summary, "screenshots": shots[:10], "trailer": movie,
            "metacritic": int(sc) if isinstance(sc, (int, float)) else None,
            "rating": info.get("rating"), "rating_kind": "users" if info.get("rating") is not None else None,
            "discount": int(price["discount_percent"]) if isinstance(price.get("discount_percent"), (int, float)) and price["discount_percent"] > 0 else None,
            "price": money(fin, price.get("currency") or "EUR") if fin else None,
            "was": money(price.get("initial"), price.get("currency") or "EUR") if price.get("discount_percent") else None,
            "free": bool(x.get("is_free")), "url": STORE_URL % appid,
            "systems": [k for k, v in (x.get("platforms") or {}).items() if v is True and k in ("windows", "mac", "linux")]
            if isinstance(x.get("platforms"), dict) else [],
        }

    def _search_platforms(self, plats: list[str]) -> list[str]:
        have = {p.lower() for p in plats}
        keys = [k for k, _l in find.PLATFORMS if k != "pc" and igdb_platforms(k) & have]
        if {"pc", "win", "pc (microsoft windows)"} & have:
            keys.insert(0, "pc")
        return keys

    async def _igdb_detail(self, ident: str) -> dict:
        rows = await self.meta._igdb(f"fields {IGDB_DETAIL_FIELDS}; where id = {int(ident)}; limit 1;")
        if not rows or not isinstance(rows[0], dict):
            raise KeyError(_("Game not found"))
        r = rows[0]
        base = self._igdb_item(r) or {"id": ident, "source": "igdb", "title": "", "cover": None}

        def imgs(key, size):
            return [IMG_URL % (size, x["image_id"]) for x in r.get(key) or []
                    if isinstance(x, dict) and isinstance(x.get("image_id"), str) and re.fullmatch(r"[a-z0-9]+", x["image_id"])]

        shots = [{"thumb": u, "full": f} for u, f in zip(imgs("screenshots", "t_screenshot_big"), imgs("screenshots", "t_1080p"))]
        art = imgs("artworks", "t_1080p")
        dev = [ic["company"]["name"] for ic in r.get("involved_companies") or []
               if isinstance(ic, dict) and ic.get("developer") and isinstance(ic.get("company"), dict) and ic["company"].get("name")]
        vid = next((v["video_id"] for v in r.get("videos") or []
                    if isinstance(v, dict) and isinstance(v.get("video_id"), str) and re.fullmatch(r"[\w-]{6,20}", v["video_id"])), None)
        allp = [p.get("abbreviation") or p.get("name") or "" for p in r.get("platforms") or [] if isinstance(p, dict)]
        allp += [p.get("name") or "" for p in r.get("platforms") or [] if isinstance(p, dict)]
        return {
            **base, "hero": art[0] if art else (shots[0]["full"] if shots else None),
            "search_platforms": self._search_platforms(allp), "released": base.get("year") or "",
            "genres": [str(g["name"]) for g in r.get("genres") or [] if isinstance(g, dict) and g.get("name")][:6],
            "developers": dev[:3], "publishers": [], "summary": strip_html(r.get("summary") or r.get("storyline"), 1200),
            "screenshots": shots[:8], "metacritic": None, "coming_soon": False, "systems": [],
            "trailer": {"thumb": None, "name": "", "mp4": None, "webm": None, "hls": None,
                        "youtube": f"https://www.youtube.com/watch?v={vid}"} if vid else None,
        }
