"""NAS library index: which movies / episodes are already there.

Layout understood (Sonarr/Radarr style, but tolerant):
    movies_dir/Title (Year)/Title (Year) ....mkv     (or loose files)
    series_dir/Show [(Year)]/Season N/Show - S01E02 - Title ....mkv
Scanning uses os.scandir only (no ffprobe) and runs in a thread: SMB on a
Pi is slow but a 1000-title library takes seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from ...i18n import _
from .files import (DL_SUB_EXT, SUB_EXT, VIDEO_EXT, check_file, delete_video_with_subs, hid,
                    inside, siblings_of, size_of)
from .ranking import ARTICLES, normalize, parse

log = logging.getLogger("hub.media.library")

FOLDER_RE = re.compile(r"^(?P<title>.+?)\s*[\(\[](?P<year>(19|20)\d\d)[\)\]]")
SEASON_DIR_RE = re.compile(r"(?:season|stagione|serie|s)\s*0*(\d{1,2})\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_lib_movies (
    path TEXT PRIMARY KEY, title TEXT, norm TEXT, year INTEGER, quality TEXT, size INTEGER,
    mtime REAL, meta_id TEXT, poster TEXT, genres TEXT);
CREATE TABLE IF NOT EXISTS media_lib_shows (
    path TEXT PRIMARY KEY, title TEXT, norm TEXT, year INTEGER, episodes INTEGER, size INTEGER,
    mtime REAL, meta_id TEXT, poster TEXT, genres TEXT);
CREATE TABLE IF NOT EXISTS media_lib_episodes (
    show_path TEXT, season INTEGER, episode INTEGER, path TEXT,
    PRIMARY KEY (show_path, season, episode));
"""


def key_variants(title: str) -> set[str]:
    n = normalize(title)
    out = {n, n.replace(" and ", " ")}
    w = n.split()
    if w and w[0] in ARTICLES:
        out.add(" ".join(w[1:]))
    return {o for o in out if o}


def _videos(folder: str, depth: int = 2):
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith((".", "@")):
                    continue
                if e.is_dir(follow_symlinks=False) and depth > 0:
                    yield from _videos(e.path, depth - 1)
                elif Path(e.name).suffix.lower() in VIDEO_EXT and "sample" not in e.name.lower():
                    st = e.stat()
                    yield e.path, st.st_size, st.st_mtime
    except OSError as err:
        log.debug("can't read %s: %s", folder, err)


def scan_movies(root: str) -> list[dict]:
    out = []
    if not root or not os.path.isdir(root):
        return out
    with os.scandir(root) as it:
        for e in it:
            if e.name.startswith((".", "@", "#")):
                continue
            if e.is_dir():
                vids = sorted(_videos(e.path), key=lambda v: -v[1])
                if not vids:
                    continue
                path, size, mtime = vids[0]
                name = e.name
            elif Path(e.name).suffix.lower() in VIDEO_EXT:
                st = e.stat()
                path, size, mtime, name = e.path, st.st_size, st.st_mtime, Path(e.name).stem
            else:
                continue
            m = FOLDER_RE.match(name)
            p = parse(Path(path).name)
            title = m.group("title").strip() if m else (p.head or name)
            year = int(m.group("year")) if m else p.year
            out.append({"path": e.path, "title": title, "norm": normalize(title), "year": year,
                        "quality": " ".join(x for x in [f"{p.resolution}p" if p.resolution else "",
                                                         p.source] if x),
                        "size": size, "mtime": mtime})
    return out


def scan_series(root: str) -> tuple[list[dict], list[dict]]:
    shows, episodes = [], []
    if not root or not os.path.isdir(root):
        return shows, episodes
    with os.scandir(root) as it:
        for e in it:
            if not e.is_dir() or e.name.startswith((".", "@", "#")):
                continue
            m = FOLDER_RE.match(e.name)
            title = m.group("title").strip() if m else e.name.strip()
            year = int(m.group("year")) if m else None
            eps, total, newest = {}, 0, 0.0
            for path, size, mtime in _videos(e.path, depth=3):
                p = parse(Path(path).name)
                season, ep = p.season, p.episode
                if season is None:
                    sm = SEASON_DIR_RE.search(Path(path).parent.name)
                    season = int(sm.group(1)) if sm else None
                if season is None or ep is None:
                    continue
                for n in range(ep, (p.episode_end or ep) + 1):
                    eps[(season, n)] = path
                total += size
                newest = max(newest, mtime)
            shows.append({"path": e.path, "title": title, "norm": normalize(title), "year": year,
                          "episodes": len(eps), "size": total, "mtime": newest})
            episodes += [{"show_path": e.path, "season": s, "episode": n, "path": p}
                         for (s, n), p in eps.items()]
    return shows, episodes


class Library:
    def __init__(self, plugin, cfg: dict):
        self.plugin = plugin
        self.store = plugin.hub.store
        self.movies_dir = cfg.get("movies_dir", "")
        self.series_dir = cfg.get("series_dir", "")
        self.store.db.executescript(SCHEMA)
        for table in ("media_lib_movies", "media_lib_shows"):  # older databases: add the genres column
            try:
                self.store.execute(f"ALTER TABLE {table} ADD COLUMN genres TEXT")
            except Exception:
                pass
        self.last_scan = self.store.get("media.library.last_scan", 0)
        self.scanning = False
        self._load()

    def _load(self):
        self.movies: dict[str, list[dict]] = {}
        for m in self.store.query("SELECT * FROM media_lib_movies"):
            for k in key_variants(m["title"]):
                self.movies.setdefault(k, []).append(m)
        self.shows: dict[str, dict] = {}
        for s in self.store.query("SELECT * FROM media_lib_shows"):
            for k in key_variants(s["title"]):
                self.shows[k] = s

    async def scan(self) -> dict:
        if self.scanning:
            return self.stats()
        self.scanning = True
        t0 = time.time()
        try:
            if not any(d and os.path.isdir(d) for d in (self.movies_dir, self.series_dir)):
                log.warning("library folders not found (%s, %s): is the NAS mounted?",
                            self.movies_dir, self.series_dir)
                return self.stats()  # keep the previous index rather than wiping it
            movies = await asyncio.to_thread(scan_movies, self.movies_dir)
            shows, eps = await asyncio.to_thread(scan_series, self.series_dir)
        except OSError as err:
            log.warning("library scan failed: %s", err)
            return self.stats()
        finally:
            self.scanning = False
        db = self.store
        # keep resolved posters across rescans
        old_m = {r["path"]: r for r in db.query("SELECT path, meta_id, poster, genres FROM media_lib_movies")}
        old_s = {r["path"]: r for r in db.query("SELECT path, meta_id, poster, genres FROM media_lib_shows")}
        db.execute("BEGIN")
        try:
            db.execute("DELETE FROM media_lib_movies")
            db.execute("DELETE FROM media_lib_shows")
            db.execute("DELETE FROM media_lib_episodes")
            for m in movies:
                o = old_m.get(m["path"], {})
                db.execute("INSERT OR REPLACE INTO media_lib_movies (path,title,norm,year,quality,size,mtime,"
                           "meta_id,poster,genres) VALUES "
                           "(:path,:title,:norm,:year,:quality,:size,:mtime,:meta_id,:poster,:genres)",
                           {**m, "meta_id": o.get("meta_id"), "poster": o.get("poster"),
                            "genres": o.get("genres")})
            for s in shows:
                o = old_s.get(s["path"], {})
                db.execute("INSERT OR REPLACE INTO media_lib_shows (path,title,norm,year,episodes,size,mtime,"
                           "meta_id,poster,genres) VALUES "
                           "(:path,:title,:norm,:year,:episodes,:size,:mtime,:meta_id,:poster,:genres)",
                           {**s, "meta_id": o.get("meta_id"), "poster": o.get("poster"),
                            "genres": o.get("genres")})
            for e in eps:
                db.execute("INSERT OR REPLACE INTO media_lib_episodes VALUES "
                           "(:show_path,:season,:episode,:path)", e)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        self.last_scan = time.time()
        self.store.set("media.library.last_scan", self.last_scan)
        self._load()
        log.info("library scanned in %.1fs: %d movies, %d shows, %d episodes",
                 time.time() - t0, len(movies), len(shows), len(eps))
        return self.stats()

    # -- lookups --------------------------------------------------------------------

    def find_movie(self, titles: list[str], year: int | None) -> dict | None:
        for t in titles:
            for k in key_variants(t or ""):
                for m in self.movies.get(k, []):
                    if year is None or m["year"] is None or abs(m["year"] - year) <= 1:
                        return m
        return None

    def find_show(self, titles: list[str]) -> dict | None:
        for t in titles:
            for k in key_variants(t or ""):
                if k in self.shows:
                    return self.shows[k]
        return None

    def episodes(self, show: dict | None) -> set[tuple[int, int]]:
        if not show:
            return set()
        return {(r["season"], r["episode"]) for r in self.store.query(
            "SELECT season, episode FROM media_lib_episodes WHERE show_path=?", (show["path"],))}

    def episode_paths(self, show: dict | None, season: int) -> dict[int, str]:
        if not show:
            return {}
        return {r["episode"]: r["path"] for r in self.store.query(
            "SELECT episode, path FROM media_lib_episodes WHERE show_path=? AND season=?",
            (show["path"], season))}

    def flag(self, item: dict) -> dict:
        """Annotate a metadata item with in_library info."""
        titles = item.get("titles") or [item.get("title"), item.get("original_title")]
        if item["type"] == "movie":
            m = self.find_movie(titles, item.get("year"))
            item["in_library"] = bool(m)
        else:
            s = self.find_show(titles)
            item["in_library"] = bool(s)
            item["library_episodes"] = s["episodes"] if s else 0
        return item

    def add_episode(self, show_path: str, season: int, episode: int, path: str):
        self.store.execute("INSERT OR REPLACE INTO media_lib_episodes VALUES (?,?,?,?)",
                           (show_path, season, episode, path))

    def stats(self) -> dict:
        q = self.store.query
        missing = [n for n, d in (("movies", self.movies_dir), ("series", self.series_dir))
                   if d and not os.path.isdir(d)]
        unset = [n for n, d in (("movies", self.movies_dir), ("series", self.series_dir)) if not d]
        problem = ""
        if missing:
            problem = "library folder not found - mount the NAS in Storage"
        elif unset:
            problem = f"{' and '.join(unset)} folder not set - choose it in Media settings"
        return {
            "problem": problem,
            "movies": q("SELECT COUNT(*) n FROM media_lib_movies")[0]["n"],
            "shows": q("SELECT COUNT(*) n FROM media_lib_shows")[0]["n"],
            "episodes": q("SELECT COUNT(*) n FROM media_lib_episodes")[0]["n"],
            "size": (q("SELECT SUM(size) s FROM media_lib_movies")[0]["s"] or 0)
                    + (q("SELECT SUM(size) s FROM media_lib_shows")[0]["s"] or 0),
            "last_scan": self.last_scan, "scanning": self.scanning,
            "movies_dir": self.movies_dir, "series_dir": self.series_dir,
            "movies_dir_ok": bool(self.movies_dir) and os.path.isdir(self.movies_dir),
            "series_dir_ok": bool(self.series_dir) and os.path.isdir(self.series_dir),
        }

    def browse(self, kind: str, genre: str = "") -> list[dict]:
        if kind == "movies":
            rows = self.store.query("SELECT path, title, year, quality, size, mtime, meta_id, poster, genres "
                                    "FROM media_lib_movies ORDER BY mtime DESC")
        else:
            rows = self.store.query("SELECT path, title, year, episodes, size, mtime, meta_id, poster, genres "
                                    "FROM media_lib_shows ORDER BY mtime DESC")
        for r in rows:
            try:
                r["genres"] = [str(g) for g in json.loads(r["genres"] or "[]")]
            except ValueError:
                r["genres"] = []
            if kind == "movies":
                r["file_id"] = "m-" + hid(r["path"])
            del r["path"]  # the client never sees (or sends back) a path
        if genre:
            rows = [r for r in rows if genre in r["genres"]]
        return rows

    # -- files: ids <-> paths (the client only ever holds ids) ---------------------------

    @property
    def roots(self) -> list[str]:
        return [d for d in (self.movies_dir, self.series_dir) if d]

    def entry(self, fid: str) -> tuple[str, str]:
        """('m'|'e', indexed path) for a file id; KeyError when the index doesn't know it."""
        kind, _sep, h = fid.partition("-")
        table = {"m": "media_lib_movies", "e": "media_lib_episodes"}.get(kind)
        if not table or not re.fullmatch(r"[0-9a-f]{20}", h):
            raise KeyError(_("unknown file"))
        for r in self.store.query(f"SELECT path FROM {table}"):
            if hid(r["path"]) == h:
                return kind, r["path"]
        raise KeyError(_("unknown file"))

    def _video(self, kind: str, entry: str) -> str:
        """The video file behind an index entry (movies index their folder)."""
        if kind == "e" or not os.path.isdir(entry):
            return entry
        vids = sorted(_videos(entry), key=lambda v: -v[1])
        if not vids:
            raise FileNotFoundError(_("The file is no longer there"))
        return vids[0][0]

    def resolve(self, fid: str, sub: int | None = None) -> tuple[Path, str]:
        """(safe real path, download name) for a video id, or its n-th subtitle. Raises KeyError
        (unknown id), PermissionError (outside the library roots), FileNotFoundError."""
        kind, entry = self.entry(fid)
        video = self._video(kind, entry)
        if sub is None:
            return check_file(video, self.roots), Path(video).name
        subs = siblings_of(video)
        if not 0 <= sub < len(subs) or Path(subs[sub]).suffix.lower() not in DL_SUB_EXT:
            raise KeyError(_("unknown file"))
        return check_file(subs[sub], self.roots), Path(subs[sub]).name

    def file_info(self, kind: str, entry: str) -> dict | None:
        """{id, name, size, subtitles: [{n, name, size}]} for an indexed movie / episode, if present."""
        try:
            video = self._video(kind, entry)
            real = check_file(video, self.roots)
        except (OSError, PermissionError):
            return None
        subs = [{"n": n, "name": Path(sp).name, "size": size_of(sp)}
                for n, sp in enumerate(siblings_of(video))
                if Path(sp).suffix.lower() in DL_SUB_EXT and inside(sp, self.roots)]
        return {"id": f"{kind}-{hid(entry)}", "name": real.name, "size": real.stat().st_size,
                "subtitles": subs}

    def delete_entry(self, fid: str) -> dict:
        """Delete a movie / episode video and its subtitles from the NAS (the UI confirmed)."""
        kind, entry = self.entry(fid)
        return delete_video_with_subs(self._video(kind, entry), self.roots)

    async def resolve_posters(self, limit: int = 40) -> None:
        """Match library folders to metadata (poster + id + genres) a few at a time."""
        meta = self.plugin.metadata
        for table, type_ in (("media_lib_movies", "movie"), ("media_lib_shows", "series")):
            rows = self.store.query(f"SELECT path, title, year, meta_id FROM {table} "
                                    f"WHERE meta_id IS NULL OR genres IS NULL LIMIT ?", (limit,))
            for r in rows:
                try:
                    res = [i for i in await meta.search(r["title"]) if i["type"] == type_]
                except Exception as err:
                    log.debug("poster lookup %s: %s", r["title"], err)
                    return
                if r["meta_id"]:  # already matched: keep the match, just learn the genres
                    best = next((i for i in res if i["id"] == r["meta_id"]), None)
                else:
                    best = next((i for i in res if r["year"] and i.get("year")
                                 and abs(i["year"] - r["year"]) <= 1), res[0] if res else None)
                genres = json.dumps([str(g) for g in (best or {}).get("genres") or []])
                if r["meta_id"]:
                    self.store.execute(f"UPDATE {table} SET genres=? WHERE path=?", (genres, r["path"]))
                else:
                    self.store.execute(f"UPDATE {table} SET meta_id=?, poster=?, genres=? WHERE path=?",
                                       (best["id"] if best else "", best.get("poster") if best else "",
                                        genres, r["path"]))
                await asyncio.sleep(0.25)  # be gentle with the APIs
