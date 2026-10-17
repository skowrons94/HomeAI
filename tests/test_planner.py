"""Meal planner: week normalisation, put/delete, fill rules, copy, shopping merge, today. No network.
Run: python tests/test_planner.py"""

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes.db import Book  # noqa: E402
from hub.plugins.recipes.planner import Planner, monday  # noqa: E402
from hub.store import Store  # noqa: E402


def setup(n=0, **extra):
    tmp = Path(tempfile.mkdtemp())
    b = Book(Store(tmp / "hub.db"), tmp, {"language": "it", "default_servings": 2})
    ids = []
    for i in range(n):
        ids.append(b.add({"title": f"Piatto {i}", "servings": 2, "ingredients": ["200 g farina", f"{i + 1} uova"],
                          "tags": extra.get("tags", [])[i:i + 1] or ["pasta"]}))
    return b, Planner(b), ids


def test_monday():
    assert monday("2026-10-18").isoformat() == "2026-10-12"  # a Sunday
    assert monday("2026-10-12").isoformat() == "2026-10-12"
    assert monday().weekday() == 0
    for bad in ("x", "2026-13-01", 5):
        try:
            monday(bad)
            assert False
        except ValueError:
            pass


def test_put_delete_week():
    b, p, ids = setup(2)
    s = p.put("2026-10-14", "dinner", {"recipe_id": ids[0]})
    assert s["title"] == "Piatto 0" and s["servings"] == 2 and s["recipe_id"] == ids[0]
    s = p.put("2026-10-14", "dinner", {"title": "Pizza fuori", "servings": 4, "note": "con Anna"})
    assert s["recipe_id"] is None and s["servings"] == 4 and s["note"] == "con Anna"
    w = p.week("2026-10-18")
    assert w["week"] == "2026-10-12" and len(w["days"]) == 7
    assert w["days"][2]["dinner"]["title"] == "Pizza fuori" and w["days"][2]["lunch"] is None
    for bad in ({}, {"recipe_id": 999}, {"title": "x", "servings": 0}):
        try:
            p.put("2026-10-14", "lunch", bad)
            assert False, bad
        except (ValueError, KeyError):
            pass
    try:
        p.put("2026-10-14", "brunch", {"title": "x"})
        assert False
    except ValueError:
        pass
    p.delete("2026-10-14", "dinner")
    assert p.week("2026-10-14")["days"][2]["dinner"] is None


def test_deleted_recipe_keeps_title():
    b, p, ids = setup(1)
    p.put("2026-10-14", "dinner", {"recipe_id": ids[0]})
    b.delete(ids[0])
    s = p.week("2026-10-14")["days"][2]["dinner"]
    assert s["recipe_id"] is None and s["title"] == "Piatto 0"


def test_today():
    b, p, ids = setup(1)
    d = date.today().isoformat()
    p.put(d, "lunch", {"title": "Avanzi"})
    t = p.today()
    assert t["date"] == d and t["lunch"]["title"] == "Avanzi" and t["dinner"] is None


def test_fill_rules():
    b, p, ids = setup(10)
    mon = monday("2026-10-12")
    p.put("2026-10-13", "dinner", {"title": "Pizza fuori"})
    n = p.fill("2026-10-12")
    assert n == 6
    w = p.week("2026-10-12")
    assert w["days"][1]["dinner"]["title"] == "Pizza fuori"  # not overwritten
    rids = [d["dinner"]["recipe_id"] for d in w["days"] if d["dinner"]["recipe_id"]]
    assert len(rids) == len(set(rids)) == 6
    assert all(d["lunch"] is None for d in w["days"])
    assert p.fill("2026-10-12") == 0  # nothing empty left
    # next week: no repeat of the last 14 days of the plan
    p.fill("2026-10-19")
    nxt = {d["dinner"]["recipe_id"] for d in p.week("2026-10-19")["days"]}
    assert not nxt & set(rids) or len(set(range(10)) - set(rids)) < 7


def test_fill_no_repeat_across_weeks_and_few_recipes():
    b, p, ids = setup(14)
    p.fill("2026-10-12")
    p.fill("2026-10-19")
    all_ids = [d["dinner"]["recipe_id"] for wk in ("2026-10-12", "2026-10-19") for d in p.week(wk)["days"]]
    assert len(all_ids) == len(set(all_ids)) == 14
    b, p, ids = setup(3)
    assert p.fill("2026-10-12") == 3
    assert setup(0)[1].fill("2026-10-12") == 0


def test_fill_prefers_favourites():
    b, p, ids = setup(8)
    for rid in ids[:3]:
        b.update(rid, {"fav": True})
    p.fill("2026-10-12", ["dinner"])
    got = {d["dinner"]["recipe_id"] for d in p.week("2026-10-12")["days"] if d["dinner"]}
    assert set(ids[:3]) <= got


def test_fill_variety_by_tag():
    b, p, ids = setup(8, tags=["fish"] * 4 + ["veg"] * 4)
    p.fill("2026-10-12")
    tags = [b.view(d["dinner"]["recipe_id"])["tags"][0] for d in p.week("2026-10-12")["days"]]
    assert all(a != c for a, c in zip(tags, tags[1:])), tags


def test_copy():
    b, p, ids = setup(2)
    p.put("2026-10-12", "dinner", {"recipe_id": ids[0], "servings": 4})
    p.put("2026-10-13", "lunch", {"title": "Avanzi"})
    p.put("2026-10-19", "dinner", {"recipe_id": ids[1]})  # already filled in the target
    assert p.copy("2026-10-12", "2026-10-21") == 1
    w = p.week("2026-10-19")
    assert w["days"][0]["dinner"]["recipe_id"] == ids[1]  # kept
    assert w["days"][1]["lunch"]["title"] == "Avanzi"


def test_shopping_merge_and_scaling():
    b, p, ids = setup(2)
    p.put("2026-10-12", "dinner", {"recipe_id": ids[0], "servings": 4})  # x2
    p.put("2026-10-13", "dinner", {"recipe_id": ids[0]})                 # x1
    p.put("2026-10-14", "dinner", {"title": "Pizza fuori"})              # ignored
    assert p.shopping("2026-10-12") == 4
    items = {i["name"].lower(): i for i in b.shopping()}
    assert abs(items["farina"]["qty"] - 600) < 1e-6, items["farina"]
    assert len(b.shopping()) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
