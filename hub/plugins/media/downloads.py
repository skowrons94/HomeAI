"""Downloads the hub started: Transmission progress, then import into the library.

post_process:
  move  remove the torrent (keeping data), move files into the library, clean up
        the leftover folder - only ever inside download_dir
  copy  copy into the library, keep seeding until seed_ratio, then let
        Transmission delete its own data
  none  leave files where Transmission put them
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .library import SUB_EXT, VIDEO_EXT
from .ranking import parse
from .transmission import TransmissionError

log = logging.getLogger("hub.media.downloads")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_downloads (
    hash TEXT PRIMARY KEY, name TEXT, key TEXT, type TEXT, title TEXT, year INTEGER,
    season INTEGER, episode INTEGER, file_idx INTEGER, quality TEXT, size INTEGER,
    added REAL, state TEXT, progress REAL DEFAULT 0, rate INTEGER DEFAULT 0, eta INTEGER,
    target TEXT, error TEXT, data TEXT);
"""
ACTIVE = ("queued", "downloading", "paused", "completed", "seeding")


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", s or "").strip(" .")
    return re.sub(r"\s{2,}", " ", s)[:180] or "Untitled"


def copy_file(src: str, dst: str) -> None:
    """Copy safely onto network shares: write ``dst.part`` and rename it, so a
    half-copied file never looks complete. Timestamps/permissions are copied
    when allowed - SMB/NFS mounts often refuse them (EPERM), which is fine."""
    tmp = dst + ".part"
    try:
        shutil.copyfile(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError(f"incomplete copy of {src}")
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _is_within(path: str, root: str) -> bool:
    try:
        return bool(root) and Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


LANG3 = {"ita": "it", "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
         "por": "pt", "rus": "ru", "pol": "pl", "dut": "nl", "nld": "nl", "jpn": "ja", "kor": "ko"}
SUB_TAGS = {"forced", "hi", "sdh", "cc"}


def _sub_suffix(name: str) -> str:
    """Keep language tags of subtitles: Movie.it.forced.srt -> '.it.forced'"""
    keep = []
    for part in Path(name).stem.lower().replace("_", ".").split(".")[-3:]:
        part = LANG3.get(part, part)
        if part in SUB_TAGS or (len(part) == 2 and part.isalpha()):
            keep.append(part)
    return "".join("." + p for p in keep)


def _unique_dests(plan: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Two subtitles mapping to the same name get .2, .3 ..."""
    seen, out = set(), []
    for src, dst in plan:
        stem, ext, n = dst[: -len(Path(dst).suffix)], Path(dst).suffix, 1
        while dst in seen:
            n += 1
            dst = f"{stem}.{n}{ext}"
        seen.add(dst)
        out.append((src, dst))
    return out


class Downloads:
    def __init__(self, plugin, cfg: dict):
        self.plugin = plugin
        self.hub = plugin.hub
        self.store = plugin.hub.store
        self.tr = plugin.transmission
        self.download_dir = cfg.get("download_dir", "")
        self.movies_dir = cfg.get("movies_dir", "")
        self.series_dir = cfg.get("series_dir", "")
        self.post_process = cfg.get("post_process", "move")
        self.seed_ratio = float(cfg.get("seed_ratio", 0))
        self.store.db.executescript(SCHEMA)
        self.ok: bool | None = None
        self.error: str | None = None
        self._importing: set[str] = set()

    # -- queries ---------------------------------------------------------------------

    def rows(self, active_only=False) -> list[dict]:
        sql = "SELECT * FROM media_downloads"
        if active_only:
            sql += f" WHERE state IN ({','.join('?' * len(ACTIVE))})"
        rows = self.store.query(sql + " ORDER BY added DESC LIMIT 200",
                                ACTIVE if active_only else ())
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    def active_for(self, key: str) -> list[dict]:
        return [r for r in self.rows(True) if r["key"] == key]

    def _update(self, hash_: str, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        self.store.execute(f"UPDATE media_downloads SET {sets} WHERE hash=?",
                           (*fields.values(), hash_))

    def _publish(self):
        self.hub.publish("media", {"kind": "downloads", "items": self.rows()[:50]})

    # -- add ---------------------------------------------------------------------------

    async def add(self, item: dict, release: dict, season=None, episode=None) -> dict:
        from .indexers import magnet

        if release.get("pack") and episode is not None and release.get("file_idx") is None:
            episode = None  # it's a pack: we'll import every episode in it
        link = release.get("magnet") or magnet(release["hash"], release["name"])
        if link.startswith(("http://", "https://")):
            # fetch .torrent files ourselves (through the indexer proxy if any)
            async with self.plugin.indexers._get(link) as r:
                r.raise_for_status()
                res = await self.tr.add(download_dir=self.download_dir or None,
                                        torrent=await r.read())
        else:
            res = await self.tr.add(link, self.download_dir or None)
        h = res["hash"] or release["hash"]
        key = f"{item['type']}:{item['id']}"
        self.store.execute(
            "INSERT OR REPLACE INTO media_downloads (hash, name, key, type, title, year, season, "
            "episode, file_idx, quality, size, added, state, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, release["name"], key, item["type"], item["title"], item.get("year"),
             season, episode, release.get("file_idx"), release.get("quality"),
             release.get("size"), time.time(), "queued",
             json.dumps({"poster": item.get("poster"), "id": item["id"],
                         "titles": item.get("titles"), "language": release.get("language"),
                         "audio": release.get("audio"), "imdb_id": item.get("imdb_id")})))
        log.info("download added: %s (%s)", release["name"], h)
        self._publish()
        return {"hash": h, "name": release["name"]}

    # -- polling -------------------------------------------------------------------------

    async def poll(self) -> None:
        rows = {r["hash"]: r for r in self.rows(True)}
        try:
            ts = {t["hash"]: t for t in await self.tr.torrents(list(rows), files=True)} if rows else {}
            if not rows:
                await self.tr.session()
            self.ok, self.error = True, None
        except TransmissionError as err:
            self.ok, self.error = False, str(err)
            return
        changed = False
        for h, row in rows.items():
            t = ts.get(h)
            if t is None:
                if row["state"] in ("queued", "downloading"):
                    self._update(h, state="removed", error="removed from Transmission")
                    changed = True
                continue
            # an episode out of a season pack: fetch only that file
            if row["file_idx"] is not None and t.get("files") and not row["data"].get("files_set"):
                await self.tr.only_file(h, row["file_idx"], len(t["files"]))
                row["data"]["files_set"] = True
                self._update(h, data=json.dumps(row["data"]))
            done = t["percentDone"] >= 1 and t["leftUntilDone"] == 0 and t["metadataPercentComplete"] >= 1
            fields = {"progress": round(t["percentDone"], 4), "rate": t["rateDownload"],
                      "eta": t["eta"], "size": t["sizeWhenDone"] or row["size"],
                      "error": t["errorString"] or None}
            if row["state"] in ("queued", "downloading"):
                fields["state"] = "completed" if done else (
                    "downloading" if t["status"] != 0 else "paused")
            if any(row.get(k) != v for k, v in fields.items()):
                self._update(h, **fields)
                changed = True
            if done and row["state"] in ("queued", "downloading", "paused") and h not in self._importing:
                self._importing.add(h)
                self.hub.spawn(self._import(h, t), f"import {row['name']}")
            if row["state"] == "seeding" and (t["uploadRatio"] >= self.seed_ratio or t["isFinished"]):
                await self._remove_torrent(h, delete_data=True)
                self._update(h, state="imported")
                changed = True
        if changed:
            self._publish()

    # -- import -------------------------------------------------------------------------

    async def _import(self, h: str, t: dict) -> None:
        row = next((r for r in self.rows() if r["hash"] == h), None)
        try:
            targets = await self._do_import(row, t)
            state = "seeding" if self.post_process == "copy" and self.seed_ratio > 0 else "imported"
            self._update(h, state=state, target=targets[0] if targets else None, error=None,
                         progress=1)
            if self.post_process == "copy" and state == "imported":
                await self._remove_torrent(h, delete_data=True)
            await self.plugin.library.scan()
            what = row["title"] + (f" S{row['season']:02d}" + (f"E{row['episode']:02d}"
                                                                if row["episode"] else "")
                                   if row["season"] is not None else "")
            await self.hub.notify.send(
                f"Ready to watch: {what}",
                f"{row['quality'] or ''} · {len(targets)} file(s) in the library - open Infuse",
                url=f"#/media/{row['type']}/{row['data'].get('id')}",
                image=row["data"].get("poster") or "", tag="tv")
            self.plugin.watcher.on_imported(row)
            self.store.delete(f"media.import.{h}")
        except Exception as err:
            log.exception("import of %s failed", row["name"] if row else h)
            self._update(h, state="failed", error=f"import: {err}")
            await self.hub.notify.send(f"Import failed: {row['title'] if row else h}", str(err),
                                       tag="warning")
        finally:
            self._importing.discard(h)
            self._publish()

    async def _do_import(self, row: dict, t: dict) -> list[str]:
        base = t["downloadDir"]
        wanted = [(i, f) for i, f in enumerate(t.get("files") or [])
                  if (row["file_idx"] is None or i == row["file_idx"])]
        videos = [(i, f) for i, f in wanted if Path(f["name"]).suffix.lower() in VIDEO_EXT
                  and "sample" not in f["name"].lower() and f["length"] > 20_000_000]
        subs = [f for _, f in enumerate(t.get("files") or [])
                if Path(f["name"]).suffix.lower() in SUB_EXT]
        if not videos:
            raise RuntimeError("no video file in the torrent")
        if self.post_process == "none":
            return [os.path.join(base, f["name"]) for _, f in videos]

        plan: list[tuple[str, str]] = []  # (source, destination)
        if row["type"] == "movie":
            _, f = max(videos, key=lambda v: v[1]["length"])
            folder = safe_name(f"{row['title']} ({row['year']})" if row["year"] else row["title"])
            stem = f"{folder} {row['quality'] or ''}".strip()
            dest_dir = os.path.join(self.movies_dir, folder)
            plan.append((os.path.join(base, f["name"]),
                         os.path.join(dest_dir, safe_name(stem) + Path(f["name"]).suffix.lower())))
            for s in subs:
                plan.append((os.path.join(base, s["name"]),
                             os.path.join(dest_dir, safe_name(stem) + _sub_suffix(s["name"])
                                          + Path(s["name"]).suffix.lower())))
        else:
            show = self.plugin.library.find_show(row["data"].get("titles") or [row["title"]])
            show_dir = show["path"] if show else os.path.join(self.series_dir, safe_name(row["title"]))
            ep_titles = await self._episode_titles(row)
            for _, f in videos:
                p = parse(Path(f["name"]).name)
                season = p.season if p.season is not None else row["season"]
                ep = p.episode if p.episode is not None else row["episode"]
                if season is None or ep is None:
                    log.warning("can't tell the episode of %s, skipped", f["name"])
                    continue
                epname = f" - S{season:02d}E{ep:02d}" + (f"-E{p.episode_end:02d}" if p.episode_end else "")
                title = ep_titles.get((season, ep), "")
                quality = " ".join(x for x in [p.source.upper() if p.source else "",
                                               f"{p.resolution}p" if p.resolution else ""] if x)
                stem = safe_name(f"{Path(show_dir).name}{epname}"
                                 + (f" - {title}" if title else "") + (f" {quality}" if quality else ""))
                dest_dir = os.path.join(show_dir, f"Season {season}")
                plan.append((os.path.join(base, f["name"]),
                             os.path.join(dest_dir, stem + Path(f["name"]).suffix.lower())))
                src_stem = Path(f["name"]).stem
                for s in subs:  # subtitles named after this episode
                    if Path(s["name"]).name.startswith(Path(src_stem).name):
                        plan.append((os.path.join(base, s["name"]),
                                     os.path.join(dest_dir, stem + _sub_suffix(s["name"])
                                                  + Path(s["name"]).suffix.lower())))
        if not plan:
            raise RuntimeError("nothing to import")
        plan = _unique_dests(plan)

        if self.post_process == "move":
            # keep what we need for "retry import": the torrent is gone after this
            self.store.set(f"media.import.{row['hash']}",
                           {"downloadDir": t["downloadDir"], "files": t.get("files") or []})
            # Transmission must let go of the files before we move them
            await self._remove_torrent(row["hash"], delete_data=False)
        await asyncio.to_thread(self._transfer, plan, self.post_process == "move")
        if self.post_process == "move":
            top = os.path.join(base, (t.get("files") or [{}])[0].get("name", "").split("/")[0])
            if "/" in (t["files"][0]["name"]) and _is_within(top, self.download_dir) \
                    and os.path.realpath(top) != os.path.realpath(self.download_dir):
                await asyncio.to_thread(shutil.rmtree, top, True)
        return [d for _, d in plan if Path(d).suffix.lower() in VIDEO_EXT]

    @staticmethod
    def _transfer(plan, move: bool):
        for src, dst in plan:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(src) and os.path.exists(dst):
                log.info("already in place: %s", dst)  # a retried import
                continue
            if os.path.exists(dst):
                log.info("replacing %s", dst)
            if move:
                try:
                    os.replace(src, dst)  # same filesystem: instant
                    log.info("moved %s -> %s", src, dst)
                    continue
                except OSError as err:
                    if err.errno != errno.EXDEV:
                        raise
            copy_file(src, dst)
            if move:
                os.remove(src)
            log.info("%s %s -> %s", "moved" if move else "copied", src, dst)

    async def _episode_titles(self, row) -> dict:
        out = {}
        try:
            seasons = {row["season"]} if row["season"] is not None else set()
            for s in seasons:
                for e in await self.plugin.metadata.episodes("series", row["data"]["id"], s):
                    out[(s, e["episode"])] = e["title"]
        except Exception as err:
            log.debug("episode titles: %s", err)
        return out

    # -- actions --------------------------------------------------------------------------

    async def _remove_torrent(self, h: str, delete_data: bool):
        try:
            await self.tr.remove(h, delete_data=delete_data)
        except TransmissionError as err:
            log.warning("remove %s: %s", h, err)

    async def action(self, h: str, action: str) -> None:
        if action == "pause":
            await self.tr.stop(h)
            self._update(h, state="paused")
        elif action == "resume":
            await self.tr.start(h)
            self._update(h, state="downloading")
        elif action == "remove":
            row = next((r for r in self.rows() if r["hash"] == h), None)
            # unfinished downloads: Transmission deletes only its own partial data
            unfinished = row and row["state"] in ("queued", "downloading", "paused")
            await self._remove_torrent(h, delete_data=bool(unfinished))
            self.store.execute("DELETE FROM media_downloads WHERE hash=?", (h,))
        elif action == "retry_import":
            ts = await self.tr.torrents([h], files=True)
            t = ts[0] if ts else self.store.get(f"media.import.{h}")
            if not t:
                raise ValueError("torrent no longer in Transmission")
            self._update(h, state="downloading")
            await self._import(h, t)
        else:
            raise ValueError(f"unknown action {action}")
        self._publish()
