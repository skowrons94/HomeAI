"""Household notes: short notes and small checklists the people in the house leave each other.

No configuration. Stored in the hub's SQLite (table ``notes``); every change is published on the
event bus as ``notes`` (full list) and a new note is pushed to the phones.
"""

from __future__ import annotations

import json
import time
from urllib.parse import unquote

from aiohttp import web

from ..core import Plugin
from ..i18n import _

COLORS = ("yellow", "blue", "green", "pink", "gray")
MAX_TEXT, MAX_ITEMS, MAX_ITEM = 2000, 50, 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL DEFAULT '', items TEXT,
    color TEXT NOT NULL DEFAULT 'yellow', pinned INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT '', created REAL, updated REAL);
"""


def _json(data, status=200):
    return web.json_response(data, status=status)


def _row(r: dict) -> dict:
    items = json.loads(r["items"]) if r["items"] else None
    return {"id": r["id"], "text": r["text"], "items": items or None, "color": r["color"],
            "pinned": bool(r["pinned"]), "author": r["author"], "created": r["created"],
            "updated": r["updated"]}


def _clean_text(v) -> str:
    if not isinstance(v, str):
        raise ValueError(_("The note text is not valid"))
    v = v.strip()
    if len(v) > MAX_TEXT:
        raise ValueError(_("The note is too long (max {n} characters)", n=MAX_TEXT))
    return v


def _clean_items(v) -> list | None:
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError(_("The checklist is not valid"))
    out = []
    for it in v:
        if not isinstance(it, dict) or not isinstance(it.get("text", ""), str):
            raise ValueError(_("The checklist is not valid"))
        text = it.get("text", "").strip()
        if not text:
            continue
        if len(text) > MAX_ITEM:
            raise ValueError(_("A checklist item is too long (max {n} characters)", n=MAX_ITEM))
        out.append({"text": text, "done": bool(it.get("done"))})
    if len(out) > MAX_ITEMS:
        raise ValueError(_("Too many checklist items (max {n})", n=MAX_ITEMS))
    return out or None


def _color(v) -> str:
    if v not in COLORS:
        raise ValueError(_("Unknown colour"))
    return v


async def _body(request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise ValueError(_("Expected a JSON body"))
    if not isinstance(b, dict):
        raise ValueError(_("Expected a JSON body"))
    return b


class NotesPlugin(Plugin):
    name = "notes"
    title = "Notes"
    description = "Shared notes and small checklists for the household"
    icon = "edit"
    schema: list = []

    async def start(self):
        self.hub.store.db.executescript(SCHEMA)

    @property
    def store(self):
        return self.hub.store

    def _all(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM notes ORDER BY pinned DESC, updated DESC, id DESC")
        return [_row(r) for r in rows]

    def _get(self, nid) -> dict:
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            raise KeyError(_("Note not found"))
        rows = self.store.query("SELECT * FROM notes WHERE id=?", (nid,))
        if not rows:
            raise KeyError(_("Note not found"))
        return _row(rows[0])

    def _changed(self):
        self.hub.publish("notes", {"items": self._all()})

    def status(self):
        return {"notes": self.store.query("SELECT COUNT(*) AS n FROM notes")[0]["n"]}

    def routes(self):
        return [
            web.get("/notes", self.r_list),
            web.post("/notes", self.r_create),
            web.patch("/notes/{id}", self.r_patch),
            web.delete("/notes/{id}", self.r_delete),
        ]

    async def r_list(self, request):
        return _json(self._all())

    async def r_create(self, request):
        b = await _body(request)
        text = _clean_text(b.get("text", ""))
        items = _clean_items(b.get("items"))
        if not text and not items:
            raise ValueError(_("The note is empty"))
        color = _color(b["color"]) if "color" in b else "yellow"
        author = unquote(request.headers.get("X-Casa-Person", "")).strip()[:60]
        now = time.time()
        cur = self.store.execute(
            "INSERT INTO notes (text, items, color, pinned, author, created, updated) VALUES (?,?,?,?,?,?,?)",
            (text, json.dumps(items) if items else None, color, int(bool(b.get("pinned"))), author, now, now))
        note = self._get(cur.lastrowid)
        self._changed()
        first = text[:80] if text else (items[0]["text"][:80] if items else "")
        await self.hub.notify.send(
            _("Note from {who}", who=author) if author else _("New note"),
            body=first, url="#/notes", tag="note")
        return _json(note)

    async def r_patch(self, request):
        cur = self._get(request.match_info["id"])
        b = await _body(request)
        text = _clean_text(b["text"]) if "text" in b else cur["text"]
        items = _clean_items(b["items"]) if "items" in b else cur["items"]
        if not text and not items:
            raise ValueError(_("The note is empty"))
        color = _color(b["color"]) if "color" in b else cur["color"]
        pinned = bool(b["pinned"]) if "pinned" in b else cur["pinned"]
        self.store.execute(
            "UPDATE notes SET text=?, items=?, color=?, pinned=?, updated=? WHERE id=?",
            (text, json.dumps(items) if items else None, color, int(pinned), time.time(), cur["id"]))
        note = self._get(cur["id"])
        self._changed()
        return _json(note)

    async def r_delete(self, request):
        nid = self._get(request.match_info["id"])["id"]
        self.store.execute("DELETE FROM notes WHERE id=?", (nid,))
        self._changed()
        return _json({"ok": True})


PLUGIN = NotesPlugin
