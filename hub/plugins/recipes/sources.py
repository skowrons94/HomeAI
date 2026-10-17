"""Fetching recipes: any URL (schema.org), TheMealDB, GialloZafferano and WordPress recipe-blog search."""

from __future__ import annotations

import asyncio
import hashlib
import html as htmllib
import ipaddress
import logging
import re
import time
import unicodedata
from urllib.parse import quote, urlparse

import aiohttp

from . import parse
from ...i18n import _

log = logging.getLogger("hub.recipes")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 Casa/1.0")
API_UA = "Casa/1.0 (home recipe hub; recipe search)"  # some CDNs block browser-like UAs on API paths
MAX_PAGE = 4 * 1024 * 1024
MAX_IMAGE = 2 * 1024 * 1024
TIMEOUT = aiohttp.ClientTimeout(total=15, connect=6)
MEALDB = "https://www.themealdb.com/api/json/v1/1"
IMG_TYPES = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "image/gif": ".gif", "image/avif": ".avif"}


def check_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(_("Enter a full web address (https://...)"))
    host = u.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError(_("Local addresses are not allowed"))
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(_("Local addresses are not allowed"))
    except ValueError as e:
        if "not allowed" in str(e):
            raise
    return u.geturl()


async def _read(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        buf += chunk
        if len(buf) > limit:
            raise ValueError(_("Response too large"))
    return bytes(buf)


async def fetch_text(http, url: str) -> tuple[str, str]:
    """GET a page -> (html, final url). Retries once with the plain Casa UA when a CDN rejects the browser-like one."""
    url = check_url(url)
    for ua in (UA, API_UA):
        async with http.get(url, headers={"User-Agent": ua, "Accept-Language": "it,en;q=0.8",
                                          "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"},
                            timeout=TIMEOUT, allow_redirects=True) as r:
            if r.status == 403 and ua is UA:
                continue
            if r.status >= 400:
                raise ValueError(_("The site answered HTTP {status}", status=r.status))
            data = await _read(r, MAX_PAGE)
            enc = r.charset or "utf-8"
            return data.decode(enc, errors="replace"), str(r.url)
    raise ValueError(_("The site answered HTTP {status}", status=403))


async def fetch_image(http, url: str, dest_dir, stem: str) -> str:
    """Download an image (<= 2 MB) into dest_dir; returns the file name or ''."""
    if not url:
        return ""
    try:
        url = check_url(url)
        async with http.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT) as r:
            if r.status >= 400:
                return ""
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = IMG_TYPES.get(ctype)
            if not ext:
                return ""
            if int(r.headers.get("content-length") or 0) > MAX_IMAGE:
                return ""
            data = await _read(r, MAX_IMAGE)
        name = f"{stem}{ext}"
        (dest_dir / name).write_bytes(data)
        return name
    except Exception as err:
        log.info("image %s: %s", url, err)
        return ""


def guess_lang(host: str, page: str = "") -> str:
    m = re.search(r"<html[^>]*\blang=[\"']?([a-z]{2})", page[:3000], re.I)
    if m:
        return m.group(1).lower()
    return "it" if host.endswith(".it") else "en"


async def import_url(http, url: str) -> dict:
    page, final = await fetch_text(http, url)
    rec = parse.parse_page(page, final)
    if not rec:
        raise ValueError(_("No recipe found on that page (it has no schema.org Recipe data)"))
    rec["lang"] = guess_lang(urlparse(final).netloc, page)
    return rec


# ---------------------------------------------------------------------------- TheMealDB


def mealdb_recipe(m: dict) -> dict:
    ing = []
    for i in range(1, 21):
        n = (m.get(f"strIngredient{i}") or "").strip()
        if n:
            ing.append(f"{(m.get(f'strMeasure{i}') or '').strip()} {n}".strip())
    steps = []
    for line in re.split(r"[\r\n]+", m.get("strInstructions") or ""):
        line = line.strip()
        if not line or re.fullmatch(r"(?:step\s*)?\d+[.)]?", line, re.I):
            continue
        steps.append(re.sub(r"^(?:step\s*)?\d+\s*[.):\-]\s+", "", line, flags=re.I))
    return {
        "title": m.get("strMeal", ""), "image_url": m.get("strMealThumb", ""), "servings": 4,  # not given; typical
        "ingredients": ing, "steps": steps, "kcal": None, "cuisine": m.get("strArea") or "",
        "category": m.get("strCategory") or "", "prep_min": None, "cook_min": None, "total_min": None,
        "keywords": [t for t in (m.get("strTags") or "").split(",") if t],
        "source_url": m.get("strSource") or f"https://www.themealdb.com/meal/{m.get('idMeal')}",
        "site": "themealdb.com", "lang": "en", "mealdb_id": m.get("idMeal"), "description": "",
    }


async def mealdb_search(http, q: str) -> list[dict]:
    async with http.get(f"{MEALDB}/search.php?s={quote(q)}", timeout=TIMEOUT,
                        headers={"User-Agent": UA}) as r:
        data = await r.json(content_type=None)
    out = []
    for m in (data.get("meals") or [])[:12]:
        out.append({"title": m["strMeal"], "image": m.get("strMealThumb", ""), "site": "themealdb.com",
                    "lang": "en", "cuisine": "world", "source": "TheMealDB", "mealdb": m["idMeal"],
                    "url": f"https://www.themealdb.com/meal/{m['idMeal']}",
                    "subtitle": " · ".join(x for x in (m.get("strArea"), m.get("strCategory")) if x)})
    return out


async def mealdb_get(http, mid: str) -> dict:
    async with http.get(f"{MEALDB}/lookup.php?i={quote(str(mid))}", timeout=TIMEOUT,
                        headers={"User-Agent": UA}) as r:
        data = await r.json(content_type=None)
    meals = data.get("meals") or []
    if not meals:
        raise KeyError(_("Recipe not found on TheMealDB"))
    return mealdb_recipe(meals[0])


# ---------------------------------------------------------------------------- GialloZafferano

_GZ_CARD = re.compile(
    r"<a\s+href=\"(https://ricette\.giallozafferano\.it/[^\"]+\.html)\"\s+title=\"([^\"]*)\"[^>]*>(.*?)</a>",
    re.S)


def gz_results(page: str) -> list[dict]:
    out, seen = [], set()
    for m in _GZ_CARD.finditer(page):
        url, title, inner = m.groups()
        if url in seen:
            continue
        seen.add(url)
        img = re.search(r"(?:data-src|src)=\"(https?://[^\"]+\.(?:jpg|jpeg|png|webp)[^\"]*)\"", inner)
        out.append({"title": htmllib.unescape(title), "image": img.group(1) if img else "",
                    "site": "giallozafferano.it", "lang": "it", "cuisine": "italian", "source": "GialloZafferano",
                    "url": url, "subtitle": "GialloZafferano"})
        if len(out) >= 12:
            break
    return out


async def gz_search(http, q: str) -> list[dict]:
    slug = quote(q.strip().replace("/", " "), safe="")
    page, _ = await fetch_text(http, f"https://www.giallozafferano.it/ricerca-ricette/{slug}/")
    return gz_results(page)


# ---------------------------------------------------------------------------- WordPress blogs

# Recipe blogs that expose the WordPress REST API. Result pages carry schema.org Recipe data, so
# importing a chosen result is the ordinary `import_url`. `type` is the REST collection (posts by default).
WP_SITES = [
    {"id": "tavolartegusto", "name": "Tavolartegusto", "base": "https://www.tavolartegusto.it", "lang": "it", "cuisine": "italian"},
    {"id": "benedetta", "name": "Fatto in casa da Benedetta", "base": "https://www.fattoincasadabenedetta.it",
     "lang": "it", "cuisine": "italian", "type": "recipe"},
    {"id": "misya", "name": "Misya", "base": "https://www.misya.info", "lang": "it", "cuisine": "italian"},
    {"id": "ricettedellanonna", "name": "Ricette della Nonna", "base": "https://www.ricettedellanonna.net",
     "lang": "it", "cuisine": "italian"},
    {"id": "vincenzosplate", "name": "Vincenzo's Plate", "base": "https://www.vincenzosplate.com", "lang": "en", "cuisine": "italian"},
    {"id": "recipetineats", "name": "RecipeTin Eats", "base": "https://www.recipetineats.com", "lang": "en", "cuisine": "world"},
    {"id": "hebbars", "name": "Hebbar's Kitchen", "base": "https://hebbarskitchen.com", "lang": "en", "cuisine": "world"},
    {"id": "isabeleats", "name": "Isabel Eats", "base": "https://www.isabeleats.com", "lang": "en", "cuisine": "world"},
    {"id": "mediterraneandish", "name": "The Mediterranean Dish", "base": "https://www.themediterraneandish.com",
     "lang": "en", "cuisine": "world"},
]
WP_BY_ID = {s["id"]: s for s in WP_SITES}
LEGACY = {"giallozafferano": ("GialloZafferano", "it", "italian"), "themealdb": ("TheMealDB", "en", "world")}
ALL_SOURCES = ["giallozafferano", "themealdb"] + [s["id"] for s in WP_SITES]
DEFAULT_SOURCES = list(ALL_SOURCES)
SOURCE_TIMEOUT = 6
TOTAL_TIMEOUT = 9
MAX_RESULTS = 30
PER_SOURCE = 8


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def wp_results(data, site: dict) -> list[dict]:
    """Map a WP REST `posts` listing (with `_embed=wp:featuredmedia`) to result dicts."""
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        url, title = p.get("link") or "", _text((p.get("title") or {}).get("rendered", ""))
        if not url.startswith("http") or not title:
            continue
        img = ""
        media = ((p.get("_embedded") or {}).get("wp:featuredmedia") or [None])[0]
        if isinstance(media, dict):
            sizes = (media.get("media_details") or {}).get("sizes") or {}
            for k in ("medium_large", "large", "medium", "full"):
                if sizes.get(k, {}).get("source_url"):
                    img = sizes[k]["source_url"]
                    break
            img = img or media.get("source_url") or ""
        out.append({"title": title, "image": img, "site": urlparse(site["base"]).netloc.removeprefix("www."),
                    "lang": site["lang"], "cuisine": site["cuisine"], "source": site["name"],
                    "url": url, "subtitle": site["name"]})
    return out


async def wp_search(http, site: dict, q: str) -> list[dict]:
    url = f"{site['base']}/wp-json/wp/v2/{site.get('type', 'posts')}"
    async with http.get(url, params={"search": q, "per_page": PER_SOURCE, "_embed": "wp:featuredmedia"},
                        headers={"User-Agent": API_UA, "Accept": "application/json"}, timeout=TIMEOUT) as r:
        if r.status >= 400:
            raise ValueError(f"HTTP {r.status}")
        data = await r.json(content_type=None)
    return wp_results(data, site)


# ---------------------------------------------------------------------------- relevance

# Blog search engines (WordPress) match words anywhere in the text: "zuppa" returns anything that
# mentions a soup, plus round-up articles. Results must name every searched word in the title
# (Italian/English forms folded), round-ups are dropped, and blog pages must carry a schema.org
# Recipe (what import needs anyway) - checked once and remembered.

_STOP = {"di", "da", "con", "e", "ed", "al", "alla", "alle", "ai", "agli", "allo", "all", "del", "della",
         "delle", "dei", "degli", "dello", "il", "lo", "la", "le", "i", "gli", "un", "una", "uno", "in", "per",
         "a", "su", "the", "of", "and", "with", "a", "an", "to", "in"}
# round-ups and articles, not single recipes ("10 zuppe…", "Le migliori ricette…", "Idee per…")
_ARTICLE = re.compile(r"^\s*\d+\s|\b(ricette|idee|migliori|menu|recipes|ideas|best|ways|tips|roundup)\b|\?",
                      re.I)
_LD_RECIPE = re.compile(r'"@type"\s*:\s*(?:\[[^\]]*?)?"Recipe"')


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _stem(w: str) -> str:
    """Light stemming so zuppa/zuppe, pomodoro/pomodori, soup/soups meet."""
    if len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w[-1] in "aeiouys":
        w = w[:-1]
    return w


def query_terms(q: str) -> list[str]:
    return [_stem(w) for w in _fold(q).split() if w not in _STOP and len(w) > 1]


def title_score(title: str, terms: list[str]) -> float:
    """0 = not about the query; higher = better (all words present, early, short title)."""
    if not terms:
        return 1.0
    words = [_stem(w) for w in _fold(title).split()]
    hits = [next((i for i, w in enumerate(words) if w.startswith(t)), None) for t in terms]
    if any(h is None for h in hits):
        return 0.0
    return 2.0 + (1.0 if hits[0] == 0 else 0.0) + 1.0 / (1 + len(words))


_TITLE_TAIL = re.compile(r"\s*(?:[:|–—-]\s*)(?:la\s+)?(?:ricetta|recipe)\b.*$|\s*\|.*$", re.I)


def clean_title(title: str) -> str:
    """'Curry di ceci : Ricetta perfetta, facilissima' -> 'Curry di ceci'."""
    t = _TITLE_TAIL.sub("", title or "").strip(" -–:|")
    return t or (title or "").strip()


def is_article(title: str) -> bool:
    return bool(_ARTICLE.search(title or ""))


_recipe_cache: dict[str, tuple[float, bool]] = {}


async def is_recipe_page(http, url: str) -> bool:
    """Does the page carry a schema.org Recipe? Reads only until it finds one. Remembered for a
    week (a failed fetch for an hour)."""
    hit = _recipe_cache.get(url)
    if hit and time.time() < hit[0]:
        return hit[1]
    found, ok = False, True
    try:
        for ua in (UA, API_UA):
            async with http.get(url, headers={"User-Agent": ua, "Accept": "text/html"},
                                timeout=aiohttp.ClientTimeout(total=6, connect=4), allow_redirects=True) as r:
                if r.status == 403 and ua is UA:
                    continue
                if r.status >= 400:
                    ok = False
                    break
                buf = ""
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk.decode("utf-8", errors="ignore")
                    if _LD_RECIPE.search(buf):
                        found = True
                        break
                    if len(buf) > 1_500_000:
                        break
                break
    except Exception as err:
        log.debug("recipe check %s: %s", url, err)
        ok = False
    _recipe_cache[url] = (time.time() + (7 * 86400 if ok else 3600), found)
    return found


async def tidy(http, by: dict[str, list[dict]], q: str) -> dict[str, list[dict]]:
    """Keep only real recipes about the query, best title matches first within each source."""
    terms = query_terms(q)
    out: dict[str, list[dict]] = {}
    for name, items in by.items():
        scored = [(title_score(it.get("title", ""), terms), it) for it in items]
        keep = [(sc, it) for sc, it in scored if sc > 0 and not is_article(it.get("title", ""))]
        out[name] = [it | {"score": round(sc, 3), "title": clean_title(it.get("title", ""))}
                     for sc, it in sorted(keep, key=lambda x: -x[0])]
    # blog posts: verify they are recipe pages (GialloZafferano and TheMealDB list recipes only)
    check = [(n, it) for n, items in out.items() if n not in LEGACY for it in items]
    if check:
        sem = asyncio.Semaphore(6)

        async def one(it):
            async with sem:
                return await is_recipe_page(http, it["url"])
        try:
            oks = await asyncio.wait_for(asyncio.gather(*(one(it) for _n, it in check)), 8)
        except asyncio.TimeoutError:
            oks = [_recipe_cache.get(it["url"], (0, False))[1] for _n, it in check]
        bad = {id(it) for (_n, it), good in zip(check, oks) if not good}
        out = {n: [it for it in items if id(it) not in bad] for n, items in out.items()}
    return out


# ---------------------------------------------------------------------------- merged search

_cache: dict[tuple, tuple[float, list]] = {}


def _tag(items: list[dict], name: str, cuisine: str) -> list[dict]:
    for it in items:
        it.setdefault("source", name)
        it.setdefault("cuisine", cuisine)
    return items


def merge(by: dict[str, list[dict]], order: list[str], limit: int = MAX_RESULTS) -> list[dict]:
    """Round-robin the sources in `order`, drop duplicate URLs, cap at `limit`."""
    queues = [list(by[n]) for n in order if by.get(n)]
    out, seen = [], set()
    while queues and len(out) < limit:
        for qu in list(queues):
            while qu:
                it = qu.pop(0)
                key = it["url"].split("#")[0].rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    out.append(it)
                    break
            if not qu:
                queues.remove(qu)
            if len(out) >= limit:
                break
    return out


def _job(http, name: str, q: str):
    if name == "giallozafferano":
        return gz_search(http, q)
    if name == "themealdb":
        return mealdb_search(http, q)
    return wp_search(http, WP_BY_ID[name], q)


def _meta(name: str) -> tuple[str, str, str]:
    if name in LEGACY:
        return LEGACY[name]
    s = WP_BY_ID[name]
    return s["name"], s["lang"], s["cuisine"]


async def _one(coro):
    return await asyncio.wait_for(coro, SOURCE_TIMEOUT)


async def search(http, q: str, sources: list[str], lang: str = "it") -> dict:
    q = q.strip()
    sources = [s for s in dict.fromkeys(sources) if s in LEGACY or s in WP_BY_ID]  # unknown ids are ignored
    key = (q.lower(), tuple(sources), lang)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    tasks = {n: asyncio.ensure_future(_one(_job(http, n, q))) for n in sources}
    if tasks:
        await asyncio.wait(tasks.values(), timeout=TOTAL_TIMEOUT)
    by, errors = {}, {}
    for name, t in tasks.items():
        if not t.done():
            t.cancel()
            errors[name] = "timeout"
        elif t.cancelled():
            errors[name] = "cancelled"
        elif t.exception():
            e = t.exception()
            errors[name] = str(e) or type(e).__name__
        else:
            sname, _l, scuisine = _meta(name)
            by[name] = _tag(t.result(), sname, scuisine)
            continue
        log.info("search %s failed: %s", name, errors[name])

    def rank(n):  # household language first (Italian sources for "it"); otherwise settings order
        return (0 if _meta(n)[1] == lang else 1, sources.index(n))
    by = await tidy(http, by, q)
    merged = merge(by, sorted(by, key=rank), limit=MAX_RESULTS * 2)
    # best matches first; the language/round-robin order breaks ties
    merged = sorted(merged, key=lambda it: -it.get("score", 0))
    seen: set[str] = set()  # the same dish from two sites: keep the first (best source)
    merged = [it for it in merged if not (_fold(it["title"]) in seen or seen.add(_fold(it["title"])))][:MAX_RESULTS]
    out = {"results": merged, "errors": errors}
    if out["results"] or not errors:
        _cache[key] = (time.time(), out)
    return out


def image_stem(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]
