"""Background subtitle scan of the NAS library.

Walks the indexed movies (and episodes) for videos without a subtitle in the wanted language
(sidecar or embedded) and without audio in it, and runs the subtitle pipeline on them one at a time:
a real subtitle from the providers first, else (when a DeepL / Claude key is set) a translation of an
English one. Gentle by design: one video per step, a daily cap, no step while the OpenSubtitles quota
is used up, while the CPU is busy or while another subtitle job runs. A machine-translated file is
looked at again every two weeks, and replaced as soon as a real subtitle exists.

State: kv ``media.subs.scan`` = {paused, day, done_today}; the per-video results are the usual
``media.subs.<sha1(path)>`` records. Everything filesystem-related runs in a thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from pathlib import Path

from .subtitles import SUB_EXTS, Video, sha

log = logging.getLogger("hub.media.scan")

SURVEY_EVERY = 30 * 60
REVISIT = {"not_found": 30 * 86400, "translated": 14 * 86400, "error": 86400}
BUSY_LOAD = 2.5               # 1-minute load average above which we wait (Pi 3 B+ has 4 cores)
CHEAP = ("skipped", "found", "busy")   # outcomes that cost no download: carry on with the next video
STEP_LIMIT = 25


class Scanner:
    def __init__(self, subs, plugin, cfg: dict):
        self.subs = subs
        self.plugin = plugin
        self.store = subs.store
        c = cfg.get("subtitles") or {}
        self.enabled = bool(c.get("scan_enabled", True)) and subs.enabled
        self.series = bool(c.get("scan_series", True))
        self.per_day = int(c.get("scan_per_day", 12) or 0)
        self.translate = bool(c.get("scan_translate", True))
        self.items: list[dict] = []
        self.surveyed_at = 0.0
        self.current = ""
        self.reason = ""
        self.counts: dict = {}
        self._lock = asyncio.Lock()

    # -- state ----------------------------------------------------------------------------

    def _st(self) -> dict:
        st = self.store.get("media.subs.scan") or {}
        if st.get("day") != date.today().isoformat():
            st = {**st, "day": date.today().isoformat(), "done_today": 0}
        return st

    def _save(self, **kw) -> dict:
        st = {**self._st(), **kw}
        self.store.set("media.subs.scan", st)
        return st

    def set_paused(self, paused: bool) -> dict:
        self._save(paused=bool(paused))
        return self.status()

    # -- survey (thread) -------------------------------------------------------------------

    def _survey(self) -> list[dict]:
        lib = self.plugin.library
        q = self.store.query
        items: list[dict] = []
        for m in q("SELECT path, title, year, meta_id FROM media_lib_movies ORDER BY title"):
            try:
                video = lib._video("m", m["path"])
            except (OSError, PermissionError):
                continue
            imdb = m["meta_id"] if str(m.get("meta_id") or "").startswith("tt") else ""
            items.append({"path": video, "kind": "movie", "title": m["title"], "year": m["year"], "imdb": imdb})
        if self.series:
            shows = {r["path"]: r for r in q("SELECT path, title, year, meta_id FROM media_lib_shows")}
            for e in q("SELECT show_path, season, episode, path FROM media_lib_episodes "
                       "ORDER BY show_path, season, episode"):
                sh = shows.get(e["show_path"])
                if not sh:
                    continue
                imdb = sh["meta_id"] if str(sh.get("meta_id") or "").startswith("tt") else ""
                items.append({"path": e["path"], "kind": "episode", "title": sh["title"], "year": sh["year"],
                              "imdb": imdb, "season": e["season"], "episode": e["episode"]})
        by_dir: dict[str, list[str]] = {}
        for it in items:
            d = os.path.dirname(it["path"])
            if d not in by_dir:
                try:
                    by_dir[d] = os.listdir(d)
                except OSError:
                    by_dir[d] = []
            stem = Path(it["path"]).stem + "."
            it["has_sub"] = False
            for n in by_dir[d]:
                if n.startswith(stem) and Path(n).suffix.lower() in SUB_EXTS:
                    tags = n[len(stem):].lower().rsplit(".", 1)[0].split(".")
                    if self.subs.lang in tags and "forced" not in tags:
                        it["has_sub"] = True
                        break
        return items

    async def refresh(self, force=False):
        if force or not self.items or time.time() - self.surveyed_at > SURVEY_EVERY:
            self.items = await asyncio.to_thread(self._survey)
            self.surveyed_at = time.time()
        self.counts = self._count()

    # -- counting ---------------------------------------------------------------------------

    def _due(self, it: dict, now: float) -> bool:
        """Still to look at: never processed, or its last result is old enough to try again."""
        st = self.subs.state(it["path"])
        if it.get("has_sub") and not (st and st.get("translated")):
            return False
        if not st:
            return True
        if st.get("status") in ("searching",) or it["path"] in self.subs._running:
            return False
        wait = REVISIT.get(st.get("status"))
        return bool(wait and now - (st.get("checked_at") or 0) > wait)

    def _count(self) -> dict:
        now = time.time()
        c = {"total": len(self.items), "todo": 0, "found": 0, "translated": 0, "not_found": 0,
             "present": 0, "skipped": 0, "error": 0}
        for it in self.items:
            st = self.subs.state(it["path"]) or {}
            s = st.get("status")
            if st.get("translated") or s == "translated":
                c["translated"] += 1
            elif it.get("has_sub") and s not in ("in_sync", "adapted", "unverified"):
                c["present"] += 1
            elif s in ("in_sync", "adapted", "unverified"):
                c["found"] += 1
            elif s == "skipped":
                c["skipped"] += 1
            elif s == "error":
                c["error"] += 1
            elif s == "not_found":
                c["not_found"] += 1
            if self._due(it, now):
                c["todo"] += 1
        return c

    def status(self) -> dict:
        st = self._st()
        return {"enabled": self.enabled, "paused": bool(st.get("paused")), "series": self.series,
                "per_day": self.per_day, "done_today": st.get("done_today", 0), "current": self.current,
                "reason": self.reason, "translate": self.translate and bool(self.subs.translator_names()),
                "counts": self.counts, "surveyed_at": self.surveyed_at}

    # -- the loop ---------------------------------------------------------------------------

    def _wait_reason(self) -> str:
        subs = self.subs
        if not subs.providers.configured() and not subs.translator_names():
            return "no provider"
        only_os = subs.providers.configured() == ["opensubtitles"]
        if only_os and not subs.providers.os_quota_left() and not subs.translator_names():
            return "OpenSubtitles quota used up"
        if subs._running or subs._job_lock.locked() or subs._ffmpeg_lock.locked():
            return "another subtitle job is running"
        try:
            if os.getloadavg()[0] > BUSY_LOAD:
                return "CPU busy"
        except (OSError, AttributeError):
            pass
        return ""

    def _next(self) -> dict | None:
        now = time.time()
        for it in self.items:
            if self._due(it, now):
                return it
        return None

    def _video(self, it: dict) -> Video:
        name = Path(it["path"]).stem
        return Video(it["path"], title=it["title"] or "", year=it.get("year"), imdb_id=it.get("imdb") or "",
                     season=it.get("season"), episode=it.get("episode"), release=name)

    async def tick(self):
        """One scan step (called every minute or two); returns quickly when there is nothing to do."""
        if not self.enabled or self._lock.locked():
            return
        async with self._lock:
            st = self._st()
            if st.get("paused"):
                self.reason = "paused"
                return
            await self.refresh()
            for _step in range(STEP_LIMIT):
                st = self._st()
                if self.per_day and st.get("done_today", 0) >= self.per_day:
                    self.reason = "daily limit reached"
                    return
                why = self._wait_reason()
                if why:
                    self.reason = why
                    return
                it = self._next()
                if it is None:
                    self.reason = "nothing left to do"
                    self.counts = self._count()
                    return
                self.reason = ""
                self.current = Path(it["path"]).name
                try:
                    outcome = await self.subs.scan_one(self._video(it), translate=self.translate)
                finally:
                    self.current = ""
                if outcome not in CHEAP:
                    self._save(done_today=st.get("done_today", 0) + 1)
                    self.counts = self._count()
                    return  # one real search per step; the next one comes on the next tick
                if self.store.get("media.subs.scan", {}).get("paused"):
                    return
            self.counts = self._count()
