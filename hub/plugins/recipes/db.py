"""Recipe book: SQLite tables (prefix ``recipes_``), shopping list and food log."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import nutrition, parse
from ...i18n import _

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes_recipe (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, servings REAL, ingredients TEXT NOT NULL,
    steps TEXT NOT NULL, kcal REAL, image TEXT, image_url TEXT, source_url TEXT, site TEXT, cuisine TEXT,
    category TEXT, prep_min INTEGER, cook_min INTEGER, total_min INTEGER, fav INTEGER NOT NULL DEFAULT 0,
    rating INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
    original TEXT, edited INTEGER NOT NULL DEFAULT 0, lang TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS recipes_recipe_src ON recipes_recipe(source_url);
CREATE TABLE IF NOT EXISTS recipes_shop (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key TEXT NOT NULL, qty REAL, unit TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 0, recipes TEXT NOT NULL DEFAULT '[]', created REAL);
CREATE TABLE IF NOT EXISTS recipes_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, ts REAL NOT NULL, name TEXT NOT NULL,
    kcal REAL NOT NULL, recipe_id INTEGER, servings REAL, est INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS recipes_log_day ON recipes_log(day);
"""

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today() -> str:
    return time.strftime("%Y-%m-%d")


def _lines(v) -> list[str]:
    if isinstance(v, str):
        v = v.split("\n")
    return [re.sub(r"\s+", " ", str(x)).strip() for x in (v or []) if str(x).strip()]


def _tags(v) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    out = []
    for t in v or []:
        t = str(t).strip().lower().lstrip("#")
        if t and t not in out:
            out.append(t[:40])
    return out[:30]


class Book:
    def __init__(self, store, data_dir: Path, config: dict):
        self.store = store
        self.config = config
        self.img_dir = Path(data_dir) / "recipes" / "img"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                store.execute(stmt)

    # -- config -------------------------------------------------------------------

    @property
    def lang(self) -> str:
        return "en" if self.config.get("language", "it") == "en" else "it"

    @property
    def default_servings(self) -> float:
        return float(self.config.get("default_servings", 2) or 2)

    @property
    def goal(self) -> float:
        return float(self.config.get("daily_kcal_goal", 2000) or 2000)

    # -- recipes ------------------------------------------------------------------

    def _row(self, rid: int) -> dict:
        rows = self.store.query("SELECT * FROM recipes_recipe WHERE id=?", (rid,))
        if not rows:
            raise KeyError(_("Recipe not found"))
        return rows[0]

    def find_by_url(self, url: str) -> int | None:
        if not url:
            return None
        rows = self.store.query("SELECT id FROM recipes_recipe WHERE source_url=?", (url,))
        return rows[0]["id"] if rows else None

    def _snapshot(self, r: dict) -> dict:
        return {"title": r["title"], "servings": r["servings"], "ingredients": _lines(r["ingredients"]),
                "steps": _lines(r["steps"]), "kcal": r.get("kcal")}

    def add(self, rec: dict, image: str = "") -> int:
        """Store a recipe dict (from an import or manual form)."""
        title = (rec.get("title") or "").strip()
        if not title:
            raise ValueError(_("A recipe needs a title"))
        ing, steps = _lines(rec.get("ingredients")), _lines(rec.get("steps"))
        now = time.time()
        servings = rec.get("servings") or self.default_servings
        imported = bool(rec.get("source_url"))
        orig = json.dumps({"title": title, "servings": servings, "ingredients": ing, "steps": steps,
                           "kcal": rec.get("kcal")}) if imported else None
        tags = rec.get("tags")
        if tags is None:
            tags = re.split(r"[,/]", f"{rec.get('category') or ''},{rec.get('cuisine') or ''}")
        tags = _tags(tags)
        cur = self.store.execute(
            "INSERT INTO recipes_recipe(title, servings, ingredients, steps, kcal, image, image_url, source_url, site,"
            " cuisine, category, prep_min, cook_min, total_min, tags, notes, original, lang, created, updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, servings, json.dumps(ing), json.dumps(steps), rec.get("kcal"), image,
             rec.get("image_url") or "", rec.get("source_url") or "", rec.get("site") or "",
             rec.get("cuisine") or "", rec.get("category") or "", rec.get("prep_min"), rec.get("cook_min"),
             rec.get("total_min"), json.dumps(tags), rec.get("notes") or "", orig,
             rec.get("lang") or self.lang, now, now))
        return cur.lastrowid

    def _kcal(self, r: dict, parsed: list[dict]) -> tuple[float | None, bool]:
        """kcal per serving: the source's value, else an estimate from the ingredients."""
        if r.get("kcal"):
            return round(r["kcal"]), False
        est = nutrition.estimate(parsed)
        sv = r["servings"] or self.default_servings
        if est["counted"] and est["coverage"] >= 0.5 and est["kcal"] > 0:
            return round(est["kcal"] / sv), True
        return None, False

    def view(self, rid: int, servings: float | None = None) -> dict:
        r = self._row(rid)
        base = r["servings"] or self.default_servings
        sv = servings if servings and servings > 0 else base
        factor = sv / base
        lang = r["lang"] or self.lang
        lines = json.loads(r["ingredients"])
        parsed = [parse.parse_ingredient(x) for x in lines if not x.startswith("# ")]
        kcal, est = self._kcal(r, parsed)
        ings = []
        for x in lines:
            if x.startswith("# "):
                ings.append({"heading": x[2:], "raw": x, "text": x[2:]})
                continue
            p = parse.parse_ingredient(x)
            ings.append({"raw": x, "text": parse.format_ingredient(p, factor, lang), "qty": p["qty"], "unit": p["unit"],
                         "name": p["name"], "taste": p["taste"],
                         "subs": nutrition.substitutions(p["name"], self.lang)})
        return {
            **self._summary(r), "servings": sv, "base_servings": base, "ingredients": ings,
            "ingredient_lines": lines, "steps": json.loads(r["steps"]), "kcal": kcal, "kcal_est": est,
            "notes": r["notes"], "has_original": bool(r["original"]),
            "description": "", "created": r["created"],
        }

    def _summary(self, r: dict) -> dict:
        kcal = round(r["kcal"]) if r.get("kcal") else None
        return {
            "id": r["id"], "title": r["title"], "image": f"/api/p/recipes/image/{r['image']}" if r["image"] else "",
            "source_url": r["source_url"], "site": r["site"], "cuisine": r["cuisine"], "category": r["category"],
            "prep_min": r["prep_min"], "cook_min": r["cook_min"], "total_min": r["total_min"],
            "fav": bool(r["fav"]), "rating": r["rating"], "tags": json.loads(r["tags"]),
            "edited": bool(r["edited"]), "lang": r["lang"], "updated": r["updated"],
            "servings": r["servings"], "kcal": kcal,
        }

    def list(self, fav: bool = False, tag: str = "", q: str = "") -> list[dict]:
        rows = self.store.query("SELECT * FROM recipes_recipe ORDER BY fav DESC, updated DESC")
        out = []
        q = parse.strip_accents(q.lower().strip())
        for r in rows:
            if fav and not r["fav"]:
                continue
            tags = json.loads(r["tags"])
            if tag and tag.lower() not in tags:
                continue
            if q:
                hay = parse.strip_accents(" ".join([r["title"], r["ingredients"], " ".join(tags), r["notes"],
                                                    r["site"] or ""]).lower())
                if not all(w in hay for w in q.split()):
                    continue
            s = self._summary(r)
            if not s["kcal"]:
                parsed = [parse.parse_ingredient(x) for x in json.loads(r["ingredients"]) if not x.startswith("# ")]
                s["kcal"], s["kcal_est"] = self._kcal(r, parsed)
            else:
                s["kcal_est"] = False
            out.append(s)
        return out

    def tags(self) -> list[dict]:
        counts: dict[str, int] = {}
        for r in self.store.query("SELECT tags FROM recipes_recipe"):
            for t in json.loads(r["tags"]):
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "n": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def update(self, rid: int, patch: dict) -> None:
        r = self._row(rid)
        sets: dict = {}
        if "title" in patch:
            t = str(patch["title"]).strip()
            if not t:
                raise ValueError("title can't be empty")
            sets["title"] = t
        if "servings" in patch:
            n = float(patch["servings"])
            if n <= 0:
                raise ValueError("servings must be positive")
            sets["servings"] = n
        if "ingredients" in patch:
            sets["ingredients"] = json.dumps(_lines(patch["ingredients"]))
        if "steps" in patch:
            sets["steps"] = json.dumps(_lines(patch["steps"]))
        if "kcal" in patch:
            sets["kcal"] = float(patch["kcal"]) if patch["kcal"] not in (None, "", 0) else None
        elif "ingredients" in sets and sets["ingredients"] != r["ingredients"]:
            sets["kcal"] = None  # the source's calories no longer describe the edited recipe
        for k in ("notes", "cuisine", "category"):
            if k in patch:
                sets[k] = str(patch[k] or "")
        if "fav" in patch:
            sets["fav"] = 1 if patch["fav"] else 0
        if "rating" in patch:
            sets["rating"] = max(0, min(5, int(patch["rating"] or 0)))
        if "tags" in patch:
            sets["tags"] = json.dumps(_tags(patch["tags"]))
        for k in ("prep_min", "cook_min", "total_min"):
            if k in patch:
                sets[k] = int(patch[k]) if patch[k] not in (None, "") else None
        if not sets:
            return
        if r["original"]:
            o = json.loads(r["original"])
            cur = {**r, **sets}
            changed = (cur["title"] != o["title"] or (cur["servings"] or 0) != (o["servings"] or 0)
                       or _lines(json.loads(cur["ingredients"])) != o["ingredients"]
                       or _lines(json.loads(cur["steps"])) != o["steps"])
            sets["edited"] = 1 if changed else 0
        if any(k in sets for k in ("title", "servings", "ingredients", "steps", "kcal", "notes", "cuisine", "category")):
            sets["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.store.execute(f"UPDATE recipes_recipe SET {cols} WHERE id=?", (*sets.values(), rid))

    def tidy_titles(self, clean) -> int:
        """Imported titles with marketing tails ("… : Ricetta originale e Consigli"): clean the
        title and the stored original alike, so it isn't shown as an edit. Returns how many."""
        n = 0
        for r in self.store.query("SELECT id, title, original FROM recipes_recipe WHERE source_url != ''"):
            new = clean(r["title"])
            if not new or new == r["title"]:
                continue
            orig = r["original"]
            if orig:
                o = json.loads(orig)
                if o.get("title") == r["title"]:
                    o["title"] = new
                orig = json.dumps(o, ensure_ascii=False)
            self.store.execute("UPDATE recipes_recipe SET title=?, original=? WHERE id=?", (new, orig, r["id"]))
            n += 1
        return n

    def reset(self, rid: int) -> None:
        r = self._row(rid)
        if not r["original"]:
            raise ValueError(_("This recipe has no original to go back to"))
        o = json.loads(r["original"])
        self.store.execute(
            "UPDATE recipes_recipe SET title=?, servings=?, ingredients=?, steps=?, kcal=?, edited=0, updated=? WHERE id=?",
            (o["title"], o["servings"], json.dumps(o["ingredients"]), json.dumps(o["steps"]), o.get("kcal"),
             time.time(), rid))

    def delete(self, rid: int) -> None:
        r = self._row(rid)
        self.store.execute("DELETE FROM recipes_recipe WHERE id=?", (rid,))
        if r["image"] and not self.store.query("SELECT 1 FROM recipes_recipe WHERE image=?", (r["image"],)):
            (self.img_dir / r["image"]).unlink(missing_ok=True)

    # -- shopping list ---------------------------------------------------------------

    def _shop_item(self, r: dict) -> dict:
        qty, unit = r["qty"], r["unit"]
        text = r["name"]
        if qty is not None:
            q, u = parse.display_unit(qty, unit)
            text = parse._compose(q, None, u, r["name"], "", "it" if self.lang == "it" else "en")
        return {"id": r["id"], "name": r["name"], "qty": qty, "unit": unit, "text": text, "checked": bool(r["checked"]),
                "recipes": json.loads(r["recipes"]), "subs": nutrition.substitutions(r["name"], self.lang)}

    def shopping(self) -> list[dict]:
        return [self._shop_item(r) for r in self.store.query(
            "SELECT * FROM recipes_shop ORDER BY checked, created, id")]

    def _shop_merge(self, name: str, key: str, qty: float | None, unit: str, recipe_title: str = "") -> None:
        if qty is not None:
            qty, unit = parse.normalize_unit(qty, unit)
        rows = self.store.query("SELECT * FROM recipes_shop WHERE key=? AND unit=? AND checked=0", (key, unit))
        titles = [recipe_title] if recipe_title else []
        if rows:
            r = rows[0]
            old = json.loads(r["recipes"])
            new_qty = (r["qty"] or 0) + qty if qty is not None and r["qty"] is not None else (r["qty"] if qty is None else qty)
            self.store.execute("UPDATE recipes_shop SET qty=?, recipes=? WHERE id=?",
                               (new_qty, json.dumps(old + [t for t in titles if t not in old]), r["id"]))
        else:
            self.store.execute("INSERT INTO recipes_shop(name, key, qty, unit, recipes, created) VALUES(?,?,?,?,?,?)",
                               (name, key, qty, unit, json.dumps(titles), time.time()))

    def shop_add_recipe(self, rid: int, servings: float | None = None) -> int:
        r = self._row(rid)
        base = r["servings"] or self.default_servings
        factor = (servings or base) / base
        n = 0
        for line in json.loads(r["ingredients"]):
            if line.startswith("# "):
                continue
            p = parse.parse_ingredient(line)
            if not p["name"]:
                continue
            qty = p["qty"] * factor if p["qty"] is not None else None
            if qty is not None and p["qty2"]:
                qty = (p["qty"] + p["qty2"]) / 2 * factor
            self._shop_merge(p["name"][:1].upper() + p["name"][1:], p["key"], qty, p["unit"], r["title"])
            n += 1
        return n

    def shop_add_manual(self, name: str, qty=None, unit: str = "") -> None:
        name = name.strip()
        if not name:
            raise ValueError(_("A name is required"))
        p = parse.parse_ingredient(name) if qty is None else None
        if p and p["qty"] is not None:  # "2 kg patate" typed into the box
            self._shop_merge(p["name"][:1].upper() + p["name"][1:], p["key"], p["qty"], p["unit"])
        else:
            u = parse.UNIT_ALIASES.get(unit.lower(), unit) if unit else ""
            self._shop_merge(name, parse.norm_key(name), float(qty) if qty not in (None, "") else None, u)

    def shop_patch(self, sid: int, patch: dict) -> None:
        sets = {}
        if "checked" in patch:
            sets["checked"] = 1 if patch["checked"] else 0
        if "name" in patch and str(patch["name"]).strip():
            sets["name"] = str(patch["name"]).strip()
            sets["key"] = parse.norm_key(sets["name"])
        if "qty" in patch:
            sets["qty"] = float(patch["qty"]) if patch["qty"] not in (None, "") else None
        if not sets:
            return
        cur = self.store.execute(
            f"UPDATE recipes_shop SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), sid))
        if not cur.rowcount:
            raise KeyError("item not found")

    def shop_delete(self, sid: int) -> None:
        self.store.execute("DELETE FROM recipes_shop WHERE id=?", (sid,))

    def shop_clear(self, everything: bool = False) -> None:
        self.store.execute("DELETE FROM recipes_shop" + ("" if everything else " WHERE checked=1"))

    # -- food log --------------------------------------------------------------------

    def log_add(self, day: str | None = None, recipe_id: int | None = None, servings: float | None = None,
                name: str = "", kcal=None) -> int:
        day = day or today()
        if not DAY.match(day):
            raise ValueError("day must be YYYY-MM-DD")
        est = 0
        if recipe_id is not None:
            v = self.view(int(recipe_id))
            sv = float(servings or 1)
            per = float(kcal) if kcal not in (None, "") else v["kcal"]
            if per is None:
                raise ValueError(_("No calories known for this recipe: enter them by hand"))
            total, name, est = per * sv, name or v["title"], int(v["kcal_est"] and kcal in (None, ""))
        else:
            if not name.strip():
                raise ValueError(_("A name is required"))
            if kcal in (None, ""):
                raise ValueError(_("Calories are required"))
            total, sv = float(kcal), None
        cur = self.store.execute(
            "INSERT INTO recipes_log(day, ts, name, kcal, recipe_id, servings, est) VALUES(?,?,?,?,?,?,?)",
            (day, time.time(), name.strip(), round(total), recipe_id, sv, est))
        return cur.lastrowid

    def log_delete(self, lid: int) -> None:
        self.store.execute("DELETE FROM recipes_log WHERE id=?", (lid,))

    def log(self, day: str | None = None) -> dict:
        day = day or today()
        if not DAY.match(day):
            raise ValueError("day must be YYYY-MM-DD")
        rows = self.store.query("SELECT * FROM recipes_log WHERE day=? ORDER BY ts, id", (day,))
        return {"day": day, "goal": self.goal, "total": sum(r["kcal"] for r in rows),
                "entries": [{"id": r["id"], "name": r["name"], "kcal": r["kcal"], "servings": r["servings"],
                             "recipe_id": r["recipe_id"], "est": bool(r["est"]),
                             "time": time.strftime("%H:%M", time.localtime(r["ts"]))} for r in rows]}

    def today_total(self) -> float:
        rows = self.store.query("SELECT COALESCE(SUM(kcal), 0) AS t FROM recipes_log WHERE day=?", (today(),))
        return rows[0]["t"]

    def nutrition(self, days: int = 7) -> dict:
        now = time.time()
        out = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            t = self.store.query("SELECT COALESCE(SUM(kcal), 0) AS t FROM recipes_log WHERE day=?", (d,))[0]["t"]
            out.append({"day": d, "kcal": t})
        return {"today": out[-1]["kcal"], "goal": self.goal, "days": out}
