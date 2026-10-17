"""Watchlist (favourites) + monitoring.

Every ``check_minutes`` a few due items are checked (staggered so the
indexers see a trickle, not a burst):

  movie   upcoming -> notify when released; released -> search trackers; a
          release with Italian audio -> notify (+ download if auto); English
          only -> wait ``italian_grace_days`` first.
  series  aired episodes of monitored seasons missing from the library and not
          downloading -> search (whole missing season = season pack first).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from ...i18n import _

log = logging.getLogger("hub.media.watcher")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_watchlist (
    key TEXT PRIMARY KEY, id TEXT, type TEXT, title TEXT, year INTEGER, poster TEXT,
    auto_download INTEGER, added REAL, last_check REAL DEFAULT 0, status TEXT, note TEXT,
    data TEXT);
"""
CHECK_EVERY = 3 * 3600      # per item
PER_TICK = 3                # items per tick
MAX_EP_SEARCHES = 4         # per series per check


class Watcher:
    def __init__(self, plugin, cfg: dict):
        self.plugin = plugin
        self.hub = plugin.hub
        self.store = plugin.hub.store
        self.auto_default = bool(cfg.get("auto_download", False))
        self.grace_days = float(cfg.get("italian_grace_days", 3))
        self.store.db.executescript(SCHEMA)

    # -- CRUD ---------------------------------------------------------------------------

    def items(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM media_watchlist ORDER BY added DESC")
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
            r["auto_download"] = bool(r["auto_download"])
        return rows

    def get(self, key: str) -> dict | None:
        return next((r for r in self.items() if r["key"] == key), None)

    async def add(self, type_: str, id_: str, auto_download: bool | None = None) -> dict:
        item = await self.plugin.metadata.details(type_, id_)
        key = f"{type_}:{item['id']}"
        data = {"titles": item.get("titles"), "imdb_id": item.get("imdb_id"),
                "original_language": item.get("original_language"), "notified": []}
        if type_ == "series":
            today = date.today().isoformat()
            aired = [s["season"] for s in item.get("seasons", []) if (s.get("air_date") or "9") <= today]
            # monitor from the latest aired season on (not the whole back catalogue)
            data["monitor_from"] = max(aired) if aired else 1
        self.store.execute(
            "INSERT OR REPLACE INTO media_watchlist (key, id, type, title, year, poster, "
            "auto_download, added, last_check, status, note, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, item["id"], type_, item["title"], item.get("year"), item.get("poster"),
             int(self.auto_default if auto_download is None else auto_download), time.time(), 0,
             "new", "", json.dumps(data)))
        self._publish()
        self.hub.spawn(self.check(key), f"check {item['title']}")
        return self.get(key)

    def update(self, key: str, **fields) -> dict:
        row = self.get(key)
        if row is None:
            raise KeyError(_("{key} is not in the watchlist", key=key))
        if "auto_download" in fields:
            self.store.execute("UPDATE media_watchlist SET auto_download=? WHERE key=?",
                               (int(bool(fields["auto_download"])), key))
        if "monitor_from" in fields:
            row["data"]["monitor_from"] = int(fields["monitor_from"])
            self._save_data(key, row["data"])
        self._publish()
        return self.get(key)

    def remove(self, key: str) -> None:
        self.store.execute("DELETE FROM media_watchlist WHERE key=?", (key,))
        self._publish()

    def keys(self) -> set[str]:
        return {r["key"] for r in self.store.query("SELECT key FROM media_watchlist")}

    def _save_data(self, key, data):
        self.store.execute("UPDATE media_watchlist SET data=? WHERE key=?", (json.dumps(data), key))

    def _status(self, key, status, note=""):
        self.store.execute("UPDATE media_watchlist SET status=?, note=?, last_check=? WHERE key=?",
                           (status, note, time.time(), key))

    def _publish(self):
        self.hub.publish("media", {"kind": "watchlist"})

    # -- monitoring ---------------------------------------------------------------------

    async def tick(self) -> None:
        due = [r for r in self.items() if time.time() - (r["last_check"] or 0) > CHECK_EVERY]
        due.sort(key=lambda r: r["last_check"] or 0)
        for r in due[:PER_TICK]:
            await self.check(r["key"])

    async def check(self, key: str) -> None:
        row = self.get(key)
        if not row:
            return
        try:
            if row["type"] == "movie":
                await self._check_movie(row)
            else:
                await self._check_series(row)
        except Exception as err:
            log.warning("watchlist check %s failed: %s", row["title"], err)
            self._status(key, row["status"] or "error", _("check failed: {err}", err=err))
        self._publish()

    async def _notify_once(self, row, tag: str, title: str, body: str):
        data = row["data"]
        if tag in data.setdefault("notified", []):
            return
        data["notified"] = (data["notified"] + [tag])[-200:]
        self._save_data(row["key"], data)
        await self.hub.notify.send(title, body, url=f"#/media/{row['type']}/{row['id']}",
                                   image=row.get("poster") or "", tag="movie_camera")

    async def _check_movie(self, row):
        p = self.plugin
        item = await p.metadata.details("movie", row["id"])
        p.library.flag(item)
        if item["in_library"]:
            self._status(row["key"], "in_library", _("In your library"))
            return
        if p.downloads.active_for(row["key"]):
            self._status(row["key"], "downloading", _("Downloading"))
            return
        today = date.today().isoformat()
        rel = item.get("release_date")
        if rel and rel > today:
            self._status(row["key"], "upcoming", _("In cinemas {date}", date=rel))
            return
        if rel:
            await self._notify_once(row, "released", _("Out now: {title}", title=item["title"]),
                                    _("Released {date}. I'll keep looking for a good copy.", date=rel))
        best, english = await self._best(item)
        if best is None:
            dr = item.get("digital_release")
            note = _("Digital release {date}", date=dr) if dr and dr > today else _("Waiting for a release")
            self._status(row["key"], "waiting", note)
            return
        await self._found(row, item, best, english)

    async def _best(self, item, season=None, episode=None):
        """Best acceptable release; (None, _) if none or still in the Italian grace period."""
        res = await self.plugin.find_releases(item, season, episode)
        ok = [r for r in res["releases"] if not r["rejected"]]
        if not ok:
            return None, False
        best = ok[0]
        if best["language"] == "it" or len(self.plugin.profile.languages) == 1:
            return best, False
        # only English so far: give Italian releases a few days
        data = self.get(f"{item['type']}:{item['id']}") or {"data": {}}
        tag = f"en_seen:{season}:{episode}"
        first = data["data"].get(tag)
        if not first:
            data["data"][tag] = time.time()
            if data.get("key"):
                self._save_data(data["key"], data["data"])
            first = time.time()
        if time.time() - first >= self.grace_days * 86400:
            return best, True
        return None, True

    async def _found(self, row, item, best, english: bool, season=None, episode=None):
        what = item["title"] + (f" S{season:02d}" + (f"E{episode:02d}" if episode else "")
                                if season is not None else "")
        lang = _("🇬🇧 English") if english or best["language"] != "it" else _("🇮🇹 Italian")
        body = _("{lang} · {quality} · {gb} GB · {n} seeders", lang=lang, quality=best["quality"],
                 gb=f"{best['size'] / 1e9:.1f}", n=best["seeders"])
        if row["auto_download"]:
            await self.plugin.downloads.add(item, best, season, episode)
            await self._notify_once(row, f"dl:{best['hash']}", _("Downloading {what}", what=what), body)
            self._status(row["key"], "downloading", _("Downloading {what}", what=what))
        else:
            await self._notify_once(row, f"found:{season}:{episode}", _("Available: {what}", what=what),
                                    body + _(" - tap to download"))
            self._status(row["key"], "found", _("{what}: {quality} ({lang})", what=what, quality=best["quality"], lang=lang))

    async def _check_series(self, row):
        p = self.plugin
        item = await p.metadata.details("series", row["id"])
        show = p.library.find_show(item.get("titles") or [item["title"]])
        have = p.library.episodes(show)
        busy = {(d["season"], d["episode"]) for d in p.downloads.active_for(row["key"])}
        busy_seasons = {s for s, e in busy if e is None}
        today = date.today().isoformat()
        start = int(row["data"].get("monitor_from", 1))
        missing: dict[int, list] = {}
        complete_seasons = set()
        for s in item.get("seasons", []):
            n = s["season"]
            if n < start or n in busy_seasons or (s.get("air_date") or "9") > today:
                continue
            eps = await p.metadata.episodes("series", item["id"], n)
            aired = [e for e in eps if e.get("air_date") and e["air_date"] <= today]
            miss = [e for e in aired if (n, e["episode"]) not in have and (n, e["episode"]) not in busy]
            if miss:
                missing[n] = miss
                if len(miss) == len(eps) and len(aired) == len(eps):
                    complete_seasons.add(n)
        nxt = item.get("next_episode")
        if not missing:
            note = (_("Next: S{season:02d}E{episode:02d} on {date}", season=nxt["season"],
                       episode=nxt["episode"], date=nxt["air_date"])
                    if nxt else _("Up to date"))
            self._status(row["key"], "downloading" if busy else "up_to_date", note)
            return
        searches = 0
        found_any = False
        for n, eps in sorted(missing.items()):
            if n in complete_seasons:
                best, english = await self._best(item, n, None)
                searches += 1
                if best:
                    await self._found(row, item, best, english, n, None)
                    found_any = True
                    continue
            for e in eps:
                if searches >= MAX_EP_SEARCHES:
                    break
                best, english = await self._best(item, n, e["episode"])
                searches += 1
                if best:
                    await self._found(row, item, best, english, n, e["episode"])
                    found_any = True
        if not found_any:
            total = sum(len(v) for v in missing.values())
            self._status(row["key"], "waiting", _("{n} aired episode(s) missing, none on trackers yet", n=total))

    def on_imported(self, dl: dict) -> None:
        if dl["key"] in self.keys():
            self.store.execute("UPDATE media_watchlist SET last_check=0 WHERE key=?", (dl["key"],))
            self._publish()
