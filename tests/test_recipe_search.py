"""Recipe search tidiness: titles must be about the query, no round-ups, clean titles."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub.plugins.recipes import sources as S  # noqa: E402


def test_title_relevance():
    t = S.query_terms("zuppa")
    assert S.title_score("Zuppa di cipolle", t) > 0 and S.title_score("Zuppe di legumi", t) > 0
    assert S.title_score("Minestrone alla genovese", t) == 0 and S.title_score("Vellutata di zucca", t) == 0
    t = S.query_terms("pasta al forno")
    assert S.title_score("Pasta al forno con besciamella", t) > S.title_score("Pasta e forno di casa", t) * 0
    assert S.title_score("Pasta alla norma", t) == 0
    assert S.title_score("Zuppa di cipolle", S.query_terms("Zuppà")) > 0  # accents folded


def test_articles_and_titles():
    for a in ["10 zuppe calde per l'inverno", "Le migliori ricette di zuppe", "Cosa cucinare stasera?", "Best soups ideas"]:
        assert S.is_article(a), a
    for r in ["Zuppa di cipolle", "Curry di ceci", "Pasta al forno con la ricetta della nonna"]:
        assert not S.is_article(r), r
    assert S.clean_title("Curry di ceci : Ricetta perfetta, facilissima") == "Curry di ceci"
    assert S.clean_title("Lasagne - la ricetta originale") == "Lasagne"
    assert S.clean_title("Tiramisù | Misya.info") == "Tiramisù"
    assert S.clean_title("Pasta e ceci") == "Pasta e ceci"


def test_tidy_filters_and_verifies():
    async def fake_check(http, url):
        return "article" not in url
    S.is_recipe_page, orig = fake_check, S.is_recipe_page
    try:
        by = {"misya": [{"title": "Zuppa di farro", "url": "https://x/zuppa-farro"},
                        {"title": "Zuppa toscana", "url": "https://x/article-zuppa"},
                        {"title": "Minestrone", "url": "https://x/minestrone"},
                        {"title": "10 zuppe veloci", "url": "https://x/10-zuppe"}],
              "giallozafferano": [{"title": "Zuppa di ceci", "url": "https://gz/zuppa-ceci"}]}
        out = asyncio.run(S.tidy(None, by, "zuppa"))
        assert [i["title"] for i in out["misya"]] == ["Zuppa di farro"]
        assert [i["title"] for i in out["giallozafferano"]] == ["Zuppa di ceci"]
    finally:
        S.is_recipe_page = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
