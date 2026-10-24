"""Fridge ("Frigo"): migration from the old pantry list, CRUD + validation, finish -> shopping, expiry logic and
the daily notification, cook_with and the shopping hook on the new storage. Temp dirs only.
Run: python tests/test_fridge.py"""

import asyncio
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes import RecipesPlugin, pantry as pm  # noqa: E402
from hub.store import Store  # noqa: E402

TODAY = date(2026, 10, 24)
iso = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731


class Req:
    def __init__(self, body=None, **match):
        self._b, self.match_info, self.query = body, match, {}

    async def json(self):
        return self._b


def mk(config=None, preload=None):
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "hub.db")
    if preload is not None:
        store.set(pm.KV, preload)
    sent = []

    async def send(title, body="", url="", image="", tag="", push=True):
        sent.append((title, body, url))

    hub = SimpleNamespace(store=store, data_dir=tmp, every=lambda *a, **k: None, publish=lambda *a, **k: None,
                          notify=SimpleNamespace(send=send))
    plug = RecipesPlugin(hub, config or {})
    asyncio.run(plug.start())
    return plug, sent


def run(c):
    return json.loads(asyncio.run(c).text)


def test_migration_lossless_and_idempotent():
    plug, _ = mk(preload=["uova", "patate", "grana"])
    es = plug.pantry.entries()
    assert [e["name"] for e in es] == ["uova", "patate", "grana"]
    assert all(e["place"] == "pantry" and e["expires"] == "" and e["qty"] == "" and e["id"] for e in es)
    ids = [e["id"] for e in es]
    p2 = pm.Pantry(plug.hub.store)  # second load: same entries, nothing duplicated
    assert [e["id"] for e in p2.entries()] == ids
    assert plug.hub.store.get(pm.KV) == ["uova", "patate", "grana"]  # old key untouched
    plug.pantry.remove(["uova"])
    plug.hub.store.set(pm.KV, ["uova", "patate", "grana"])
    assert [e["name"] for e in pm.Pantry(plug.hub.store).entries()] == ["patate", "grana"]  # no re-migration


def test_crud_and_validation():
    plug, _ = mk()
    r = run(plug.r_fridge_add(Req({"name": "Latte", "place": "fridge", "qty": "1 l", "expires": iso(2)})))
    e = r["item"]
    assert e["name"] == "latte" and e["qty"] == "1 l" and e["expires"] == iso(2) and e["added"]
    # same ingredient again updates instead of duplicating
    r = run(plug.r_fridge_add(Req({"name": "latte", "place": "freezer", "qty": "2 l"})))
    assert len(r["items"]) == 1 and r["items"][0]["place"] == "freezer" and r["items"][0]["expires"] == iso(2)
    r = run(plug.r_fridge_patch(Req({"place": "fridge", "expires": ""}, id=e["id"])))
    assert r["item"]["place"] == "fridge" and r["item"]["expires"] == "" and r["item"]["name"] == "latte"
    run(plug.r_fridge_add(Req({"name": "uova"})))
    assert run(plug.r_fridge(Req()))["items"][1]["place"] == "fridge"  # default place
    for bad in ({"name": ""}, {"name": "x" * 80}, {"name": "a", "place": "garage"},
                {"name": "a", "expires": "28/10/2026"}, {"name": "a", "expires": "2026-13-40"},
                {"name": "a", "qty": "q" * 50}):
        try:
            run(plug.r_fridge_add(Req(bad)))
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        run(plug.r_fridge_patch(Req({"name": "uova"}, id=e["id"])))
        raise AssertionError("duplicate rename")
    except ValueError:
        pass
    try:
        run(plug.r_fridge_patch(Req({"qty": "1"}, id="nope")))
        raise AssertionError
    except KeyError:
        pass
    r = run(plug.r_fridge_delete(Req(id=e["id"])))
    assert [x["name"] for x in r["items"]] == ["uova"]


def test_finish_to_shopping():
    plug, _ = mk()
    a = run(plug.r_fridge_add(Req({"name": "burro"})))["item"]
    b = run(plug.r_fridge_add(Req({"name": "yogurt"})))["item"]
    r = run(plug.r_fridge_finish(Req({"to_shopping": True}, id=a["id"])))
    assert r["finished"]["name"] == "burro" and [x["name"] for x in r["items"]] == ["yogurt"]
    assert [x["name"].lower() for x in plug.book.shopping()] == ["burro"]
    run(plug.r_fridge_finish(Req({}, id=b["id"])))
    assert len(plug.book.shopping()) == 1 and run(plug.r_fridge(Req()))["items"] == []


def test_summary_and_expiry_logic():
    plug, _ = mk()
    for n, pl, d in (("latte", "fridge", iso(-1)), ("uova", "fridge", iso(1)), ("burro", "fridge", iso(3)),
                     ("piselli", "freezer", iso(30)), ("riso", "pantry", "")):
        plug.pantry.add_entry({"name": n, "place": pl, "expires": d})
    s = plug.pantry.summary(TODAY)
    assert s["total"] == 5 and s["places"] == {"fridge": 3, "freezer": 1, "pantry": 1}
    assert [(e["name"], e["days"]) for e in s["expiring"]] == [("latte", -1), ("uova", 1), ("burro", 3)]
    assert s["expired"] == 1
    assert pm.days_left("", TODAY) is None and pm.days_left(iso(0), TODAY) == 0


def test_daily_notification_dedupe():
    plug, sent = mk()
    asyncio.run(plug._expiry_check(TODAY, 9))
    assert sent == []
    plug.pantry.add_entry({"name": "burro", "expires": iso(3)})
    asyncio.run(plug._expiry_check(TODAY, 9))
    assert sent == []  # 3 days away: not yet
    plug.pantry.add_entry({"name": "latte", "expires": iso(1)})
    asyncio.run(plug._expiry_check(TODAY, 7))
    assert sent == []  # too early
    asyncio.run(plug._expiry_check(TODAY, 9))
    asyncio.run(plug._expiry_check(TODAY, 15))
    assert len(sent) == 1 and "latte" in sent[0][0] and sent[0][2] == "#/recipes/fridge", sent
    asyncio.run(plug._expiry_check(TODAY + timedelta(days=1), 9))
    assert len(sent) == 2  # next day: again (latte now expires today)
    plug.config["expiry_alerts"] = False
    asyncio.run(plug._expiry_check(TODAY + timedelta(days=2), 9))
    assert len(sent) == 2


def test_cook_with_and_legacy_routes_on_new_storage():
    plug, _ = mk(preload=["uova"])
    plug.book.add({"title": "Frittata", "ingredients": ["4 uova", "2 zucchine"]})
    plug.pantry.add_entry({"name": "zucchine", "place": "fridge"})
    assert run(plug.r_pantry(Req()))["items"] == ["uova", "zucchine"]
    out = run(plug.r_cook_with(Req({"ingredients": ["latte"], "use_pantry": True})))["results"]
    assert out == []
    out = run(plug.r_cook_with(Req({"ingredients": ["uova"], "use_pantry": True})))["results"]
    assert out[0]["missing"] == []
    # put keeps entries (place, qty) of names that stay
    plug.pantry.patch_entry(plug.pantry.entries()[1]["id"], {"qty": "3"})
    run(plug.r_pantry_put(Req({"items": ["zucchine", "farina"]})))
    es = plug.pantry.entries()
    assert [(e["name"], e["place"], e["qty"]) for e in es] == [("zucchine", "fridge", "3"), ("farina", "pantry", "")]
    assert run(plug.r_pantry_remove(Req({"items": ["farina"]})))["items"] == ["zucchine"]


def test_shopping_hook_uses_place():
    plug, _ = mk({"pantry_place": "freezer"})
    plug.book.shop_add_manual("Piselli")
    sid = plug.book.shopping()[0]["id"]
    run(plug.r_shop_patch(Req({"checked": True}, id=str(sid))))
    e = plug.pantry.entries()[0]
    assert e["name"] == "piselli" and e["place"] == "freezer"
    plug2, _ = mk({"pantry_place": "bogus"})
    plug2.book.shop_add_manual("Latte")
    run(plug2.r_shop_patch(Req({"checked": True}, id=str(plug2.book.shopping()[0]["id"]))))
    assert plug2.pantry.entries()[0]["place"] == "fridge"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
