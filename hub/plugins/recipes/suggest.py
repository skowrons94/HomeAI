"""Ideas for the empty Discover tab: a theme from the hour of day (plus a touch of season)."""

from __future__ import annotations

import random

from . import parse
from ...i18n import _

# One place for every keyword list. (first hour, theme id, label, search words)
HOURS = [
    (6, "breakfast", "Breakfast", ["torta", "biscotti", "pancake", "crostata", "muffin"]),
    (11, "lunch", "Lunch", ["pasta", "risotto", "insalata", "gnocchi", "lasagne"]),
    (15, "snack", "Snack", ["ciambellone", "dolce", "plumcake", "biscotti", "torta"]),
    (18, "dinner", "Dinner", ["secondo", "zuppa", "pizza", "frittata", "arrosto"]),
    (22, "quick", "Something quick", ["veloce", "uova", "insalata", "toast"]),
]
SEASON = {  # month -> extra words
    12: ["zampone", "panettone", "lenticchie"], 1: ["minestrone", "cavolo", "arancia"], 2: ["carciofi", "frittelle", "broccoli"],
    3: ["asparagi", "carciofi", "spinaci"], 4: ["asparagi", "piselli", "fave"], 5: ["fragole", "piselli", "zucchine"],
    6: ["insalata", "pesto", "melanzane"], 7: ["insalata", "pesto", "pomodori"], 8: ["pomodori", "melanzane", "peperoni"],
    9: ["funghi", "uva", "fichi"], 10: ["zucca", "funghi", "castagne"], 11: ["zucca", "castagne", "cavolfiore"],
}


def theme_for(hour: int) -> tuple[str, str, list[str]]:
    cur = HOURS[-1]
    for h in HOURS:
        if hour >= h[0]:
            cur = h
    if hour < HOURS[0][0]:
        cur = HOURS[-1]
    return cur[1], cur[2], cur[3]


def words_for(hour: int, month: int) -> tuple[str, str, list[str]]:
    tid, label, words = theme_for(hour)
    return tid, label, words + SEASON.get(month, [])


def pick_saved(items: list[dict], words: list[str], rng: random.Random, n: int = 1) -> list[dict]:
    """Saved recipes whose title, tags or category mention one of the words."""
    ws = [parse.strip_accents(w.lower()) for w in words]
    hits = []
    for r in items:
        hay = parse.strip_accents(" ".join([r["title"], r.get("category") or "", " ".join(r.get("tags") or [])]).lower())
        if any(w in hay for w in ws):
            hits.append(r)
    rng.shuffle(hits)
    return hits[:n]


async def suggest(book, http, source_names: list[str], lang: str, hour: int, month: int, seed: str, search) -> dict:
    tid, label, words = words_for(hour, month)
    rng = random.Random(seed)
    items = []
    for r in pick_saved(book.list(), words, rng, 1):
        items.append({"recipe_id": r["id"], "title": r["title"], "image": r["image"], "source": r.get("site") or _("Your recipes"),
                      "lang": r.get("lang") or lang})
    pool = list(words)
    rng.shuffle(pool)
    errors = 0
    for q in pool[:2]:
        if len(items) >= 3:
            break
        try:
            res = await search(http, q, source_names, lang)
        except Exception:
            errors += 1
            continue
        seen = {x["title"].lower() for x in items}
        cand = [x for x in res.get("results", []) if x["title"].lower() not in seen]
        rng.shuffle(cand)
        for x in cand[: 3 - len(items)]:
            sid = book.find_by_url(x["url"])
            items.append({"title": x["title"], "image": x.get("image", ""), "url": x["url"], "mealdb": x.get("mealdb"),
                          "source": x.get("source") or x.get("subtitle") or x.get("site", ""), "lang": x.get("lang") or lang,
                          **({"recipe_id": sid} if sid else {})})
    return {"theme": tid, "label": _(label), "items": items[:3]}
