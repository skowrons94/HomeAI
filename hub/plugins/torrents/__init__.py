"""Torrent search: one box, any number of pluggable providers, one normalised result list.

Providers live in ``sources/`` (built in) or in the folder set as ``extra_dir`` (yours, outside
the repo). Nothing is searched until a provider is enabled. See docs/torrents.md.
"""

from __future__ import annotations

import logging
import secrets
import time

import aiohttp
from aiohttp import web

from ...core import Plugin
from ...i18n import _
from .engine import SORTS, Engine, discover, refine
from .model import Provider, to_int
from .torznab import TorznabProvider, clean_url

log = logging.getLogger("hub.torrents")
MAX_QUERY = 100
MAX_CUSTOM = 30
STORE_TRACKERS = "torrents.trackers"   # custom Torznab trackers [{id, name, url, key}]
STORE_ENABLED = "torrents.enabled"     # ids to search; absent = the `providers` config list


def _json(data, status=200):
    return web.json_response(data, status=status)


class TorrentsPlugin(Plugin):
    name = "torrents"
    title = "Torrent search"
    description = "Search the providers you enable and open the result in Transmission or a client"
    icon = "search"
    schema = [
        {"key": "providers", "label": "Providers", "type": "list", "default": ["internetarchive"],
         "help": "Provider ids to search (empty = all found)."},
        {"key": "extra_dir", "label": "Your providers folder", "type": "path", "default": "",
         "advanced": True, "help": "Folder with your own provider .py files (see docs/torrents.md)."},
        {"key": "timeout", "label": "Timeout", "type": "number", "default": 10, "min": 3, "max": 60,
         "unit": "s", "advanced": True},
        {"key": "page_size", "label": "Results per provider and page", "type": "number",
         "default": 20, "min": 5, "max": 50, "advanced": True},
    ]

    async def start(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent": "Casa-torrent-search/1.0"})
        found = discover(str(self.config.get("extra_dir") or ""))
        self.health: dict[str, dict] = {}
        self.media = None
        try:   # the Media plugin's sites, with its mirrors and proxy (nothing is opened until used)
            from .media_adapter import SITES, MediaBackend, MediaProvider
            cfg = (self.hub.config.get("plugins") or {}).get("media") or {}
            self.media = MediaBackend(cfg)
            for site in SITES:
                found.setdefault(site, MediaProvider(self.media, site))
        except Exception:
            log.exception("media trackers unavailable")
        for rec in self._custom():
            found.setdefault(rec["id"], TorznabProvider(rec["id"], rec["name"], rec["url"], rec.get("key", "")))
        self.engine = Engine(found, float(self.config.get("timeout", 10) or 10))

    async def stop(self):
        await self.session.close()
        if self.media:
            await self.media.close()

    def status(self):
        return {"providers": sorted(self.enabled_ids())}

    # -- tracker registry (custom list + enabled set live in the hub store) ----------------

    def _custom(self) -> list[dict]:
        v = self.hub.store.get(STORE_TRACKERS, [])
        return [r for r in v if isinstance(r, dict) and r.get("id") and r.get("url")] if isinstance(v, list) else []

    def _save_custom(self, recs: list[dict]) -> None:
        self.hub.store.set(STORE_TRACKERS, recs)

    def enabled_ids(self) -> set[str]:
        stored = self.hub.store.get(STORE_ENABLED)
        known = self.engine.providers
        if isinstance(stored, list):
            return {i for i in stored if i in known}
        want = [str(x) for x in (self.config.get("providers") or [])]
        if want:
            return {i for i in want if i in known}
        return {i for i, p in known.items() if p.kind not in ("media", "torznab")}

    def _set_enabled(self, ids) -> None:
        self.hub.store.set(STORE_ENABLED, sorted(ids))

    def _describe(self, p: Provider, enabled: set[str]) -> dict:
        d = {"id": p.id, "name": p.name, "kind": p.kind or "file", "enabled": p.id in enabled,
             "editable": p.kind == "torznab", "has_key": False, "url": "", "health": self.health.get(p.id)}
        if isinstance(p, TorznabProvider):
            d.update(has_key=bool(p.key), url=p.url)
        return d

    def _list(self) -> list[dict]:
        en = self.enabled_ids()
        order = {"builtin": 0, "file": 1, "media": 2, "torznab": 3}
        ps = sorted(self.engine.providers.values(), key=lambda p: (order.get(p.kind or "file", 1), p.name.lower()))
        return [self._describe(p, en) for p in ps]

    def _note(self, st: dict) -> None:
        self.health[st["id"]] = {"ok": st["ok"], "count": st.get("count", 0), "ms": st.get("ms"),
                                 "error": st.get("error"), "kind": st.get("kind"), "checked": time.time()}

    def _tracker(self, request) -> Provider:
        p = self.engine.providers.get(request.match_info["id"])
        if p is None:
            raise KeyError(_("Unknown tracker"))
        return p

    @staticmethod
    def _fields(body, need_url: bool) -> tuple[str | None, str | None, str | None]:
        if not isinstance(body, dict):
            raise ValueError(_("Invalid request"))
        name = body.get("name")
        name = " ".join(str(name).split()) if name is not None else None
        if name is not None and not 1 <= len(name) <= 40:
            raise ValueError(_("The name must be 1-40 characters"))
        url, key = body.get("url"), body.get("key")
        if need_url or (url not in (None, "")):
            try:
                url, found = clean_url(str(url or ""))
            except ValueError as err:
                raise ValueError(_("The URL must start with http:// or https://")) from err
            key = (str(key).strip() if key else "") or found
        elif url == "":
            url = None
        key = (str(key).strip() if key else "") or None
        if key and len(key) > 200:
            raise ValueError(_("The API key is too long"))
        return name, url, key

    # -- routes --------------------------------------------------------------------------------

    def routes(self):
        return [web.get("/providers", self.r_providers), web.get("/search", self.r_search),
                web.get("/trackers", self.r_trackers), web.post("/trackers", self.r_add),
                web.post("/trackers/test", self.r_test_draft),
                web.put("/trackers/{id}", self.r_edit), web.delete("/trackers/{id}", self.r_delete),
                web.post("/trackers/{id}/test", self.r_test)]

    async def r_providers(self, request):
        en = self.enabled_ids()
        return _json([{"id": p.id, "name": p.name} for i, p in self.engine.providers.items() if i in en])

    async def r_trackers(self, request):
        return _json({"trackers": self._list()})

    async def r_add(self, request):
        name, url, key = self._fields(await request.json(), True)
        recs = self._custom()
        if len(recs) >= MAX_CUSTOM:
            raise ValueError(_("Too many custom trackers"))
        if not name:
            raise ValueError(_("The name must be 1-40 characters"))
        rec = {"id": "tz" + secrets.token_hex(4), "name": name, "url": url, "key": key or ""}
        self._save_custom(recs + [rec])
        self.engine.providers[rec["id"]] = TorznabProvider(rec["id"], name, url, rec["key"])
        self._set_enabled(self.enabled_ids() | {rec["id"]})
        return _json(self._describe(self.engine.providers[rec["id"]], self.enabled_ids()), 201)

    async def r_edit(self, request):
        p = self._tracker(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError(_("Invalid request"))
        if p.kind != "torznab" and any(body.get(k) not in (None, "") for k in ("name", "url", "key")):
            raise ValueError(_("Built-in trackers can only be switched on or off"))
        if p.kind == "torznab" and any(k in body for k in ("name", "url", "key")):
            name, url, key = self._fields(body, False)
            recs = self._custom()
            rec = next((r for r in recs if r["id"] == p.id), None)
            if rec is None:
                raise KeyError(_("Unknown tracker"))
            if name:
                rec["name"] = name
            if url:
                rec["url"] = url
            if key:                     # an empty key field keeps the saved one
                rec["key"] = key
            self._save_custom(recs)
            self.engine.providers[p.id] = p = TorznabProvider(p.id, rec["name"], rec["url"], rec.get("key", ""))
            self.health.pop(p.id, None)
            self.engine.cooldown.pop(p.id, None)
        if "enabled" in body:
            ids = self.enabled_ids()
            (ids.add if body["enabled"] else ids.discard)(p.id)
            self._set_enabled(ids)
        return _json(self._describe(p, self.enabled_ids()))

    async def r_delete(self, request):
        p = self._tracker(request)
        if p.kind != "torznab":
            raise ValueError(_("Built-in trackers can only be switched on or off"))
        ids = self.enabled_ids() - {p.id}
        self._save_custom([r for r in self._custom() if r["id"] != p.id])
        self._set_enabled(ids)
        self.engine.providers.pop(p.id, None)
        self.health.pop(p.id, None)
        return _json({"ok": True})

    async def _probe(self, p: Provider) -> dict:
        self.engine.cooldown.pop(p.id, None)
        st, _items = await self.engine._one(self.session, p, "ubuntu", 1, 5)
        st.pop("raw", None)
        return st

    async def r_test(self, request):
        p = self._tracker(request)
        st = await self._probe(p)
        self._note(st)
        return _json(st)

    async def r_test_draft(self, request):
        """Try an endpoint before saving it (``id`` of an existing tracker supplies a saved key)."""
        body = await request.json()
        _name, url, key = self._fields(body, True)
        old = self.engine.providers.get(str((body or {}).get("id") or ""))
        if not key and isinstance(old, TorznabProvider):
            key = old.key
        return _json(await self._probe(TorznabProvider("draft", (body.get("name") or "Draft"), url, key or "")))

    async def r_search(self, request):
        q = request.query
        query = " ".join(q.get("q", "").split())
        if len(query) < 2:
            raise ValueError(_("Type at least 2 characters"))
        if len(query) > MAX_QUERY:
            raise ValueError(_("The search is too long (max {n} characters)", n=MAX_QUERY))
        sort = q.get("sort", "relevance")
        if sort not in SORTS:
            raise ValueError(_("Unknown sort"))
        page = min(max(to_int(q.get("page")) or 1, 1), 50)
        size = min(max(to_int(self.config.get("page_size", 20)) or 20, 5), 50)
        ids = self.enabled_ids()
        asked = [i for i in q.get("providers", "").split(",") if i]
        if asked:
            ids &= set(asked)
        if not ids:
            return _json({"query": query, "page": page, "results": [], "providers": [], "has_more": False,
                          "error": _("No tracker is enabled. Turn one on in Trackers.")})
        merged, statuses, has_more = await self.engine.search(self.session, query, page, size, sorted(ids))
        for st in statuses:
            self._note(st)
        results = refine(merged, sort, q.get("order", "desc"), to_int(q.get("min_seeders")) or 0,
                         to_int(q.get("min_size")) or 0, to_int(q.get("max_size")) or 0)
        error = None if any(s["ok"] for s in statuses) else _("Every provider failed. See details below.")
        return _json({"query": query, "page": page, "results": [r.to_dict() for r in results],
                      "providers": statuses, "has_more": has_more, "error": error})


PLUGIN = TorrentsPlugin
