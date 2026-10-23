"""Torrent search: one box, any number of pluggable providers, one normalised result list.

Providers live in ``sources/`` (built in) or in the folder set as ``extra_dir`` (yours, outside
the repo). Nothing is searched until a provider is enabled. See docs/torrents.md.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web

from ...core import Plugin
from ...i18n import _
from .engine import SORTS, Engine, discover, refine
from .model import to_int

MAX_QUERY = 100


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
        want = [str(x) for x in (self.config.get("providers") or [])]
        self.engine = Engine({i: p for i, p in found.items() if not want or i in want},
                             float(self.config.get("timeout", 10) or 10))
        self.known = found

    async def stop(self):
        await self.session.close()

    def status(self):
        return {"providers": list(self.engine.providers)}

    def routes(self):
        return [web.get("/providers", self.r_providers), web.get("/search", self.r_search)]

    async def r_providers(self, request):
        return _json([{"id": p.id, "name": p.name} for p in self.engine.providers.values()])

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
        ids = [i for i in q.get("providers", "").split(",") if i] or None
        if not self.engine.providers:
            return _json({"query": query, "page": page, "results": [], "providers": [], "has_more": False,
                          "error": _("No provider is enabled. Add one in Settings → Torrent search.")})
        merged, statuses, has_more = await self.engine.search(self.session, query, page, size, ids)
        results = refine(merged, sort, q.get("order", "desc"), to_int(q.get("min_seeders")) or 0,
                         to_int(q.get("min_size")) or 0, to_int(q.get("max_size")) or 0)
        error = None if any(s["ok"] for s in statuses) else _("Every provider failed. See details below.")
        return _json({"query": query, "page": page, "results": [r.to_dict() for r in results],
                      "providers": statuses, "has_more": has_more, "error": error})


PLUGIN = TorrentsPlugin
