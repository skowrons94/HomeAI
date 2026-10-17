"""Recipes plugin: page parsing, ingredient parsing/scaling, shopping merge, calories, food log.
No network; uses a temp-dir Store. Run: python tests/test_recipes.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes import nutrition, parse, sources  # noqa: E402
from hub.plugins.recipes.db import Book, today  # noqa: E402
from hub.store import Store  # noqa: E402

GRAPH_PAGE = """<html lang="en"><head><meta property="og:image" content="https://x.test/og.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"Site"},
 {"@type":["Recipe","Thing"],"name":"Pasta &amp; Beans","image":{"@type":"ImageObject","url":"https://x.test/p.jpg"},
  "recipeYield":["4","4 servings"],"prepTime":"PT15M","cookTime":"PT1H5M",
  "recipeIngredient":["200g pasta","1 x 400g can cannellini beans","2 tbsp olive oil","salt, to taste"],
  "recipeInstructions":[{"@type":"HowToSection","name":"Sauce","itemListElement":[
     {"@type":"HowToStep","text":"Fry the onion."},{"@type":"HowToStep","text":"Add beans."}]},
   {"@type":"HowToSection","name":"Finish","itemListElement":[{"@type":"HowToStep","text":"Toss with pasta."}]}],
  "nutrition":{"@type":"NutritionInformation","calories":"456 kcal"},"recipeCategory":["Dinner"],"recipeCuisine":"Italian"}]}
</script></head><body></body></html>"""

LIST_PAGE = """<script type="application/ld+json">[{"@type":"Recipe","name":"Torta","recipeYield":8,
"recipeIngredient":["Farina 00 250 g","Uova (medie) 3","Zucchero 150 g"],
"recipeInstructions":"Mescolate tutto.\\nInfornate a 180 gradi.","nutrition":{"calories":"358,7 kcal"},
"totalTime":"PT50M"}]</script>"""

MICRO_PAGE = """<div itemscope itemtype="http://schema.org/Recipe"><h1 itemprop="name">Micro Soup</h1>
<img itemprop="image" src="https://x.test/soup.jpg"><meta itemprop="prepTime" content="PT10M">
<span itemprop="recipeYield">6 servings</span>
<ul><li itemprop="recipeIngredient">1 carrot</li><li itemprop="recipeIngredient">500 ml stock</li></ul>
<div itemprop="recipeInstructions"><p>Chop.</p><p>Simmer.</p></div>
<span itemprop="nutrition" itemscope itemtype="http://schema.org/NutritionInformation">
<span itemprop="calories">120 calories</span></span></div>"""


def book(config=None):
    tmp = Path(tempfile.mkdtemp())
    return Book(Store(tmp / "hub.db"), tmp, config or {"language": "it", "daily_kcal_goal": 1800})


def test_jsonld_graph_and_sections():
    r = parse.parse_page(GRAPH_PAGE, "https://www.example.com/r")
    assert r["title"] == "Pasta & Beans" and r["image_url"] == "https://x.test/p.jpg"
    assert r["servings"] == 4 and r["prep_min"] == 15 and r["cook_min"] == 65 and r["total_min"] == 80
    assert r["kcal"] == 456 and r["category"] == "Dinner" and r["cuisine"] == "Italian"
    assert r["steps"] == ["# Sauce", "Fry the onion.", "Add beans.", "# Finish", "Toss with pasta."]
    assert len(r["ingredients"]) == 4 and r["site"] == "example.com"


def test_jsonld_list_and_string_steps():
    r = parse.parse_page(LIST_PAGE, "https://ricette.giallozafferano.it/x.html")
    assert r["servings"] == 8 and r["kcal"] == 359 and r["total_min"] == 50
    assert r["steps"] == ["Mescolate tutto.", "Infornate a 180 gradi."]


def test_gz_footnotes_removed():
    page = ('<script type="application/ld+json">{"@type":"Recipe","name":"A","recipeIngredient":["x 1 g"],'
            '"recipeInstructions":["Tagliate a strisce spesse circa 1cm 2 3 . Poi cuocete."]}</script>')
    r = parse.parse_page(page, "https://ricette.giallozafferano.it/a.html")
    assert r["steps"] == ["Tagliate a strisce spesse circa 1cm. Poi cuocete."], r["steps"]


def test_microdata_fallback():
    r = parse.parse_page(MICRO_PAGE, "https://x.test/soup")
    assert r["title"] == "Micro Soup" and r["servings"] == 6 and r["prep_min"] == 10
    assert r["ingredients"] == ["1 carrot", "500 ml stock"] and r["steps"] == ["Chop.", "Simmer."]
    assert r["kcal"] == 120 and r["image_url"] == "https://x.test/soup.jpg"


def test_no_recipe():
    assert parse.parse_page("<html><body>hello</body></html>", "https://x.test") is None


def test_ingredient_parsing():
    P = parse.parse_ingredient
    cases = {
        "Spaghetti 320 g": (320, "g", "Spaghetti"), "200g pancetta, cubed": (200, "g", "pancetta"),
        "1 ½ cups flour": (1.5, "cup", "flour"), "2 cucchiai di olio": (2, "tbsp", "olio"),
        "1 spicchio d'aglio": (1, "clove", "aglio"), "Uova (medie) 3": (3, "", "Uova"),
        "400g/14oz can chopped tomatoes": (400, "g", "chopped tomatoes"), "3 large eggs": (3, "", "large eggs"),
        "1/2 tsp salt": (0.5, "tsp", "salt"), "un pizzico di sale": (1, "pinch", "sale"),
        "500 ml latte": (500, "ml", "latte"),
    }
    for line, (q, u, n) in cases.items():
        p = P(line)
        assert (p["qty"], p["unit"], p["name"]) == (q, u, n), (line, p)
    p = P("Pepe nero q.b.")
    assert p["qty"] is None and p["taste"] and p["name"] == "Pepe nero"
    assert P("salt, to taste")["taste"]
    p = P("2-3 cucchiai di olio")
    assert (p["qty"], p["qty2"]) == (2, 3)
    assert P("uova 2 intere")["qty"] == 2


def test_scaling_and_format():
    p = parse.parse_ingredient("Spaghetti 320 g")
    assert parse.format_ingredient(p, 1, "it") == "Spaghetti 320 g"
    assert parse.format_ingredient(p, 2, "it") == "640 g di Spaghetti"
    assert parse.format_ingredient(p, 4, "it") == "1,28 kg di Spaghetti"
    p = parse.parse_ingredient("1 tbsp olive oil")
    assert parse.format_ingredient(p, 1.5, "en") == "1½ tbsp of olive oil"
    p = parse.parse_ingredient("Pepe nero q.b.")
    assert parse.format_ingredient(p, 3, "it") == "Pepe nero q.b."
    assert parse.fmt_qty(2.25) == "2¼" and parse.fmt_qty(0.5) == "½"


def test_norm_key_merges_variants():
    assert parse.norm_key("Uova") == parse.norm_key("uovo")
    assert parse.norm_key("Pomodori") == parse.norm_key("pomodoro")
    assert parse.norm_key("onions") == parse.norm_key("onion")
    assert parse.norm_key("Olio d'oliva") == parse.norm_key("olio di oliva")


def test_calorie_estimate():
    ings = [parse.parse_ingredient(x) for x in
            ["Spaghetti 320 g", "Guanciale 150 g", "Tuorli (di uova medie) 6", "Pecorino Romano DOP 50 g",
             "Pepe nero q.b."]]
    e = nutrition.estimate(ings)
    assert e["coverage"] == 1.0 and 2200 < e["kcal"] < 2800, e
    e = nutrition.estimate([parse.parse_ingredient("2 tbsp olive oil")])
    assert 220 < e["kcal"] < 270, e  # 2 tbsp = 30 ml * 0.92 g/ml * 8.84
    e = nutrition.estimate([parse.parse_ingredient("3 eggs"), parse.parse_ingredient("zzz unknown 100 g")])
    assert 200 < e["kcal"] < 270 and e["coverage"] == 0.5
    assert nutrition.lookup_food("olio extravergine di oliva")[1] == 884


def test_substitutions():
    assert any("olio" in s for s in nutrition.substitutions("Burro", "it"))
    assert any("oil" in s for s in nutrition.substitutions("butter", "en"))
    assert nutrition.substitutions("guanciale", "it")[0] == "pancetta"
    assert nutrition.substitutions("Panna da cucina", "it")
    assert nutrition.substitutions("xyzzy") == []


def test_book_import_view_edit_reset():
    b = book()
    rec = parse.parse_page(LIST_PAGE, "https://ricette.giallozafferano.it/torta.html")
    rec["lang"] = "it"
    rid = b.add(rec)
    assert b.find_by_url("https://ricette.giallozafferano.it/torta.html") == rid
    v = b.view(rid)
    assert v["servings"] == 8 and v["kcal"] == 359 and not v["kcal_est"]
    v2 = b.view(rid, servings=4)
    assert v2["ingredients"][0]["text"] == "125 g di Farina 00" and v2["servings"] == 4
    assert v2["ingredients"][2]["subs"] or v2["ingredients"][1]["subs"]
    b.update(rid, {"title": "Torta mia", "ingredients": ["Farina 00 300 g", "Uova 2"], "notes": "ottima", "fav": True,
                   "tags": "Dolci, torte,dolci"})
    v = b.view(rid)
    assert v["title"] == "Torta mia" and v["edited"] and v["notes"] == "ottima" and v["fav"]
    assert v["tags"] == ["dolci", "torte"] and v["kcal_est"] and v["kcal"] > 0  # source kcal dropped after edit
    b.reset(rid)
    v = b.view(rid)
    assert v["title"] != "Torta mia" and not v["edited"] and v["kcal"] == 359 and len(v["ingredients"]) == 3
    assert v["notes"] == "ottima" and v["fav"]  # personal data survives a reset
    assert [r["id"] for r in b.list(fav=True)] == [rid] and b.list(q="farina") and not b.list(q="pizza")
    assert b.list(tag="torte") and b.tags()[0]["tag"] in ("dolci", "torte")
    b.delete(rid)
    assert b.list() == []


def test_manual_recipe_and_kcal_estimate():
    b = book()
    rid = b.add({"title": "Pasta al burro", "servings": 2,
                 "ingredients": "200 g spaghetti\n40 g burro\n30 g parmigiano", "steps": ["Cuoci", "Mantecare"]})
    v = b.view(rid)
    assert v["kcal_est"] and 400 < v["kcal"] < 700, v["kcal"]
    assert not v["has_original"]
    try:
        b.reset(rid)
        assert False
    except ValueError:
        pass


def test_shopping_merge():
    b = book()
    r1 = b.add({"title": "A", "servings": 2, "ingredients": ["200 g farina", "2 uova", "1 spicchio d'aglio", "sale q.b."]})
    r2 = b.add({"title": "B", "servings": 4, "ingredients": ["0,3 kg Farina", "3 Uova", "500 ml latte", "1 l latte"]})
    b.shop_add_recipe(r1)
    b.shop_add_recipe(r2, servings=8)  # double
    items = {i["name"].lower(): i for i in b.shopping()}
    assert items["farina"]["qty"] == 200 + 600 and items["farina"]["text"] == "800 g di Farina", items["farina"]
    assert items["uova"]["qty"] == 2 + 6 and items["farina"]["recipes"] == ["A", "B"]
    assert items["latte"]["qty"] == 3000 and items["latte"]["text"].startswith("3 l")
    assert items["sale"]["qty"] is None
    b.shop_add_manual("2 kg patate")
    b.shop_add_manual("carta forno")
    names = [i["name"].lower() for i in b.shopping()]
    assert "patate" in names and "carta forno" in names
    fid = items["farina"]["id"]
    b.shop_patch(fid, {"checked": True})
    b.shop_add_recipe(r1)  # checked items don't absorb new quantities
    farine = [i for i in b.shopping() if i["name"].lower() == "farina"]
    assert len(farine) == 2 and [f["checked"] for f in farine].count(True) == 1
    assert items["uova"]["subs"] and b.shopping()[-1]["checked"]  # checked sort last
    b.shop_clear()
    assert all(not i["checked"] for i in b.shopping())
    b.shop_clear(everything=True)
    assert b.shopping() == []


def test_food_log_totals():
    b = book()
    rid = b.add({"title": "Torta", "servings": 8, "kcal": 400, "ingredients": ["100 g farina"]})
    b.log_add(recipe_id=rid, servings=1.5)
    b.log_add(name="Caffe", kcal=5)
    b.log_add(day="2020-01-01", name="Old", kcal=900)
    t = b.log()
    assert t["total"] == 605 and t["goal"] == 1800 and len(t["entries"]) == 2
    assert t["entries"][0]["name"] == "Torta" and t["entries"][0]["kcal"] == 600
    n = b.nutrition()
    assert n["today"] == 605 and len(n["days"]) == 7 and n["days"][-1]["day"] == today()
    assert b.log("2020-01-01")["total"] == 900
    assert b.today_total() == 605
    b.log_delete(t["entries"][1]["id"])
    assert b.today_total() == 600
    for bad in ({"name": "x"}, {"kcal": 5}, {"day": "bad", "name": "x", "kcal": 1}):
        try:
            b.log_add(**bad)
            assert False, bad
        except ValueError:
            pass


def test_mealdb_and_gz_parsers():
    m = {"idMeal": "1", "strMeal": "Carbonara", "strArea": "Italian", "strCategory": "Pasta", "strMealThumb": "u",
         "strInstructions": "STEP 1\r\n1. Boil water.\r\nFry guanciale.", "strIngredient1": "Spaghetti",
         "strMeasure1": "400g", "strIngredient2": "Egg", "strMeasure2": "3", "strIngredient3": "", "strMeasure3": " "}
    r = sources.mealdb_recipe(m)
    assert r["ingredients"] == ["400g Spaghetti", "3 Egg"] and r["steps"] == ["Boil water.", "Fry guanciale."]
    html = ('<a href="https://ricette.giallozafferano.it/Carbonara-di-mare.html" title="Carbonara di mare">'
            '<div class="gz-card-image"><img src="https://www.giallozafferano.it/images/a_360x300.jpg" /></div></a>'
            '<a href="https://ricette.giallozafferano.it/Carbonara-di-mare.html" title="Carbonara di mare"><div></div></a>')
    res = sources.gz_results(html)
    assert len(res) == 1 and res[0]["image"].endswith("360x300.jpg")


def test_check_url():
    for bad in ("ftp://x.com", "http://localhost/x", "http://192.168.1.5/x", "http://127.0.0.1:8080", "nope"):
        try:
            sources.check_url(bad)
            assert False, bad
        except ValueError:
            pass
    assert sources.check_url("https://ricette.giallozafferano.it/x.html")


def test_scaling_countable_whole_pieces():
    from hub.plugins.recipes import parse
    p = parse.parse_ingredient("3 uova")
    assert parse.format_ingredient(p, 0.5, "it").startswith("2 uova")
    p = parse.parse_ingredient("200 g farina")
    assert parse.format_ingredient(p, 0.5, "it") == "100 g di farina"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
