"""Listen history: one row per track that was really listened to (Last.fm rule: half of it or 4 minutes)."""

from __future__ import annotations

import time

MIN_TRACK = 30          # shorter tracks never count
HALF = 0.5
FOUR_MIN = 240
DEDUP = 30              # the same song on the same output twice within this many seconds is one listen

SCHEMA = """
CREATE TABLE IF NOT EXISTS music_listens (
    id INTEGER PRIMARY KEY AUTOINCREMENT, song_id TEXT NOT NULL, title TEXT, artist TEXT, album TEXT,
    album_id TEXT, artist_id TEXT, cover TEXT, duration REAL, played_at REAL NOT NULL, seconds REAL NOT NULL,
    output TEXT, person TEXT);
CREATE INDEX IF NOT EXISTS music_listens_at ON music_listens(played_at);
CREATE INDEX IF NOT EXISTS music_listens_song ON music_listens(song_id);
"""
PERIODS = (7, 30, 365, 0)
KINDS = {"track": ("song_id", "title", "artist, album, album_id, cover"),
         "album": ("album_id", "album", "artist, album_id AS aid, cover"),
         "artist": ("artist", "artist", "'' AS x")}


def counts(seconds: float, duration: float) -> bool:
    """Is this a listen? ≥ 50% of the track or ≥ 4 minutes; tracks under 30 s never count."""
    seconds, duration = float(seconds or 0), float(duration or 0)
    if duration and duration < MIN_TRACK:
        return False
    return seconds >= FOUR_MIN or (duration > 0 and seconds >= duration * HALF)


class Listens:
    def __init__(self, store):
        self.store = store
        store.db.executescript(SCHEMA)

    def add(self, song: dict, seconds: float, output: str = "", person: str = "", at: float | None = None) -> bool:
        """Record one listen if it counts and is not a duplicate. → True when stored."""
        if not counts(seconds, song.get("duration") or 0) or not song.get("id"):
            return False
        at = at or time.time()
        dup = self.store.query("SELECT 1 FROM music_listens WHERE song_id=? AND output=? AND played_at>? LIMIT 1",
                               (song["id"], output, at - DEDUP))
        if dup:
            return False
        self.store.execute(
            "INSERT INTO music_listens(song_id, title, artist, album, album_id, artist_id, cover, duration, played_at,"
            " seconds, output, person) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (song["id"], song.get("title", ""), song.get("artist", ""), song.get("album", ""),
             song.get("album_id", ""), song.get("artist_id", ""), song.get("cover", ""), song.get("duration") or 0,
             at, round(float(seconds), 1), output, person))
        return True

    def recent(self, limit: int = 30) -> list[dict]:
        return self.store.query("SELECT * FROM music_listens ORDER BY played_at DESC LIMIT ?", (min(max(limit, 1), 200),))

    def top(self, kind: str = "track", days: int = 30, limit: int = 20) -> list[dict]:
        if kind not in KINDS:
            raise ValueError("kind")
        key, name, extra = KINDS[kind]
        where, params = "", []
        if days and days > 0:
            where, params = "WHERE played_at >= ?", [time.time() - days * 86400]
        sql = (f"SELECT {key} AS id, {name} AS name, {extra}, COUNT(*) AS plays, ROUND(SUM(seconds)) AS seconds, "
               f"MAX(played_at) AS last FROM music_listens {where} GROUP BY {key} "
               "ORDER BY plays DESC, last DESC LIMIT ?")
        rows = self.store.query(sql, tuple(params) + (min(max(limit, 1), 100),))
        for r in rows:
            if kind == "album":
                r["id"] = r.pop("aid", r["id"])
            r.pop("x", None)
        return rows

    def stats(self, days: int = 30) -> dict:
        row = self.store.query("SELECT COUNT(*) AS n, COALESCE(SUM(seconds),0) AS s FROM music_listens")[0]
        since = time.time() - days * 86400
        per = {r["d"]: r["n"] for r in self.store.query(
            "SELECT date(played_at,'unixepoch','localtime') AS d, COUNT(*) AS n FROM music_listens "
            "WHERE played_at >= ? GROUP BY d", (since,))}
        out = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            out.append({"day": d, "listens": per.get(d, 0)})
        return {"total": row["n"], "hours": round(row["s"] / 3600, 1), "days": out}

    def plays_of(self, song_ids: list[str]) -> dict[str, int]:
        ids = [i for i in song_ids if i][:500]
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["song_id"]: r["n"] for r in self.store.query(
            f"SELECT song_id, COUNT(*) AS n FROM music_listens WHERE song_id IN ({q}) GROUP BY song_id", tuple(ids))}
