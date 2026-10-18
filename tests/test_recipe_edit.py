"""Recipe editing: editor rows <-> stored lines round trip, permanent rescale rounding.
Run: python tests/test_recipe_edit.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes import parse  # noqa: E402
from hub.plugins.recipes.db import Book  # noqa: E402
from hub.store import Store  # noqa: E402

LINES = ["200 g farina 00", "3 uova", "½ cipolla", "olio extravergine q.b.", "2-3 spicchi d'aglio", "1 kg patate",
         "2 cucchiai di olio", "Spaghetti 320 g", "1½ tazze di latte", "250 ml latte (intero)", "sale", "basilico, qualche foglia",
         "1 pizzico di sale", "3 foglie di alloro", "1,5 l brodo", "400 g pomodori pelati, a pezzetti", "salsa di soia"]
KEYS = ("qty", "qty2", "unit", "name", "note", "taste")


def same(a, b):
    pa, pb = parse.parse_ingredient(a), parse.parse_ingredient(b)
    assert all(pa[k] == pb[k] for k in KEYS), (a, b, pa, pb)


def test_roundtrip_untouched_keeps_text():
    for l in LINES:
        assert parse.line_from_row(parse.row_from_line(l)) == l
    assert parse.line_from_row(parse.row_from_line("# Per la salsa")) == "# Per la salsa"


def test_roundtrip_without_raw_loses_nothing():
    for l in LINES:
        row = parse.row_from_line(l)
        row.pop("raw")
        same(parse.line_from_row(row), l)
        for lang in ("it", "en"):
            row2 = dict(row)
            same(parse.line_from_row(row2, lang), l)


def test_edited_row_is_rewritten():
    row = parse.row_from_line("200 g farina 00")
    row["qty"] = "1,5"
    row["unit"] = "kg"
    assert parse.line_from_row(row) == "1½ kg di farina 00"
    row = parse.row_from_line("sale q.b.")
    assert row["taste"] and parse.line_from_row({**row, "raw": ""}) == "sale q.b."
    assert parse.rows_to_lines([{"name": " "}, {"heading": ""}, {"heading": "Base"}, "x"]) == ["# Base", "x"]


def test_rescale_halve():
    f = parse.rescale_line
    assert f("200 g farina", .5) == "100 g di farina"
    assert f("125 g burro", .5) == "65 g di burro"
    assert f("30 g parmigiano", .5) == "15 g di parmigiano"
    assert f("3 g lievito", .5) == "2 g di lievito"
    assert f("1 g sale", .5) == "1 g di sale"  # never 0
    assert f("1 kg patate", .5) == "500 g di patate"
    assert f("2 kg patate", .5) == "1 kg di patate"
    assert f("1 cucchiaio di olio", .5) == "½ cucchiaio di olio"
    assert f("3 cucchiai di olio", .5) == "1½ cucchiai di olio"
    assert f("1 cucchiaino di sale", .5) == "½ cucchiaino di sale"
    assert f("4 uova", .5) == "2 uova" and f("3 uova", .5) == "2 uova" and f("1 uovo", .5) == "½ uovo"
    assert f("2 spicchi d'aglio", .5) == "1 spicchio di aglio"
    assert f("½ cipolla", .5) == "½ cipolla"  # not 1/4, not 0


def test_rescale_other_factors():
    f = parse.rescale_line
    assert f("400 g pasta", .75) == "300 g di pasta"       # 4 -> 3
    assert f("250 g riso", .75) == "190 g di riso"
    assert f("6 uova", 2 / 6) == "2 uova"                    # 6 -> 2
    assert f("600 g carne", 2 / 6) == "200 g di carne"
    assert f("4 uova", 3 / 4) == "3 uova"
    assert f("2 uova", 2) == "4 uova"
    assert f("100 g farina", 1) == "100 g farina"            # unchanged text when factor is 1


def test_rescale_untouched_lines():
    f = parse.rescale_line
    assert f("olio q.b.", .5) == "olio q.b."
    assert f("sale", .5) == "sale"
    assert f("# Per la salsa", .5) == "# Per la salsa"
    assert f("un po' di prezzemolo", .5) == "un po' di prezzemolo"


def test_rescale_ranges():
    f = parse.rescale_line
    assert f("2-3 spicchi d'aglio", .5) == "1-2 spicchi di aglio"
    assert f("2-3 cucchiai di olio", .5) == "1-1½ cucchiai di olio"
    assert f("4-5 uova", .5) == "2-3 uova"
    assert f("2-3 uova", .5) == "1-2 uova"
    assert f("1-2 spicchi d'aglio", .5) == "½-1 spicchio di aglio"


def test_book_update_with_rows_and_reset():
    tmp = Path(tempfile.mkdtemp())
    b = Book(Store(tmp / "hub.db"), tmp, {"language": "it", "default_servings": 2})
    rid = b.add({"title": "Torta", "servings": 4, "ingredients": ["400 g farina", "4 uova", "sale q.b.", "# Crema", "½ cipolla"],
                 "steps": ["Mescolate.", "Infornate."], "lang": "it", "source_url": "https://x.test/t"})
    v = b.view(rid)
    assert [r.get("heading") or r["name"] for r in v["rows"]] == ["farina", "uova", "sale", "Crema", "cipolla"]
    rows = parse.row_from_line  # noqa: F841
    new = [{**r} for r in v["rows"]]
    lines = parse.rescale_lines(v["ingredient_lines"], .5)
    b.update(rid, {"servings": 2, "ingredients": [parse.row_from_line(x) for x in lines]})
    v = b.view(rid)
    assert v["ingredient_lines"] == ["200 g di farina", "2 uova", "sale q.b.", "# Crema", "½ cipolla"] or \
        v["ingredient_lines"][:2] == ["200 g di farina", "2 uova"]
    assert v["edited"] and v["base_servings"] == 2 and new
    b.reset(rid)
    v = b.view(rid)
    assert v["ingredient_lines"][0] == "400 g farina" and v["base_servings"] == 4 and not v["edited"]


def test_suggest_themes_and_pick():
    import asyncio
    from hub.plugins.recipes import suggest
    assert suggest.theme_for(7)[0] == "breakfast" and suggest.theme_for(12)[0] == "lunch"
    assert suggest.theme_for(16)[0] == "snack" and suggest.theme_for(20)[0] == "dinner"
    assert suggest.theme_for(23)[0] == "quick" and suggest.theme_for(3)[0] == "quick"
    assert "zucca" in suggest.words_for(20, 10)[2]
    tmp = Path(tempfile.mkdtemp())
    b = Book(Store(tmp / "hub.db"), tmp, {"language": "it"})
    b.add({"title": "Torta di mele", "servings": 4, "ingredients": ["2 mele"], "steps": ["x"]})

    async def fake(http, q, names, lang):
        return {"results": [{"title": f"{q} {i}", "url": f"https://x.test/{q}{i}", "image": "", "source": "x"} for i in range(5)]}

    a = asyncio.run(suggest.suggest(b, None, [], "it", 7, 10, "s1", fake))
    c = asyncio.run(suggest.suggest(b, None, [], "it", 7, 10, "s1", fake))
    assert a == c and len(a["items"]) == 3 and a["theme"] == "breakfast"
    assert any(i.get("recipe_id") for i in a["items"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
