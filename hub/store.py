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
CREATE TABLE IF NOT EXISTS history (
    entity TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS history_idx ON history (entity, key, ts);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, title TEXT NOT NULL,
    body TEXT, url TEXT, image TEXT, tag TEXT, read INTEGER NOT NULL DEFAULT 0);
"""

HISTORY_DAYS = 30


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # gentle on the Pi's SD card
        self.db.executescript(SCHEMA)
        self._last_prune = 0.0

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

    # -- sensor history -----------------------------------------------------------

    def add_history(self, entity: str, key: str, value: float, ts: float) -> None:
        self.db.execute(
            "INSERT INTO history(entity, key, ts, value) VALUES(?,?,?,?)",
            (entity, key, ts, value),
        )
        if ts - self._last_prune > 3600:
            self._last_prune = ts
            self.db.execute("DELETE FROM history WHERE ts < ?", (ts - HISTORY_DAYS * 86400,))

    def history(self, entity: str, key: str, since: float) -> list[tuple[float, float]]:
        rows = self.db.execute(
            "SELECT ts, value FROM history WHERE entity=? AND key=? AND ts>=? ORDER BY ts",
            (entity, key, since),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
