"""Recipe page parsing (schema.org JSON-LD + microdata) and ingredient lines (IT + EN)."""

from __future__ import annotations

import html as htmllib
import json
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

# ----------------------------------------------------------------------------- text utils


def clean(text: Any) -> str:
    """HTML-ish text -> plain single-line-ish string."""
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"<\s*br\s*/?>|</\s*p\s*>|</\s*li\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _stem(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("oes") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    if len(w) >= 4 and w[-1] in "aeio":
        w = w[:-1]
    return w


_STOP = {"di", "d", "of", "the", "a", "an", "il", "lo", "la", "le", "i", "gli", "un", "una", "uno",
         "del", "della", "dello", "dei", "delle", "degli", "al", "alla", "in", "con", "e", "and", "or", "o"}


def key_tokens(name: str) -> tuple[str, ...]:
    s = strip_accents(name.lower())
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s).replace("'", " ")
    return tuple(_stem(w) for w in s.split() if w not in _STOP)


def norm_key(name: str) -> str:
    """Merge key for shopping lists: accents, plurals and filler words removed."""
    return " ".join(key_tokens(name))


# ----------------------------------------------------------------------------- durations / numbers


def minutes(v: Any) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*P(?:(\d+)D)?T?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:\d+S)?\s*", str(v), re.I)
    if m and any(m.groups()):
        d, h, mi = (float(x) if x else 0 for x in m.groups())
        return int(d * 1440 + h * 60 + mi)
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None


def number(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def servings_of(v: Any) -> int | None:
    if isinstance(v, list):
        for x in v:  # prefer the entry that is a plain number
            n = servings_of(x)
            if n:
                return n
        return None
    n = number(v)
    return int(round(n)) if n and 0 < n <= 200 else None


# ----------------------------------------------------------------------------- JSON-LD


def _walk(node: Any):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "mainEntity", "mainEntityOfPage", "hasPart", "itemListElement"):
            if k in node:
                yield from _walk(node[k])


def _is_recipe(n: dict) -> bool:
    t = n.get("@type")
    ts = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.lower().endswith("recipe") for x in ts)


def jsonld_recipes(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<script[^>]+type=[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", page, re.S | re.I):
        raw = m.group(1).strip()
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw)
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            try:  # some sites put raw newlines / trailing commas
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
            except ValueError:
                continue
        out += [n for n in _walk(data) if _is_recipe(n)]
    return out


def _image(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        for x in v:
            r = _image(x)
            if r:
                return r
        return ""
    if isinstance(v, dict):
        return _image(v.get("url") or v.get("contentUrl") or v.get("@id") or "")
    return ""


def _steps(v: Any, out: list[str] | None = None) -> list[str]:
    """recipeInstructions: text, list of strings / HowToStep / HowToSection (heading lines start with '# ')."""
    out = [] if out is None else out
    if v is None:
        return out
    if isinstance(v, str):
        txt = clean(v)
        parts = [p.strip() for p in txt.split("\n") if p.strip()]
        out.extend(re.sub(r"^\s*(?:step\s*)?\d+\s*[.):\-]\s+", "", p, flags=re.I) for p in parts)
    elif isinstance(v, list):
        for x in v:
            _steps(x, out)
    elif isinstance(v, dict):
        t = v.get("@type")
        if t == "HowToSection" or "itemListElement" in v and not v.get("text"):
            name = clean(v.get("name"))
            if name:
                out.append("# " + name)
            _steps(v.get("itemListElement"), out)
        else:
            _steps(v.get("text") or v.get("description") or v.get("name"), out)
    return out


def _ingredients(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"\n", v)]
    lines = []
    for x in v or []:
        if isinstance(x, dict):
            x = x.get("text") or x.get("name") or ""
        x = clean(x).replace("\n", " ")
        if x:
            lines.append(x)
    return lines


def _kcal(nut: Any) -> float | None:
    if not isinstance(nut, dict):
        return None
    c = nut.get("calories")
    if c is None:
        return None
    s = str(c)
    if re.search(r"kj", s, re.I) and not re.search(r"kcal|cal", s, re.I):
        return None
    n = number(s)
    return round(n) if n and 0 < n < 5000 else None


def _names(v: Any) -> list[str]:
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [y for x in v for y in _names(x)]
    if isinstance(v, dict):
        return _names(v.get("name"))
    return []


def from_jsonld(node: dict, url: str = "") -> dict:
    ing = _ingredients(node.get("recipeIngredient") or node.get("ingredients"))
    steps = _steps(node.get("recipeInstructions"))
    cats = _names(node.get("recipeCategory"))
    cuis = _names(node.get("recipeCuisine"))
    kw = _names(node.get("keywords"))
    prep, cook, total = (minutes(node.get(k)) for k in ("prepTime", "cookTime", "totalTime"))
    return {
        "title": clean(node.get("name")) or clean(node.get("headline")),
        "image_url": _image(node.get("image")),
        "servings": servings_of(node.get("recipeYield")),
        "yield_text": clean(node.get("recipeYield") if isinstance(node.get("recipeYield"), (str, int)) else ""),
        "prep_min": prep, "cook_min": cook, "total_min": total or ((prep or 0) + (cook or 0) or None),
        "ingredients": ing, "steps": steps,
        "kcal": _kcal(node.get("nutrition")),
        "cuisine": ", ".join(cuis[:2]), "category": ", ".join(cats[:2]),
        "keywords": kw[:8],
        "description": clean(node.get("description")),
    }


# ----------------------------------------------------------------------------- microdata fallback


class _Micro(HTMLParser):
    VOID = {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "col", "embed", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[list] = []  # [tag, prop|None, is_recipe_scope, text parts]
        self.depth_recipe = 0
        self.props: dict[str, list[str]] = {}

    def _add(self, prop, val):
        if val:
            self.props.setdefault(prop, []).append(val.strip())

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("p", "li", "br", "div"):
            self.handle_data("\n")
        scope = "itemscope" in a and "recipe" in (a.get("itemtype") or "").lower().rsplit("/", 1)[-1]
        prop = a.get("itemprop")
        if tag in self.VOID:
            if self.depth_recipe and prop:
                self._add(prop, a.get("content") or a.get("src") or a.get("href") or "")
            return
        self.stack.append([tag, prop, scope, [], a.get("content") or a.get("datetime") or ""])
        if scope:
            self.depth_recipe += 1

    def handle_data(self, data):
        for fr in self.stack:
            fr[3].append(data)

    def handle_endtag(self, tag):
        if tag in ("p", "li"):
            self.handle_data("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for fr in self.stack[i + 1:]:  # unclosed children
                    if fr[2]:
                        self.depth_recipe -= 1
                fr = self.stack[i]
                del self.stack[i:]
                if fr[2]:
                    self.depth_recipe -= 1
                if self.depth_recipe and fr[1]:
                    for p in fr[1].split():
                        self._add(p, fr[4] or clean("".join(fr[3])))
                return


def from_microdata(page: str) -> dict | None:
    if "schema.org/Recipe" not in page and "schema.org/recipe" not in page:
        return None
    p = _Micro()
    try:
        p.feed(page)
    except Exception:
        return None
    pr = p.props
    if not (pr.get("recipeIngredient") or pr.get("ingredients")) and not pr.get("recipeInstructions"):
        return None
    node = {
        "name": (pr.get("name") or [""])[0], "image": (pr.get("image") or [""])[0],
        "recipeYield": (pr.get("recipeYield") or [None])[0],
        "prepTime": (pr.get("prepTime") or [None])[0], "cookTime": (pr.get("cookTime") or [None])[0],
        "totalTime": (pr.get("totalTime") or [None])[0],
        "recipeIngredient": pr.get("recipeIngredient") or pr.get("ingredients") or [],
        "recipeInstructions": pr.get("recipeInstructions") or [],
        "nutrition": {"calories": (pr.get("calories") or [None])[0]},
        "recipeCategory": pr.get("recipeCategory") or [], "recipeCuisine": pr.get("recipeCuisine") or [],
        "description": (pr.get("description") or [""])[0],
    }
    return from_jsonld(node)


def _meta(page: str, name: str) -> str:
    for m in re.finditer(r"<meta\s+[^>]*>", page, re.I):
        tag = m.group(0)
        if re.search(rf"(?:property|name)=[\"']{re.escape(name)}[\"']", tag, re.I):
            c = re.search(r"content=([\"'])(.*?)\1", tag, re.I | re.S)
            if c:
                return htmllib.unescape(c.group(2))
    return ""


GZ_FOOTNOTE = re.compile(r"\s\d{1,2}(?:\s\d{1,2})*(?:\s+(?=[.;,])|(?=[.;,])|\s*$)")


def parse_page(page: str, url: str = "") -> dict | None:
    """Extract a recipe dict from an HTML page, or None when there is none."""
    r = None
    for node in jsonld_recipes(page):
        cand = from_jsonld(node, url)
        if cand["ingredients"] or cand["steps"]:
            r = cand
            break
    if r is None:
        r = from_microdata(page)
    if r is None:
        return None
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if not r["title"]:
        r["title"] = clean(_meta(page, "og:title")) or host
    if not r["image_url"]:
        r["image_url"] = _meta(page, "og:image")
    if "giallozafferano" in host:  # step photo numbers are glued to the text
        r["steps"] = [GZ_FOOTNOTE.sub("", s).strip() for s in r["steps"]]
    r["site"] = host
    r["source_url"] = url
    return r


# ----------------------------------------------------------------------------- ingredient lines

UNIT_ALIASES = {
    "g": "g", "gr": "g", "grammi": "g", "grammo": "g", "gram": "g", "grams": "g", "gramme": "g",
    "kg": "kg", "kilo": "kg", "kili": "kg", "chilo": "kg", "chili": "kg", "kilogram": "kg", "kilograms": "kg",
    "ml": "ml", "millilitri": "ml", "millilitres": "ml", "milliliters": "ml", "millilitre": "ml",
    "cl": "cl", "dl": "dl", "l": "l", "lt": "l", "litro": "l", "litri": "l", "litre": "l", "litres": "l",
    "liter": "l", "liters": "l",
    "cucchiaio": "tbsp", "cucchiai": "tbsp", "tbsp": "tbsp", "tbs": "tbsp", "tbsps": "tbsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp",
    "cucchiaino": "tsp", "cucchiaini": "tsp", "tsp": "tsp", "tsps": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "cup": "cup", "cups": "cup", "tazza": "cup", "tazze": "cup", "tazzina": "cup", "tazzine": "cup",
    "bicchiere": "glass", "bicchieri": "glass", "glass": "glass", "glasses": "glass",
    "oz": "oz", "ounce": "oz", "ounces": "oz", "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "pz": "", "pezzo": "", "pezzi": "", "pcs": "", "pc": "", "piece": "", "pieces": "", "n": "", "nr": "",
    "spicchio": "clove", "spicchi": "clove", "clove": "clove", "cloves": "clove",
    "pizzico": "pinch", "pizzichi": "pinch", "pinch": "pinch", "presa": "pinch", "dash": "pinch",
    "manciata": "handful", "manciate": "handful", "handful": "handful", "handfuls": "handful",
    "bustina": "pack", "bustine": "pack", "pack": "pack", "packet": "pack", "sachet": "pack", "confezione": "pack",
    "fetta": "slice", "fette": "slice", "slice": "slice", "slices": "slice",
    "mazzetto": "bunch", "mazzo": "bunch", "bunch": "bunch", "ciuffo": "bunch", "ciuffi": "bunch",
    "rametto": "sprig", "rametti": "sprig", "ramo": "sprig", "rami": "sprig", "sprig": "sprig", "sprigs": "sprig",
    "foglia": "leaf", "foglie": "leaf", "leaf": "leaf", "leaves": "leaf",
    "lattina": "can", "lattine": "can", "scatola": "can", "scatole": "can", "can": "can", "cans": "can",
    "tin": "can", "tins": "can",
}

UNIT_LABEL = {
    "it": {"g": "g", "kg": "kg", "ml": "ml", "cl": "cl", "dl": "dl", "l": "l", "tbsp": ("cucchiaio", "cucchiai"),
           "tsp": ("cucchiaino", "cucchiaini"), "cup": ("tazza", "tazze"), "glass": ("bicchiere", "bicchieri"),
           "oz": "oz", "lb": "lb", "clove": ("spicchio", "spicchi"), "pinch": ("pizzico", "pizzichi"),
           "handful": ("manciata", "manciate"), "pack": ("bustina", "bustine"), "slice": ("fetta", "fette"),
           "bunch": ("mazzetto", "mazzetti"), "sprig": ("rametto", "rametti"), "leaf": ("foglia", "foglie"),
           "can": ("lattina", "lattine")},
    "en": {"g": "g", "kg": "kg", "ml": "ml", "cl": "cl", "dl": "dl", "l": "l", "tbsp": "tbsp", "tsp": "tsp",
           "cup": ("cup", "cups"), "glass": ("glass", "glasses"), "oz": "oz", "lb": "lb",
           "clove": ("clove", "cloves"), "pinch": ("pinch", "pinches"), "handful": ("handful", "handfuls"),
           "pack": ("packet", "packets"), "slice": ("slice", "slices"), "bunch": ("bunch", "bunches"),
           "sprig": ("sprig", "sprigs"), "leaf": ("leaf", "leaves"), "can": ("can", "cans")},
}

_FRAC = {"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3", "⅕": " 1/5", "⅛": " 1/8",
         "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8"}
_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)"
_NUM_RE = re.compile(_NUM)
_RANGE = rf"(?P<q1>{_NUM})(?:\s*(?:-|–|—|to|a|or)\s*(?P<q2>{_NUM}))?"
_TASTE = re.compile(
    r"(?:\bq\.?\s?b\.?(?=$|[\s,;)])|\bquanto basta\b|\bto taste\b|\bas needed\b|\bas required\b|\ba piacere\b|"
    r"\bfor (?:serving|frying|dusting|greasing|sprinkling)\b|\bper (?:friggere|spolverizzare|ungere|servire|decorare)\b)", re.I)


def to_num(s: str) -> float:
    s = s.strip().replace(",", ".")
    if " " in s:
        w, f = s.split(None, 1)
        return float(w) + to_num(f)
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    return float(s)


def _unit_at(s: str) -> tuple[str | None, str]:
    """Unit word at the start of s -> (canonical unit or None when not a unit, rest)."""
    m = re.match(r"\s*([A-Za-zàèéìòùÀ-Ý]+)\.?(?=$|[\s,;/)(])", s)
    if not m:
        return None, s
    w = strip_accents(m.group(1).lower())
    if w in UNIT_ALIASES:
        return UNIT_ALIASES[w], s[m.end():]
    return None, s


def parse_ingredient(line: str) -> dict:
    raw = clean(line).replace("\n", " ")
    s = raw
    for k, v in _FRAC.items():
        s = s.replace(k, v)
    s = re.sub(r"^\s*[-•*▢□☐]+\s*", "", s).strip()
    s = re.sub(r"(\d)\s+(\d/\d)", r"\1 \2", s)
    m1 = re.match(r"^(?:un|una|uno|a|an|one)\s+([A-Za-zà-ù]+)", s, re.I)
    if m1 and strip_accents(m1.group(1).lower()) in UNIT_ALIASES:
        s = "1 " + s[m1.start(1):]
    taste = bool(_TASTE.search(s))
    s = _TASTE.sub(" ", s)
    qty = qty2 = None
    unit = ""
    name = s
    # glued units: 200g, 500ml
    s2 = re.sub(r"^(\d+(?:[.,]\d+)?)([A-Za-z]{1,3})\b", r"\1 \2", s)
    m = re.match(rf"\s*{_RANGE}\s*(?P<rest>.*)$", s2)
    if m and m.group("rest") is not None:
        qty = to_num(m.group("q1"))
        qty2 = to_num(m.group("q2")) if m.group("q2") else None
        rest = m.group("rest")
        u, rest2 = _unit_at(rest)
        if u is not None:
            unit, rest = u, rest2
        rest = re.sub(r"^\s*/\s*\d+(?:[.,]\d+)?\s*[A-Za-z]+\b", "", rest)  # "400g/14oz"
        rest = re.sub(r"^\s*(?:x|×)\s*", "", rest)
        name = rest.strip()
        for _ in range(2):
            name = re.sub(r"^(?:(?:di|of|del|della|dello|dei|delle)\s+|d')", "", name, flags=re.I).strip()
        name = re.sub(r"^(?:can|tin|jar|packet|pack)\s+(?:of\s+)?", "", name, flags=re.I)
    else:
        # Italian sites: "Spaghetti 320 g", "Uova (medie) 3"
        m = re.match(rf"^(?P<name>.+?)\s+{_RANGE}\s*(?P<u>[A-Za-zàèéì]+\.?)?\s*$", s2)
        if m:
            word = (m.group("u") or "").rstrip(".")
            u = UNIT_ALIASES.get(strip_accents(word.lower()), None) if word else ""
            qty = to_num(m.group("q1"))
            qty2 = to_num(m.group("q2")) if m.group("q2") else None
            unit, name = u or "", m.group("name")
            if u is None:  # "uova 2 intere": the word is a remark, not a unit
                name += f" ({word})"
    name = name.strip(" ,;:-")
    note = ""
    paren = re.findall(r"\(([^)]*)\)", name)
    if paren:
        note = "; ".join(p.strip() for p in paren if p.strip())
        name = re.sub(r"\([^)]*\)", " ", name)
    if "," in name:
        name, extra = name.split(",", 1)
        note = "; ".join(x for x in (note, extra.strip()) if x)
    name = re.sub(r"\s+", " ", name).strip(" ,;:-")
    if qty is None and not taste:
        taste = False
    if qty is None and not name:
        name = raw
    return {"raw": raw, "qty": qty, "qty2": qty2, "unit": unit, "name": name, "note": note,
            "taste": taste and qty is None, "key": norm_key(name) or norm_key(raw)}


# ----------------------------------------------------------------------------- scaling / formatting

_VULGAR = {0.25: "¼", 0.5: "½", 0.75: "¾", 1 / 3: "⅓", 2 / 3: "⅔"}


def fmt_qty(q: float, unit: str = "", pieces: bool = False) -> str:
    if unit in ("g", "ml"):
        q = round(q, 1) if q < 10 else round(q) if q < 100 else round(q / 5) * 5
        return str(int(q)) if q == int(q) else str(q).replace(".", ",")
    if unit in ("kg", "l"):
        q = round(q, 2)
        return str(int(q)) if q == int(q) else str(q).replace(".", ",")
    if pieces and unit in ("", "pz") and q >= 1:  # countable things (eggs...): whole pieces
        return str(int(q + 0.5))
    whole = int(q)
    frac = q - whole
    if frac < 0.08:
        return str(whole)
    for v, ch in _VULGAR.items():
        if abs(frac - v) < 0.09:
            return f"{whole}{ch}" if whole else ch
    if frac > 0.92:
        return str(whole + 1)
    r = round(q, 1)
    return str(int(r)) if r == int(r) else str(r).replace(".", ",")


def _unit_text(unit: str, qty: float, lang: str) -> str:
    lab = UNIT_LABEL.get(lang, UNIT_LABEL["en"]).get(unit, unit)
    if isinstance(lab, tuple):
        return lab[0] if qty <= 1 else lab[1]
    return lab


def normalize_unit(qty: float, unit: str) -> tuple[float, str]:
    """Convert to the merge base (g / ml) and back up to kg / l for display."""
    return {"kg": (qty * 1000, "g"), "cl": (qty * 10, "ml"), "dl": (qty * 100, "ml"),
            "l": (qty * 1000, "ml")}.get(unit, (qty, unit))


def display_unit(qty: float, unit: str) -> tuple[float, str]:
    if unit == "g" and qty >= 1000:
        return qty / 1000, "kg"
    if unit == "ml" and qty >= 1000:
        return qty / 1000, "l"
    return qty, unit


def format_ingredient(ing: dict, factor: float = 1.0, lang: str = "it") -> str:
    if factor == 1 or (ing["qty"] is None):
        return ing["raw"]
    q, u = display_unit(*normalize_unit(ing["qty"] * factor, ing["unit"]))
    return _compose(q, ing["qty2"] and normalize_unit(ing["qty2"] * factor, ing["unit"])[0] or None,
                    u, ing["name"], ing["note"], lang)


def _compose(q: float, q2: float | None, unit: str, name: str, note: str, lang: str) -> str:
    qs = fmt_qty(q, unit, True)
    if q2:
        qs += "-" + fmt_qty(q2, unit, True)
    parts = [qs]
    if unit:
        parts.append(_unit_text(unit, q2 or q, lang))
        if unit not in ("g", "kg", "ml", "l", "cl", "dl", "oz", "lb") or lang == "it":
            parts.append("di" if lang == "it" else "of")
    parts.append(name)
    txt = " ".join(parts)
    return f"{txt} ({note})" if note else txt


# ----------------------------------------------------------------------------- editing: rows, rescale

# Units the editor offers (canonical codes from UNIT_ALIASES); "" = no unit (3 eggs).
EDIT_UNITS = ["g", "kg", "ml", "cl", "dl", "l", "tbsp", "tsp", "cup", "glass", "clove", "leaf", "pinch",
              "handful", "pack", "slice", "bunch", "sprig", "can", "oz", "lb"]
_METRIC = {"g", "kg", "ml", "cl", "dl", "l", "oz", "lb"}
_CONTINUOUS = {"cup", "glass", "oz", "lb"}  # quarters make sense
_SAME = ("qty", "qty2", "unit", "name", "note", "taste")


def exact_qty(q: float) -> str:
    """A quantity written so parse_ingredient reads back the same number (½, 1½, 2,5 ...)."""
    q = round(float(q), 2)
    whole = int(q)
    frac = round(q - whole, 4)
    for v, ch in _VULGAR.items():
        if abs(frac - v) < 0.006:
            return f"{whole}{ch}" if whole else ch
    return str(int(q)) if q == int(q) else f"{q:.2f}".rstrip("0").replace(".", ",")


def _fnum(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return to_num(str(v))
    except (ValueError, ZeroDivisionError):
        return None


def row_from_line(line: str) -> dict:
    """One stored line -> an editor row: {heading} or {qty, qty2, unit, name, note, taste, raw}."""
    if line.startswith("# "):
        return {"heading": line[2:].strip()}
    p = parse_ingredient(line)
    return {"qty": p["qty"], "qty2": p["qty2"], "unit": p["unit"], "name": p["name"], "note": p["note"],
            "taste": p["taste"], "raw": line}


def line_from_row(row: dict, lang: str = "it") -> str:
    """An editor row -> the stored line. Untouched rows keep their original text exactly."""
    if row.get("heading") is not None:
        h = re.sub(r"\s+", " ", str(row["heading"])).strip()
        return f"# {h}" if h else ""
    name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
    note = re.sub(r"\s+", " ", str(row.get("note") or "")).strip()
    qty, qty2 = _fnum(row.get("qty")), _fnum(row.get("qty2"))
    unit = str(row.get("unit") or "")
    taste = bool(row.get("taste")) and qty is None
    raw = str(row.get("raw") or "").strip()
    if raw:
        p = parse_ingredient(raw)
        same = (p["name"] == name and p["note"] == note and p["unit"] == (unit if qty is not None else p["unit"])
                and p["taste"] == taste and (p["qty"] is None) == (qty is None)
                and (qty is None or abs(p["qty"] - qty) < 0.005)
                and ((p["qty2"] is None) == (qty2 is None) or qty is None)
                and (qty2 is None or p["qty2"] is None or abs(p["qty2"] - qty2) < 0.005))
        if same:
            return raw
    if not name:
        return ""
    if qty is None:
        txt = f"{name} q.b." if taste else name
    else:
        qs = exact_qty(qty) + (f"-{exact_qty(qty2)}" if qty2 and qty2 != qty else "")
        parts = [qs]
        if unit:
            parts.append(_unit_text(unit, qty2 or qty, lang))
            if unit not in _METRIC or lang == "it":
                parts.append("di" if lang == "it" else "of")
        parts.append(name)
        txt = " ".join(parts)
    return f"{txt} ({note})" if note else txt


def rows_to_lines(rows: list, lang: str = "it") -> list[str]:
    out = []
    for r in rows or []:
        if isinstance(r, str):
            out.append(r)
        elif isinstance(r, dict):
            line = line_from_row(r, lang)
            if line:
                out.append(line)
    return out


def _hu(x: float) -> int:
    """Round half up (Python's round() is banker's: 12.5 -> 12)."""
    return int(x + 0.5)


def _round_qty(q: float, unit: str) -> tuple[float, str]:
    """Round a rescaled quantity the way a cook would. Never 0 for a non-zero input."""
    if unit in ("kg", "l", "cl", "dl"):
        base, bu = normalize_unit(q, unit)
        r, bu = _round_qty(base, bu)
        if unit in ("kg", "l") and r >= 1000:
            return round(r / 1000, 2), unit
        if unit in ("kg", "l"):
            return r, bu          # 0.5 kg -> 500 g
        return round(r / (10 if unit == "cl" else 100), 1), unit
    if unit in ("g", "ml"):
        if q < 10:
            return max(1.0, float(_hu(q))), unit
        if q < 100:
            return max(5.0, _hu(q / 5) * 5.0), unit
        return _hu(q / 10) * 10.0, unit
    if unit in ("tbsp", "tsp"):
        return max(0.5, _hu(q * 2) / 2), unit
    if unit in _CONTINUOUS:
        return max(0.25, _hu(q * 4) / 4), unit
    # countable: pieces, eggs, cloves, packs...
    if q < 1:
        return max(0.5, _hu(q * 2) / 2), unit
    return float(max(1, int(q + 0.5))), unit


def rescale_line(line: str, factor: float, lang: str = "it") -> str:
    """Scale one stored line by `factor` (new servings / old servings) with cook-style rounding.
    Headings, "q.b." and lines without a quantity come back unchanged."""
    if line.startswith("# ") or factor <= 0 or abs(factor - 1) < 1e-9:
        return line
    p = parse_ingredient(line)
    if p["qty"] is None or p["qty"] <= 0:
        return line
    q, unit = _round_qty(p["qty"] * factor, p["unit"])
    q2 = None
    if p["qty2"]:
        q2, u2 = _round_qty(p["qty2"] * factor, p["unit"])
        if u2 != unit:  # kg range falling back to g on one side: use the base unit for both
            q, unit = _round_qty(normalize_unit(p["qty"] * factor, p["unit"])[0], normalize_unit(1, p["unit"])[1])
            q2, _ = _round_qty(normalize_unit(p["qty2"] * factor, p["unit"])[0], normalize_unit(1, p["unit"])[1])
        if q2 == q:
            q2 = None
    return line_from_row({"qty": q, "qty2": q2, "unit": unit, "name": p["name"], "note": p["note"]}, lang)


def rescale_lines(lines: list[str], factor: float, lang: str = "it") -> list[str]:
    return [rescale_line(x, factor, lang) for x in lines]
