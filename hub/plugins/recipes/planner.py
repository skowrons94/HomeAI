"""Weekly meal planner: one row per (date, meal) in ``recipes_plan``, on top of the recipe Book."""

from __future__ import annotations

import random
import re
import time
from datetime import date, timedelta

from ...i18n import _

MEALS = ("lunch", "dinner")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HISTORY_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes_plan (
    day TEXT NOT NULL, meal TEXT NOT NULL, recipe_id INTEGER, title TEXT NOT NULL DEFAULT '',
    servings INTEGER NOT NULL DEFAULT 2, note TEXT NOT NULL DEFAULT '', updated REAL,
    PRIMARY KEY (day, meal));
"""


def parse_day(v, default: date | None = None) -> date:
    if v in (None, ""):
        if default is not None:
            return default
        raise ValueError(_("Pick a day"))
    if not isinstance(v, str) or not DAY.match(v):
        raise ValueError(_("Not a valid date"))
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(_("Not a valid date"))


def monday(v=None) -> date:
    d = parse_day(v, date.today())
    return d - timedelta(days=d.weekday())


def check_meal(meal) -> str:
    if meal not in MEALS:
        raise ValueError(_("Meal must be lunch or dinner"))
    return meal


class Planner:
    def __init__(self, book):
        self.book = book
        self.store = book.store
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                self.store.execute(stmt)

    # -- reading ------------------------------------------------------------------

    def _slots(self, start: date, end: date) -> dict[tuple[str, str], dict]:
        rows = self.store.query(
            "SELECT p.*, r.title AS r_title, r.image AS r_image FROM recipes_plan p "
            "LEFT JOIN recipes_recipe r ON r.id = p.recipe_id WHERE p.day BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()))
        out = {}
        for r in rows:
            alive = r["r_title"] is not None
            out[(r["day"], r["meal"])] = {
                "date": r["day"], "meal": r["meal"], "recipe_id": r["recipe_id"] if alive else None,
                "title": r["r_title"] if alive else r["title"],
                "image": f"/api/p/recipes/image/{r['r_image']}" if alive and r["r_image"] else None,
                "servings": r["servings"], "note": r["note"]}
        return out

    def day(self, d: date) -> dict:
        s = self._slots(d, d)
        return {"date": d.isoformat(), **{m: s.get((d.isoformat(), m)) for m in MEALS}}

    def week(self, v=None) -> dict:
        mon = monday(v)
        s = self._slots(mon, mon + timedelta(days=6))
        days = []
        for i in range(7):
            d = (mon + timedelta(days=i)).isoformat()
            days.append({"date": d, **{m: s.get((d, m)) for m in MEALS}})
        return {"week": mon.isoformat(), "days": days}

    def today(self) -> dict:
        return self.day(date.today())

    # -- editing ------------------------------------------------------------------

    def put(self, day: str, meal: str, body: dict) -> dict:
        d = parse_day(day)
        meal = check_meal(meal)
        rid = body.get("recipe_id")
        title = str(body.get("title") or "").strip()[:80]
        if rid not in (None, ""):
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                raise ValueError(_("Recipe not found"))
            row = self.store.query("SELECT title FROM recipes_recipe WHERE id=?", (rid,))
            if not row:
                raise KeyError(_("Recipe not found"))
            title = row[0]["title"]
        else:
            rid = None
            if not title:
                raise ValueError(_("Choose a recipe or write a dish"))
        try:
            raw = body.get("servings")
            sv = int(round(float(self.book.default_servings if raw in (None, "") else raw)))
        except (TypeError, ValueError):
            raise ValueError(_("Servings must be a number"))
        if not 1 <= sv <= 50:
            raise ValueError(_("Servings must be between 1 and 50"))
        note = str(body.get("note") or "").strip()[:200]
        self.store.execute(
            "INSERT INTO recipes_plan(day, meal, recipe_id, title, servings, note, updated) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(day, meal) DO UPDATE SET recipe_id=excluded.recipe_id, title=excluded.title, "
            "servings=excluded.servings, note=excluded.note, updated=excluded.updated",
            (d.isoformat(), meal, rid, title, sv, note, time.time()))
        return self._slots(d, d)[(d.isoformat(), meal)]

    def delete(self, day: str, meal: str) -> None:
        d = parse_day(day)
        self.store.execute("DELETE FROM recipes_plan WHERE day=? AND meal=?", (d.isoformat(), check_meal(meal)))

    # -- suggestions ----------------------------------------------------------------

    def fill(self, week=None, meals=None) -> int:
        """Fill the EMPTY slots of a week from the saved recipes; returns how many were filled."""
        mon = monday(week)
        meals = [check_meal(m) for m in (meals or ["dinner"])]
        recipes = [r for r in self.book.list()]
        if not recipes:
            return 0
        end = mon + timedelta(days=6)
        slots = self._slots(mon - timedelta(days=HISTORY_DAYS), end + timedelta(days=HISTORY_DAYS))
        rng = random.Random(mon.isoformat())
        by_id = {r["id"]: r for r in recipes}

        def feats(rid):
            r = by_id.get(rid)
            if not r:
                return set()
            return set(r["tags"]) | ({r["category"].lower()} if r["category"] else set())

        filled = 0
        for i in range(7):
            d = mon + timedelta(days=i)
            for meal in meals:
                key = (d.isoformat(), meal)
                if key in slots:
                    continue
                lo, hi = d - timedelta(days=HISTORY_DAYS), d + timedelta(days=HISTORY_DAYS)
                recent = {s["recipe_id"] for (day, _m), s in slots.items()
                          if s["recipe_id"] and lo.isoformat() <= day <= hi.isoformat()}
                week_ids = {s["recipe_id"] for (day, _m), s in slots.items()
                            if s["recipe_id"] and mon.isoformat() <= day <= end.isoformat()}
                pool = [r for r in recipes if r["id"] not in recent]
                if not pool:  # too few recipes: only never twice in the same week
                    pool = [r for r in recipes if r["id"] not in week_ids]
                if not pool:
                    continue
                used: dict[str, int] = {}
                for (day, _m), s in slots.items():
                    if s["recipe_id"] and mon.isoformat() <= day <= end.isoformat():
                        for f in feats(s["recipe_id"]):
                            used[f] = used.get(f, 0) + 1
                prev = slots.get(((d - timedelta(days=1)).isoformat(), meal))

                def score(r):
                    f = feats(r["id"])
                    s = (2.0 if r["fav"] else 0.0) + rng.random() * 1.5
                    s -= 0.6 * sum(min(used.get(x, 0), 3) for x in f)
                    if prev and prev["recipe_id"] and f & feats(prev["recipe_id"]):
                        s -= 1.5
                    if r["rating"]:
                        s += r["rating"] * 0.1
                    return s

                best = max(pool, key=score)
                self.put(d.isoformat(), meal, {"recipe_id": best["id"]})
                slots[key] = {"recipe_id": best["id"], "date": d.isoformat(), "meal": meal}
                filled += 1
        return filled

    def copy(self, from_week, to_week) -> int:
        a, b = monday(from_week), monday(to_week)
        if a == b:
            raise ValueError(_("Pick two different weeks"))
        src = self._slots(a, a + timedelta(days=6))
        dst = self._slots(b, b + timedelta(days=6))
        n = 0
        for (day, meal), s in sorted(src.items()):
            nd = (date.fromisoformat(day) - a + b).isoformat()
            if (nd, meal) in dst:
                continue
            self.put(nd, meal, {"recipe_id": s["recipe_id"], "title": s["title"], "servings": s["servings"],
                                "note": s["note"]} if s["recipe_id"] else
                     {"title": s["title"], "servings": s["servings"], "note": s["note"]})
            n += 1
        return n

    def shopping(self, week=None) -> int:
        """Add the week's planned recipes to the shopping list (merged, scaled per slot)."""
        mon = monday(week)
        n = 0
        for s in self._slots(mon, mon + timedelta(days=6)).values():
            if s["recipe_id"]:
                n += self.book.shop_add_recipe(s["recipe_id"], float(s["servings"]))
        return n
