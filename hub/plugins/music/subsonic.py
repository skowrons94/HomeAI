"""Minimal Subsonic/OpenSubsonic client for Navidrome (JSON). Credentials never leave the hub."""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import urlencode

import aiohttp

from ...i18n import _

API_VERSION = "1.16.1"
CLIENT = "casa"
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,100}$")
LISTS = ("newest", "recent", "frequent", "random", "alphabeticalByName")


class SubsonicError(Exception):
    pass


def auth_params(user: str, password: str, salt: str | None = None) -> dict:
    """Token auth: t = md5(password + salt)."""
    salt = salt or secrets.token_hex(6)
    return {"u": user, "t": hashlib.md5((password + salt).encode()).hexdigest(), "s": salt,
            "v": API_VERSION, "c": CLIENT, "f": "json"}


def valid_id(v) -> bool:
    return isinstance(v, str) and bool(SAFE_ID.match(v))


def map_song(s: dict) -> dict:
    return {"id": str(s.get("id", "")), "title": s.get("title") or "", "artist": s.get("artist") or "",
            "album": s.get("album") or "", "album_id": str(s.get("albumId") or ""),
            "artist_id": str(s.get("artistId") or ""), "track": s.get("track") or 0,
            "disc": s.get("discNumber") or 1, "duration": s.get("duration") or 0,
            "suffix": (s.get("suffix") or "").lower(), "bitrate": s.get("bitRate") or 0,
            "cover": str(s.get("coverArt") or s.get("albumId") or s.get("id") or ""),
            "year": s.get("year") or None}


def map_album(a: dict) -> dict:
    return {"id": str(a.get("id", "")), "name": a.get("name") or a.get("title") or "",
            "artist": a.get("artist") or "", "artist_id": str(a.get("artistId") or ""),
            "year": a.get("year") or None, "cover": str(a.get("coverArt") or a.get("id") or ""),
            "songs": a.get("songCount") or 0, "duration": a.get("duration") or 0,
            "mbid": a.get("musicBrainzId") or "", "added": a.get("created") or ""}


def map_artist(a: dict) -> dict:
    return {"id": str(a.get("id", "")), "name": a.get("name") or "", "albums": a.get("albumCount") or 0,
            "cover": str(a.get("coverArt") or "")}


class Subsonic:
    def __init__(self, url: str, user: str, password: str, session: aiohttp.ClientSession):
        self.base = (url or "").rstrip("/")
        self.user, self.password, self.session = user, password, session

    @property
    def configured(self) -> bool:
        return bool(self.base and self.user)

    def url(self, endpoint: str, **params) -> str:
        q = {**auth_params(self.user, self.password), **{k: v for k, v in params.items() if v is not None}}
        return f"{self.base}/rest/{endpoint}?{urlencode(q)}"

    def stream_url(self, song_id: str, fmt: str = "mp3", bitrate: int = 320) -> str:
        return self.url("stream", id=song_id, format=fmt, maxBitRate=bitrate)

    async def call(self, endpoint: str, **params) -> dict:
        if not self.configured:
            raise SubsonicError(_("Set the Navidrome address and user in Music settings"))
        try:
            async with self.session.get(self.url(endpoint, **params),
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status >= 400:
                    raise SubsonicError(_("Navidrome answered HTTP {code}", code=r.status))
                data = await r.json(content_type=None)
        except aiohttp.ClientError as err:
            raise SubsonicError(_("Navidrome not reachable at {url}", url=self.base)) from err
        except ValueError as err:
            raise SubsonicError(_("Navidrome sent an unexpected answer")) from err
        resp = data.get("subsonic-response") if isinstance(data, dict) else None
        if not isinstance(resp, dict):
            raise SubsonicError(_("Navidrome sent an unexpected answer"))
        if resp.get("status") != "ok":
            err = resp.get("error") or {}
            if err.get("code") in (40, 41):
                raise SubsonicError(_("Navidrome: wrong user or password"))
            raise SubsonicError(str(err.get("message") or "Navidrome error"))
        return resp

    async def ping(self) -> dict:
        r = await self.call("ping")
        return {"version": r.get("serverVersion") or r.get("version"), "type": r.get("type")}

    async def artists(self) -> list[dict]:
        r = await self.call("getArtists")
        out = []
        for idx in (r.get("artists") or {}).get("index") or []:
            out += [map_artist(a) for a in idx.get("artist") or []]
        return out

    async def artist(self, aid: str) -> dict:
        a = (await self.call("getArtist", id=aid)).get("artist") or {}
        return {**map_artist(a), "list": [map_album(x) for x in a.get("album") or []]}

    async def album_list(self, kind: str = "newest", size: int = 30, offset: int = 0) -> list[dict]:
        if kind not in LISTS:
            raise ValueError(_("Unknown list"))
        r = await self.call("getAlbumList2", type=kind, size=size, offset=offset)
        return [map_album(a) for a in (r.get("albumList2") or {}).get("album") or []]

    async def album(self, aid: str) -> dict:
        a = (await self.call("getAlbum", id=aid)).get("album") or {}
        return {**map_album(a), "tracks": [map_song(s) for s in a.get("song") or []]}

    async def song(self, sid: str) -> dict:
        return map_song((await self.call("getSong", id=sid)).get("song") or {})

    async def search(self, q: str) -> dict:
        r = (await self.call("search3", query=q, artistCount=12, albumCount=24, songCount=30)).get("searchResult3") or {}
        return {"artists": [map_artist(a) for a in r.get("artist") or []],
                "albums": [map_album(a) for a in r.get("album") or []],
                "songs": [map_song(s) for s in r.get("song") or []]}

    async def start_scan(self) -> dict:
        r = await self.call("startScan")
        return {"scanning": bool((r.get("scanStatus") or {}).get("scanning"))}

    async def scan_status(self) -> dict:
        s = (await self.call("getScanStatus")).get("scanStatus") or {}
        return {"scanning": bool(s.get("scanning")), "count": s.get("count") or 0}
