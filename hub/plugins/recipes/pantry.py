"""Pantry ("Dispensa") and "what can I cook with what I have": Italian-aware ingredient matching.

Everything here is deterministic and offline. ``entity(name)`` folds a free-text ingredient into a set of
stems (accents, quantities, plurals, fillers, synonyms and English names removed); ``match`` compares two
entities and says whether it is an exact hit, a "near" substitute, or nothing.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date

from ...i18n import _
from . import parse

KV = "recipes.pantry"

# -- words that carry no identity ---------------------------------------------------------------
_FILLER = {
    "di", "d", "da", "del", "della", "dello", "dei", "delle", "degli", "al", "alla", "allo", "ai", "alle", "all",
    "il", "lo", "la", "le", "i", "gli", "un", "una", "uno", "in", "con", "e", "o", "per", "a", "the", "of", "and",
    "or", "fresco", "fresca", "freschi", "fresche", "fresh", "tritato", "tritata", "tritati", "tritate", "chopped",
    "sbucciato", "sbucciata", "grattugiato", "grattugiata", "grattugiati", "grated", "medio", "media", "medie",
    "medi", "grande", "grandi", "piccolo", "piccola", "piccoli", "piccole", "large", "small", "medium", "intero",
    "intera", "interi", "intere", "whole", "maturo", "matura", "maturi", "mature", "ripe", "biologico", "bio",
    "facoltativo", "opzionale", "optional", "qb", "q", "b", "circa", "about", "pezzi", "pezzo", "cubetti",
    "fette", "fetta", "sliced", "diced", "peeled", "cucina", "cooking", "cut",
}

# -- English -> Italian (single words, singular and plural spelled out) -------------------------
_EN = {
    "egg": "uovo", "eggs": "uovo", "onion": "cipolla", "onions": "cipolla", "potato": "patata",
    "potatoes": "patata", "tomato": "pomodoro", "tomatoes": "pomodoro", "carrot": "carota", "carrots": "carota",
    "garlic": "aglio", "mushroom": "fungo", "mushrooms": "fungo", "zucchini": "zucchina", "zucchinis": "zucchina",
    "courgette": "zucchina", "courgettes": "zucchina", "milk": "latte", "butter": "burro", "flour": "farina",
    "cream": "panna", "chicken": "pollo", "rice": "riso", "beef": "manzo", "pork": "maiale", "bread": "pane",
    "cheese": "formaggio", "lemon": "limone", "lemons": "limone", "salt": "sale", "pepper": "pepe",
    "sugar": "zucchero", "water": "acqua", "oil": "olio", "vinegar": "aceto", "eggplant": "melanzana",
    "aubergine": "melanzana", "spinach": "spinaci", "basil": "basilico", "parsley": "prezzemolo",
    "bacon": "pancetta", "ham": "prosciutto", "tuna": "tonno", "salmon": "salmone", "beans": "fagioli",
    "bean": "fagiolo", "peas": "piselli", "pea": "pisello", "lentils": "lenticchie", "apple": "mela",
    "apples": "mela", "banana": "banana", "bananas": "banana", "yogurt": "yogurt", "honey": "miele",
    "chickpeas": "ceci", "leek": "porro", "leeks": "porro", "celery": "sedano", "cabbage": "cavolo",
    "cauliflower": "cavolfiore", "broccoli": "broccoli", "peppers": "peperone", "orange": "arancia",
    "oranges": "arancia", "walnuts": "noci", "almonds": "mandorle", "shrimp": "gamberi", "prawns": "gamberi",
}
_IRREGULAR = {"uova": "uovo", "uovo": "uovo", "dita": "dito", "paia": "paio", "mani": "mano"}

# multi-word synonyms, applied on the cleaned, space-joined text (longest first)
_PHRASES = {
    "parmigiano reggiano": "parmigiano", "grana padano": "parmigiano", "grana": "parmigiano",
    "panna fresca": "panna", "panna liquida": "panna", "panna da cucina": "panna", "panna cooking": "panna",
    "bell pepper": "peperone", "bell peppers": "peperone", "red pepper": "peperone", "green pepper": "peperone",
    "pomodori ciliegino": "pomodorini", "pomodoro ciliegino": "pomodorini", "cherry tomatoes": "pomodorini",
    "cherry tomato": "pomodorini", "uova di gallina": "uovo", "olio extra vergine": "olio",
    "olio extravergine": "olio", "extra vergine": "olio", "extravergine": "olio", "evo": "olio",
}
_PHRASE_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _PHRASES), key=len, reverse=True)) + r")\b")


def _stem(w: str) -> str:
    """Fold singular/plural: -a/-e, -o/-i, -e/-i all collapse; -io/-i (aglio/agli) and -ch/-gh (funghi) too."""
    w = _IRREGULAR.get(w, w)
    if len(w) < 4:
        return w
    if w[-1] in "aeiou":
        w = w[:-1]
    if len(w) >= 4 and w.endswith("i"):
        w = w[:-1]
    if w.endswith(("ch", "gh")):
        w = w[:-1]
    return w


def words(name: str) -> list[str]:
    """Clean ingredient text -> identity words (accents/punctuation/fillers gone, EN->IT, synonyms applied)."""
    s = parse.strip_accents(str(name).lower())
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = " ".join(s.split())
    s = _PHRASE_RE.sub(lambda m: _PHRASES[m.group(1)], s)
    out = []
    for w in s.split():
        if w in _FILLER or w.isdigit():
            continue
        out.append(_EN.get(w, w))
    s = _PHRASE_RE.sub(lambda m: _PHRASES[m.group(1)], " ".join(out))
    return s.split()


def entity(name: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in words(name))


def key(name: str) -> str:
    return "|".join(sorted(entity(name)))


STAPLES = {_stem(w) for w in ("sale", "pepe", "olio", "acqua", "zucchero", "aceto")}
_PASTA_FORMATS = {_stem(w) for w in (
    "spaghetti", "spaghettini", "penne", "rigatoni", "fusilli", "linguine", "tagliatelle", "farfalle", "maccheroni",
    "bucatini", "paccheri", "tortiglioni", "mezze maniche", "orecchiette", "pennette", "tonnarelli", "fettuccine",
    "pappardelle", "sedani", "ditalini", "conchiglie", "cavatappi", "trofie", "strozzapreti", "vermicelli")}
PASTA = _stem("pasta")
_NEAR_GROUPS = [
    {_stem(w) for w in ("pancetta", "guanciale", "bacon", "speck")},
    {_stem(w) for w in ("parmigiano", "pecorino")},
    {_stem(w) for w in ("cipolla", "scalogno", "porro")},
    {_stem(w) for w in ("burro", "margarina")},
    {_stem(w) for w in ("prosciutto cotto", "prosciutto crudo", "prosciutto")},
    {_stem(w) for w in ("mozzarella", "scamorza", "provola")},
]


def is_staple(ent: frozenset[str]) -> bool:
    return bool(ent) and bool(ent & STAPLES)


def match(have: frozenset[str], need: frozenset[str]) -> str | None:
    """'exact' (same / one contains the other / pasta format vs plain 'pasta'), 'near' (substitute) or None."""
    if not have or not need:
        return None
    if have <= need or need <= have:
        return "exact"
    pasta = frozenset({PASTA})
    if (have & _PASTA_FORMATS and need == pasta) or (need & _PASTA_FORMATS and have == pasta):
        return "exact"
    if have & _PASTA_FORMATS and need & _PASTA_FORMATS:
        return "near"  # another shape: usable, but not what the recipe asked for
    for g in _NEAR_GROUPS:
        if (have & g) and (need & g):
            return "near"
    return None


def pretty(name: str) -> str:
    """Display form for the pantry: quantity/unit/notes removed, lowercase ("2 kg Patate (nuove)" -> "patate")."""
    p = parse.parse_ingredient(str(name))
    s = (p["name"] or str(name)).strip().lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    return " ".join(s.split()).strip(" ,;:-")


def clean_items(items) -> list[str]:
    """Normalise + dedupe (by entity key), keep order."""
    out, seen = [], set()
    for it in items or []:
        if isinstance(it, str):
            for part in re.split(r"[,;\n]", it):
                n = pretty(part)
                k = key(n)
                if n and k and k not in seen:
                    seen.add(k)
                    out.append(n)
    return out


# -- persistence ---------------------------------------------------------------------------------
# "Frigo": the household food inventory. Entries are dicts {id, name, place, qty, expires, added} stored
# under KV_FRIDGE. The legacy KV ("recipes.pantry", a list of names) is migrated once, losslessly, into
# entries with place "pantry" and is then left untouched (so the migration is idempotent and reversible).

KV_FRIDGE = "recipes.fridge"
PLACES = ("fridge", "freezer", "pantry")
SOON_DAYS = 3


def _today() -> date:
    return date.today()


def check_date(v) -> str:
    """'' for none, else a valid ISO date (YYYY-MM-DD)."""
    if v in (None, ""):
        return ""
    s = str(v).strip()
    try:
        if len(s) != 10:
            raise ValueError
        date.fromisoformat(s)
    except ValueError:
        raise ValueError(_("The date is not valid")) from None
    return s


def days_left(expires: str, today: date | None = None) -> int | None:
    if not expires:
        return None
    return (date.fromisoformat(expires) - (today or _today())).days


class Pantry:
    def __init__(self, store):
        self.store = store

    # entries ------------------------------------------------------------------------------
    def entries(self) -> list[dict]:
        v = self.store.get(KV_FRIDGE)
        if not isinstance(v, list):  # first load: migrate the old list of names
            old = self.store.get(KV)
            now = _today().isoformat()
            v = [self._new(n, "pantry", added=now) for n in clean_items(old if isinstance(old, list) else [])]
            self.store.set(KV_FRIDGE, v)
        return [e for e in v if isinstance(e, dict) and e.get("name")]

    @staticmethod
    def _new(name, place, qty="", expires="", added=None) -> dict:
        return {"id": secrets.token_hex(4), "name": name, "place": place, "qty": qty, "expires": expires,
                "added": added or _today().isoformat()}

    def _save(self, entries: list[dict]) -> list[dict]:
        self.store.set(KV_FRIDGE, entries)
        return entries

    @staticmethod
    def _clean(b: dict, partial: bool = False) -> dict:
        out = {}
        if "name" in b or not partial:
            name = pretty(str(b.get("name") or ""))
            if not name or not key(name):
                raise ValueError(_("A name is required"))
            if len(name) > 60:
                raise ValueError(_("The name is too long"))
            out["name"] = name
        if "place" in b or not partial:
            place = b.get("place") or "fridge"
            if place not in PLACES:
                raise ValueError(_("Unknown place"))
            out["place"] = place
        if "qty" in b:
            q = " ".join(str(b.get("qty") or "").split())
            if len(q) > 40:
                raise ValueError(_("The quantity is too long"))
            out["qty"] = q
        if "expires" in b:
            out["expires"] = check_date(b.get("expires"))
        return out

    def add_entry(self, b: dict) -> dict:
        """Add (or, for the same ingredient, update) an entry; returns it."""
        c = self._clean(b)
        es = self.entries()
        for e in es:
            if key(e["name"]) == key(c["name"]):
                e.update({k: v for k, v in c.items() if k != "name" and (k in b)})
                self._save(es)
                return e
        e = self._new(c["name"], c["place"], c.get("qty", ""), c.get("expires", ""))
        es.append(e)
        self._save(es)
        return e

    def patch_entry(self, eid: str, b: dict) -> dict:
        c = self._clean(b, partial=True)
        es = self.entries()
        for e in es:
            if e["id"] == eid:
                if "name" in c and any(o is not e and key(o["name"]) == key(c["name"]) for o in es):
                    raise ValueError(_("Already in the list"))
                e.update(c)
                self._save(es)
                return e
        raise KeyError("item not found")

    def remove_entry(self, eid: str) -> dict:
        es = self.entries()
        for e in es:
            if e["id"] == eid:
                self._save([x for x in es if x is not e])
                return e
        raise KeyError("item not found")

    def summary(self, today: date | None = None) -> dict:
        es = self.entries()
        today = today or _today()
        soon = sorted(({**e, "days": days_left(e["expires"], today)} for e in es
                       if e.get("expires") and days_left(e["expires"], today) <= SOON_DAYS),
                      key=lambda e: (e["days"], e["name"]))
        return {"total": len(es), "places": {p: sum(1 for e in es if e["place"] == p) for p in PLACES},
                "expiring": soon, "expired": sum(1 for e in soon if e["days"] < 0)}

    # legacy name-based API (cook_with, shopping hook, old routes) ---------------------------
    def items(self) -> list[str]:
        return [e["name"] for e in self.entries()]

    def put(self, items, place: str = "pantry") -> list[str]:
        """Replace the set of names: kept names keep their entry (place, quantity, expiry)."""
        want = clean_items(items)
        old = {key(e["name"]): e for e in self.entries()}
        self._save([old.get(key(n)) or self._new(n, place) for n in want])
        return want

    def add(self, items, place: str = "pantry") -> list[str]:
        es = self.entries()
        have = {key(e["name"]) for e in es}
        for n in clean_items(items):
            if key(n) not in have:
                have.add(key(n))
                es.append(self._new(n, place))
        self._save(es)
        return [e["name"] for e in es]

    def remove(self, items) -> list[str]:
        gone = {key(x) for x in clean_items(items)}
        es = [e for e in self.entries() if key(e["name"]) not in gone]
        self._save(es)
        return [e["name"] for e in es]


# -- search --------------------------------------------------------------------------------------

def _line_taste(p: dict) -> bool:
    return bool(p["taste"]) or bool(re.search(r"facoltativ|opzional|optional|a piacere|\bqb\b", p["raw"], re.I))


def analyse(lines: list[str], typed: list[str], pantry: list[str]) -> dict:
    """Compare a recipe's ingredient lines with what we have. Staples and "q.b." lines are ignored."""
    t_ents = [(x, entity(x)) for x in typed]
    p_ents = [(x, entity(x)) for x in pantry]
    have, missing, subs = [], [], []
    hit_typed: set[int] = set()
    seen: set[str] = set()
    for line in lines:
        if line.startswith("# "):
            continue
        p = parse.parse_ingredient(line)
        need = entity(p["name"])
        if not need or is_staple(need) or _line_taste(p):
            continue
        k = "|".join(sorted(need))
        if k in seen:
            continue
        seen.add(k)
        # shown to people: "d'aglio" -> "aglio", "Burro freddo" stays as written
        name = re.sub(r"^(?:d|di|del|della|dello|degli|delle)\s*['’]?\s*(?=\w)", "", p["name"].strip(), flags=re.I) \
            if re.match(r"^d['’]", p["name"].strip(), re.I) else p["name"].strip()
        level, src = None, None
        for i, (raw, e) in enumerate(t_ents):
            m = match(e, need)
            if m == "exact":
                hit_typed.add(i)
                level, src = "exact", raw
            elif m == "near":
                hit_typed.add(i)
                if level is None:
                    level, src = "near", raw
        if level != "exact":
            for raw, e in p_ents:
                m = match(e, need)
                if m == "exact":
                    level, src = "exact", raw
                    break
                if m == "near" and level is None:
                    level, src = "near", raw
        if level == "exact":
            have.append(name)
        elif level == "near":
            subs.append({"have": src, "need": name})
        else:
            missing.append(name)
    total = len(have) + len(subs) + len(missing)
    return {"have": have, "missing": missing, "substitutes": subs, "typed_hits": len(hit_typed),
            "score": round((len(have) + 0.5 * len(subs)) / total, 2) if total else 0.0}


def cook_with(book, typed, pantry_items=None, limit: int = 30) -> list[dict]:
    typed = clean_items(typed)
    pantry_items = [x for x in clean_items(pantry_items) if key(x) not in {key(t) for t in typed}]
    if not typed:
        return []
    out = []
    for r in book.store.query("SELECT * FROM recipes_recipe"):
        res = analyse(json.loads(r["ingredients"]), typed, pantry_items)
        if not res["typed_hits"]:
            continue
        s = book._summary(r)
        out.append({"id": r["id"], "title": r["title"], "image": s["image"], "have": res["have"],
                    "missing": res["missing"], "substitutes": res["substitutes"], "score": res["score"],
                    "typed_hits": res["typed_hits"], "total_min": r["total_min"]})
    out.sort(key=lambda x: (len(x["missing"]), len(x["substitutes"]), -x["typed_hits"], -x["score"], x["title"].lower()))
    return out[:limit]


COMMON = [
    "uova", "latte", "burro", "farina", "pasta", "riso", "patate", "cipolla", "aglio", "carote", "zucchine",
    "melanzane", "pomodori", "pomodorini", "passata di pomodoro", "peperoni", "spinaci", "funghi", "pollo",
    "manzo", "macinato", "salsiccia", "pancetta", "guanciale", "prosciutto", "tonno", "mozzarella",
    "parmigiano", "pecorino", "ricotta", "panna", "yogurt", "pane", "limone", "basilico", "prezzemolo",
    "fagioli", "ceci", "lenticchie", "piselli", "mele", "banane", "miele", "cioccolato", "lievito",
]


def known_ingredients(book) -> list[str]:
    """Autocomplete list: ingredient names of saved recipes (most used first) + common Italian ones."""
    counts: dict[str, list] = {}
    for r in book.store.query("SELECT ingredients FROM recipes_recipe"):
        for line in json.loads(r["ingredients"]):
            if line.startswith("# "):
                continue
            n = pretty(parse.parse_ingredient(line)["name"])
            e = entity(n)
            if n and e and len(n) <= 40 and not is_staple(e):
                c = counts.setdefault("|".join(sorted(e)), [0, n])
                c[0] += 1
    names = [v[1] for v in sorted(counts.values(), key=lambda v: (-v[0], v[1]))]
    seen = {key(n) for n in names}
    return names + [c for c in COMMON if key(c) not in seen]
