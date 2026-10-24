"""Music: your Navidrome library, MusicBrainz discovery, tracker downloads and playback on the house speakers.

    [plugins.music]
    music_dir = "/mnt/media/music"
    default_output = ""                 # name or id of an AirPlay device
    [plugins.music.navidrome]
    url = "http://127.0.0.1:4533"
    user = "…"
    password = "…"

See docs/music.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import socket
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from ...core import Entity, Plugin, current_plugin
from ...i18n import _
from ...settings import get_path
from . import find as findmod
from . import dlna
from . import libfiles
from . import outputs as outmod
from .discover import Discover
from .find import Finder
from .musicbrainz import MBID, MetaError, MusicBrainz, find_owned
from .player import Player
from .subsonic import Subsonic, SubsonicError, valid_id

log = logging.getLogger("hub.music")

STORE_SECRET = "music.stream_secret"   # signs the links DLNA speakers fetch tracks from
STREAM_TTL = 6 * 3600
STREAM_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,120}\.(mp3|flac|m4a|ogg|opus|wav)$")
STORE_OUTPUTS = "music.outputs"      # {device id: {credentials, password}}
OWNED_TTL = 300
MAX_TRACK = 400 * 1024 * 1024
KEEP_TMP = 8
EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac",
       "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a", "audio/ogg": ".ogg", "audio/opus": ".opus",
       "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"}
BROWSER_OK = {"mp3", "m4a", "aac", "flac", "wav"}
PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition")


def content_disposition(name: str) -> str:
    """Attachment header with a safe name: ASCII fallback plus RFC 5987 filename*."""
    clean = "".join(c for c in str(name) if c >= " " and c != "\x7f")
    clean = findmod.safe_name(clean, "music")
    ascii_name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", clean.encode("ascii", "ignore").decode() or clean)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def _json(data, status=200):
    return web.json_response(data, status=status)


class MusicPlugin(Plugin):
    name = "music"
    title = "Music"
    description = "Your music library (Navidrome), discovery on MusicBrainz and playback on AirPlay speakers"
    icon = "music"
    schema = [
        {"key": "navidrome.url", "label": "Navidrome address", "type": "text", "default": "http://127.0.0.1:4533",
         "required": True},
        {"key": "navidrome.user", "label": "Navidrome user", "type": "text", "required": True},
        {"key": "navidrome.password", "label": "Navidrome password", "type": "password", "required": True,
         "help": "Empty = keep the saved one."},
        {"key": "music_dir", "label": "Music folder", "type": "path", "default": "/mnt/media/music",
         "help": "Downloads are filed here as Artist/Album (year). It must be the folder Navidrome reads."},
        {"key": "default_output", "label": "Default output", "type": "text", "placeholder": "KEF LSX",
         "help": "Name of the AirPlay device used first. Empty = this phone."},
        {"key": "airplay.devices", "label": "Extra AirPlay devices", "type": "list", "advanced": True,
         "placeholder": "192.168.1.50", "help": "IP addresses to always include when discovery misses a device."},
    ]

    async def start(self):
        self.session = aiohttp.ClientSession()
        data_dir = Path(self.hub.data_dir)
        self.nav = Subsonic(str(get_path(self.config, "navidrome.url", "http://127.0.0.1:4533") or ""),
                            str(get_path(self.config, "navidrome.user", "") or ""),
                            str(get_path(self.config, "navidrome.password", "") or ""), self.session)
        self.mb = MusicBrainz(self.hub.store, self.session, data_dir / "music-art")
        self.mb.prune()
        self.tmp = data_dir / "music-tmp"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.disc = Discover(lambda: self.session, self.owned)
        self.finder = Finder(self)
        self.players: dict[str, Player] = {}
        self.outs: dict[str, dict] = {}            # discovered devices by id
        self.out_scanned = 0.0
        self._scan_lock = asyncio.Lock()
        self._owned: tuple[float, list] = (0.0, [])
        self._fetching: dict[str, asyncio.Future] = {}
        self._pairing = None
        self.active: str | None = None
        self.eid = "music.player"
        self.hub.add_entity(Entity(
            id=self.eid, name=_("Music"), kind="media_player", plugin=self.name, available=True,
            state={"on": True, "state": "idle", "title": "", "artist": "", "album": "", "artwork": "", "output": ""},
            attrs={"manufacturer": "Casa", "model": "Navidrome"},
            actions={"play_pause": self.a_toggle, "next": self.a_next, "previous": self.a_previous,
                     "turn_off": self.a_stop}))
        self.hub.every(5, self.finder.poll, name="music-downloads")
        self.hub.spawn(self.finder.resume(), name="music-resume")
        self.hub.spawn(self.refresh_outputs(), name="music-discover")

    async def stop(self):
        for p in list(self.players.values()):
            try:
                await p._halt()
                await p.output.close()
            except Exception:
                pass
        await self.session.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, coro, name=""):
        """Tasks started from a request still belong to this plugin (cancelled on reload/disable)."""
        tok = current_plugin.set(self.name)
        try:
            return self.hub.spawn(coro, name)
        finally:
            current_plugin.reset(tok)

    @property
    def music_dir(self) -> str:
        return str(self.config.get("music_dir") or "/mnt/media/music")

    def status(self):
        return {"configured": self.nav.configured, "outputs": len(self.outs)}

    def attention(self):
        if self.nav.configured:
            return []
        return [{"id": "music-setup", "level": "info", "title": _("Music needs Navidrome"),
                 "detail": _("Set the Navidrome user and password."), "action": {"type": "settings", "plugin": self.name}}]

    # -- library helpers ---------------------------------------------------------------------------
    async def rescan(self):
        self._owned = (0.0, [])
        return await self.nav.start_scan()

    async def owned(self) -> list[dict]:
        ts, cached = self._owned
        if cached and time.time() - ts < OWNED_TTL:
            return cached
        out, offset = [], 0
        while offset < 10000:
            page = await self.nav.album_list("alphabeticalByName", 500, offset)
            out += page
            offset += 500
            if len(page) < 500:
                break
        self._owned = (time.time(), out)
        return out

    async def fetch(self, track: dict) -> str:
        """Download a track from Navidrome (mp3 320) to a temp file the AirPlay streamer can read."""
        sid = track["id"]
        if not valid_id(sid):
            raise ValueError("bad track id")
        for f in self.tmp.glob(sid + ".*"):
            if not f.name.startswith(".") and f.suffix != ".part":
                return str(f)
        if sid in self._fetching:
            return await asyncio.shield(self._fetching[sid])
        fut = asyncio.get_running_loop().create_future()
        self._fetching[sid] = fut
        try:
            path = await self._download(sid)
            fut.set_result(path)
            return path
        except BaseException as err:
            fut.set_exception(err if isinstance(err, Exception) else RuntimeError("cancelled"))
            fut.exception()
            raise
        finally:
            self._fetching.pop(sid, None)

    async def _download(self, sid: str) -> str:
        part = self.tmp / (sid + ".part")
        try:
            async with self.session.get(self.nav.stream_url(sid, "mp3", 320),
                                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.status != 200 or ctype not in EXT:
                    raise SubsonicError(_("Navidrome could not provide this track"))
                size = 0
                with open(part, "wb") as out:
                    async for chunk in r.content.iter_chunked(1 << 18):
                        size += len(chunk)
                        if size > MAX_TRACK:
                            raise SubsonicError(_("The track is too large"))
                        out.write(chunk)
            final = self.tmp / (sid + EXT[ctype])
            os.replace(part, final)
        except aiohttp.ClientError as err:
            raise SubsonicError(_("Navidrome not reachable at {url}", url=self.nav.base)) from err
        finally:
            part.unlink(missing_ok=True)
        files = sorted((f for f in self.tmp.iterdir() if f.is_file() and not f.name.startswith(".")),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[KEEP_TMP:]:
            old.unlink(missing_ok=True)
        return str(final)

    # -- outputs -----------------------------------------------------------------------------------
    def output_creds(self) -> dict:
        v = self.hub.store.get(STORE_OUTPUTS, {})
        return v if isinstance(v, dict) else {}

    async def refresh_outputs(self):
        async with self._scan_lock:
            try:
                confs = await outmod.scan([str(x) for x in get_path(self.config, "airplay.devices", []) or []])
            except Exception as err:
                log.warning("AirPlay discovery failed: %s", err)
                return
            creds = self.output_creds()
            found = {}
            for c in confs:
                try:
                    found[c.identifier] = outmod.describe(c, creds)
                except Exception:
                    log.exception("describe %s", c)
            try:
                renderers = await dlna.discover(self.session)
            except Exception as err:
                log.info("DLNA discovery failed: %s", err)
                renderers = []
            for r in renderers:
                same = next((o for o in found.values() if o["address"] == r["address"]), None)
                if same is not None:
                    # AirPlay 2 receivers of other brands (the KEF LSX) can stay silent with pyatv's stream:
                    # when the same speaker is a DLNA renderer, let it fetch the track itself
                    if "apple" not in r["manufacturer"].lower():
                        same["dlna"] = r
                    continue
                rid = "dlna-" + re.sub(r"[^A-Za-z0-9]", "", r["udn"])[-24:]
                found[rid] = {"id": rid, "name": r["name"] or r["address"], "address": r["address"],
                              "model": " ".join(x for x in (r["manufacturer"], r["model"]) if x), "kind": "dlna",
                              "needs_pairing": False, "password": False, "paired": False, "has_password": False,
                              "dlna": r}
            for pid, p in self.players.items():     # keep devices that are playing even if discovery missed them
                if pid not in found and pid in self.outs:
                    found[pid] = self.outs[pid]
            self.outs = found
            self.out_scanned = time.time()

    def find_output(self, key: str) -> dict:
        d = self.outs.get(key)
        if d is None:
            d = next((x for x in self.outs.values() if x["name"].lower() == str(key).lower()), None)
        if d is None:
            raise KeyError(_("That output was not found. Use Find devices."))
        return d

    def player_for(self, key: str) -> Player:
        desc = self.find_output(key)
        pl = self.players.get(desc["id"])
        if pl is None:
            out = dlna.DlnaOutput(self, desc) if desc.get("dlna") else outmod.AirPlayOutput(self, desc)
            pl = self.players[desc["id"]] = Player(out, self.fetch, self.spawn, self._player_changed)
        return pl

    def _player_changed(self, pl: Player) -> None:
        if pl.state != "idle" or self.active in (None, pl.output.id):
            self.active = pl.output.id
        if self.active != pl.output.id:
            return
        tr = pl.track or {}
        self.hub.update(self.eid, state=pl.state,
                        title=tr.get("title", ""), artist=tr.get("artist", ""), album=tr.get("album", ""),
                        artwork=f"/api/p/music/cover/{tr['cover']}?size=300" if tr.get("cover") else "",
                        position=round(pl.position(), 1), duration=tr.get("duration") or 0,
                        position_at=time.time(), output=pl.output.name, volume=pl.volume)

    def _active_player(self) -> Player:
        pl = self.players.get(self.active or "")
        if pl is None:
            raise ValueError(_("Nothing is playing"))
        return pl

    async def a_toggle(self):
        await self._active_player().toggle()

    async def a_next(self):
        await self._active_player().next()

    async def a_previous(self):
        await self._active_player().previous()

    async def a_stop(self):
        await self._active_player().stop()

    # -- downloads API for the Torrents page -------------------------------------------------------
    def downloads_listing(self) -> list[dict]:
        return self.finder.listing()

    async def accept_download(self, result: dict, guess: dict) -> dict:
        result, guess = result or {}, guess or {}
        title = result.get("title") or ""
        g = findmod.guess_from_title(title)
        h = await self.finder.add(title, guess.get("artist") or g["artist"], guess.get("album") or g["album"],
                                  guess.get("year") or g["year"], str(result.get("magnet") or ""),
                                  str(result.get("url") or ""), bool(guess.get("discography")))
        return {"ok": True, "hash": h}

    # -- routes ------------------------------------------------------------------------------------
    # -- links DLNA speakers fetch the cached track from (public: the speaker has no login cookie) ---
    def _stream_secret(self) -> bytes:
        sec = self.hub.store.get(STORE_SECRET)
        if not isinstance(sec, str) or len(sec) < 32:
            sec = secrets.token_hex(32)
            self.hub.store.set(STORE_SECRET, sec)
        return bytes.fromhex(sec)

    def _stream_sig(self, name: str, exp: int) -> str:
        return hmac.new(self._stream_secret(), f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()

    def stream_link(self, path: str, peer: str) -> str:
        """URL of a cached track for the speaker at ``peer``: our address as seen from it, signed, expiring."""
        name = os.path.basename(path)
        if not STREAM_NAME.match(name) or os.path.dirname(os.path.realpath(path)) != str(self.tmp.resolve()):
            raise ValueError("not a cached track")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect((peer, 9))              # no packet is sent: picks the interface that reaches the speaker
            host = sk.getsockname()[0]
        port = int(get_path(self.hub.config, "web.port", 8080) or 8080)
        exp = int(time.time()) + STREAM_TTL
        return f"http://{host}:{port}/api/p/music/speaker/{exp}/{self._stream_sig(name, exp)}/{quote(name)}"

    async def r_speaker_stream(self, request):
        mi = request.match_info
        name = mi["name"]
        try:
            exp = int(mi["exp"])
        except ValueError:
            raise web.HTTPNotFound()
        if (not STREAM_NAME.match(name) or exp < time.time()
                or not hmac.compare_digest(mi["sig"], self._stream_sig(name, exp))):
            raise web.HTTPNotFound()
        f = self.tmp / name
        if not f.is_file():
            raise web.HTTPNotFound()
        ctype = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
                 ".opus": "audio/ogg", ".wav": "audio/wav"}[f.suffix]
        return web.FileResponse(f, headers={"Content-Type": ctype})

    def routes(self):
        g, p, d = web.get, web.post, web.delete
        return [g("/albums", self.r_albums), g("/artists", self.r_artists), g("/genres", self.r_genres),
                g("/save/{id}", self.r_save), g("/artist/{id}", self.r_artist),
                g("/album/{id}", self.r_album), g("/search", self.r_search),
                g("/cover/{id}", self.r_cover), g("/stream/{id}", self.r_stream),
                g("/mb/search", self.r_mb_search), g("/mb/discography/{mbid}", self.r_mb_disco),
                g("/mb/tracks/{mbid}", self.r_mb_tracks), g("/mbcover/{mbid}", self.r_mb_cover),
                g("/discover", self.r_discover), g("/discover/genres", self.r_disc_genres),
                g("/discover/genre/{id}", self.r_disc_genre), g("/discover/album/{id}", self.r_disc_album),
                g("/discover/artist/{id}", self.r_disc_artist), g("/discover/search", self.r_disc_search),
                g("/discover/resolve", self.r_disc_resolve),
                g("/find", self.r_find), p("/download", self.r_download), g("/downloads", self.r_downloads),
                d("/downloads/{hash}", self.r_cancel), p("/downloads/{hash}/retry", self.r_retry),
                g("/downloads/{hash}/info", self.r_dl_info), p("/downloads/{hash}/library-delete", self.r_dl_library),
                g("/outputs", self.r_outputs), p("/outputs/scan", self.r_outputs_scan),
                p("/outputs/{id}/pair", self.r_pair), p("/outputs/{id}/password", self.r_password),
                g("/speaker/{exp}/{sig}/{name}", self.r_speaker_stream),
                g("/library/{kind}/{id}/files", self.r_lib_files), p("/library/{kind}/{id}/delete", self.r_lib_delete),
                g("/player", self.r_player), p("/player/play", self.r_play), p("/player/{action}", self.r_control),
                p("/test", self.r_test), p("/rescan", self.r_rescan), g("/scan", self.r_scan)]

    @staticmethod
    async def _body(request) -> dict:
        try:
            b = await request.json()
        except Exception:
            b = {}
        return b if isinstance(b, dict) else {}

    async def _call(self, coro):
        try:
            return await coro
        except SubsonicError as err:
            raise ValueError(str(err))
        except MetaError as err:
            raise ValueError(str(err))

    def _id(self, request, key="id") -> str:
        v = request.match_info[key]
        if not valid_id(v):
            raise ValueError(_("Invalid request"))
        return v

    async def r_albums(self, request):
        q = request.query
        size = min(max(int(q.get("size") or 30), 1), 100)
        offset = max(int(q.get("offset") or 0), 0)
        return _json({"configured": self.nav.configured,
                      "albums": await self._call(self.nav.album_list(q.get("type", "newest"), size, offset,
                                                                     q.get("genre", "")))})

    async def r_genres(self, request):
        return _json({"genres": await self._call(self.nav.genres())})

    async def r_artists(self, request):
        return _json({"artists": await self._call(self.nav.artists())})

    async def r_artist(self, request):
        a = await self._call(self.nav.artist(self._id(request)))
        return _json(a)

    async def r_album(self, request):
        return _json(await self._call(self.nav.album(self._id(request))))

    # -- delete songs / albums from the library (the files on the NAS) --------------------------------
    async def _lib_files(self, request) -> tuple[list[dict], list[tuple[dict, Path | None]]]:
        kind, iid = request.match_info["kind"], self._id(request)
        if kind == "album":
            songs = (await self._call(self.nav.album(iid)))["tracks"]
        elif kind == "song":
            songs = [await self._call(self.nav.song(iid))]
        else:
            raise KeyError(_("Not found"))
        music = Path(self.music_dir)
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, lambda: [(x, libfiles.locate(music, x)) for x in songs])
        return songs, found

    async def r_lib_files(self, request):
        _songs, found = await self._lib_files(request)
        music = Path(self.music_dir).resolve()
        ok = [(x, f) for x, f in found if f]
        return _json({"files": [{"title": x["title"], "path": str(f.resolve().relative_to(music)), "size": x["size"]}
                                for x, f in ok],
                      "missing": [x["title"] for x, f in found if not f],
                      "size": sum(x["size"] for x, _f in ok), "folder": self.music_dir})

    async def r_lib_delete(self, request):
        b = await self._body(request)
        if b.get("confirm") is not True:
            raise ValueError(_("Confirm the deletion first"))
        _songs, found = await self._lib_files(request)
        files = [f for _x, f in found if f]
        if not files:
            raise ValueError(_("The files of this music were not found on the NAS"))
        loop = asyncio.get_running_loop()
        n = await loop.run_in_executor(None, libfiles.delete, Path(self.music_dir), files)
        with contextlib.suppress(Exception):
            await self.rescan()
        return _json({"ok": True, "deleted": n, "missing": len(found) - len(files)})

    async def r_search(self, request):
        q = " ".join(request.query.get("q", "").split())
        if len(q) < 2:
            raise ValueError(_("Type at least 2 characters"))
        return _json(await self._call(self.nav.search(q[:100])))

    async def r_cover(self, request):
        cid = self._id(request)
        try:
            size = min(max(int(request.query.get("size") or 300), 32), 1000)
        except ValueError:
            size = 300
        try:
            async with self.session.get(self.nav.url("getCoverArt", id=cid, size=size),
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status != 200 or not ctype.startswith("image/"):
                    raise KeyError(_("No cover"))
                body = await r.content.read(6 * 1024 * 1024)
        except aiohttp.ClientError:
            raise KeyError(_("No cover"))
        return web.Response(body=body, content_type=ctype.split(";")[0],
                            headers={"Cache-Control": "max-age=86400"})

    async def r_stream(self, request):
        sid = self._id(request)
        suffix = request.query.get("suffix", "").lower()
        raw = suffix in BROWSER_OK
        params = {"id": sid}
        if not raw:
            params.update(format="mp3", maxBitRate=320, estimateContentLength="true")
        headers = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
        up = await self.session.get(self.nav.url("stream", **params), headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60))
        try:
            if up.status >= 400 or up.headers.get("Content-Type", "").startswith(("application/json", "text/")):
                raise KeyError(_("Navidrome could not provide this track"))
            resp = web.StreamResponse(status=up.status, headers={k: up.headers[k] for k in PASS_HEADERS if k in up.headers})
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(1 << 16):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            up.release()

    async def r_save(self, request):
        """Download to the phone: the original file of a track, or a zip of an album, proxied from Navidrome
        in chunks (nothing is buffered, Range is passed through, credentials stay here)."""
        iid = self._id(request)
        kind = request.query.get("kind", "track")
        if kind not in ("track", "album"):
            raise ValueError(_("Invalid request"))
        name = iid
        try:
            if kind == "album":
                a = await self.nav.album(iid)
                name = f"{a['artist']} - {a['name']}.zip"
            else:
                s = await self.nav.song(iid)
                name = f"{s['artist']} - {s['title']}.{s['suffix'] or 'mp3'}" if s["title"] else iid
        except SubsonicError as err:
            raise ValueError(str(err))
        headers = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
        try:
            up = await self.session.get(self.nav.download_url(iid), headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60))
        except aiohttp.ClientError:
            raise ValueError(_("Navidrome not reachable at {url}", url=self.nav.base))
        try:
            ctype = up.headers.get("Content-Type", "")
            if up.status >= 400 or ctype.startswith(("application/json", "text/", "application/xml")):
                raise KeyError(_("Navidrome could not provide this file"))
            out = {k: up.headers[k] for k in PASS_HEADERS if k in up.headers and k != "Content-Disposition"}
            if kind == "album" and not ctype.startswith(("application/zip", "application/x-zip")):
                out["Content-Type"] = "application/zip"
            out["Content-Disposition"] = content_disposition(name)
            out["Cache-Control"] = "no-store"
            resp = web.StreamResponse(status=up.status, headers=out)
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(1 << 16):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            up.release()

    # -- MusicBrainz
    async def r_mb_search(self, request):
        q = " ".join(request.query.get("q", "").split())
        if len(q) < 2:
            raise ValueError(_("Type at least 2 characters"))
        kind = request.query.get("kind", "artist")
        if kind == "album":
            groups = await self._call(self.mb.search_albums(q[:100]))
            try:
                owned = await self.owned()
            except SubsonicError:
                owned = []
            for gr in groups:
                o = find_owned(owned, gr["artist"], gr["title"], gr["mbid"])
                gr["owned"] = o["id"] if o else None
            return _json({"albums": groups})
        return _json({"artists": await self._call(self.mb.search_artists(q[:100]))})

    async def r_mb_disco(self, request):
        mbid = request.match_info["mbid"]
        name = request.query.get("name", "")
        groups = await self._call(self.mb.discography(mbid))
        try:
            owned = await self.owned()
        except SubsonicError:
            owned = []
        for gr in groups:
            o = find_owned(owned, name, gr["title"], gr["mbid"]) if name else None
            gr["owned"] = o["id"] if o else None
            gr["artist"] = name
        return _json({"albums": groups})

    async def r_mb_tracks(self, request):
        return _json({"tracks": await self._call(self.mb.tracklist(request.match_info["mbid"]))})

    async def r_mb_cover(self, request):
        f = await self.mb.cover(request.match_info["mbid"])
        if f is None:
            raise KeyError(_("No cover"))
        return web.FileResponse(f, headers={"Cache-Control": "max-age=604800"})

    # -- Discover (Deezer charts, Apple Music top Italia)
    async def r_discover(self, request):
        keys = [k for k in request.query.get("rails", "").split(",") if k]
        return _json(await self.disc.rails(keys))

    async def r_disc_genres(self, request):
        return _json({"genres": await self.disc.genres()})

    async def r_disc_genre(self, request):
        return _json(await self.disc.genre_page(request.match_info["id"]))

    async def r_disc_album(self, request):
        return _json(await self.disc.album(request.match_info["id"]))

    async def r_disc_artist(self, request):
        return _json(await self.disc.artist(request.match_info["id"]))

    async def r_disc_search(self, request):
        return _json(await self.disc.search(request.query.get("q", "")))

    async def r_disc_resolve(self, request):
        q = request.query
        return _json({"id": await self.disc.resolve(q.get("artist", ""), q.get("title", ""))})

    # -- find + download
    async def r_find(self, request):
        q = request.query
        try:
            page = min(max(int(q.get("page") or 1), 1), 50)
        except ValueError:
            page = 1
        return _json(await self.finder.search(q.get("artist", ""), q.get("album", ""),
                                              q.get("discography") in ("1", "true"), page))

    async def r_download(self, request):
        b = await self._body(request)
        h = await self.finder.add(b.get("title"), b.get("artist"), b.get("album"), b.get("year"),
                                  str(b.get("magnet") or ""), str(b.get("url") or ""), bool(b.get("discography")))
        return _json({"ok": True, "hash": h})

    async def r_downloads(self, request):
        return _json(self.finder.listing())

    async def r_retry(self, request):
        await self.finder.retry(request.match_info["hash"].lower())
        return _json({"ok": True})

    async def r_cancel(self, request):
        await self.finder.cancel(request.match_info["hash"].lower(),
                                 delete_files=request.query.get("files") in ("1", "true"))
        return _json({"ok": True})

    async def r_dl_info(self, request):
        return _json(await self.finder.info(request.match_info["hash"].lower()))

    async def r_dl_library(self, request):
        return _json(await self.finder.delete_from_library(request.match_info["hash"].lower()))

    # -- outputs
    async def r_outputs(self, request):
        if not self.outs and time.time() - self.out_scanned > 60:
            await self.refresh_outputs()
        elif time.time() - self.out_scanned > 600:
            self.spawn(self.refresh_outputs(), "music-discover")
        return _json({"outputs": sorted(self.outs.values(), key=lambda o: o["name"].lower()),
                      "default": str(self.config.get("default_output") or ""),
                      "playing": {k: v.state for k, v in self.players.items() if v.state != "idle"}})

    async def r_outputs_scan(self, request):
        await self.refresh_outputs()
        return await self.r_outputs(request)

    async def r_password(self, request):
        d = self.find_output(request.match_info["id"])
        b = await self._body(request)
        creds = self.output_creds()
        creds.setdefault(d["id"], {})["password"] = str(b.get("password") or "")
        self.hub.store.set(STORE_OUTPUTS, creds)
        d["has_password"] = bool(creds[d["id"]]["password"])
        pl = self.players.get(d["id"])
        if pl:
            await pl.output.close()
        return _json({"ok": True})

    async def r_pair(self, request):
        import pyatv
        from pyatv.const import Protocol

        d = self.find_output(request.match_info["id"])
        b = await self._body(request)
        step, value = b.get("step"), str(b.get("value") or "").strip()

        def reply(message, step=None, input=None, done=False, error=None):
            return _json({"message": message, "step": step, "input": input, "done": done, "error": error})

        loop = asyncio.get_running_loop()
        if step is None:
            confs = await pyatv.scan(loop, timeout=4, identifier=d["id"], hosts=[d["address"]], protocol={Protocol.RAOP})
            if not confs:
                return reply(_("{name} not found on the network", name=d["name"]), error="not found")
            if self._pairing:
                await self._pairing.close()
            self._pairing = await pyatv.pair(confs[0], Protocol.RAOP, loop, name="Casa")
            await self._pairing.begin()
            if self._pairing.device_provides_pin:
                return reply(_("Enter the PIN shown by {name}.", name=d["name"]), step="pin",
                             input={"label": _("PIN"), "type": "number"})
            self._pairing.pin(1234)
            return reply(_("Enter 1234 on {name}, then continue.", name=d["name"]), step="pin",
                         input={"label": _("Press OK when done"), "type": "text", "optional": True})
        if step == "pin" and self._pairing is not None:
            try:
                if self._pairing.device_provides_pin:
                    self._pairing.pin(int(value))
                await self._pairing.finish()
                ok = self._pairing.has_paired
                cred = self._pairing.service.credentials if ok else None
            except Exception as err:
                ok, cred = False, None
                log.info("pairing failed: %s", err)
            finally:
                await self._pairing.close()
                self._pairing = None
            if not ok:
                return reply(_("Wrong PIN or pairing timed out. Start again."), error="failed")
            creds = self.output_creds()
            creds.setdefault(d["id"], {})["credentials"] = cred
            self.hub.store.set(STORE_OUTPUTS, creds)
            d.update(paired=True, needs_pairing=False)
            return reply(_("Paired with {name}.", name=d["name"]), done=True)
        return reply(_("Start again."), error="unknown step")

    # -- player
    def _out_key(self, b) -> str:
        key = str(b.get("output") or "")
        if not key:
            raise ValueError(_("Pick an output"))
        return key

    async def r_player(self, request):
        key = request.query.get("output")
        if key:
            pl = self.players.get(self.find_output(key)["id"])
            return _json(pl.to_json() if pl else {"state": "idle", "queue": [], "track": None})
        return _json({"players": {k: v.to_json() for k, v in self.players.items()}, "active": self.active})

    async def _resolve(self, b: dict) -> list[dict]:
        if b.get("album"):
            aid = str(b["album"])
            if not valid_id(aid):
                raise ValueError(_("Invalid request"))
            return (await self._call(self.nav.album(aid)))["tracks"]
        ids = [str(i) for i in (b.get("songs") or [])][:200]
        if not ids or not all(valid_id(i) for i in ids):
            raise ValueError(_("Nothing to play"))
        return await self._call(asyncio.gather(*(self.nav.song(i) for i in ids)))

    async def r_play(self, request):
        b = await self._body(request)
        pl = self.player_for(self._out_key(b))
        tracks = await self._resolve(b)
        mode = b.get("mode", "play")
        try:
            start = int(b.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        if mode in ("next", "queue"):
            await pl.enqueue(tracks, next_=(mode == "next"))
        else:
            await pl.play(tracks, start, bool(b.get("shuffle")))
        self.active = pl.output.id
        return _json(pl.to_json())

    async def r_control(self, request):
        action = request.match_info["action"]
        b = await self._body(request)
        pl = self.players.get(self.find_output(self._out_key(b))["id"]) if action != "volume" else \
            self.player_for(self._out_key(b))
        if pl is None:
            raise ValueError(_("Nothing is playing"))
        if action == "volume":
            try:
                await pl.set_volume(int(b.get("value")))
            except (TypeError, ValueError):
                raise ValueError(_("Invalid request"))
            except outmod.OutputError as err:
                raise ValueError(str(err))
        elif action == "jump":
            await pl.jump(int(b.get("index", -1)))
        elif action in ("pause", "resume", "toggle", "next", "previous", "stop"):
            await getattr(pl, action)()
        else:
            raise KeyError(_("Unknown action"))
        return _json(pl.to_json())

    # -- settings sheet
    async def r_test(self, request):
        try:
            info = await self.nav.ping()
            return _json({"ok": True, **info, "music_dir": self.music_dir, "music_dir_exists": os.path.isdir(self.music_dir)})
        except SubsonicError as err:
            return _json({"ok": False, "error": str(err)})

    async def r_rescan(self, request):
        return _json({"ok": True, **await self._call(self.rescan())})

    async def r_scan(self, request):
        return _json(await self._call(self.nav.scan_status()))


PLUGIN = MusicPlugin
