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
import hashlib
import hmac
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
from . import library
from .deck import Deck, DeckError

TTL = 6 * 3600          # life of a download link
RESCAN = 600


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
        await self._scan()
        self.hub.every(RESCAN, self._scan, name="games-rescan")

    async def stop(self):
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
            web.post("/install", self.r_install),
            web.post("/uninstall", self.r_uninstall),
            web.post("/apply", self.r_apply),
            web.get("/cover/{id}", self.r_cover),
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

    async def r_library(self, request):
        st = await self._deck_state()
        items = [{**library.public(g), **self._state(g, st)} for g in self.games.values()]
        return _json({"games": items, "problems": self.problems, "root": self.root, "scanned": self.scanned,
                      "deck": {k: st.get(k) for k in ("online", "error", "pending", "steam_running")}})

    async def r_rescan(self, request):
        await self._scan()
        return await self.r_library(request)

    def _base(self, request) -> str:
        return (str(self.config.get("base_url") or "").rstrip("/")) or str(request.url.origin())

    async def r_install(self, request):
        g = self._game((await self._body(request)).get("id"))
        exp = int(time.time()) + TTL
        job = {"id": g["id"], "title": g["title"], "version": g["version"], "kind": g["kind"],
               "system": g["system"], "exe": g["exe"], "folder": g["folder"] or g["title"] if g["kind"] != "rom" else g["folder"],
               "files": g["_list"], "base": self._base(request), "exp": exp, "sig": self._sig(g["id"], exp)}
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
        if not g.get("_cover") or not Path(g["_cover"]).is_file():
            raise KeyError(_("Game not found"))
        return web.FileResponse(g["_cover"], headers={"Cache-Control": "max-age=3600"})

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
