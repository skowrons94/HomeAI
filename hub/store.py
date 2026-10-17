"""Tiny SQLite store: key/value settings, sensor history, notifications.

Calls are synchronous; every query is a few hundred microseconds on a Pi, so
they run inline on the event loop. Plugins may create their own tables with
``store.execute`` (prefix them with the plugin name).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY, entity TEXT NOT NULL, key TEXT NOT NULL, UNIQUE (entity, key));
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, title TEXT NOT NULL,
    body TEXT, url TEXT, image TEXT, tag TEXT, read INTEGER NOT NULL DEFAULT 0);
"""

# Graphite-style rollups: (table, bucket seconds, default retention days).
# Every sample updates one bucket per table with a running avg/min/max, so any
# range reads a few hundred to a few thousand rows (benchmarked vs Whisper:
# same speed class, smaller, no daemon, min/max included).
ROLLUPS = (("h1m", 60, 2), ("h5m", 300, 30), ("h1h", 3600, 730))
_UPSERT = (
    "INSERT INTO {t}(s, ts, avg, min, max, n) VALUES(?,?,?,?,?,1) "
    "ON CONFLICT(s, ts) DO UPDATE SET avg=(avg*n+excluded.avg)/(n+1), "
    "min=MIN(min, excluded.min), max=MAX(max, excluded.max), n=n+1"
)


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # gentle on the Pi's SD card
        self.db.executescript(SCHEMA)
        for table, _, _ in ROLLUPS:
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (s INTEGER NOT NULL, ts INTEGER NOT NULL, "
                "avg REAL, min REAL, max REAL, n INTEGER, PRIMARY KEY (s, ts)) WITHOUT ROWID")
        self.retention = {t: days for t, _, days in ROLLUPS}
        self._series: dict[tuple[str, str], int] = {
            (r[1], r[2]): r[0] for r in self.db.execute("SELECT id, entity, key FROM series")}
        self._last_prune = 0.0
        self._migrate_history()

    def close(self) -> None:
        self.db.close()

    # -- generic --------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    # -- key/value --------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    # -- history ------------------------------------------------------------------

    def _series_id(self, entity: str, key: str) -> int:
        sid = self._series.get((entity, key))
        if sid is None:
            sid = self.db.execute("INSERT INTO series(entity, key) VALUES(?, ?)",
                                  (entity, key)).lastrowid
            self._series[(entity, key)] = sid
        return sid

    def _migrate_history(self) -> None:
        """Fold the old raw ``history`` table into the rollups (once)."""
        if not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history'").fetchone():
            return
        rows = self.db.execute("SELECT entity, key, ts, value FROM history ORDER BY ts").fetchall()
        self.db.execute("BEGIN")
        for r in rows:
            self._upsert(self._series_id(r[0], r[1]), r[2], r[3])
        self.db.execute("DROP TABLE history")
        self.db.execute("COMMIT")

    def _upsert(self, sid: int, ts: float, value: float) -> None:
        for table, bucket, _ in ROLLUPS:
            b = int(ts) // bucket * bucket
            self.db.execute(_UPSERT.format(t=table), (sid, b, value, value, value))

    def add_history(self, entity: str, key: str, value: float, ts: float) -> None:
        sid = self._series_id(entity, key)
        self.db.execute("BEGIN")
        try:
            self._upsert(sid, ts, float(value))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        if ts - self._last_prune > 3600:
            self._last_prune = ts
            for table, _, _ in ROLLUPS:
                self.db.execute(f"DELETE FROM {table} WHERE ts < ?",
                                (ts - self.retention[table] * 86400,))

    def history(self, entity: str, key: str, since: float,
                until: float | None = None) -> list[list[float]]:
        """``[[ts, avg, min, max], ...]`` from the finest rollup that covers ``since``."""
        sid = self._series.get((entity, key))
        if sid is None:
            return []
        now = time.time()
        until = until or now
        span = until - since
        table = ROLLUPS[-1][0]
        for t, bucket, _ in ROLLUPS:
            # finest table that still holds data back to `since` and isn't too dense
            if now - since <= self.retention[t] * 86400 and span / bucket <= 3000:
                table = t
                break
        rows = self.db.execute(
            f"SELECT ts, avg, min, max FROM {table} WHERE s=? AND ts>=? AND ts<=? ORDER BY ts",
            (sid, int(since), int(until)),
        ).fetchall()
        return [[r[0], round(r[1], 3), round(r[2], 3), round(r[3], 3)] for r in rows]

    def delete_series(self, entity: str, key: str | None = None) -> None:
        """Forget recorded history of an entity (or one of its keys)."""
        for (ent, k), sid in list(self._series.items()):
            if ent == entity and key in (None, k):
                for table, _, _ in ROLLUPS:
                    self.db.execute(f"DELETE FROM {table} WHERE s=?", (sid,))
                self.db.execute("DELETE FROM series WHERE id=?", (sid,))
                del self._series[(ent, k)]

    def series(self) -> list[dict]:
        """All recorded series with their latest hourly bucket."""
        return self.query(
            "SELECT s.entity, s.key, MAX(h.ts) AS last FROM series s "
            "LEFT JOIN h1h h ON h.s = s.id GROUP BY s.id ORDER BY s.entity, s.key")

    # -- notifications ------------------------------------------------------------

    def add_notification(self, title: str, body: str = "", url: str = "",
                         image: str = "", tag: str = "") -> dict:
        ts = time.time()
        cur = self.db.execute(
            "INSERT INTO notifications(ts, title, body, url, image, tag) VALUES(?,?,?,?,?,?)",
            (ts, title, body, url, image, tag),
        )
        self.db.execute(
            "DELETE FROM notifications WHERE id <= ?", (cur.lastrowid - 300,)
        )
        return {"id": cur.lastrowid, "ts": ts, "title": title, "body": body,
                "url": url, "image": image, "tag": tag, "read": 0}

    def notifications(self, limit: int = 50) -> list[dict]:
        return self.query(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        )

    def mark_notifications_read(self) -> None:
        self.db.execute("UPDATE notifications SET read=1 WHERE read=0")
