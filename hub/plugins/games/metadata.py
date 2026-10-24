"""Game metadata (IGDB) and artwork (SteamGridDB), cached. Network only on demand / in the background.

Metadata lives in the hub store under ``games.meta.<game id>``; images in ``<data dir>/games-art``
(capped, oldest evicted). Missing keys simply turn the feature off.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp

from ...i18n import _

log = logging.getLogger("hub.games")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_URL = "https://api.igdb.com/v4/games"
IMG_URL = "https://images.igdb.com/igdb/image/upload/%s/%s.jpg"
SGDB_URL = "https://www.steamgriddb.com/api/v2"
IGDB_GAP = 0.3                    # IGDB allows 4 requests/s
MAX_IMAGE = 8 * 1024 * 1024
CACHE_CAP = 200 * 1024 * 1024
SHOTS = 4
FIELDS = ("name,summary,genres.name,first_release_date,total_rating,involved_companies.company.name,"
          "involved_companies.developer,cover.image_id,screenshots.image_id,artworks.image_id,"
          "platforms.abbreviation,platforms.name")
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,150}$")


class MetaError(Exception):
    pass


# -- titles and platforms -----------------------------------------------------------------------

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_NOISE = re.compile(
    r"\b(v\s?\d[\w.+-]*|build\s?\d+|repack|fitgirl|dodi|gog|drm[- ]?free|multi\d*|portable|setup|installer|"
    r"update\s?\d*|incl\.?|dlcs?|goty|proper|rev\s?\w+|beta|\d+\.\d+(\.\d+)*)\b", re.I)


def clean_title(title: str, kind: str = "") -> str:
    """'Celeste v1.4 [FitGirl Repack]' → 'Celeste'; ROM region/dump tags ((USA), [!]) go too."""
    t = title or ""
    if t.count(" ") == 0 and t.count(".") >= 2:        # scene name: The.Game.Name.v1.2-GROUP
        t = t.replace(".", " ")
    t = t.replace("_", " ")
    t = _BRACKETS.sub(" ", t)
    t = re.sub(r"\[!\]|\[[a-z]\d*\]", " ", t)           # leftovers such as [!]
    t = _NOISE.sub(" ", t)
    t = re.sub(r"\s*[-–]\s*$|^\s*[-–]\s*", " ", t)
    t = re.sub(r"[-–]\s+(?=[-–]|$)", " ", t)
    return " ".join(t.split()) or " ".join((title or "").split())


# EmuDeck system folder → IGDB platform abbreviations / names (lower case)
PLATFORMS = {
    "pc": {"pc", "win", "pc (microsoft windows)"}, "linux": {"linux", "pc", "win"},
    "nes": {"nes", "famicom"}, "snes": {"snes", "sfam", "super famicom"}, "n64": {"n64"},
    "gb": {"gb", "game boy"}, "gbc": {"gbc", "game boy color"}, "gba": {"gba", "game boy advance"},
    "nds": {"nds", "nintendo ds"}, "3ds": {"3ds", "nintendo 3ds"}, "n3ds": {"3ds", "nintendo 3ds"}, "gc": {"ngc", "gc", "nintendo gamecube"},
    "gamecube": {"ngc", "gc", "nintendo gamecube"}, "wii": {"wii"}, "wiiu": {"wiiu", "wii u"},
    "switch": {"switch", "nintendo switch"},
    "psx": {"ps", "ps1", "psx", "playstation"}, "ps2": {"ps2", "playstation 2"}, "ps3": {"ps3", "playstation 3"},
    "psp": {"psp", "playstation portable"}, "psvita": {"vita", "playstation vita"},
    "genesis": {"genesis", "md", "megadrive", "sega mega drive/genesis", "genesis/mega drive"},
    "megadrive": {"genesis", "md", "megadrive", "sega mega drive/genesis", "genesis/mega drive"},
    "mastersystem": {"sms", "sega master system/mark iii"}, "gamegear": {"gg", "sega game gear"},
    "segacd": {"segacd", "sega cd"}, "saturn": {"saturn", "sega saturn"}, "dreamcast": {"dc", "dreamcast"},
    "atari2600": {"atari2600", "atari 2600"}, "arcade": {"arcade"}, "neogeo": {"neogeo", "neo geo aes", "neo geo mvs"},
    "pcengine": {"tg16", "pce", "turbografx-16/pc engine"}, "tg16": {"tg16", "pce", "turbografx-16/pc engine"},
}


def igdb_platforms(platform: str) -> set[str]:
    return PLATFORMS.get(platform or "", set())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("&", " and ")).strip()


def score(wanted: str, cand: dict, platform: str = "") -> float:
    """How well an IGDB hit matches the title (0-1.4); a platform match breaks ties and is preferred."""
    a, b = _norm(wanted), _norm(cand.get("name", ""))
    if not a or not b:
        return 0.0
    s = difflib.SequenceMatcher(None, a, b).ratio()
    if a == b:
        s = 1.0
    elif b.startswith(a + " ") or a.startswith(b + " "):
        s = max(s, 0.7)
    want = igdb_platforms(platform)
    if want:
        have = {str(p.get(k, "")).lower() for p in cand.get("platforms") or [] for k in ("abbreviation", "name")}
        if have & want:
            s += 0.3
        elif have:
            s -= 0.1
    return s


def pick_best(wanted: str, results: list[dict], platform: str = "") -> dict | None:
    ranked = sorted(((score(wanted, r, platform), -i, r) for i, r in enumerate(results or []) if isinstance(r, dict)),
                    key=lambda x: (x[0], x[1]), reverse=True)
    return ranked[0][2] if ranked and ranked[0][0] >= 0.62 else None


def to_meta(item: dict) -> dict:
    """One IGDB game → our metadata (image ids included, files are added once downloaded)."""
    def ids(key):
        return [x["image_id"] for x in item.get(key) or [] if isinstance(x, dict) and x.get("image_id")]

    dev = None
    for ic in item.get("involved_companies") or []:
        if isinstance(ic, dict) and ic.get("developer") and isinstance(ic.get("company"), dict):
            dev = ic["company"].get("name")
            break
    released = None
    if isinstance(item.get("first_release_date"), (int, float)):
        try:
            released = datetime.fromtimestamp(item["first_release_date"], timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass
    rating = item.get("total_rating")
    return {
        "igdb_id": item.get("id"), "title": item.get("name") or "", "summary": item.get("summary") or "",
        "genres": [g["name"] for g in item.get("genres") or [] if isinstance(g, dict) and g.get("name")],
        "released": released, "rating": int(round(rating)) if isinstance(rating, (int, float)) else None,
        "developer": dev,
        "platforms": [p.get("abbreviation") or p.get("name") for p in item.get("platforms") or [] if isinstance(p, dict)],
        "_cover": (item.get("cover") or {}).get("image_id") if isinstance(item.get("cover"), dict) else None,
        "_shots": ids("screenshots")[:SHOTS], "_art": ids("artworks")[:1],
    }


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


# -- image cache --------------------------------------------------------------------------------

class ImageCache:
    def __init__(self, folder: Path, cap: int = CACHE_CAP):
        self.dir, self.cap = Path(folder), cap

    def path(self, name: str) -> Path | None:
        """File for a cache name, or None for an unsafe/missing one."""
        if not isinstance(name, str) or not SAFE_NAME.match(name):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def put(self, name: str, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.dir / name)

    def purge(self, prefix: str) -> None:
        if self.dir.is_dir():
            for f in self.dir.glob(prefix + ".*"):
                try:
                    f.unlink()
                except OSError:
                    pass

    def total(self) -> int:
        try:
            return sum(f.stat().st_size for f in self.dir.iterdir() if f.is_file())
        except OSError:
            return 0

    def evict(self) -> int:
        """Delete the oldest files until the cache fits; returns files removed."""
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f) for f in self.dir.iterdir() if f.is_file()]
        except OSError:
            return 0
        total, n = sum(s for _p, s, _t in files), 0
        for _m, size, f in sorted(files, key=lambda x: x[0]):
            if total <= self.cap:
                break
            try:
                f.unlink()
                total -= size
                n += 1
            except OSError:
                pass
        return n


# -- the service --------------------------------------------------------------------------------

class Metadata:
    def __init__(self, store, session: aiohttp.ClientSession, folder: Path, igdb_id: str = "",
                 igdb_secret: str = "", sgdb_key: str = "", cap: int = CACHE_CAP):
        self.store, self.session = store, session
        self.cache = ImageCache(folder, cap)
        self.igdb_id, self.igdb_secret, self.sgdb_key = igdb_id.strip(), igdb_secret.strip(), sgdb_key.strip()
        self._token, self._token_exp = "", 0.0
        self._gap = asyncio.Lock()
        self._last = 0.0
        self.error = ""
        self._mem: dict[str, dict | None] = {}

    @property
    def igdb(self) -> bool:
        return bool(self.igdb_id and self.igdb_secret)

    @property
    def steamgriddb(self) -> bool:
        return bool(self.sgdb_key)

    @property
    def configured(self) -> bool:
        return self.igdb or self.steamgriddb

    # -- stored records
    def get(self, gid: str) -> dict | None:
        if gid not in self._mem:
            v = self.store.get("games.meta." + gid)
            self._mem[gid] = v if isinstance(v, dict) else None
        return self._mem[gid]

    def _save(self, gid: str, meta: dict) -> None:
        self._mem[gid] = meta
        self.store.set("games.meta." + gid, meta)

    def public(self, gid: str) -> dict | None:
        m = self.get(gid)
        if not m or not m.get("igdb_id"):
            return None
        files = m.get("files") or {}
        return {"title": m.get("title") or "", "summary": m.get("summary") or "", "genres": m.get("genres") or [],
                "released": m.get("released"), "rating": m.get("rating"), "developer": m.get("developer"),
                "igdb_id": m.get("igdb_id"), "matched_by": m.get("matched_by", "auto"),
                "screenshots": ["/api/p/games/img/" + n for n in files.get("shots") or [] if self.cache.path(n)],
                "has_hero": bool(files.get("hero") and self.cache.path(files["hero"]))}

    def file(self, gid: str, role: str) -> Path | None:
        m = self.get(gid) or {}
        name = (m.get("files") or {}).get(role)
        return self.cache.path(name) if name else None

    # -- IGDB
    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            async with self.session.post(TOKEN_URL, params={
                    "client_id": self.igdb_id, "client_secret": self.igdb_secret, "grant_type": "client_credentials"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
                if r.status >= 400 or not isinstance(data, dict) or not data.get("access_token"):
                    raise MetaError(_("IGDB: check the client id and secret"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise MetaError(_("IGDB unreachable: {err}", err=str(err) or type(err).__name__))
        self._token, self._token_exp = data["access_token"], time.time() + float(data.get("expires_in") or 3600)
        return self._token

    async def _igdb(self, body: str) -> list[dict]:
        if not self.igdb:
            raise MetaError(_("IGDB keys not set"))
        for attempt in (0, 1):
            token = await self._auth()
            async with self._gap:               # serialised, with a pause: 4 req/s limit
                wait = self._last + IGDB_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self.session.post(IGDB_URL, data=body, headers={
                            "Client-ID": self.igdb_id, "Authorization": "Bearer " + token, "Accept": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
                        status = r.status
                        data = await r.json(content_type=None) if status < 400 else None
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                    raise MetaError(_("IGDB unreachable: {err}", err=str(err) or type(err).__name__))
                finally:
                    self._last = time.monotonic()
            if status == 401 and attempt == 0:
                self._token = ""
                continue
            if status == 429:
                raise MetaError(_("IGDB rate limit, try again later"))
            if status >= 400 or not isinstance(data, list):
                raise MetaError(_("IGDB answered HTTP {code}", code=status))
            return data
        raise MetaError(_("IGDB refused the token"))

    @staticmethod
    def _q(s: str) -> str:
        return s.replace("\\", " ").replace('"', " ")

    async def search(self, term: str, limit: int = 10) -> list[dict]:
        return await self._igdb('search "%s"; fields %s; limit %d;' % (self._q(term), FIELDS, limit))

    async def by_id(self, igdb_id: int) -> dict | None:
        res = await self._igdb("fields %s; where id = %d; limit 1;" % (FIELDS, int(igdb_id)))
        return res[0] if res else None

    # -- images
    async def _download(self, url: str, name_base: str) -> str | None:
        if not url.startswith("https://"):
            return None
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200 or (r.content_length or 0) > MAX_IMAGE:
                    return None
                data = await r.content.read(MAX_IMAGE + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        ext = sniff(data) if len(data) <= MAX_IMAGE else None
        if not ext:
            return None
        name = name_base + ext
        if not SAFE_NAME.match(name):
            return None
        await asyncio.get_running_loop().run_in_executor(None, self.cache.put, name, data)
        return name

    async def _sgdb(self, path: str) -> list[dict]:
        try:
            async with self.session.get(SGDB_URL + path, headers={"Authorization": "Bearer " + self.sgdb_key},
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status in (401, 403):
                    raise MetaError(_("SteamGridDB: check the API key"))
                if r.status >= 400:
                    return []
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []
        d = data.get("data") if isinstance(data, dict) else None
        return d if isinstance(d, list) else []

    async def _sgdb_art(self, gid: str, title: str, files: dict) -> None:
        found = await self._sgdb("/search/autocomplete/" + quote(title, safe=""))
        hit = pick_best(title, [{"name": f.get("name"), "id": f.get("id")} for f in found if isinstance(f, dict)])
        if not hit:
            return
        sid = hit["id"]
        for role, route, dims in (("grid", "grids", "600x900"), ("wide", "grids", "920x430"),
                                  ("hero", "heroes", ""), ("logo", "logos", "")):
            # the Deck agent takes PNG/JPEG only (SGDB would also return webp); logos are PNG/webp
            mimes = "image/png" if route == "logos" else "image/png,image/jpeg"
            q = "?types=static&limit=3&mimes=" + quote(mimes, safe="") + ("&dimensions=" + dims if dims else "")
            for item in await self._sgdb("/%s/game/%d%s" % (route, sid, q)):
                url = item.get("url") if isinstance(item, dict) else None
                name = await self._download(url, "%s.%s" % (gid, role)) if isinstance(url, str) else None
                if name:
                    files[role] = name
                    break

    # -- one game
    async def fetch(self, g: dict, igdb_id: int | None = None, manual: bool = False, raise_errors: bool = False) -> dict | None:
        """Look the game up and cache its metadata and images. Returns the stored record (or None)."""
        gid, kind = g["id"], g.get("kind", "")
        title = clean_title(g["title"], kind)
        old = self.get(gid) or {}
        try:
            meta = None
            if self.igdb:
                wanted = igdb_id or (old.get("igdb_id") if old.get("matched_by") == "manual" else None)
                if wanted:
                    item = await self.by_id(wanted)
                else:
                    item = pick_best(title, await self.search(title), g.get("platform", ""))
                if item:
                    meta = to_meta(item)
                    meta["matched_by"] = "manual" if (igdb_id or manual or wanted) else "auto"
            if meta is None:
                meta = {"igdb_id": None, "title": title, "matched_by": "auto", "_cover": None, "_shots": [], "_art": []}
            self.cache.purge(gid)
            files: dict = {}
            if meta["_cover"]:
                n = await self._download(IMG_URL % ("t_cover_big", meta["_cover"]), gid + ".cover")
                if n:
                    files["cover"] = n
            for i, sid in enumerate(meta["_shots"]):
                n = await self._download(IMG_URL % ("t_screenshot_big", sid), "%s.shot%d" % (gid, i))
                if n:
                    files.setdefault("shots", []).append(n)
            if meta["_art"]:
                n = await self._download(IMG_URL % ("t_1080p", meta["_art"][0]), gid + ".hero")
                if n:
                    files["hero"] = n
            if self.steamgriddb:
                await self._sgdb_art(gid, meta["title"] or title, files)
            if "hero" not in files and files.get("shots"):      # a screenshot beats an empty banner
                files["hero"] = files["shots"][0]
            if "cover" not in files and files.get("grid"):
                files["cover"] = files["grid"]
            for k in ("_cover", "_shots", "_art"):
                meta.pop(k, None)
            meta.update(files=files, fetched=time.time())
            self._save(gid, meta)
            await asyncio.get_running_loop().run_in_executor(None, self.cache.evict)
            self.error = ""
            return meta
        except MetaError as err:
            self.error = str(err)
            log.info("games metadata: %s", err)
            if raise_errors:
                raise
            return None

    def forget(self, gid: str) -> None:
        self._mem.pop(gid, None)
        self.store.delete("games.meta." + gid)
        self.cache.purge(gid)
