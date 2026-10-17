"""Pantry + "cook with what we have": matching, ranking, persistence, shopping hook, API shape. No network.
Run: python tests/test_pantry.py"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes import RecipesPlugin, pantry as pm  # noqa: E402
from hub.plugins.recipes.db import Book  # noqa: E402
from hub.store import Store  # noqa: E402

E = pm.entity


def test_plural_folding_and_accents():
    for a, b in [("zucchina", "zucchine"), ("uovo", "uova"), ("pomodoro", "pomodori"), ("cipolla", "cipolle"),
                 ("patata", "patate"), ("carota", "carote"), ("fungo", "funghi"), ("aglio", "agli"),
                 ("peperone", "peperoni"), ("pesce", "pesci"), ("Più Pomodori", "piu pomodoro")]:
        assert E(a) == E(b), (a, b)
    assert E("Èrbe") == E("erbe")


def test_quantities_and_english():
    assert pm.pretty("200 g spaghetti") == "spaghetti"
    assert pm.pretty("2 kg Patate (nuove)") == "patate"
    assert E("eggs") == E("uova") and E("onions") == E("cipolle") and E("tomatoes") == E("pomodori")
    assert E("bell pepper") == E("peperoni")


def test_synonyms_and_containment():
    assert E("grana") == E("parmigiano") == E("Parmigiano Reggiano") == E("grana padano")
    assert E("panna fresca") == E("panna da cucina") == E("panna")
    assert pm.match(E("funghi"), E("funghi porcini")) == "exact"
    assert pm.match(E("funghi porcini"), E("funghi")) == "exact"
    assert pm.match(E("penne"), E("pasta")) == "exact" and pm.match(E("pasta"), E("rigatoni")) == "exact"
    assert pm.match(E("penne"), E("spaghetti")) == "near"
    assert pm.match(E("guanciale"), E("pancetta")) == "near"
    assert pm.match(E("latte"), E("panna")) is None


def test_staples():
    for s in ("sale", "Pepe nero", "olio extravergine di oliva", "acqua", "zucchero", "aceto balsamico"):
        assert pm.is_staple(E(s)), s
    assert not pm.is_staple(E("farina")) and not pm.is_staple(E("aglio"))


def test_analyse_optional_and_staples():
    r = pm.analyse(["200 g spaghetti", "2 uova", "sale q.b.", "Olio extravergine 2 cucchiai", "Basilico q.b.",
                    "100 g guanciale", "50 g pecorino"], ["pasta", "uova", "pancetta"], [])
    assert r["have"] == ["spaghetti", "uova"], r
    assert r["missing"] == ["pecorino"] and r["substitutes"] == [{"have": "pancetta", "need": "guanciale"}], r
    assert r["typed_hits"] == 3


def mkbook():
    tmp = Path(tempfile.mkdtemp())
    return Book(Store(tmp / "hub.db"), tmp, {"language": "it"}), tmp


def test_ranking_fewest_missing_first():
    b, _ = mkbook()
    b.add({"title": "Frittata", "ingredients": ["4 uova", "2 zucchine", "sale q.b."]})
    b.add({"title": "Carbonara", "ingredients": ["320 g spaghetti", "4 uova", "150 g guanciale", "pecorino"]})
    b.add({"title": "Zucchine ripiene", "ingredients": ["4 zucchine", "uova", "parmigiano", "pangrattato"]})
    b.add({"title": "Brodo", "ingredients": ["1 carota", "sedano"]})
    res = pm.cook_with(b, ["zucchine", "uova"], [])
    assert [r["title"] for r in res] == ["Frittata", "Zucchine ripiene", "Carbonara"], res
    assert res[0]["missing"] == [] and res[1]["missing"] == ["parmigiano", "pangrattato"]
    res = pm.cook_with(b, ["zucchine"], ["uova", "grana", "pangrattato"])
    assert res[0]["missing"] == [] and res[0]["title"] in ("Frittata", "Zucchine ripiene")
    assert {"id", "title", "image", "have", "missing", "substitutes", "score"} <= set(res[0])
    assert pm.cook_with(b, [], ["uova"]) == []


def test_pantry_persistence():
    b, tmp = mkbook()
    p = pm.Pantry(b.store)
    assert p.items() == []
    p.put(["Uova", "2 kg patate"])
    assert pm.Pantry(Store(tmp / "hub.db")).items() == ["uova", "patate"]
    p.add(["uova", "Zucchine, farina"])
    assert p.items() == ["uova", "patate", "zucchine", "farina"]
    p.remove(["patata"])
    assert p.items() == ["uova", "zucchine", "farina"]


class Req:
    def __init__(self, body=None, **match):
        self._b, self.match_info, self.query = body, match, {}

    async def json(self):
        return self._b


def test_api_and_shopping_hook():
    tmp = Path(tempfile.mkdtemp())
    hub = SimpleNamespace(store=Store(tmp / "hub.db"), data_dir=tmp, every=lambda *a, **k: None,
                          publish=lambda *a, **k: None)
    plug = RecipesPlugin(hub, {})
    asyncio.run(plug.start())

    def run(c):
        return json.loads(asyncio.run(c).text)

    plug.book.add({"title": "Frittata", "ingredients": ["4 uova", "2 zucchine"]})
    plug.book.shop_add_manual("Parmigiano")
    sid = plug.book.shopping()[0]["id"]
    run(plug.r_shop_patch(Req({"checked": False}, id=str(sid))))
    assert run(plug.r_pantry(Req()))["items"] == []
    run(plug.r_shop_patch(Req({"checked": True}, id=str(sid))))
    assert run(plug.r_pantry(Req()))["items"] == ["parmigiano"]
    plug.config["pantry_from_shopping"] = False
    plug.book.shop_add_manual("Latte")
    lid = [x for x in plug.book.shopping() if x["name"] == "Latte"][0]["id"]
    run(plug.r_shop_patch(Req({"checked": True}, id=str(lid))))
    assert run(plug.r_pantry(Req()))["items"] == ["parmigiano"]
    assert run(plug.r_pantry_put(Req({"items": ["uova"]})))["items"] == ["uova"]
    assert run(plug.r_pantry_add(Req({"items": ["zucchine"]})))["items"] == ["uova", "zucchine"]
    assert run(plug.r_pantry_remove(Req({"items": ["uova"]})))["items"] == ["zucchine"]
    out = run(plug.r_cook_with(Req({"ingredients": ["uova"], "use_pantry": True})))["results"]
    assert out[0]["title"] == "Frittata" and out[0]["missing"] == []
    out = run(plug.r_cook_with(Req({"ingredients": ["uova"], "use_pantry": False})))["results"]
    assert out[0]["missing"] == ["zucchine"]
    ing = run(plug.r_ingredients(Req()))["items"]
    assert "zucchine" in ing and "farina" in ing


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
