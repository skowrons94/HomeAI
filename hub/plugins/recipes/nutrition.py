"""Calorie estimates from the built-in table and ingredient substitutions."""

from __future__ import annotations

from .parse import key_tokens, normalize_unit
from .tables import KCAL, SUBS, UNIT_G, UNIT_ML


def _load():
    foods: list[tuple[tuple[str, ...], float, float | None, float | None]] = []
    for line in KCAL.strip().splitlines():
        parts = line.split(";")
        names, kcal = parts[0], float(parts[1])
        dens = float(parts[2]) if len(parts) > 2 and parts[2] else None
        piece = float(parts[3]) if len(parts) > 3 and parts[3] else None
        for n in names.split("|"):
            t = key_tokens(n)
            if t:
                foods.append((t, kcal, dens, piece))
    subs = []
    for names, it, en in SUBS:
        for n in names.split("|"):
            t = key_tokens(n)
            if t:
                subs.append((t, it, en))
    return foods, subs


_FOODS, _SUBS = _load()


def _match(tokens: tuple[str, ...], table):
    """Longest table entry whose tokens occur contiguously in `tokens`.
    Later entries win ties, so specific lines can override general ones."""
    best = None
    best_len = 0
    n = len(tokens)
    for entry in table:
        t = entry[0]
        k = len(t)
        if k > n or k < best_len:
            continue
        if any(tokens[i:i + k] == t for i in range(n - k + 1)):
            if k >= best_len:
                best, best_len = entry, k
    return best


def lookup_food(name: str):
    return _match(key_tokens(name), _FOODS)


def substitutions(name: str, lang: str = "it") -> list[str]:
    e = _match(key_tokens(name), _SUBS)
    if not e:
        return []
    return list(e[1] if lang == "it" else e[2])


def grams(ing: dict, food) -> float | None:
    """Weight in grams of a parsed ingredient (None when unknown)."""
    qty = ing.get("qty")
    if qty is None:
        return None
    if ing.get("qty2"):
        qty = (qty + ing["qty2"]) / 2
    unit = ing.get("unit", "")
    if unit in ("g", "kg", "oz", "lb", "pinch", "handful", "clove", "slice", "pack", "bunch", "sprig", "leaf"):
        if unit == "clove" and food and food[0][0] not in ("agl", "garlic"):
            return qty * (food[3] or 30)
        if unit == "slice" and food and food[3]:
            return qty * food[3]
        return qty * UNIT_G[unit]
    if unit in ("ml", "cl", "dl", "l", "tbsp", "tsp", "cup", "glass", "can"):
        ml = qty * UNIT_ML[unit]
        dens = (food[2] if food and food[2] else 1.0)
        if unit == "can":
            return qty * 240  # a tin is ~400 g with liquid, drained ~240
        return ml * dens
    if unit == "":
        if food and food[3]:
            return qty * food[3]
        if food is None:
            return None
        return None
    return None


def estimate(ingredients: list[dict]) -> dict:
    """Total kcal of a list of parsed ingredients -> {kcal, coverage, items}."""
    total = 0.0
    known = 0
    counted = 0
    for ing in ingredients:
        if ing.get("taste") or ing.get("qty") is None:
            continue  # q.b.: negligible, don't count against coverage
        counted += 1
        food = lookup_food(ing["name"])
        g = grams(ing, food) if food else None
        if food is None or g is None:
            continue
        known += 1
        total += g * food[1] / 100
    return {"kcal": round(total), "coverage": (known / counted) if counted else 0.0, "known": known,
            "counted": counted}
