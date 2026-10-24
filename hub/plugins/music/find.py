"""Find albums on the trackers enabled in Torrent search, download with Transmission, import into the library.

Downloads go to ``<media download dir or music dir>/.incoming``; when one finishes the torrent leaves
Transmission (data stays), audio + cover are moved to ``<music_dir>/<Artist>/<Album (year)>/`` keeping the
track file names, the leftovers inside the incoming folder are deleted and Navidrome rescans.
Nothing is ever written or deleted outside ``music_dir`` and the incoming folder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from ...i18n import _
from ..games.find import UnpackError, unzip

log = logging.getLogger("hub.music")

STORE = "music.downloads"
AUDIO = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv"}
COVERS = {"cover", "folder", "front", "album", "albumart"}
IMAGES = {".jpg", ".jpeg", ".png"}
DISC = re.compile(r"^(cd|disc|disk)\s*0*\d+\b", re.I)
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
YEAR = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
MAX_FILES = 3000


def safe_name(s: str, fallback: str = "Unknown") -> str:
    s = BAD_CHARS.sub(" ", str(s or ""))
    s = " ".join(s.split()).strip(" .")[:120].strip(" .")
    return fallback if s in ("", ".", "..") else s


def inside(base: Path, target: Path) -> bool:
    base, target = base.resolve(), target.resolve()
    return target == base or base in target.parents


def album_folder(album: str, year) -> str:
    y = str(year or "").strip()
    return safe_name(album, "Unknown album") + (f" ({y})" if re.fullmatch(r"\d{4}", y) else "")


def guess_from_title(title: str) -> dict:
    """'Artist - Album (2001) [FLAC]' → {artist, album, year}. Best effort, may be empty."""
    t = title or ""
    year = YEAR.search(t)
    t = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", t).replace("_", " ")
    t = re.sub(r"\b(flac|mp3|320|v0|lossless|web|cd|discography|24\s?bit)\b", " ", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", " ".join(t.split())) if p.strip()]
    return {"artist": parts[0] if len(parts) > 1 else "", "album": parts[1] if len(parts) > 1 else (parts[0] if parts else ""),
            "year": year.group(1) if year else ""}


def hints(title: str) -> list[str]:
    t = title or ""
    out = []
    if re.search(r"\bflac\b|lossless|\bape\b", t, re.I):
        out.append("FLAC")
    if re.search(r"24[\s-]?bit|hi-?res", t, re.I):
        out.append("24-bit")
    if re.search(r"\b320\b", t):
        out.append("MP3 320")
    elif re.search(r"\bv0\b", t, re.I):
        out.append("MP3 V0")
    elif re.search(r"\bmp3\b", t, re.I):
        out.append("MP3")
    if re.search(r"discograph|complete|collection|anthology", t, re.I):
        out.append("discography")
    return out


# -- importing ------------------------------------------------------------------------------------

def _is_cover(p: Path) -> bool:
    return p.suffix.lower() in IMAGES and p.stem.lower() in COVERS


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in range(2, 100):
        c = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not c.exists():
            return c
    raise OSError("too many files with the same name")


def _move(src: Path, dest: Path, music_dir: Path) -> bool:
    if not inside(music_dir, dest.parent):
        raise UnpackError("destination outside the music folder")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        return False
    shutil.move(str(src), str(_unique(dest)))
    return True


def import_payload(payload: Path, incoming: Path, music_dir: Path, artist: str, album: str, year,
                   discography: bool = False) -> dict:
    """Move audio + covers of a finished download into the library. Blocking; run in an executor.

    Returns {files, albums: [folder, …]}. Raises UnpackError when nothing usable was found."""
    music_dir, incoming, payload = music_dir.resolve(), incoming.resolve(), payload.resolve()
    if not inside(incoming, payload) or payload == incoming:
        raise UnpackError("outside the incoming folder")
    artist_dir = safe_name(artist, "Unknown artist")
    temps: list[Path] = []
    try:
        roots = [payload]
        # unpack zips (next to themselves, inside incoming) and treat the result as part of the payload
        zips = [payload] if payload.is_file() and payload.suffix.lower() == ".zip" else \
            sorted(p for p in payload.rglob("*") if p.is_file() and p.suffix.lower() == ".zip") if payload.is_dir() else []
        for i, z in enumerate(zips):
            tmp = incoming / f".unzip-{os.getpid()}-{i}-{int(time.time())}"
            temps.append(tmp)
            unzip(z, tmp)
            roots.append(tmp)
        files: list[tuple[Path, Path]] = []        # (file, root it belongs to)
        for r in roots:
            cands = [r] if r.is_file() else sorted(p for p in r.rglob("*") if p.is_file())
            if len(cands) > MAX_FILES:
                raise UnpackError("too many files")
            files += [(p, r) for p in cands if not any(part.startswith(".unzip-") for part in p.parts[len(r.parts):])]
        audio = [(p, r) for p, r in files if p.suffix.lower() in AUDIO and not p.is_symlink()]
        if not audio:
            raise UnpackError("no audio files in the download")

        def group_key(p: Path, r: Path) -> Path:
            """Folder an audio file belongs to as an album (CD1/CD2 fold into their parent)."""
            d = p.parent
            while d != r and d != incoming and DISC.match(d.name):
                d = d.parent
            return d

        groups: dict[Path, list[Path]] = {}
        for p, r in audio:
            groups.setdefault(group_key(p, r), []).append(p)
        albums, moved = [], 0
        for gdir, members in sorted(groups.items()):
            if discography and len(groups) > 0 and gdir != payload and gdir.name:
                g = guess_from_title(gdir.name.replace(".", " ") if " " not in gdir.name else gdir.name)
                name = g["album"] or gdir.name
                y = g["year"] or YEAR.search(gdir.name) and YEAR.search(gdir.name).group(1) or ""
                dest = music_dir / artist_dir / album_folder(re.sub(r"^\s*[-–.]\s*|^(19|20)\d\d\s*[-–.]\s*", "", name), y)
            else:
                dest = music_dir / artist_dir / album_folder(album or payload.stem, year)
            if not inside(music_dir, dest):
                raise UnpackError("destination outside the music folder")
            multi = {p.parent for p in members}
            for p in members:
                rel_sub = p.parent.name if (len(multi) > 1 and p.parent != gdir) else ""
                moved += _move(p, dest / safe_name(rel_sub, "") / p.name if rel_sub else dest / p.name, music_dir)
            # cover: from the same folder tree, folder.jpg/cover.jpg
            for p, r in files:
                if _is_cover(p) and (p.parent == gdir or (len(groups) == 1 and p.parent != incoming)):
                    _move(p, dest / ("cover" + p.suffix.lower()), music_dir)
                    break
            albums.append(str(dest))
        return {"files": moved, "albums": sorted(set(albums))}
    finally:
        for tmp in temps:
            if inside(incoming, tmp) and tmp != incoming:
                shutil.rmtree(tmp, ignore_errors=True)
        # leftovers: only what this download created inside the incoming folder
        if inside(incoming, payload) and payload != incoming:
            if payload.is_dir():
                shutil.rmtree(payload, ignore_errors=True)
            else:
                payload.unlink(missing_ok=True)


# -- the service ----------------------------------------------------------------------------------

class Finder:
    def __init__(self, plugin):
        self.p = plugin
        self.live: dict[str, dict] = {}
        self._busy: set[str] = set()

    def entries(self) -> list[dict]:
        v = self.p.hub.store.get(STORE, [])
        return [e for e in v if isinstance(e, dict) and e.get("hash")] if isinstance(v, list) else []

    def _save(self, entries: list[dict]) -> None:
        self.p.hub.store.set(STORE, entries)

    def _update(self, h: str, **kw) -> None:
        es = self.entries()
        for e in es:
            if e["hash"] == h:
                e.update(kw)
        self._save(es)

    def _torrents(self):
        t = self.p.hub.plugins.get("torrents")
        if t is None or not getattr(t, "engine", None) or not t.enabled_ids():
            raise ValueError(_("Turn on Torrent search and enable some trackers"))
        return t

    def _tx(self):
        m = self.p.hub.plugins.get("media")
        tx = getattr(m, "transmission", None)
        if tx is None or not getattr(tx, "enabled", False):
            raise ValueError(_("Downloads need the Media plugin with Transmission turned on"))
        return tx

    @property
    def music_dir(self) -> Path:
        return Path(os.path.expanduser(self.p.music_dir))

    def incoming(self) -> Path:
        m = self.p.hub.plugins.get("media")
        base = (getattr(m, "config", None) or {}).get("download_dir") or str(self.music_dir)
        return Path(os.path.expanduser(base)) / ".incoming"

    # -- search
    async def search(self, artist: str, album: str, discography: bool = False, page: int = 1) -> dict:
        artist, album = " ".join((artist or "").split()), " ".join((album or "").split())
        q = f"{artist} discography" if discography else f"{artist} {album}".strip()
        if len(q) < 2 or len(q) > 100:
            raise ValueError(_("Type at least 2 characters"))
        try:
            t = self._torrents()
        except ValueError as err:
            return {"disabled": True, "error": str(err), "results": [], "providers": [], "has_more": False, "query": q}
        from ..torrents.engine import refine
        size = min(max(int(t.config.get("page_size", 20) or 20), 5), 50)
        merged, statuses, more = await t.engine.search(t.session, q, max(page, 1), size, sorted(t.enabled_ids()),
                                                       category="music")
        for st in statuses:
            t._note(st)
        want = set(re.findall(r"[a-z0-9]+", (artist + " " + ("" if discography else album)).lower()))
        out = []
        for r in refine(merged, "seeders", "desc"):
            words = set(re.findall(r"[a-z0-9]+", r.title.lower()))
            out.append({"title": r.title, "size": r.size, "seeders": r.seeders, "leechers": r.leechers,
                        "source": r.source, "sources": r.sources, "magnet": r.magnet, "url": r.url, "hash": r.hash,
                        "hints": hints(r.title), "match": bool(want) and len(want & words) / len(want) >= 0.7})
        out.sort(key=lambda x: (not x["match"], -(x["seeders"] or 0)))
        error = None if any(s["ok"] for s in statuses) else _("Every provider failed. See details below.")
        provs = [{"id": s["id"], "name": s["name"], "ok": s["ok"], "count": s.get("count", 0), "error": s.get("error")}
                 for s in statuses]
        return {"query": q, "results": out, "providers": provs, "has_more": bool(more), "error": error}

    # -- download
    async def add(self, title: str, artist: str, album: str, year="", magnet: str = "", url: str = "",
                  discography: bool = False) -> str:
        title = " ".join(str(title or "").split())[:300]
        if not title:
            raise ValueError(_("Missing title"))
        g = guess_from_title(title)
        artist = " ".join(str(artist or g["artist"]).split())[:120]
        album = " ".join(str(album or g["album"]).split())[:160]
        year = str(year or g["year"] or "")[:4]
        if not artist:
            raise ValueError(_("Missing artist"))
        link = magnet or url
        if not (isinstance(link, str) and (link.startswith("magnet:?") or link.startswith(("http://", "https://")))):
            raise ValueError(_("Give a magnet link or a torrent URL"))
        tx = self._tx()
        folder = self.incoming()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if inside(self.music_dir, folder):
                (folder / ".ndignore").touch()       # Navidrome must not index half-downloaded files
        except OSError as err:
            raise ValueError(_("Cannot create {path}: {err}", path=str(folder), err=str(err)))
        from ..media.transmission import TransmissionError
        try:
            res = await tx.add(link, download_dir=str(folder))
        except TransmissionError as err:
            raise ValueError(str(err))
        h = (res.get("hash") or "").lower()
        if not h:
            raise ValueError(_("Transmission did not accept the torrent"))
        es = [e for e in self.entries() if e["hash"] != h]
        es.append({"hash": h, "title": title, "artist": artist, "album": album, "year": year,
                   "discography": bool(discography), "added": time.time(), "state": "downloading",
                   "error": None, "dir": str(folder), "payload": None})
        self._save(es)
        return h

    def listing(self) -> list[dict]:
        out = []
        for e in self.entries():
            lv = self.live.get(e["hash"]) or {}
            done = e["state"] == "done"
            out.append({"hash": e["hash"], "title": e["title"], "artist": e.get("artist"), "album": e.get("album"),
                        "year": e.get("year"), "added": e.get("added"),
                        "progress": 1.0 if done else lv.get("progress", 0.0), "rate": 0 if done else lv.get("rate", 0),
                        "eta": None if done else lv.get("eta"), "state": e["state"], "error": e.get("error")})
        return out

    async def cancel(self, h: str) -> None:
        es = self.entries()
        e = next((x for x in es if x["hash"] == h), None)
        if not e:
            raise KeyError(_("Download not found"))
        if e["state"] == "downloading":
            try:
                await self._tx().remove(h, delete_data=True)
            except Exception as err:
                log.info("cancel %s: %s", h, err)
        self._save([x for x in es if x["hash"] != h])
        self.live.pop(h, None)

    async def resume(self) -> None:
        for e in self.entries():
            if e["state"] == "importing" and e.get("payload"):
                self.p.hub.spawn(self._finish(e["hash"]), name="music-finish")

    async def poll(self) -> None:
        active = [e for e in self.entries() if e["state"] == "downloading"]
        if not active:
            return
        try:
            found = {t["hash"]: t for t in await self._tx().torrents([e["hash"] for e in active])}
        except Exception as err:
            log.debug("music poll: %s", err)
            return
        for e in active:
            h, t = e["hash"], found.get(e["hash"])
            if t is None:
                self._update(h, state="error", error=_("The torrent is no longer in Transmission"))
                continue
            done = t.get("percentDone", 0) >= 1 or (t.get("leftUntilDone", 1) == 0 and t.get("metadataPercentComplete", 1) >= 1)
            self.live[h] = {"progress": 1.0 if done else float(t.get("percentDone") or 0.0),
                            "rate": int(t.get("rateDownload") or 0),
                            "eta": t["eta"] if isinstance(t.get("eta"), int) and t["eta"] >= 0 else None}
            if t.get("error"):
                self._update(h, error=str(t.get("errorString") or "")[:200] or None)
            if done and h not in self._busy:
                payload = os.path.join(t.get("downloadDir") or e["dir"], t.get("name") or "")
                self._update(h, state="importing", payload=payload, error=None)
                self.p.hub.spawn(self._finish(h), name="music-finish")

    async def _finish(self, h: str) -> None:
        if h in self._busy:
            return
        self._busy.add(h)
        try:
            e = next((x for x in self.entries() if x["hash"] == h), None)
            if not e:
                return
            try:
                await self._tx().remove(h, delete_data=False)
            except Exception as err:
                log.info("remove %s: %s", h, err)
            state, msg = "done", None
            try:
                payload = Path(e["payload"])
                if not payload.exists():
                    state, msg = "error", _("The downloaded files were not found")
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, lambda: import_payload(
                        payload, Path(e["dir"]), self.music_dir, e["artist"], e["album"], e.get("year"),
                        bool(e.get("discography"))))
            except UnpackError as err:
                state, msg = "error", _("Could not import: {err}", err=str(err))
            except (OSError, zipfile.BadZipFile) as err:
                state, msg = "error", str(err)
            self._update(h, state=state, error=msg)
            if state == "done":
                try:
                    await self.p.rescan()
                except Exception:
                    log.exception("rescan after download")
        finally:
            self._busy.discard(h)
