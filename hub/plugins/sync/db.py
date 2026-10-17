"""Local tables of the sync plugin (prefix ``sync_``): a read-only mirror of the repo files plus the outbox."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import recipes_md
from .feed import read_feed
from .merge import is_active, merge, newest_first

log = logging.getLogger("hub.sync.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_events (id TEXT PRIMARY KEY, user TEXT, ts TEXT, kind TEXT, media TEXT,
    imdb_id TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS sync_events_imdb ON sync_events(imdb_id);
CREATE TABLE IF NOT EXISTS sync_media (imdb_id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS sync_recipes (slug TEXT PRIMARY KEY, title TEXT, author TEXT, updated REAL, data TEXT);
CREATE TABLE IF NOT EXISTS sync_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT, data TEXT, created REAL);
"""


class SyncDB:
    def __init__(self, store):
        self.store = store
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                store.execute(stmt)

    # -- outbox ---------------------------------------------------------------------------------

    def queue(self, op: str, data: dict) -> int:
        cur = self.store.execute("INSERT INTO sync_outbox(op, data, created) VALUES(?,?,?)",
                                 (op, json.dumps(data), time.time()))
        self.apply_local(op, data)
        return cur.lastrowid

    def outbox(self) -> list[dict]:
        return [{"seq": r["seq"], "op": r["op"], "data": json.loads(r["data"])}
                for r in self.store.query("SELECT * FROM sync_outbox ORDER BY seq")]

    def drop(self, seqs: list[int]) -> None:
        for s in seqs:
            self.store.execute("DELETE FROM sync_outbox WHERE seq=?", (s,))

    def pending(self) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM sync_outbox")[0]["n"]

    # -- mirror ---------------------------------------------------------------------------------

    def _put_event(self, ev: dict) -> None:
        self.store.execute("INSERT OR REPLACE INTO sync_events VALUES(?,?,?,?,?,?,?)",
                           (ev["id"], ev["user"], ev["ts"], ev["kind"], ev["media"], ev["imdb_id"], json.dumps(ev)))

    def _put_recipe(self, slug: str, rec: dict, updated: float) -> None:
        self.store.execute("INSERT OR REPLACE INTO sync_recipes VALUES(?,?,?,?,?)",
                           (slug, rec["title"], rec["author"], updated, json.dumps(rec)))

    def apply_local(self, op: str, d: dict) -> None:
        """Show a queued write in the mirror right away (a rebuild replays what is still queued)."""
        if op == "event":
            self._put_event(d["event"])
        elif op == "media":
            self.store.execute("INSERT OR IGNORE INTO sync_media VALUES(?,?)", (d["imdb_id"], json.dumps(d["card"])))
        elif op == "recipe":
            old = self.recipe(d["slug"])
            rec = dict(d["rec"])
            rec["tips"] = recipes_md.merge_tips(old["tips"] if old else [], rec.get("tips") or [])
            self._put_recipe(d["slug"], rec, time.time())
        elif op == "tip":
            old = self.recipe(d["slug"])
            if old:
                old["tips"] = recipes_md.merge_tips(old["tips"], [{"user": d["user"], "text": d["text"]}])
                self._put_recipe(d["slug"], old, time.time())

    def rebuild(self, repo: Path) -> None:
        """Replace the mirror with the contents of the working copy, then replay the outbox."""
        self.store.execute("BEGIN")
        try:
            for t in ("sync_events", "sync_media", "sync_recipes"):
                self.store.execute(f"DELETE FROM {t}")
            for f in sorted((repo / "feeds").glob("*.jsonl")):
                for ev in read_feed(f):
                    self._put_event(ev)
            for f in sorted((repo / "media").glob("tt*.json")):
                try:
                    card = json.loads(f.read_text(encoding="utf-8"))
                    self.store.execute("INSERT OR REPLACE INTO sync_media VALUES(?,?)", (f.stem, json.dumps(card)))
                except (ValueError, OSError) as err:
                    log.warning("ignoring %s: %s", f.name, err)
            for f in sorted((repo / "recipes").glob("*.md")):
                try:
                    self._put_recipe(f.stem, recipes_md.parse(f.read_text(encoding="utf-8")), f.stat().st_mtime)
                except (ValueError, OSError) as err:
                    log.warning("ignoring %s: %s", f.name, err)
            for o in self.outbox():
                self.apply_local(o["op"], o["data"])
            self.store.execute("COMMIT")
        except Exception:
            self.store.execute("ROLLBACK")
            raise

    # -- queries --------------------------------------------------------------------------------

    def events(self, imdb_id: str | None = None) -> list[dict]:
        if imdb_id:
            rows = self.store.query("SELECT data FROM sync_events WHERE imdb_id=?", (imdb_id,))
        else:
            rows = self.store.query("SELECT data FROM sync_events")
        return [json.loads(r["data"]) for r in rows]

    def card(self, imdb_id: str) -> dict | None:
        rows = self.store.query("SELECT data FROM sync_media WHERE imdb_id=?", (imdb_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def users(self) -> list[str]:
        return [r["user"] for r in self.store.query("SELECT DISTINCT user FROM sync_events ORDER BY user")]

    def feed(self, user: str = "", kind: str = "", media: str = "", limit: int = 100, before: str = "") -> list[dict]:
        out = []
        for ev in newest_first(merge(self.events()).values()):
            if not is_active(ev) or (user and ev["user"] != user) or (kind and ev["kind"] != kind) \
                    or (media and ev["media"] != media) or (before and ev["ts"] >= before):
                continue
            c = self.card(ev["imdb_id"]) or {}
            out.append({"id": ev["id"], "ts": ev["ts"], "user": ev["user"], "kind": ev["kind"], "media": ev["media"],
                        "imdb_id": ev["imdb_id"], "rating": ev["rating"], "on": ev["on"],
                        "watched_at": ev["watched_at"], "note": ev["note"],
                        "title": c.get("title"), "year": c.get("year"),
                        "poster": f"/api/p/sync/poster/{ev['imdb_id']}" if c.get("poster") else None})
            if len(out) >= limit:
                break
        return out

    def title(self, imdb_id: str, me: str) -> dict:
        state = merge(self.events(imdb_id))
        notes: dict[str, str] = {}
        for ev in newest_first(self.events(imdb_id)):
            if ev["note"]:
                notes.setdefault(ev["user"], ev["note"])

        def vals(u: str) -> dict:
            g = lambda k: state.get((u, imdb_id, k))  # noqa: E731
            return {"watch": g("watch") and g("watch")["watched_at"], "rating": g("rating") and g("rating")["rating"],
                    "watchlist": g("watchlist") and g("watchlist")["on"]}

        users = sorted({u for (u, _, _) in state} - {me})
        return {"card": self.card(imdb_id), "mine": vals(me),
                "others": [{"user": u, **vals(u), "note": notes.get(u, "")} for u in users]}

    def recipe(self, slug: str) -> dict | None:
        rows = self.store.query("SELECT data FROM sync_recipes WHERE slug=?", (slug,))
        return json.loads(rows[0]["data"]) if rows else None

    def recipes(self) -> list[tuple[str, dict, float]]:
        return [(r["slug"], json.loads(r["data"]), r["updated"])
                for r in self.store.query("SELECT * FROM sync_recipes ORDER BY updated DESC")]

    def recipe_index(self) -> dict[str, tuple[str, str]]:
        return {r["slug"]: (r["title"], r["author"]) for r in self.store.query("SELECT * FROM sync_recipes")}
