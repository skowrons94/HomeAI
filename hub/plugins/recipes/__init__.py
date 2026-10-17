"""Recipes: import from any site (schema.org), TheMealDB / GialloZafferano search, a personal recipe
book stored locally (works offline), shopping list with substitutions and a daily calorie log.

    [plugins.recipes]
    daily_kcal_goal = 2000
    default_servings = 2
    sources = ["themealdb", "giallozafferano"]
    language = "it"                  # it | en
"""

from __future__ import annotations

import json
import re

from aiohttp import web

from ...core import Plugin
from ...i18n import _
from . import sources
from .db import Book


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


async def _body(request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise ValueError("expected a JSON body")
    if not isinstance(b, dict):
        raise ValueError("expected a JSON object")
    return b


class RecipesPlugin(Plugin):
    name = "recipes"
    title = "Recipes"
    description = "Recipe book with web import, shopping list and calorie log."
    icon = "utensils"
    schema = [
        {"key": "daily_kcal_goal", "label": "Daily calorie goal", "type": "number", "default": 2000,
         "min": 500, "max": 6000, "step": 50, "unit": "kcal"},
        {"key": "default_servings", "label": "Default servings", "type": "number", "default": 2,
         "min": 1, "max": 12, "step": 1, "help": "Recipes open already scaled for this many people."},
        {"key": "sources", "label": "Search sources", "type": "list", "default": ["themealdb", "giallozafferano"],
         "advanced": True, "help": "themealdb (English) and giallozafferano (Italian). Any URL can be imported anyway."},
        {"key": "language", "label": "Language", "type": "select", "default": "it",
         "options": [{"value": "it", "label": "Italiano"}, {"value": "en", "label": "English"}]},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.book: Book | None = None

    async def start(self):
        self.book = Book(self.hub.store, self.hub.data_dir, self.config)
        # calories are shown per recipe and in the Today tab, not as a chart
        self.hub.store.delete_series("recipes.calories")

    def _publish(self):
        self.hub.publish("recipes", None)

    @property
    def sources(self) -> list[str]:
        s = self.config.get("sources")
        return [x for x in (s if isinstance(s, list) else ["themealdb", "giallozafferano"])]

    def status(self):
        n = self.book.store.query("SELECT COUNT(*) AS n FROM recipes_recipe")[0]["n"] if self.book else 0
        return {"recipes": n, "goal": self.book.goal if self.book else 2000,
                "language": self.book.lang if self.book else "it",
                "default_servings": self.book.default_servings if self.book else 2,
                "sources": self.sources}

    def routes(self):
        return [
            web.get("/status", lambda r: _json(self.status())),
            web.get("/search", self.r_search),
            web.post("/import", self.r_import),
            web.get("/recipes", self.r_list),
            web.post("/recipes", self.r_create),
            web.get("/tags", lambda r: _json(self.book.tags())),
            web.get("/recipe/{id}", self.r_get),
            web.put("/recipe/{id}", self.r_put),
            web.delete("/recipe/{id}", self.r_delete),
            web.post("/recipe/{id}/reset", self.r_reset),
            web.get("/image/{name}", self.r_image),
            web.get("/shopping", lambda r: _json(self.book.shopping())),
            web.post("/shopping", self.r_shop_add),
            web.patch("/shopping/{id}", self.r_shop_patch),
            web.delete("/shopping", self.r_shop_clear),
            web.delete("/shopping/{id}", self.r_shop_delete),
            web.get("/log", self.r_log),
            web.post("/log", self.r_log_add),
            web.delete("/log/{id}", self.r_log_delete),
            web.get("/nutrition", lambda r: _json(self.book.nutrition())),
        ]

    # -- discovery / import -------------------------------------------------------

    async def r_search(self, request):
        q = request.query.get("q", "").strip()
        if not q:
            return _json({"results": [], "errors": {}})
        res = await sources.search(self.hub.http, q, self.sources, self.book.lang)
        for it in res["results"]:  # mark what's already saved
            sid = self.book.find_by_url(it["url"])
            it["saved"] = sid
        return _json(res)

    async def r_import(self, request):
        b = await _body(request)
        http = self.hub.http
        if b.get("mealdb"):
            rec = await sources.mealdb_get(http, b["mealdb"])
            url = rec["source_url"]
            if not rec.get("mealdb_id"):
                raise ValueError("bad recipe")
            # the meal page is the canonical key (strSource points to many different sites)
            rec["source_url"] = f"https://www.themealdb.com/meal/{rec['mealdb_id']}"
        else:
            url = (b.get("url") or "").strip()
            if not url:
                raise ValueError(_("A link is required"))
            rec = None
        if rec is None:
            url = sources.check_url(url)
            existing = self.book.find_by_url(url)
            if existing:
                return _json({"id": existing, "existing": True})
            rec = await sources.import_url(http, url)
            existing = self.book.find_by_url(rec["source_url"])
            if existing:
                return _json({"id": existing, "existing": True})
        else:
            existing = self.book.find_by_url(rec["source_url"])
            if existing:
                return _json({"id": existing, "existing": True})
        image = await sources.fetch_image(http, rec.get("image_url", ""), self.book.img_dir,
                                          sources.image_stem(rec["image_url"])) if rec.get("image_url") else ""
        rid = self.book.add(rec, image)
        return _json({"id": rid, "existing": False})

    # -- recipes ------------------------------------------------------------------

    async def r_list(self, request):
        q = request.query
        return _json(self.book.list(fav=q.get("fav") in ("1", "true"), tag=q.get("tag", ""), q=q.get("q", "")))

    async def r_create(self, request):
        b = await _body(request)
        rid = self.book.add({k: b.get(k) for k in ("title", "servings", "ingredients", "steps", "notes", "tags",
                                                    "kcal", "prep_min", "cook_min", "total_min", "category",
                                                    "cuisine")} | {"lang": self.book.lang})
        if b.get("fav"):
            self.book.update(rid, {"fav": True})
        return _json({"id": rid})

    def _rid(self, request) -> int:
        try:
            return int(request.match_info["id"])
        except ValueError:
            raise KeyError(_("Recipe not found"))

    async def r_get(self, request):
        sv = request.query.get("servings")
        return _json(self.book.view(self._rid(request), float(sv) if sv else None))

    async def r_put(self, request):
        rid = self._rid(request)
        self.book.update(rid, await _body(request))
        return _json(self.book.view(rid))

    async def r_delete(self, request):
        self.book.delete(self._rid(request))
        return _json({"ok": True})

    async def r_reset(self, request):
        rid = self._rid(request)
        self.book.reset(rid)
        return _json(self.book.view(rid))

    async def r_image(self, request):
        name = request.match_info["name"]
        if not re.fullmatch(r"[0-9a-f]{16}\.(?:jpg|png|webp|gif|avif)", name):
            raise KeyError("image not found")
        path = self.book.img_dir / name
        if not path.is_file():
            raise KeyError("image not found")
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=2592000, immutable"})

    # -- shopping list ------------------------------------------------------------

    async def r_shop_add(self, request):
        b = await _body(request)
        if b.get("recipe_id") is not None:
            n = self.book.shop_add_recipe(int(b["recipe_id"]), float(b["servings"]) if b.get("servings") else None)
            return _json({"added": n, "items": self.book.shopping()})
        self.book.shop_add_manual(str(b.get("name", "")), b.get("qty"), str(b.get("unit") or ""))
        return _json({"items": self.book.shopping()})

    async def r_shop_patch(self, request):
        self.book.shop_patch(int(request.match_info["id"]), await _body(request))
        return _json({"items": self.book.shopping()})

    async def r_shop_delete(self, request):
        self.book.shop_delete(int(request.match_info["id"]))
        return _json({"items": self.book.shopping()})

    async def r_shop_clear(self, request):
        self.book.shop_clear(everything=request.query.get("all") in ("1", "true"))
        return _json({"items": self.book.shopping()})

    # -- calories -----------------------------------------------------------------

    async def r_log(self, request):
        return _json(self.book.log(request.query.get("day")))

    async def r_log_add(self, request):
        b = await _body(request)
        self.book.log_add(b.get("day"), b.get("recipe_id"), b.get("servings"), str(b.get("name") or ""), b.get("kcal"))
        self._publish()
        return _json(self.book.log(b.get("day")))

    async def r_log_delete(self, request):
        self.book.log_delete(int(request.match_info["id"]))
        self._publish()
        return _json({"ok": True})


PLUGIN = RecipesPlugin
