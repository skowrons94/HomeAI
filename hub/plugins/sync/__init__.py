"""Shared feed: a social layer (watched, ratings, watchlist, recipes and tips) shared with other Casa hubs
through a private git repo. No hub ever reaches another hub; each one pulls/pushes the repo with its own
deploy key. Contract: docs/sync.md.

    [plugins.sync]
    repo_url = "git@github.com:you/casa-feed.git"   # private repo
    user = "mango"                                  # your name in the feed
    # branch = "main"
    # interval = 10                                 # minutes
    # key_path = ""                                 # empty: data/sync/id_ed25519 (generated)
    # share_watchlist = true
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path

from aiohttp import web

from ...core import Plugin
from ...i18n import _
from ..recipes.parse import strip_accents
from . import recipes_md
from .db import SyncDB
from .feed import IMDB_RE, USER_RE, build_event
from .remote import Remote, Syncer

log = logging.getLogger("hub.sync")
EXTS = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}
MAX_IMAGE = 2_000_000
MAX_POSTER = 500_000
WATCHLIST_PER_CYCLE = 20


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


async def _body(request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise ValueError("expected a JSON body")
    if not isinstance(b, dict):
        raise ValueError("expected a JSON object")
    return b


def _media(t: str) -> str:
    t = {"tv": "series", "show": "series"}.get(str(t or "").lower(), str(t or "movie").lower())
    if t not in ("movie", "series"):
        raise ValueError("type must be movie or series")
    return t


class SyncPlugin(Plugin):
    name = "sync"
    title = "Shared feed"
    description = "Share watched titles, ratings, watchlist and recipes with other houses through a private git repo."
    icon = "users"
    ui = True
    schema = [
        {"key": "repo_url", "label": "Repository", "type": "text", "default": "", "required": True,
         "placeholder": "git@github.com:you/casa-feed.git",
         "help": "A private git repo shared by all houses. Add the public key shown below as a deploy key with write access."},
        {"key": "user", "label": "Your name in the feed", "type": "text", "default": "", "required": True,
         "placeholder": "mango", "help": "Lower-case letters, digits, _ and -."},
        {"key": "share_watchlist", "label": "Share my watchlist", "type": "bool", "default": True},
        {"key": "branch", "label": "Branch", "type": "text", "default": "main", "advanced": True},
        {"key": "interval", "label": "Sync every", "type": "number", "default": 10, "min": 2, "max": 240,
         "step": 1, "unit": "min", "advanced": True},
        {"key": "key_path", "label": "SSH key", "type": "path", "default": "", "advanced": True,
         "help": "Empty: Casa generates data/sync/id_ed25519 on first start."},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.db: SyncDB | None = None
        self.lock = asyncio.Lock()
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self.debounce = 5.0
        self._kick_task = None

    # -- config ---------------------------------------------------------------------------------

    @property
    def user(self) -> str:
        return str(self.config.get("user") or "").strip()

    @property
    def repo_url(self) -> str:
        return str(self.config.get("repo_url") or "").strip()

    @property
    def branch(self) -> str:
        return str(self.config.get("branch") or "main").strip()

    @property
    def interval(self) -> float:
        try:
            return min(240.0, max(2.0, float(self.config.get("interval", 10))))
        except (TypeError, ValueError):
            return 10.0

    @property
    def dir(self) -> Path:
        return Path(self.hub.data_dir) / "sync"

    @property
    def key_path(self) -> Path:
        return Path(str(self.config.get("key_path") or "").strip() or self.dir / "id_ed25519").expanduser()

    def missing(self) -> list[str]:
        return [k for k, ok in (("repo_url", bool(self.repo_url)), ("user", bool(USER_RE.match(self.user)))) if not ok]

    @property
    def configured(self) -> bool:
        return not self.missing()

    def _need(self) -> None:
        if self.missing():
            raise ValueError(_("Shared feed isn't set up: fill in {fields}", fields=" / ".join(_("Repository") if k == "repo_url" else _("Your name in the feed") for k in self.missing())))

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self):
        self.db = SyncDB(self.hub.store)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "pending").mkdir(exist_ok=True)
        self.last_sync = self.hub.store.get("sync.last_sync")
        self.hub.spawn(self._ensure_key(), "sync key")
        if self.configured:
            self.hub.every(self.interval * 60, self.sync, "shared feed sync", initial_delay=20)

    async def stop(self):
        if self._kick_task:
            self._kick_task.cancel()

    async def _ensure_key(self):
        key = self.key_path
        if key.exists():
            return
        key.parent.mkdir(parents=True, exist_ok=True)
        try:
            p = await asyncio.create_subprocess_exec(
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"casa-{self.user or 'hub'}", "-f", str(key),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(p.communicate(), 30)
            if p.returncode:
                log.warning("ssh-keygen failed: %s", err.decode(errors="replace").strip())
        except (OSError, asyncio.TimeoutError) as err:
            log.warning("can't create the SSH key: %s", err)

    def public_key(self) -> str:
        try:
            return self.key_path.with_name(self.key_path.name + ".pub").read_text().strip()
        except OSError:
            return ""

    def info(self) -> dict:
        users = set(self.db.users()) if self.db else set()
        if self.user:
            users.add(self.user)
        return {"configured": self.configured, "missing": self.missing(), "user": self.user,
                "users": sorted(users), "last_sync": self.last_sync, "last_error": self.last_error,
                "pending": self.db.pending() if self.db else 0, "public_key": self.public_key(),
                "repo_url": re.sub(r"://[^/@]*@", "://", self.repo_url)}

    def status(self):
        return self.info()

    # -- the cycle ------------------------------------------------------------------------------

    def kick(self) -> None:
        """Sync soon (debounced) after a local write."""
        if not self.configured:
            return
        if self._kick_task:
            self._kick_task.cancel()
        self._kick_task = self.hub.spawn(self._later(), "shared feed soon")

    async def _later(self):
        await asyncio.sleep(self.debounce)
        self._kick_task = None
        await self.sync()

    async def sync(self) -> dict:
        self._need()
        async with self.lock:
            try:
                await self._mirror_watchlist()
                key = self.key_path
                remote = Remote(self.repo_url, self.branch, self.dir / "repo", self.user,
                                str(key) if key.exists() else None)
                res = await Syncer(self.db, remote, self.user, self.dir / "pending").run()
                self.last_sync, self.last_error = time.time(), None
                self.hub.store.set("sync.last_sync", self.last_sync)
                log.debug("sync ok: %s", res)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.last_error = str(err)
                log.warning("shared feed sync failed: %s", err)
        return self.info()

    async def _mirror_watchlist(self) -> None:
        """Queue watchlist on/off events for what changed in the media plugin's watchlist."""
        if not self.config.get("share_watchlist", True) or not self.hub.plugins.get("media"):
            return
        try:
            rows = self.hub.store.query("SELECT id, type FROM media_watchlist")
        except sqlite3.Error:
            return
        now = {f"{r['type']}|{r['id']}" for r in rows}
        last = set(self.hub.store.get("sync.watchlist") or [])
        done, budget = set(last), WATCHLIST_PER_CYCLE
        for k, on in [*((k, True) for k in sorted(now - last)), *((k, False) for k in sorted(last - now))]:
            if budget <= 0:
                break
            budget -= 1
            type_, id_ = k.split("|", 1)
            try:
                await self.add_event("watchlist", type_, id_, on=on, kick=False)
                done.add(k) if on else done.discard(k)
            except Exception as err:
                log.debug("watchlist item %s not shared: %s", k, err)
        self.hub.store.set("sync.watchlist", sorted(done))

    # -- titles ---------------------------------------------------------------------------------

    async def _details(self, type_: str, id_: str) -> dict:
        media = self.hub.plugins.get("media")
        if media is None or getattr(media, "metadata", None) is None:
            raise ValueError(_("The Media plugin is needed to look this title up"))
        return await media.metadata.details(type_, id_)

    async def resolve(self, type_: str, id_: str) -> tuple[str, dict | None]:
        """imdb id (+ media metadata if it had to be fetched) for a media-plugin id or an imdb id."""
        id_ = str(id_ or "").strip()
        if IMDB_RE.match(id_):
            return id_, None
        if not re.fullmatch(r"tmdb:\d+", id_):
            raise ValueError("id must be an IMDb id (tt…) or tmdb:<number>")
        item = await self._details(type_, id_)
        imdb = item.get("imdb_id")
        if not imdb or not IMDB_RE.match(imdb):
            raise ValueError(_("This title has no IMDb id, so it can't be shared"))
        return imdb, item

    @staticmethod
    def make_card(type_: str, imdb: str, item: dict) -> dict:
        tmdb = str(item.get("id") or "")
        return {"imdb_id": imdb, "tmdb_id": int(tmdb[5:]) if re.fullmatch(r"tmdb:\d+", tmdb) else None,
                "type": type_, "title": item.get("title") or item.get("original_title") or "",
                "original_title": item.get("original_title") or item.get("title") or "",
                "year": item.get("year"), "poster": f"{imdb}.jpg" if item.get("poster") else None}

    async def _poster(self, url: str | None, imdb: str) -> str | None:
        http = self.hub.http
        if not url or http is None:
            return None
        try:
            async with http.get(url, timeout=20) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        except Exception as err:
            log.debug("poster download failed: %s", err)
            return None
        if not data or len(data) > MAX_POSTER:
            return None
        dest = self.dir / "pending" / f"{imdb}.jpg"
        dest.write_bytes(data)
        return str(dest)

    async def ensure_card(self, type_: str, imdb: str, item: dict | None = None) -> None:
        """Queue the title card (+ poster) unless the repo or the outbox already has it."""
        if self.db.card(imdb):
            return
        if item is None:
            try:
                item = await self._details(type_, imdb)
            except Exception as err:
                log.debug("no title card for %s: %s", imdb, err)
                return
        card = self.make_card(type_, imdb, item)
        poster = await self._poster(item.get("poster"), imdb)
        if not poster:
            card["poster"] = None
        self.db.queue("media", {"imdb_id": imdb, "card": card, "poster_file": poster})

    async def add_event(self, kind: str, type_: str, id_: str, rating=None, watched_at=None, on=None,
                        note: str = "", kick: bool = True) -> dict:
        self._need()
        media = _media(type_)
        imdb, item = await self.resolve(media, id_)
        tmdb = int(id_[5:]) if str(id_).startswith("tmdb:") else (self.make_card(media, imdb, item)["tmdb_id"] if item else None)
        ev = build_event(self.user, kind, media, imdb, tmdb_id=tmdb, rating=rating, on=on,
                         watched_at=watched_at, note=note or "")
        await self.ensure_card(media, imdb, item)
        self.db.queue("event", {"event": ev})
        if kick:
            self.kick()
        return ev

    # -- recipes --------------------------------------------------------------------------------

    def _book(self):
        r = self.hub.plugins.get("recipes")
        book = getattr(r, "book", None)
        if book is None:
            raise ValueError(_("The Recipes plugin isn't running"))
        return book

    def _file(self, sub: str, name: str) -> Path | None:
        for base in (self.dir / "repo" / sub, self.dir / "pending"):
            if (base / name).is_file():
                return base / name
        return None

    def recipe_view(self, slug: str) -> dict:
        rec = self.db.recipe(slug)
        if rec is None:
            raise KeyError(_("Recipe not found"))
        return {"slug": slug, "title": rec["title"], "author": rec["author"], "servings": rec["servings"],
                "time_min": rec["time_min"], "tags": rec["tags"], "source": rec["source"],
                "image": f"/api/p/sync/image/{rec['image']}" if rec["image"] else None,
                "ingredients": rec["ingredients"], "steps": rec["steps"], "tips": rec["tips"]}

    def share_recipe(self, recipe_id) -> dict:
        self._need()
        book = self._book()
        try:
            v = book.view(int(recipe_id))
        except (TypeError, ValueError):
            raise KeyError(_("Recipe not found"))
        title = recipes_md.one_line(v["title"])
        slug = recipes_md.pick_slug(title, self.user, self.db.recipe_index())
        total = v.get("total_min") or ((v.get("prep_min") or 0) + (v.get("cook_min") or 0)) or None
        try:
            source = recipes_md.check_source(v.get("source_url") or "")
        except ValueError:
            source = None
        image, image_file = None, None
        name = Path(v.get("image") or "").name
        src = book.img_dir / name if name else None
        if src and src.is_file() and src.suffix.lower() in EXTS and src.stat().st_size <= MAX_IMAGE:
            image = slug + EXTS[src.suffix.lower()]
            image_file = self.dir / "pending" / image
            shutil.copyfile(src, image_file)
        rec = recipes_md.validate_recipe({
            "title": title, "servings": v.get("base_servings") or v.get("servings"), "time_min": total,
            "tags": v.get("tags") or [], "author": self.user, "source": source, "image": image,
            "ingredients": v.get("ingredient_lines") or [], "steps": v.get("steps") or [], "tips": []})
        self.db.queue("recipe", {"slug": slug, "rec": rec, "image_file": str(image_file) if image_file else None})
        self.kick()
        return {"slug": slug}

    def add_tip(self, slug: str, text: str) -> dict:
        self._need()
        if self.db.recipe(slug) is None:
            raise KeyError(_("Recipe not found"))
        tip = recipes_md.check_tip(self.user, text)
        self.db.queue("tip", {"slug": slug, **tip})
        self.kick()
        return self.recipe_view(slug)

    def import_recipe(self, slug: str) -> dict:
        rec = self.db.recipe(slug)
        if rec is None:
            raise KeyError(_("Recipe not found"))
        book = self._book()
        image = ""
        src = self._file("recipes", rec["image"]) if rec["image"] else None
        if src:
            image = hashlib.sha1(f"{slug}".encode()).hexdigest()[:16] + src.suffix
            shutil.copyfile(src, book.img_dir / image)
        notes = "\n".join([f"Dalla ricetta condivisa di {rec['author']}"] + [f"({t['user']}) {t['text']}" for t in rec["tips"]])
        rid = book.add({"title": rec["title"], "servings": rec["servings"], "ingredients": rec["ingredients"],
                        "steps": rec["steps"], "tags": rec["tags"], "total_min": rec["time_min"], "notes": notes,
                        "lang": "it"}, image)
        return {"id": rid}

    # -- routes ---------------------------------------------------------------------------------

    def routes(self):
        return [
            web.get("/status", lambda r: _json(self.info())),
            web.post("/sync", self.r_sync),
            web.get("/feed", self.r_feed),
            web.get("/title/{imdb_id}", self.r_title),
            web.get("/lookup", self.r_lookup),
            web.post("/events", self.r_event),
            web.get("/poster/{imdb_id}", self.r_poster),
            web.get("/recipes", self.r_recipes),
            web.post("/recipes/share", self.r_share),
            web.get("/recipes/{slug}", lambda r: _json(self.recipe_view(r.match_info["slug"]))),
            web.post("/recipes/{slug}/tips", self.r_tip),
            web.post("/recipes/{slug}/import", lambda r: _json(self.import_recipe(r.match_info["slug"]))),
            web.get("/image/{name}", self.r_image),
        ]

    async def r_sync(self, request):
        return _json(await self.sync())

    async def r_feed(self, request):
        q = request.query
        try:
            limit = max(1, min(500, int(q.get("limit", 100))))
        except ValueError:
            raise ValueError("limit must be a number")
        return _json(self.db.feed(q.get("user", ""), q.get("kind", ""), q.get("media", ""), limit, q.get("before", "")))

    def _imdb(self, request) -> str:
        i = request.match_info["imdb_id"]
        if not IMDB_RE.match(i):
            raise ValueError("bad IMDb id")
        return i

    async def r_title(self, request):
        return _json(self.db.title(self._imdb(request), self.user))

    async def r_lookup(self, request):
        type_ = _media(request.query.get("type", "movie"))
        imdb, item = await self.resolve(type_, request.query.get("id", ""))
        res = self.db.title(imdb, self.user)
        if res["card"] is None:
            try:
                res["card"] = self.make_card(type_, imdb, item or await self._details(type_, imdb))
            except Exception as err:
                log.debug("lookup without card: %s", err)
        return _json({"imdb_id": imdb, **res})

    async def r_event(self, request):
        b = await _body(request)
        kind = b.get("kind")
        w = b.get("watched_at")
        if kind == "watch" and "watched_at" not in b:
            w = date.today().isoformat()
        return _json(await self.add_event(kind, b.get("type", "movie"), b.get("id") or b.get("imdb_id"),
                                          rating=b.get("rating"), watched_at=w, on=b.get("on"),
                                          note=b.get("note") or ""))

    async def r_poster(self, request):
        f = self._file("media", f"{self._imdb(request)}.jpg")
        if not f:
            raise KeyError("poster not found")
        return web.FileResponse(f, headers={"Cache-Control": "public, max-age=2592000"})

    async def r_recipes(self, request):
        q, tag, author = request.query.get("q", ""), request.query.get("tag", "").lower(), request.query.get("author", "")
        words = strip_accents(q.lower()).split()
        out = []
        for slug, r, updated in self.db.recipes():
            if (tag and tag not in r["tags"]) or (author and r["author"] != author):
                continue
            hay = strip_accents(" ".join([r["title"], *r["ingredients"], *r["tags"]]).lower())
            if not all(w in hay for w in words):
                continue
            out.append({"slug": slug, "title": r["title"], "author": r["author"], "tags": r["tags"],
                        "servings": r["servings"], "time_min": r["time_min"],
                        "image": f"/api/p/sync/image/{r['image']}" if r["image"] else None,
                        "tips_count": len(r["tips"]), "updated": updated})
        return _json(out)

    async def r_share(self, request):
        return _json(self.share_recipe((await _body(request)).get("recipe_id")))

    async def r_tip(self, request):
        return _json(self.add_tip(request.match_info["slug"], (await _body(request)).get("text") or ""))

    async def r_image(self, request):
        name = request.match_info["name"]
        f = self._file("recipes", name) if re.fullmatch(r"[a-z0-9-]+\.(?:jpg|png|webp)", name) else None
        if not f:
            raise KeyError("image not found")
        return web.FileResponse(f, headers={"Cache-Control": "public, max-age=2592000"})


PLUGIN = SyncPlugin
