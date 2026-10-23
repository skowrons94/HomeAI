"""Games: browse the games on the NAS and install them on the Steam Deck from the phone.

    [plugins.games]
    library = "/mnt/nas/games"        # default: <NAS mount>/games
    [plugins.games.deck]
    host = "steamdeck.local"
    port = 8765
    token = "…"                       # printed by deploy/deck/install-deck.sh

The Deck runs a small agent (deploy/deck/). Casa tells it what to install, the agent downloads the
files from Casa (signed, expiring links), puts them in place and registers them with Steam.
See docs/games.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from ...core import Plugin
from ...i18n import _
from ...settings import get_path
from ..storage import DEFAULT_MOUNT
from . import find, library
from .deck import Deck, DeckError
from .find import Finder
from .metadata import MetaError, Metadata

log = logging.getLogger("hub.games")

TTL = 6 * 3600          # life of a download link
RESCAN = 600
ART_MAX = 1536 * 1024        # skip any image bigger than this when sending art to the Deck
ART_BUDGET = 4 * 1024 * 1024 # the agent accepts 8 MB per request (base64 adds a third)


def _json(data, status=200):
    return web.json_response(data, status=status)


def _fail(err: DeckError):
    """404 from the agent is "not found" for the app; everything else is a 400 with its message."""
    return KeyError(str(err)) if err.status == 404 else ValueError(str(err))


class GamesPlugin(Plugin):
    name = "games"
    title = "Games"
    description = "Games on the NAS, installed on the Steam Deck with one tap"
    icon = "play"
    schema = [
        {"key": "library", "label": "Games folder", "type": "path", "default": "",
         "help": "Contains pc/ and emu/ (see docs/games.md). Empty = games/ on the NAS."},
        {"key": "deck.host", "label": "Steam Deck address", "type": "text", "placeholder": "steamdeck.local",
         "required": True, "help": "Name or IP of the Deck. It must be awake and on Wi-Fi."},
        {"key": "deck.token", "label": "Deck token", "type": "password", "required": True,
         "help": "Printed by deploy/deck/install-deck.sh on the Deck."},
        {"key": "deck.port", "label": "Agent port", "type": "number", "default": 8765, "min": 1024,
         "max": 65535, "advanced": True},
        {"key": "base_url", "label": "Address of this hub, as the Deck sees it", "type": "text",
         "advanced": True, "placeholder": "http://casa.local:8080",
         "help": "Empty = the address you are using in the app right now."},
        {"key": "igdb.client_id", "label": "IGDB client id", "type": "text", "advanced": True,
         "help": "Covers, descriptions, genres and screenshots. Free Twitch developer app.",
         "link": {"url": "https://dev.twitch.tv/console/apps", "label": "Twitch developer console"}},
        {"key": "igdb.client_secret", "label": "IGDB client secret", "type": "password", "advanced": True,
         "help": "The secret of the same Twitch app (empty = keep).",
         "link": {"url": "https://dev.twitch.tv/console/apps", "label": "Twitch developer console"}},
        {"key": "steamgriddb.key", "label": "SteamGridDB API key", "type": "password", "advanced": True,
         "help": "Nicer covers, banners and logos, also used for the Steam shortcuts on the Deck.",
         "link": {"url": "https://www.steamgriddb.com/profile/preferences/api", "label": "SteamGridDB preferences"}},
    ]

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.secret = secrets.token_bytes(32)
        self.games: dict[str, dict] = {}
        self.problems: list[dict] = []
        self.scanned = 0.0
        self.deck = Deck(str(get_path(self.config, "deck.host", "") or ""),
                         int(get_path(self.config, "deck.port", 8765) or 8765),
                         str(get_path(self.config, "deck.token", "") or ""), self.session)
        self._deck_cache: tuple[float, dict] = (0, {})
        self.meta = Metadata(self.hub.store, self.session, Path(self.hub.data_dir) / "games-art",
                             str(get_path(self.config, "igdb.client_id", "") or ""),
                             str(get_path(self.config, "igdb.client_secret", "") or ""),
                             str(get_path(self.config, "steamgriddb.key", "") or ""))
        self.finder = Finder(self)
        self._meta_queue: dict[str, bool] = {}      # game id → force
        self._meta_task: asyncio.Task | None = None
        self._meta_busy = False
        await self._scan()
        self.hub.every(RESCAN, self._scan, name="games-rescan")
        self.hub.every(5, self.finder.poll, name="games-downloads")
        self.hub.spawn(self.finder.resume(), name="games-resume")

    async def stop(self):
        if self._meta_task and not self._meta_task.done():
            self._meta_task.cancel()
        await self.session.close()

    # -- library ---------------------------------------------------------------------------

    @property
    def root(self) -> str:
        cfg = str(self.config.get("library") or "")
        if cfg:
            return cfg
        nas = ((self.hub.config.get("plugins") or {}).get("storage") or {}).get("nas") or {}
        return os.path.join(nas.get("mount_point") or DEFAULT_MOUNT, "games")

    async def _scan(self):
        loop = asyncio.get_running_loop()
        self.games, self.problems = {}, []
        games, self.problems = await loop.run_in_executor(None, library.scan, self.root)
        self.games = {g["id"]: g for g in games}
        self.scanned = time.time()
        if self.meta.configured:
            for gid in self.games:
                if self.meta.get(gid) is None:
                    self._meta_queue.setdefault(gid, False)
            self._kick_meta()

    async def rescan(self):
        await self._scan()

    # -- metadata: one game at a time, in the background, never during start()
    def _kick_meta(self, delay: float = 5.0):
        if self._meta_queue and (self._meta_task is None or self._meta_task.done()):
            self._meta_task = asyncio.ensure_future(self._meta_worker(delay))

    async def _meta_worker(self, delay: float):
        await asyncio.sleep(delay)
        while self._meta_queue:
            gid, force = next(iter(self._meta_queue.items()))
            self._meta_queue.pop(gid)
            g = self.games.get(gid)
            if not g:
                continue
            self._meta_busy = True
            try:
                if await self.meta.fetch(g) is None and self.meta.error:
                    self._meta_queue.clear()    # IGDB/network trouble: try again after the next rescan
                    return
            except Exception:
                log.exception("metadata for %s", gid)
            finally:
                self._meta_busy = False
            await asyncio.sleep(1)

    def status(self):
        return {"games": len(self.games), "library": self.root}

    # -- signed download links ---------------------------------------------------------------

    def _sig(self, gid: str, exp: int) -> str:
        return hmac.new(self.secret, f"{gid}|{exp}".encode(), hashlib.sha256).hexdigest()

    # -- deck state --------------------------------------------------------------------------

    async def _deck_state(self, fresh: bool = False) -> dict:
        if not self.deck.configured:
            return {"online": False, "error": _("Set the Steam Deck address and token in Settings → Games."),
                    "installed": {}, "jobs": {}, "pending": 0}
        if not fresh and time.monotonic() - self._deck_cache[0] < 2:
            return self._deck_cache[1]
        try:
            d = await self.deck.call("GET", "/status")
            st = {"online": True, "error": None, "installed": d.get("installed") or {},
                  "jobs": d.get("jobs") or {}, "pending": int(d.get("pending") or 0),
                  "steam_running": bool(d.get("steam_running"))}
        except DeckError as err:
            st = {"online": False, "error": str(err), "installed": {}, "jobs": {}, "pending": 0}
        self._deck_cache = (time.monotonic(), st)
        return st

    @staticmethod
    def _state(g: dict, st: dict) -> dict:
        inst, job = st["installed"].get(g["id"]), st["jobs"].get(g["id"])
        out = {"state": "not_installed", "installed_version": inst.get("version") if inst else None,
               "note": (inst or {}).get("note") or None, "progress": None, "message": None}
        if inst:
            out["state"] = "update" if g["version"] and inst.get("version") != g["version"] else "installed"
        if job and job["state"] in ("queued", "downloading", "finalizing"):
            out.update(state="installing", progress=job.get("done"), message=job.get("message"))
        elif job and job["state"] == "failed":
            out.update(state="failed", message=job.get("message"))
        elif job and job["state"] == "needs_manual":
            out.update(state="needs_manual", message=job.get("message"))
        return out

    # -- routes ------------------------------------------------------------------------------

    def routes(self):
        return [
            web.get("/library", self.r_library),
            web.post("/rescan", self.r_rescan),
            web.post("/test", self.r_test),
            web.post("/install", self.r_install),
            web.post("/uninstall", self.r_uninstall),
            web.post("/apply", self.r_apply),
            web.get("/cover/{id}", self.r_cover),
            web.get("/hero/{id}", self.r_hero),
            web.get("/img/{name}", self.r_img),
            web.post("/meta/refresh", self.r_meta_refresh),
            web.get("/meta/search", self.r_meta_search),
            web.post("/meta/match", self.r_meta_match),
            web.get("/find", self.r_find),
            web.get("/find/platforms", self.r_find_platforms),
            web.post("/find/download", self.r_find_download),
            web.get("/find/downloads", self.r_find_downloads),
            web.delete("/find/downloads/{hash}", self.r_find_cancel),
            web.get("/dl/{id}/{path:.*}", self.r_download),
        ]

    async def _body(self, request) -> dict:
        try:
            b = await request.json()
        except Exception:
            raise ValueError(_("Expected a JSON body"))
        if not isinstance(b, dict):
            raise ValueError(_("Expected a JSON body"))
        return b

    def _game(self, gid) -> dict:
        g = self.games.get(str(gid))
        if not g:
            raise KeyError(_("Game not found"))
        return g

    def _systems(self) -> list[dict]:
        """[{key, count}] per platform (pc, linux, snes…), biggest first."""
        counts: dict[str, int] = {}
        for g in self.games.values():
            k = library.platform(g)
            counts[k] = counts.get(k, 0) + 1
        return [{"key": k, "count": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def _item(self, g: dict, st: dict) -> dict:
        item = {**library.public(g), **self._state(g, st), "meta": self.meta.public(g["id"])}
        item["cover"] = bool(g.get("_cover") or self.meta.file(g["id"], "cover"))
        return item

    async def r_library(self, request):
        st = await self._deck_state()
        items = [self._item(g, st) for g in self.games.values()]
        pending = len(self._meta_queue) + (1 if self._meta_busy else 0)
        return _json({"metadata": {"configured": self.meta.configured, "igdb": self.meta.igdb,
                                   "steamgriddb": self.meta.steamgriddb, "pending": pending,
                                   "error": self.meta.error or None},
                      "games": items, "problems": self.problems, "root": self.root, "scanned": self.scanned,
                      "exists": os.path.isdir(os.path.expanduser(self.root)), "systems": self._systems(),
                      "deck": {**{k: st.get(k) for k in ("online", "error", "pending", "steam_running")},
                               "configured": self.deck.configured, "host": self.deck.host}})

    async def r_test(self, request):
        """Ask the Deck agent right now (no cache) and say what is wrong, if anything."""
        st = await self._deck_state(fresh=True)
        return _json({"ok": bool(st["online"]), "error": st.get("error"), "pending": st.get("pending", 0),
                      "steam_running": bool(st.get("steam_running")), "installed": len(st.get("installed") or {})})

    async def r_rescan(self, request):
        await self._scan()
        return await self.r_library(request)

    def _base(self, request) -> str:
        return (str(self.config.get("base_url") or "").rstrip("/")) or str(request.url.origin())

    def _art(self, g: dict) -> dict:
        """Cover art for the Steam shortcut of a portable/native game (only what we have, small)."""
        if g["kind"] not in ("portable", "native"):
            return {}
        out, used = {}, 0
        local = Path(g["_cover"]) if g.get("_cover") else None
        for role in ("grid", "wide", "logo", "hero"):
            f = self.meta.file(g["id"], role)
            if role == "grid" and local and local.suffix.lower() in (".jpg", ".jpeg", ".png"):
                f = local                       # a cover.jpg in the game folder wins
            try:
                if not f or f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                size = f.stat().st_size
                if size > ART_MAX or used + size > ART_BUDGET:
                    continue
                raw = f.read_bytes()
                if not raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):    # the agent takes PNG/JPEG only
                    continue
                out[role] = base64.b64encode(raw).decode()
                used += size
            except OSError:
                continue
        return out

    async def r_install(self, request):
        g = self._game((await self._body(request)).get("id"))
        exp = int(time.time()) + TTL
        art = self._art(g)
        job = {"id": g["id"], "title": g["title"], "version": g["version"], "kind": g["kind"],
               "system": g["system"], "exe": g["exe"], "folder": g["folder"] or g["title"] if g["kind"] != "rom" else g["folder"],
               "files": g["_list"], "base": self._base(request), "exp": exp, "sig": self._sig(g["id"], exp)}
        if art:
            job["art"] = art
        try:
            await self.deck.call("POST", "/install", job, timeout=15)
        except DeckError as err:
            raise _fail(err)
        self._deck_cache = (0, {})
        return _json({"ok": True})

    async def r_uninstall(self, request):
        gid = str((await self._body(request)).get("id") or "")
        try:
            res = await self.deck.call("POST", "/uninstall", {"id": gid}, timeout=15)
        except DeckError as err:
            raise _fail(err)
        self._deck_cache = (0, {})
        return _json(res if isinstance(res, dict) else {"ok": True})

    async def r_apply(self, request):
        b = await self._body(request)
        try:
            return _json(await self.deck.call("POST", "/apply", {"restart": bool(b.get("restart"))}, timeout=90))
        except DeckError as err:
            raise _fail(err)
        finally:
            self._deck_cache = (0, {})

    async def r_cover(self, request):
        g = self._game(request.match_info["id"])
        if g.get("_cover") and Path(g["_cover"]).is_file():
            return web.FileResponse(g["_cover"], headers={"Cache-Control": "max-age=3600"})
        f = self.meta.file(g["id"], "cover")
        if not f:
            raise KeyError(_("Game not found"))
        return web.FileResponse(f, headers={"Cache-Control": "max-age=3600"})

    async def r_hero(self, request):
        g = self._game(request.match_info["id"])
        f = self.meta.file(g["id"], "hero")
        if not f:
            raise KeyError(_("Game not found"))
        return web.FileResponse(f, headers={"Cache-Control": "max-age=3600"})

    async def r_img(self, request):
        f = self.meta.cache.path(request.match_info["name"])
        if not f:
            raise KeyError(_("Game not found"))
        return web.FileResponse(f, headers={"Cache-Control": "max-age=86400"})

    # -- metadata
    async def r_meta_refresh(self, request):
        try:
            b = await request.json()
        except Exception:
            b = {}
        gid = (b or {}).get("id") if isinstance(b, dict) else None
        if not self.meta.configured:
            raise ValueError(_("Set the IGDB or SteamGridDB keys in Settings → Games."))
        if gid:
            g = self._game(gid)
            try:
                await self.meta.fetch(g, raise_errors=True)
            except MetaError as err:
                raise ValueError(str(err))
        else:
            self._meta_queue.update({k: True for k in self.games})
            self._kick_meta(delay=0.5)
        return _json({"ok": True})

    async def r_meta_search(self, request):
        q = " ".join(request.query.get("q", "").split())
        if len(q) < 2:
            raise ValueError(_("Type at least 2 characters"))
        if not self.meta.igdb:
            raise ValueError(_("Set the IGDB keys in Settings → Games."))
        from .metadata import PLATFORMS, clean_title, IMG_URL, score
        platform = request.query.get("platform", "")
        try:
            hits = await self.meta.search(clean_title(q), 10)
        except MetaError as err:
            raise ValueError(str(err))
        hits.sort(key=lambda h: score(q, h, platform), reverse=True)
        from datetime import datetime, timezone
        out = []
        for h in hits:
            cid = (h.get("cover") or {}).get("image_id") if isinstance(h.get("cover"), dict) else None
            ts = h.get("first_release_date")
            out.append({"igdb_id": h.get("id"), "title": h.get("name") or "",
                        "year": datetime.fromtimestamp(ts, timezone.utc).year if isinstance(ts, (int, float)) else None,
                        "platforms": [p.get("abbreviation") or p.get("name") for p in h.get("platforms") or []
                                      if isinstance(p, dict)],
                        "cover_url": IMG_URL % ("t_cover_big", cid) if cid else None})
        return _json({"results": out})

    async def r_meta_match(self, request):
        b = await self._body(request)
        g = self._game(b.get("id"))
        try:
            igdb_id = int(b.get("igdb_id"))
        except (TypeError, ValueError):
            raise ValueError(_("Pick a game from the list"))
        if not self.meta.igdb:
            raise ValueError(_("Set the IGDB keys in Settings → Games."))
        try:
            m = await self.meta.fetch(g, igdb_id=igdb_id, manual=True, raise_errors=True)
        except MetaError as err:
            raise ValueError(str(err))
        if not m or not m.get("igdb_id"):
            raise ValueError(_("IGDB has no such game"))
        return _json({"ok": True, "game": self._item(g, await self._deck_state())})

    # -- find + download
    async def r_find(self, request):
        q = request.query
        try:
            page = max(int(q.get("page") or 1), 1)
        except ValueError:
            page = 1
        return _json(await self.finder.search(q.get("q", ""), q.get("platform", "pc"), min(page, 50)))

    async def r_find_platforms(self, request):
        return _json(find.platforms())

    async def r_find_download(self, request):
        b = await self._body(request)
        h = await self.finder.add(b.get("title"), b.get("platform"), str(b.get("magnet") or ""), str(b.get("url") or ""))
        return _json({"ok": True, "hash": h})

    async def r_find_downloads(self, request):
        return _json(self.finder.listing())

    async def r_find_cancel(self, request):
        await self.finder.cancel(request.match_info["hash"].lower())
        return _json({"ok": True})

    async def r_download(self, request):
        """Called by the Deck agent, not the browser: authenticated by the signed link."""
        gid, rel = request.match_info["id"], request.match_info["path"]
        try:
            exp = int(request.query.get("e", "0"))
        except ValueError:
            exp = 0
        sig = request.query.get("t", "")
        if exp < time.time() or not hmac.compare_digest(sig, self._sig(gid, exp)):
            return _json({"error": "link expired or invalid"}, 403)
        g = self.games.get(gid)
        path = library.safe_file(g, rel) if g else None
        if not path:
            return _json({"error": "not found"}, 404)
        return web.FileResponse(path)

    def attention(self):
        return []


PLUGIN = GamesPlugin
