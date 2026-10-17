"""Fetching recipes: any URL (schema.org), TheMealDB search, GialloZafferano search."""

from __future__ import annotations

import asyncio
import hashlib
import html as htmllib
import ipaddress
import logging
import re
import time
from urllib.parse import quote, urlparse

import aiohttp

from . import parse
from ...i18n import _

log = logging.getLogger("hub.recipes")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 Casa/1.0")
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
    """GET a page -> (html, final url)."""
    url = check_url(url)
    async with http.get(url, headers={"User-Agent": UA, "Accept-Language": "it,en;q=0.8",
                                      "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"},
                        timeout=TIMEOUT, allow_redirects=True) as r:
        if r.status >= 400:
            raise ValueError(_("The site answered HTTP {status}", status=r.status))
        data = await _read(r, MAX_PAGE)
        enc = r.charset or "utf-8"
        return data.decode(enc, errors="replace"), str(r.url)


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
                    "lang": "en", "mealdb": m["idMeal"],
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
                    "site": "giallozafferano.it", "lang": "it", "url": url, "subtitle": "GialloZafferano"})
        if len(out) >= 12:
            break
    return out


async def gz_search(http, q: str) -> list[dict]:
    slug = quote(q.strip().replace("/", " "), safe="")
    page, _ = await fetch_text(http, f"https://www.giallozafferano.it/ricerca-ricette/{slug}/")
    return gz_results(page)


# ---------------------------------------------------------------------------- merged search

_cache: dict[tuple, tuple[float, list]] = {}


async def search(http, q: str, sources: list[str], lang: str = "it") -> dict:
    q = q.strip()
    key = (q.lower(), tuple(sources), lang)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    jobs = {}
    if "giallozafferano" in sources:
        jobs["giallozafferano"] = gz_search(http, q)
    if "themealdb" in sources:
        jobs["themealdb"] = mealdb_search(http, q)
    res = await asyncio.gather(*jobs.values(), return_exceptions=True)
    by = {}
    errors = {}
    for name, r in zip(jobs, res):
        if isinstance(r, Exception):
            errors[name] = str(r) or type(r).__name__
            log.info("search %s failed: %s", name, errors[name])
        else:
            by[name] = r
    ordered = []
    first = ["giallozafferano", "themealdb"] if lang == "it" else ["themealdb", "giallozafferano"]
    for n in first:
        ordered += by.get(n, [])
    out = {"results": ordered, "errors": errors}
    if ordered or not errors:
        _cache[key] = (time.time(), out)
    return out


def image_stem(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]
