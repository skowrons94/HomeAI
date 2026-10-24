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
import re
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
from .discover import Discover
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
        {"key": "deck.auto_install", "label": "Install new games on the Steam Deck automatically", "type": "bool",
         "default": True, "help": "When a download finishes and the game shows up in the library, send it to the Deck "
         "(retried until the Deck is awake and online)."},
        {"key": "deck.location", "label": "Install location on the Deck", "type": "text", "advanced": True,
         "placeholder": "/home/deck/Games", "help": "Folder on the Deck where PC games are installed. Empty = internal "
         "storage (~/Games). Choose it from the list in Games settings; needs agent 1.2 or newer."},
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
        # kept across restarts: saving Settings must not break a Deck install in progress
        sec = self.hub.store.get("games.dl_secret")
        if not isinstance(sec, str) or len(sec) < 64:
            sec = secrets.token_hex(32)
            self.hub.store.set("games.dl_secret", sec)
        self.secret = bytes.fromhex(sec)
        self._scan_lock = asyncio.Lock()
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
        self.discover = Discover(self.session, self.meta, self._library_index,
                                 lambda: str(self.hub.config.get("language") or "it"))
        self.finder = Finder(self)
        self._auto_lock = asyncio.Lock()
        self._loc_cache: tuple[float, dict] = (0, {})
        self._meta_queue: dict[str, bool] = {}      # game id → force
        self._meta_task: asyncio.Task | None = None
        self._meta_busy = False
        self.hub.every(RESCAN, self._scan, name="games-rescan", initial_delay=0)   # first scan off start()
        self.hub.every(5, self.finder.poll, name="games-downloads")
        self.hub.every(60, self._auto_tick, name="games-auto-install", initial_delay=45)
        self.hub.spawn(self.finder.resume(), name="games-resume")

    async def stop(self):
        if self._meta_task and not self._meta_task.done():
            self._meta_task.cancel()
        await self.meta.close()
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
        # swap in the new list only when complete: a Deck download running during a rescan
        # must keep finding its game (a 404 aborts the install)
        async with self._scan_lock:
            games, problems = await asyncio.get_running_loop().run_in_executor(None, library.scan, self.root)
            self.games, self.problems = {g["id"]: g for g in games}, problems
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
                  "steam_running": bool(d.get("steam_running")), "agent": str(d.get("agent") or "1.0")}
        except DeckError as err:
            st = {"online": False, "error": str(err), "installed": {}, "jobs": {}, "pending": 0}
        self._deck_cache = (time.monotonic(), st)
        return st

    @staticmethod
    def agent_has_locations(agent) -> bool:
        """Deck agents from 1.2 can install somewhere other than ~/Games."""
        try:
            return tuple(int(x) for x in str(agent).split(".")[:2]) >= (1, 2)
        except ValueError:
            return False

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
            web.post("/meta/override", self.r_meta_override),
            web.post("/meta/reset", self.r_meta_reset),
            web.get("/meta/art", self.r_meta_art),
            web.post("/meta/image", self.r_meta_image),
            web.post("/delete", self.r_delete),
            web.get("/deck/locations", self.r_deck_locations),
            web.post("/deck/location/check", self.r_deck_location_check),
            web.get("/discover/genres", self.r_discover_genres),
            web.get("/discover/genre/{key}", self.r_discover_genre),
            web.get("/find/downloads/{hash}/files", self.r_find_files),
            web.get("/discover", self.r_discover),
            web.get("/discover/item/{source}/{id}", self.r_discover_item),
            web.get("/find", self.r_find),
            web.get("/find/platforms", self.r_find_platforms),
            web.post("/find/download", self.r_find_download),
            web.get("/find/downloads", self.r_find_downloads),
            web.delete("/find/downloads/{hash}", self.r_find_cancel),
            web.post("/find/downloads/{hash}/retry", self.r_find_retry),
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
        item["auto"] = self.auto_for_game(g["id"])
        return item

    def _remember_origin(self, request) -> None:
        """Background installs have no request: remember the address the app is reached at (not localhost)."""
        origin = str(request.url.origin())
        host = (request.url.host or "").lower()
        if host in ("localhost", "127.0.0.1", "::1") or origin == self.hub.store.get("games.last_origin"):
            return
        self.hub.store.set("games.last_origin", origin)

    # -- auto-install: a finished download goes to the Deck, retried until the Deck answers ----------

    def _auto_enabled(self) -> bool:
        return bool(get_path(self.config, "deck.auto_install", True))

    def _auto_all(self) -> dict:
        v = self.hub.store.get("games.auto", {})
        return v if isinstance(v, dict) else {}

    def _auto_save(self, auto: dict) -> None:
        if auto:
            self.hub.store.set("games.auto", auto)
        else:
            self.hub.store.delete("games.auto")

    def _auto_clear(self, gid: str) -> None:
        auto = self._auto_all()
        if auto.pop(gid, None) is not None:
            self._auto_save(auto)

    def auto_forget(self, h: str) -> None:
        auto = {k: v for k, v in self._auto_all().items() if v.get("hash") != h}
        self._auto_save(auto)

    def auto_for_game(self, gid: str) -> dict | None:
        a = self._auto_all().get(gid)
        if not isinstance(a, dict):
            return None
        return {"state": a.get("state", "pending"), "message": a.get("message") or None}

    def games_in(self, owned: list[Path]) -> list[str]:
        """Library games that live inside the paths a download created."""
        roots = []
        for p in owned:
            try:
                roots.append(p.resolve())
            except OSError:
                continue
        out = []
        for g in self.games.values():
            for key in ("_entry", "_dir"):
                try:
                    here = Path(g.get(key) or "").resolve()
                except OSError:
                    continue
                if any(here == r or r in here.parents for r in roots):
                    out.append(g["id"])
                    break
        return out

    def auto_queue(self, h: str) -> None:
        """A download is done and the library rescanned: mark its games for the Deck (and say which they are)."""
        e = next((x for x in self.finder.entries() if x["hash"] == h), None)
        if not e:
            return
        ids = self.games_in(self.finder.owned(e))
        if not ids:
            return
        self.finder._update(h, games=ids)
        if not (self._auto_enabled() and self.deck.configured):
            return
        auto = self._auto_all()
        for gid in ids:
            if self.games[gid]["kind"] == "installer":
                continue              # an installer would only be copied to the Deck, to run by hand
            auto[gid] = {"state": "pending", "hash": h, "since": time.time(), "tries": 0, "message": None}
        self._auto_save(auto)
        self.hub.spawn(self._auto_tick(), name="games-auto-install-now")

    def auto_status(self, e: dict) -> dict | None:
        """For a download row: what happened on the Deck with the games it brought."""
        ids = [i for i in e.get("games") or [] if isinstance(i, str)]
        if not ids or e.get("state") != "done":
            return None
        st = self._deck_cache[1] or {}
        auto = self._auto_all()
        states, msg = [], None
        for gid in ids:
            a = auto.get(gid)
            if a:
                states.append("failed" if a.get("state") == "failed" else "installing" if a.get("state") == "sent" else "pending")
                msg = msg or a.get("message")
            elif gid in (st.get("installed") or {}):
                states.append("installed")
        if not states:
            return None
        order = ("failed", "pending", "installing", "installed")
        state = next(x for x in order if x in states)
        return {"state": state, "message": msg, "online": bool(st.get("online"))}

    def _auto_base(self) -> str:
        return (str(self.config.get("base_url") or "").rstrip("/")) or str(self.hub.store.get("games.last_origin") or "")

    async def _auto_tick(self) -> None:
        if not self._auto_enabled() or not self.deck.configured:
            return
        async with self._auto_lock:
            auto = self._auto_all()
            if not auto:
                return
            st = await self._deck_state(fresh=True)
            if not st["online"]:
                return                    # asleep or off Wi-Fi: the next tick tries again
            changed = False
            for gid, a in list(auto.items()):
                g = self.games.get(gid)
                if g is None:
                    if time.time() - a.get("since", 0) > 900:     # the library never showed it
                        del auto[gid]
                        changed = True
                    continue
                inst, job = st["installed"].get(gid), st["jobs"].get(gid)
                jstate = (job or {}).get("state")
                if jstate in ("queued", "downloading", "finalizing"):
                    if a.get("state") != "sent":
                        a["state"], changed = "sent", True
                    continue
                if a.get("state") == "sent":
                    if jstate == "failed":
                        a.update(state="failed", message=(job or {}).get("message"))
                        changed = True
                        continue
                    if inst or jstate == "needs_manual":
                        del auto[gid]
                        changed = True
                        continue
                elif a.get("state") == "failed":
                    continue
                if inst and (not g["version"] or inst.get("version") == g["version"]):
                    del auto[gid]
                    changed = True
                    continue
                base = self._auto_base()
                if not base:
                    a["message"], changed = _("Open the Games page once from the Deck's network, or set the hub address in Settings → Games."), True
                    continue
                try:
                    await self._send_install(g, base, "", explicit=False)
                    a.update(state="sent", message=None)
                except (KeyError, ValueError) as err:
                    a["tries"] = a.get("tries", 0) + 1
                    a["message"] = str(err)
                    if "already installing" in str(err):
                        a["state"] = "sent"
                    elif a["tries"] >= 5:
                        a["state"] = "failed"
                changed = True
            if changed:
                self._auto_save(auto)

    async def r_library(self, request):
        self._remember_origin(request)
        st = await self._deck_state()
        items = [self._item(g, st) for g in self.games.values()]
        pending = len(self._meta_queue) + (1 if self._meta_busy else 0)
        return _json({"metadata": {"configured": self.meta.configured, "igdb": self.meta.igdb,
                                   "steamgriddb": self.meta.steamgriddb, "pending": pending,
                                   "error": self.meta.error or None},
                      "games": items, "problems": self.problems, "root": self.root, "scanned": self.scanned,
                      "exists": os.path.isdir(os.path.expanduser(self.root)), "systems": self._systems(),
                      "deck": {**{k: st.get(k) for k in ("online", "error", "pending", "steam_running", "agent")},
                               "configured": self.deck.configured, "host": self.deck.host,
                               "locations": self.agent_has_locations(st.get("agent")) if st.get("online") else None,
                               "location": str(get_path(self.config, "deck.location", "") or ""),
                               "auto_install": self._auto_enabled()}})

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
            if role == "grid" and local and local.suffix.lower() in (".jpg", ".jpeg", ".png") \
                    and not self.meta.has_override_image(g["id"], "cover"):
                f = local                       # a cover.jpg in the game folder wins (unless the user picked one)
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

    async def _send_install(self, g: dict, base: str, location: str = "", explicit: bool = True) -> None:
        """Tell the Deck agent to install a game. ``location`` is a folder on the Deck (agent 1.2+); empty means
        the default from Settings. ``explicit`` = the user asked for this location (an old agent is an error)."""
        exp = int(time.time()) + TTL
        art = await asyncio.get_running_loop().run_in_executor(None, self._art, g)   # up to 4 MB from disk
        job = {"id": g["id"], "title": self.meta.display_title(g["id"], g["title"]), "version": g["version"],
               "kind": g["kind"], "system": g["system"], "exe": g["exe"],
               "folder": g["folder"] or g["title"] if g["kind"] != "rom" else g["folder"],
               "files": g["_list"], "base": base, "exp": exp, "sig": self._sig(g["id"], exp)}
        if g.get("workdir"):
            job["workdir"] = g["workdir"]
        if art:
            job["art"] = art
        loc = (location or "").strip()
        if g["kind"] != "rom" and (loc or get_path(self.config, "deck.location", "")):
            st = await self._deck_state()
            supported = self.agent_has_locations(st.get("agent"))
            if loc and not supported:
                raise ValueError(_("Update the Deck agent to choose where games are installed"))
            if supported:
                job["location"] = loc or str(get_path(self.config, "deck.location", "") or "")
        try:
            await self.deck.call("POST", "/install", job, timeout=15)
        except DeckError as err:
            raise _fail(err)
        self._deck_cache = (0, {})
        if explicit:
            self._auto_clear(g["id"])

    async def r_install(self, request):
        b = await self._body(request)
        g = self._game(b.get("id"))
        self._remember_origin(request)
        loc = b.get("location") or ""
        if not isinstance(loc, str) or len(loc) > 400:
            raise ValueError(_("Invalid install location"))
        await self._send_install(g, self._base(request), loc)
        return _json({"ok": True})

    async def r_deck_locations(self, request):
        """Where the Deck can install games (internal storage, SD cards) with free space; old agents say so."""
        st = await self._deck_state()
        if not st["online"]:
            return _json({"online": False, "supported": False, "error": st.get("error"), "locations": []})
        if not self.agent_has_locations(st.get("agent")):
            return _json({"online": True, "supported": False, "agent": st.get("agent"), "locations": [],
                          "error": _("Update the Deck agent to choose where games are installed")})
        if time.monotonic() - self._loc_cache[0] > 15 or request.query.get("fresh") == "1":
            try:
                d = await self.deck.call("GET", "/locations")
            except DeckError as err:
                raise _fail(err)
            self._loc_cache = (time.monotonic(), d if isinstance(d, dict) else {})
        d = self._loc_cache[1]
        locs = [dict(x) for x in d.get("locations") or [] if isinstance(x, dict)]
        for x in locs:   # SD cards formatted by SteamOS are labelled by their UUID: not a name worth showing
            if x.get("kind") == "sd" and re.fullmatch(r"[0-9a-fA-F-]{8,}", str(x.get("label") or "")):
                x["label"] = ""
        return _json({"online": True, "supported": True, "agent": st.get("agent"), "locations": locs,
                      "default": d.get("default"), "home": d.get("home"),
                      "current": str(get_path(self.config, "deck.location", "") or "")})

    async def r_deck_location_check(self, request):
        b = await self._body(request)
        path = b.get("path")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 400:
            raise ValueError(_("Invalid install location"))
        st = await self._deck_state()
        if not self.agent_has_locations(st.get("agent")):
            raise ValueError(_("Update the Deck agent to choose where games are installed"))
        try:
            return _json(await self.deck.call("POST", "/location/check", {"path": path}, timeout=10))
        except DeckError as err:
            raise _fail(err)

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
        chosen = self.meta.file(g["id"], "cover") if self.meta.has_override_image(g["id"], "cover") else None
        if chosen:
            return web.FileResponse(chosen, headers={"Cache-Control": "max-age=3600"})
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

    async def _item_now(self, g: dict) -> dict:
        return self._item(g, await self._deck_state())

    async def r_meta_override(self, request):
        """Edit title, year, genres or description of a game; each field can be put back to automatic with ""."""
        b = await self._body(request)
        g = self._game(b.get("id"))
        fields = {k: b[k] for k in ("title", "year", "genres", "summary") if k in b}
        try:
            self.meta.set_fields(g["id"], fields)
        except MetaError as err:
            raise ValueError(str(err))
        return _json({"ok": True, "game": await self._item_now(g)})

    async def r_meta_reset(self, request):
        """Back to automatic: edits and chosen pictures go, the match is forgotten and looked up again."""
        g = self._game((await self._body(request)).get("id"))
        self.meta.reset(g["id"], forget_match=True)
        if self.meta.configured:
            self._meta_queue[g["id"]] = True
            self._kick_meta(delay=0.5)
        return _json({"ok": True, "game": await self._item_now(g)})

    async def r_meta_art(self, request):
        """Alternatives for the cover or the banner (SteamGridDB, plus the game's screenshots for the banner)."""
        g = self._game(request.query.get("id"))
        kind = request.query.get("kind", "")
        m = self.meta.public(g["id"]) or {}
        title = m.get("title") or g["title"]
        from .metadata import clean_title
        if kind == "cover" and not self.meta.steamgriddb:
            raise ValueError(_("Set the SteamGridDB key in Settings → Games to see more covers, or paste an image address."))
        try:
            opts = await self.meta.art_options(g["id"], kind, clean_title(title, g["kind"]), g.get("platform", ""))
        except MetaError as err:
            raise ValueError(str(err))
        return _json({"options": opts, "steamgriddb": self.meta.steamgriddb})

    async def r_meta_image(self, request):
        """Use a picture as cover/banner: ``url`` (https, downloaded and cached here), ``cached`` (a screenshot
        already in the cache) or ``clear`` to go back to the automatic one."""
        b = await self._body(request)
        g = self._game(b.get("id"))
        kind = str(b.get("kind") or "")
        try:
            if b.get("clear"):
                self.meta.clear_image(g["id"], kind)
            else:
                await self.meta.set_image(g["id"], kind, url=str(b.get("url") or ""), cached=str(b.get("cached") or ""))
        except MetaError as err:
            raise ValueError(str(err))
        return _json({"ok": True, "game": await self._item_now(g)})

    # -- delete a game's files from the library
    async def r_delete(self, request):
        """Delete a game's files on the NAS (the folder / ROM only, checked to be inside the library)."""
        g = self._game((await self._body(request)).get("id"))
        root = Path(os.path.expanduser(self.root))
        entry = Path(g.get("_entry") or "")
        targets = [find.deletable(root, entry)]
        if g["kind"] == "rom" and entry.is_file():           # cover pictures that sit next to a ROM
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                side = entry.with_suffix(ext)
                if side.is_file():
                    try:
                        targets.append(find.deletable(root, side))
                    except ValueError:
                        pass
        for e in self.finder.entries():           # not while a download or unpacking is writing there
            if e.get("state") in find.ACTIVE:
                busy = self.finder.owned(e)
                for p in busy:
                    for t in targets:
                        if p.resolve() == t or t in p.resolve().parents or p.resolve() in t.parents:
                            raise ValueError(_("This game is still being downloaded or unpacked"))
        loop = asyncio.get_running_loop()
        for t in targets:
            await loop.run_in_executor(None, find.delete_tree, t)
        self.meta.reset(g["id"], forget_match=True)
        self._auto_clear(g["id"])
        await self._scan()
        return _json({"ok": True})

    # -- discover
    def _library_index(self) -> dict[str, set[str]]:
        """{normalised title: {platform keys}} of the scanned library, for the "in library" badge."""
        from .discover import norm_title
        idx: dict[str, set[str]] = {}
        for g in self.games.values():
            plat = library.platform(g)
            names = {g.get("title") or ""}
            m = self.meta.get(g["id"]) or {}
            if m.get("title"):
                names.add(m["title"])
            for n in names:
                k = norm_title(n)
                if k:
                    idx.setdefault(k, set()).add(plat)
        return idx

    async def r_discover(self, request):
        which = request.query.get("set", "all")
        if which not in ("all", "steam", "igdb"):
            raise ValueError(_("Unknown set"))
        return _json(await self.discover.rails(which, refresh=request.query.get("refresh") == "1"))

    async def r_discover_genres(self, request):
        return _json(self.discover.genre_list())

    async def r_discover_genre(self, request):
        from .discover import DiscoverError
        q = request.query
        try:
            page = int(q.get("page") or 0)
        except ValueError:
            page = 0
        try:
            return _json(await self.discover.genre(request.match_info["key"], q.get("platform", "pc")[:20], page,
                                                   refresh=q.get("refresh") == "1"))
        except (DiscoverError, MetaError) as err:
            raise ValueError(str(err))

    async def r_discover_item(self, request):
        from .discover import DiscoverError
        try:
            return _json(await self.discover.detail(request.match_info["source"], request.match_info["id"],
                                                    request.query.get("platform", "")[:20]))
        except (DiscoverError, MetaError) as err:
            raise ValueError(str(err))

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

    async def r_find_retry(self, request):
        await self.finder.retry(request.match_info["hash"].lower())
        return _json({"ok": True})

    async def r_find_cancel(self, request):
        n = await self.finder.cancel(request.match_info["hash"].lower(), delete_files=request.query.get("files") == "1")
        return _json({"ok": True, "deleted": n})

    async def r_find_files(self, request):
        return _json(await self.finder.files_info(request.match_info["hash"].lower()))

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
