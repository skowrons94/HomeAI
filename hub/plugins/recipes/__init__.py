"""Recipes: import from any site (schema.org), TheMealDB / GialloZafferano search, a personal recipe
book stored locally (works offline), shopping list with substitutions and a daily calorie log.

    [plugins.recipes]
    daily_kcal_goal = 2000
    default_servings = 2
    sources = ["giallozafferano", "misya", "themealdb"]   # default: every source in sources.ALL_SOURCES
    language = "it"                  # it | en
"""

from __future__ import annotations

import json
import re
import time

from aiohttp import web

from ...core import Plugin
from ...i18n import _
from . import pantry as pantry_mod
from . import parse
from . import suggest as suggest_mod
from . import sources
from .db import Book
from .planner import Planner, monday


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
        {"key": "sources", "label": "Search sources", "type": "list", "default": sources.DEFAULT_SOURCES,
         "advanced": True, "help": "Sites searched for recipes (giallozafferano, tavolartegusto, benedetta, misya, ricettedellanonna, "
         "vincenzosplate, themealdb, recipetineats, hebbars, isabeleats, mediterraneandish). "
         "Remove any you don't like. Any web address can be imported anyway."},
        {"key": "language", "label": "Language", "type": "select", "default": "it",
         "options": [{"value": "it", "label": "Italiano"}, {"value": "en", "label": "English"}]},
        {"key": "plan_reminder", "label": "Remind tonight's dinner", "type": "bool", "default": False,
         "help": "A notification in the afternoon with what's for dinner."},
        {"key": "pantry_from_shopping", "label": "Ticked shopping items go to the pantry", "type": "bool",
         "default": True},
        {"key": "pantry_place", "label": "Where ticked shopping items go", "type": "select", "default": "fridge",
         "options": [{"value": "fridge", "label": "Fridge"}, {"value": "freezer", "label": "Freezer"},
                     {"value": "pantry", "label": "Pantry"}],
         "help": "Place in the Fridge tab for items moved from the shopping list."},
        {"key": "expiry_alerts", "label": "Alert for food about to expire", "type": "bool", "default": True,
         "help": "One notification a day when something in the fridge expires tomorrow or has expired."},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.book: Book | None = None
        self.planner: Planner | None = None
        self.pantry: pantry_mod.Pantry | None = None

    async def start(self):
        self.book = Book(self.hub.store, self.hub.data_dir, self.config)
        self.planner = Planner(self.book)
        self.pantry = pantry_mod.Pantry(self.hub.store)
        if self.book.tidy_titles(sources.clean_title):
            self.log.info("tidied imported recipe titles")
        self.hub.every(3600, self._expiry_check, name="recipes.expiry", initial_delay=45)
        self.hub.every(60, self._remind, name="recipes.plan_reminder", initial_delay=20)
        # calories are shown per recipe and in the Today tab, not as a chart
        self.hub.store.delete_series("recipes.calories")

    def _publish(self):
        self.hub.publish("recipes", None)

    @property
    def sources(self) -> list[str]:
        s = self.config.get("sources")
        return [x for x in (s if isinstance(s, list) else sources.DEFAULT_SOURCES)]

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
            web.get("/suggest", self.r_suggest),
            web.post("/import", self.r_import),
            web.get("/recipes", self.r_list),
            web.post("/recipes", self.r_create),
            web.get("/tags", lambda r: _json(self.book.tags())),
            web.get("/recipe/{id}", self.r_get),
            web.put("/recipe/{id}", self.r_put),
            web.delete("/recipe/{id}", self.r_delete),
            web.post("/recipe/{id}/reset", self.r_reset),
            web.post("/edit/parse", self.r_edit_parse),
            web.post("/edit/rescale", self.r_edit_rescale),
            web.get("/image/{name}", self.r_image),
            web.get("/shopping", lambda r: _json(self.book.shopping())),
            web.post("/shopping", self.r_shop_add),
            web.patch("/shopping/{id}", self.r_shop_patch),
            web.delete("/shopping", self.r_shop_clear),
            web.delete("/shopping/{id}", self.r_shop_delete),
            web.get("/pantry", self.r_pantry),
            web.put("/pantry", self.r_pantry_put),
            web.post("/pantry/add", self.r_pantry_add),
            web.post("/pantry/remove", self.r_pantry_remove),
            web.get("/fridge", self.r_fridge),
            web.get("/fridge/summary", self.r_fridge_summary),
            web.post("/fridge", self.r_fridge_add),
            web.patch("/fridge/{id}", self.r_fridge_patch),
            web.delete("/fridge/{id}", self.r_fridge_delete),
            web.post("/fridge/{id}/finish", self.r_fridge_finish),
            web.get("/ingredients", self.r_ingredients),
            web.post("/cook-with", self.r_cook_with),
            web.get("/log", self.r_log),
            web.post("/log", self.r_log_add),
            web.delete("/log/{id}", self.r_log_delete),
            web.get("/nutrition", lambda r: _json(self.book.nutrition())),
            web.get("/plan", self.r_plan),
            web.get("/plan/today", lambda r: _json({**self.planner.today()})),
            web.post("/plan/fill", self.r_plan_fill),
            web.post("/plan/copy", self.r_plan_copy),
            web.post("/plan/shopping", self.r_plan_shopping),
            web.put("/plan/{date}/{meal}", self.r_plan_put),
            web.delete("/plan/{date}/{meal}", self.r_plan_delete),
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

    async def r_suggest(self, request):
        """Three ideas for the hour (saved recipes + web), stable within the hour; ?n= asks for another set."""
        now = time.localtime()
        n = int(request.query.get("n") or 0)
        key = (time.strftime("%Y-%m-%d", now), now.tm_hour, n, self.book.lang)
        cache = self.__dict__.setdefault("_suggest_cache", {})
        if key not in cache:
            cache.clear() if len(cache) > 20 else None
            res = await suggest_mod.suggest(self.book, self.hub.http, self.sources, self.book.lang, now.tm_hour,
                                            now.tm_mon, "|".join(map(str, key)), sources.search)
            if len(res["items"]) < 3:  # partial answer (a source was down): don't pin it for the hour
                return _json(res)
            cache[key] = res
        return _json(cache[key])

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

    async def r_edit_parse(self, request):
        """Pasted text -> editor rows (same parser as the import)."""
        b = await _body(request)
        text = b.get("text", "")
        lines = [x.strip() for x in (text.split("\n") if isinstance(text, str) else text) if str(x).strip()]
        return _json({"rows": [parse.row_from_line(x) for x in lines]})

    async def r_edit_rescale(self, request):
        """Editor rows scaled by to/from servings, with cook-style rounding (nothing is saved)."""
        b = await _body(request)
        src, dst = float(b.get("from") or 0), float(b.get("to") or 0)
        if src <= 0 or dst <= 0:
            raise ValueError(_("Servings must be positive"))
        lang = self.book.lang
        lines = parse.rows_to_lines(b.get("rows") or [], lang)
        return _json({"rows": [parse.row_from_line(x) for x in parse.rescale_lines(lines, dst / src, lang)]})

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
        sid, b = int(request.match_info["id"]), await _body(request)
        row = self.book.store.query("SELECT name FROM recipes_shop WHERE id=?", (sid,))
        self.book.shop_patch(sid, b)
        if b.get("checked") and row and self.config.get("pantry_from_shopping", True):
            self.pantry.add([row[0]["name"]], place=self._place())
        return _json({"items": self.book.shopping()})

    async def r_shop_delete(self, request):
        self.book.shop_delete(int(request.match_info["id"]))
        return _json({"items": self.book.shopping()})

    async def r_shop_clear(self, request):
        self.book.shop_clear(everything=request.query.get("all") in ("1", "true"))
        return _json({"items": self.book.shopping()})

    # -- pantry / cook with what we have ---------------------------------------------

    async def r_pantry(self, request):
        return _json({"items": self.pantry.items()})

    @staticmethod
    def _items(b: dict) -> list[str]:
        v = b.get("items")
        if not isinstance(v, list):
            raise ValueError(_("A list of items is required"))
        return [str(x) for x in v]

    async def r_pantry_put(self, request):
        return _json({"items": self.pantry.put(self._items(await _body(request)))})

    async def r_pantry_add(self, request):
        return _json({"items": self.pantry.add(self._items(await _body(request)))})

    async def r_pantry_remove(self, request):
        return _json({"items": self.pantry.remove(self._items(await _body(request)))})

    # -- fridge ("Frigo"): structured inventory ------------------------------------------

    def _place(self) -> str:
        p = self.config.get("pantry_place", "fridge")
        return p if p in pantry_mod.PLACES else "fridge"

    def _fridge_out(self):
        return {"items": self.pantry.entries(), "places": list(pantry_mod.PLACES)}

    async def r_fridge(self, request):
        return _json(self._fridge_out())

    async def r_fridge_summary(self, request):
        return _json(self.pantry.summary())

    async def r_fridge_add(self, request):
        e = self.pantry.add_entry(await _body(request))
        return _json({**self._fridge_out(), "item": e})

    async def r_fridge_patch(self, request):
        e = self.pantry.patch_entry(request.match_info["id"], await _body(request))
        return _json({**self._fridge_out(), "item": e})

    async def r_fridge_delete(self, request):
        self.pantry.remove_entry(request.match_info["id"])
        return _json(self._fridge_out())

    async def r_fridge_finish(self, request):
        try:
            b = await _body(request)
        except ValueError:
            b = {}
        e = self.pantry.remove_entry(request.match_info["id"])
        if b.get("to_shopping"):
            self.book.shop_add_manual(e["name"])
        return _json({**self._fridge_out(), "finished": e})

    async def _expiry_check(self, today=None, hour=None):
        """Once a day (from 8:00): one notification if something expires tomorrow or already has."""
        if not self.config.get("expiry_alerts", True) or not self.pantry:
            return
        now = time.localtime()
        if (now.tm_hour if hour is None else hour) < 8:
            return
        today = today or pantry_mod._today()
        if self.hub.store.get("recipes.expiry_sent") == today.isoformat():
            return
        due = [e for e in self.pantry.summary(today)["expiring"] if e["days"] <= 1]
        if not due:
            return
        self.hub.store.set("recipes.expiry_sent", today.isoformat())
        names = ", ".join(e["name"] for e in due[:6]) + ("…" if len(due) > 6 else "")
        gone = [e for e in due if e["days"] < 0]
        title = _("Expired in the fridge: {names}", names=", ".join(e["name"] for e in gone[:4])) if gone and len(gone) == len(due) \
            else _("Expiring soon: {names}", names=names)
        await self.hub.notify.send(title, body=_("Open the Fridge to check"), url="#/recipes/fridge", tag="note")

    async def r_ingredients(self, request):
        return _json({"items": pantry_mod.known_ingredients(self.book)})

    async def r_cook_with(self, request):
        b = await _body(request)
        typed = self._items({"items": b.get("ingredients")})
        use = b.get("use_pantry", True)
        return _json({"results": pantry_mod.cook_with(self.book, typed, self.pantry.items() if use else [])})

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

    # -- meal planner -------------------------------------------------------------

    def _plan_changed(self, day) -> None:
        self.hub.publish("recipes_plan", {"week": monday(day).isoformat()})

    async def r_plan(self, request):
        return _json(self.planner.week(request.query.get("week")))

    async def r_plan_put(self, request):
        m = request.match_info
        slot = self.planner.put(m["date"], m["meal"], await _body(request))
        self._plan_changed(m["date"])
        return _json(slot)

    async def r_plan_delete(self, request):
        m = request.match_info
        self.planner.delete(m["date"], m["meal"])
        self._plan_changed(m["date"])
        return _json({"ok": True})

    async def r_plan_fill(self, request):
        b = await _body(request)
        n = self.planner.fill(b.get("week"), b.get("meals"))
        self._plan_changed(b.get("week"))
        return _json({"filled": n, **self.planner.week(b.get("week"))})

    async def r_plan_copy(self, request):
        b = await _body(request)
        n = self.planner.copy(b.get("from_week"), b.get("to_week"))
        self._plan_changed(b.get("to_week"))
        return _json({"copied": n, **self.planner.week(b.get("to_week"))})

    async def r_plan_shopping(self, request):
        b = await _body(request)
        return _json({"added": self.planner.shopping(b.get("week"))})

    async def _remind(self):
        """Once a day, from 16:30, tell what's for dinner (only when the plan has one)."""
        if not self.config.get("plan_reminder") or not self.planner:
            return
        now = time.localtime()
        if (now.tm_hour, now.tm_min) < (16, 30) or now.tm_hour >= 20:
            return
        day = time.strftime("%Y-%m-%d", now)
        if self.hub.store.get("recipes.plan_reminder_sent") == day:
            return
        dinner = self.planner.today()["dinner"]
        if not dinner:
            return
        self.hub.store.set("recipes.plan_reminder_sent", day)
        url = f"#/recipes/recipe/{dinner['recipe_id']}" if dinner["recipe_id"] else "#/recipes/week"
        await self.hub.notify.send(_("Tonight: {title}", title=dinner["title"]),
                                   body=_("Open the recipe for {n}", n=dinner["servings"]), url=url, tag="note")


PLUGIN = RecipesPlugin
