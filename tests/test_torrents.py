"""Torrent search: schema cleaning, dedupe, sort/filter, error handling, provider loading,
Internet Archive parsing and the route. No network (fake sessions and providers)."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.plugins.torrents import PLUGIN, TorrentsPlugin  # noqa: E402
from hub.plugins.torrents import engine as E  # noqa: E402
from hub.plugins.torrents.model import Provider, ProviderError, Result, clean, fetch_json  # noqa: E402
from hub.plugins.torrents.sources.internetarchive import InternetArchive  # noqa: E402

H1 = "a" * 40


def R(title, **kw):
    kw.setdefault("url", "https://example.org/" + title.replace(" ", "-"))
    return Result(title=title, **kw)


def test_clean():
    r = clean(R("  Big   Buck  Bunny ", size="1024", seeders="5", leechers=-3, source="x"))
    assert r.title == "Big Buck Bunny" and r.size == 1024 and r.seeders == 5 and r.leechers is None
    assert r.sources == ["x"]
    assert clean(Result(title="no link")) is None and clean(R("")) is None
    assert clean(R("x", url="javascript:alert(1)")) is None
    m = clean(Result(title="m", magnet="magnet:?xt=urn:btih:%s&dn=m" % H1.upper(), url="ftp://x"))
    assert m.hash == H1 and m.url is None
    assert clean(Result(title="bad", magnet="http://x")) is None


def test_dedupe_merges_same_hash_and_title_size():
    a = clean(R("Foo", hash=H1, seeders=1, source="A", rank=3))
    b = clean(Result(title="foo (copy)", hash=H1, seeders=9, source="B", magnet="magnet:?xt=urn:btih:" + H1, rank=1))
    c = clean(R("Same Title", size=10, source="A"))
    d = clean(R("same  title", size=10, source="B", seeders=2))
    e = clean(R("same title", size=11, source="B"))
    out = E.dedupe([a, b, c, d, e])
    assert len(out) == 3
    hit = [r for r in out if r.hash == H1][0]
    assert hit.seeders == 9 and sorted(hit.sources) == ["A", "B"] and hit.url and hit.magnet and hit.rank == 1


def test_refine_sort_filter():
    rs = [clean(R("a", size=300, seeders=1, rank=2)), clean(R("b", size=100, seeders=50, rank=0)),
          clean(R("c", size=None, seeders=None, rank=1)), clean(R("d", size=200, seeders=7, rank=3))]
    assert [r.title for r in E.refine(rs)] == ["b", "c", "a", "d"]
    assert [r.title for r in E.refine(rs, "seeders")] == ["b", "d", "a", "c"]
    assert [r.title for r in E.refine(rs, "seeders", "asc")] == ["a", "d", "b", "c"]
    assert [r.title for r in E.refine(rs, "size")] == ["a", "d", "b", "c"]          # unknown always last
    assert [r.title for r in E.refine(rs, "size", "asc")] == ["b", "d", "a", "c"]
    assert [r.title for r in E.refine(rs, min_seeders=5)] == ["b", "d"]
    assert [r.title for r in E.refine(rs, max_size=250, sort="size")] == ["d", "b"]  # unknown size fails max_size
    assert [r.title for r in E.refine(rs, min_size=200, sort="size", order="asc")] == ["d", "a"]


class Fake(Provider):
    def __init__(self, id, behave):
        self.id, self.name, self.behave, self.calls = id, id.upper(), behave, 0

    async def search(self, session, query, page, page_size, timeout):
        self.calls += 1
        r = self.behave()
        if isinstance(r, Exception):
            raise r
        if asyncio.iscoroutine(r):
            return await r
        return r


def test_engine_isolates_failures_and_rate_limits():
    async def slow():
        await asyncio.sleep(5)

    ps = {p.id: p for p in (
        Fake("ok", lambda: [R("one", rank=0), R("two", rank=1)]),
        Fake("slow", lambda: slow()),
        Fake("limited", lambda: ProviderError("rate_limited", "rate limited (HTTP 429)", 60)),
        Fake("bad", lambda: "not a list"),
        Fake("boom", lambda: 1 / 0),
        Fake("down", lambda: ProviderError("unavailable", "HTTP 502")),
    )}
    eng = E.Engine(ps, timeout=0.2)
    merged, st, more = asyncio.run(eng.search(None, "q", 1, 2))
    by = {s["id"]: s for s in st}
    assert [r.title for r in merged] == ["one", "two"] and more is True
    assert by["ok"]["ok"] and by["ok"]["count"] == 2
    assert by["slow"]["kind"] == "timeout" and by["limited"]["kind"] == "rate_limited"
    assert by["bad"]["kind"] == "malformed" and by["boom"]["kind"] == "error" and by["down"]["kind"] == "unavailable"
    asyncio.run(eng.search(None, "q", 1, 2))
    assert ps["limited"].calls == 1                      # cooling down: not asked again
    _, st, _ = asyncio.run(eng.search(None, "q", 1, 2, ids=["ok"]))
    assert [s["id"] for s in st] == ["ok"]


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status, self.headers, self._b, self._t = status, headers or {}, body, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if self._t is not None:
            return json.loads(self._t)
        return self._b


class Sess:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, **kw):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def kind(resp):
    try:
        asyncio.run(fetch_json(Sess(resp), "http://x"))
    except ProviderError as err:
        return err.kind, err.retry_after
    return "ok", 0


def test_fetch_json_error_kinds():
    assert kind(Resp(body={"a": 1})) == ("ok", 0)
    assert kind(Resp(429, headers={"Retry-After": "7"})) == ("rate_limited", 7)
    assert kind(Resp(429, headers={"Retry-After": "99999"})) == ("rate_limited", 300)
    assert kind(Resp(500))[0] == "unavailable"
    assert kind(Resp(text="<html>"))[0] == "malformed"
    assert kind(asyncio.TimeoutError())[0] == "timeout"
    assert kind(aiohttp.ClientConnectionError("down"))[0] == "unavailable"


def test_internetarchive_parsing_and_errors():
    docs = {"response": {"docs": [
        {"identifier": "bbb", "title": ["Big Buck Bunny"], "item_size": 276134947},
        {"identifier": "x2", "title": "Other", "item_size": "12"},
        {"title": "no id"}, "junk"]}}
    out = asyncio.run(InternetArchive().search(Sess(Resp(body=docs)), 'big "buck" (bunny)', 1, 20, 5))
    assert [r.title for r in out] == ["Big Buck Bunny", "Other"] and out[0].rank == 0
    assert out[0].url == "https://archive.org/download/bbb/bbb_archive.torrent" and out[0].source == "Internet Archive"
    assert [r.title for r in map(clean, out)] == ["Big Buck Bunny", "Other"]
    for bad in ({"response": {}}, {"nope": 1}, [], {"response": {"docs": "x"}}):
        try:
            asyncio.run(InternetArchive().search(Sess(Resp(body=bad)), "q", 1, 20, 5))
        except ProviderError as err:
            assert err.kind == "malformed"
        else:
            raise AssertionError(bad)
    assert asyncio.run(InternetArchive().search(Sess(Resp(body=docs)), '"()"', 1, 20, 5)) == []


def test_discover_loads_user_providers_and_survives_broken_ones():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mine.py").write_text(
            "from hub.plugins.torrents.model import Provider, Result\n"
            "class Mine(Provider):\n    id='mine'; name='Mine'\n"
            "    async def search(self, s, q, p, n, t):\n        return [Result(title=q, url='https://e.org/1')]\n"
            "SOURCE = Mine\n")
        (Path(d) / "broken.py").write_text("raise RuntimeError('oops')\n")
        (Path(d) / "_skip.py").write_text("SOURCE = 1\n")
        found = E.discover(d)
    assert set(found) == {"internetarchive", "mine"}
    assert set(E.discover("")) == {"internetarchive"}
    assert set(E.discover("/nonexistent")) == {"internetarchive"}


class Req:
    def __init__(self, query=None):
        self.query = query or {}


def make(tmp, cfg=None):
    hub = Hub({"homekit": {"enabled": False}}, Path(tmp))
    p = TorrentsPlugin(hub, cfg or {})
    asyncio.run(p.start())
    return p


def call(p, fn, req):
    try:
        r = asyncio.run(fn(req))
    except Exception:
        asyncio.run(p.stop())
        raise
    return r.status, json.loads(r.text)


def test_contract_and_route():
    assert PLUGIN is TorrentsPlugin and TorrentsPlugin.name == "torrents"
    with tempfile.TemporaryDirectory() as d:
        async def go():
            p = TorrentsPlugin(Hub({"homekit": {"enabled": False}}, Path(d)), {})
            await p.start()
            p.engine.providers = {"fake": Fake("fake", lambda: [R("Alpha", size=5, seeders=1, rank=0),
                                                                  R("Beta", size=9, seeders=8, rank=1)])}
            try:
                out = {}
                out["all"] = json.loads((await p.r_search(Req({"q": "x y"}))).text)
                out["seed"] = json.loads((await p.r_search(Req({"q": "xy", "sort": "seeders"}))).text)
                out["flt"] = json.loads((await p.r_search(Req({"q": "xy", "min_seeders": "5"}))).text)
                for bad in ({}, {"q": "a"}, {"q": "x" * 101}, {"q": "ok", "sort": "bogus"}):
                    try:
                        await p.r_search(Req(bad))
                        raise AssertionError(bad)
                    except ValueError:
                        pass
                p.engine.providers = {"d": Fake("d", lambda: ProviderError("unavailable", "HTTP 500"))}
                out["down"] = json.loads((await p.r_search(Req({"q": "xy"}))).text)
                p.engine.providers = {}
                out["none"] = json.loads((await p.r_search(Req({"q": "xy"}))).text)
                return out
            finally:
                await p.stop()
        o = asyncio.run(go())
    assert [r["title"] for r in o["all"]["results"]] == ["Alpha", "Beta"] and o["all"]["error"] is None
    assert {"id", "title", "size", "seeders", "leechers", "source", "magnet", "url"} <= set(o["all"]["results"][0])
    assert [r["title"] for r in o["seed"]["results"]] == ["Beta", "Alpha"]
    assert [r["title"] for r in o["flt"]["results"]] == ["Beta"]
    assert o["down"]["results"] == [] and o["down"]["error"] and o["down"]["providers"][0]["kind"] == "unavailable"
    assert o["none"]["error"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
