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
from hub.plugins.torrents.torznab import TorznabProvider, clean_url  # noqa: E402

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


TZ = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Ubuntu 24.04 ISO</title><link>http://jk/dl/1?apikey=SECRET</link>
<enclosure url="http://jk/dl/1?apikey=SECRET" length="5000" type="application/x-bittorrent"/>
<torznab:attr name="seeders" value="40"/><torznab:attr name="peers" value="55"/>
<torznab:attr name="size" value="6000"/><torznab:attr name="infohash" value="%s"/></item>
<item><title>Other</title><enclosure url="http://jk/dl/2" length="77"/>
<torznab:attr name="magneturl" value="magnet:?xt=urn:btih:%s&amp;dn=o"/><torznab:attr name="seeders" value="x"/></item>
<item><title></title></item></channel></rss>""" % (H1, "b" * 40)


class TextResp(Resp):
    async def text(self, errors=None):
        return self._t


def tz_search(body, status=200, **kw):
    prov = TorznabProvider("tz1", "My JK", "http://jk/api", "SECRET")
    sess = Sess(TextResp(status, text=body))
    seen = {}
    orig = sess.get
    sess.get = lambda url, **k: (seen.update(k, url=url), orig(url, **k))[1]
    out = asyncio.run(prov.search(sess, "ubuntu", 2, 10, 5))
    return out, seen


def test_torznab_parsing():
    out, seen = tz_search(TZ)
    assert seen["params"] == {"t": "search", "q": "ubuntu", "offset": 10, "limit": 10, "apikey": "SECRET"}
    assert [r.title for r in out] == ["Ubuntu 24.04 ISO", "Other"]
    a, b = out
    assert (a.seeders, a.leechers, a.size, a.hash) == (40, 15, 6000, H1) and a.source == "My JK"
    assert a.magnet.startswith("magnet:?xt=urn:btih:" + H1) and a.url is None    # key-carrying link dropped
    assert b.magnet.startswith("magnet:?xt=urn:btih:" + "b" * 40) and b.seeders is None and b.size == 77
    assert b.url == "http://jk/dl/2" and all(clean(r) for r in out)
    for body, kind in (('<error code="100" description="Wrong key"/>', "error"), ("<html/>", "malformed"),
                       ("nope", "malformed"), ('<!DOCTYPE x [<!ENTITY a "b">]><rss/>', "malformed")):
        try:
            tz_search(body)
        except ProviderError as err:
            assert err.kind == kind, (body, err.kind)
        else:
            raise AssertionError(body)
    try:
        tz_search("", 429)
    except ProviderError as err:
        assert err.kind == "rate_limited"
    try:
        tz_search("", 401)
    except ProviderError as err:
        assert "API key" in str(err)
    assert clean_url("http://h:9117/a/b?apikey=K&t=x&foo=1") == ("http://h:9117/a/b?foo=1", "K")
    for bad in ("ftp://x", "javascript:1", "", "http://"):
        try:
            clean_url(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


class FakeIx:
    """Stands in for media.indexers.Indexers: no network."""
    def __init__(self, cfg):
        self.cfg, self.health, self.opened, self.calls = cfg, {}, False, []

    async def open(self):
        self.opened = True

    async def close(self):
        self.opened = False

    async def _probe_one(self, name):
        ok = name != "yts"
        self.health[name] = {"ok": ok, "mirror": "http://m/" + name if ok else None,
                             "error": None if ok else "unreachable", "checked": 1e12}

    async def _s_knaben(self, base, q):
        self.calls.append((base, q.titles, q.type, q.imdb_id))
        return [{"name": "Low", "hash": "C" * 40, "size": 5, "seeders": 1, "leechers": 0, "source": "x"},
                {"name": "High", "hash": H1.upper(), "size": "9", "seeders": 30, "leechers": 4, "url": "https://d/1"},
                {"name": "", "hash": H1}]


def test_media_adapter_maps_and_isolates():
    from hub.plugins.torrents import media_adapter as M
    be = M.MediaBackend({"proxy": {"url": "http://p:1"}, "mirrors": {"knaben": ["http://z"]}}, FakeIx)
    assert be.idx.cfg["indexers"] == list(M.SITES) and "torrentio" not in M.SITES and "eztv" not in M.SITES
    prov = M.MediaProvider(be, "knaben")
    out = asyncio.run(prov.search(None, "my query", 1, 20, 5))
    assert be.idx.opened and be.idx.calls == [("http://m/knaben", ["my query"], "movie", None)]
    assert [r.title for r in out] == ["High", "Low"] and out[0].rank == 0 and out[0].source == "Knaben"
    assert out[0].hash == H1 and out[0].magnet.startswith("magnet:?xt=urn:btih:" + H1) and out[0].url == "https://d/1"
    assert (out[0].size, out[0].seeders, out[0].leechers) == (9, 30, 4)
    assert asyncio.run(prov.search(None, "q", 2, 20, 5)) == []
    try:
        asyncio.run(M.MediaProvider(be, "yts").search(None, "q", 1, 20, 5))
    except ProviderError as err:
        assert err.kind == "unavailable" and "unreachable" in str(err)
    else:
        raise AssertionError


TZ_EMPTY = '<rss version="2.0"><channel></channel></rss>'


def test_category_hint_only_reaches_providers_that_take_it():
    class Cat(Provider):
        id, name = "cat", "Cat"

        def __init__(self):
            self.got = []

        async def search(self, session, query, page, page_size, timeout, category=""):
            self.got.append(category)
            return [R("a")]

    old, cat = Fake("old", lambda: [R("b")]), Cat()
    eng = E.Engine({"old": old, "cat": cat})
    merged, st, _more = asyncio.run(eng.search(None, "x", 1, 10, category="games"))
    assert cat.got == ["games"] and old.calls == 1 and all(s["ok"] for s in st) and len(merged) == 2
    asyncio.run(eng.search(None, "x", 1, 10))
    assert cat.got == ["games", ""]
    # Torznab turns it into cat=4000,1000 (PC, Console), and sends nothing without it
    prov = TorznabProvider("tz1", "JK", "http://jk/api", "SECRET")
    for cat_arg, want in (("games", "4000,1000"), ("", None)):
        sess = Sess(TextResp(200, text=TZ_EMPTY))
        seen = {}
        orig = sess.get
        sess.get = lambda url, _o=orig, **k: (seen.update(k), _o(url, **k))[1]
        asyncio.run(prov.search(sess, "q", 1, 10, 5, category=cat_arg))
        assert seen["params"].get("cat") == want


class JReq:
    def __init__(self, body=None, id=None, query=None):
        self._b, self.match_info, self.query = body, {"id": id}, query or {}

    async def json(self):
        return self._b


def test_tracker_management_persists_in_store():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub = Hub({"homekit": {"enabled": False}}, Path(d))
            p = TorrentsPlugin(hub, {"providers": ["internetarchive"]})
            await p.start()
            try:
                st, lst = (await p.r_trackers(None)).status, json.loads((await p.r_trackers(None)).text)["trackers"]
                ids = {t["id"]: t for t in lst}
                assert {"internetarchive", "knaben", "apibay", "yts", "ilcorsaronero", "1337x"} <= set(ids)
                assert "torrentio" not in ids and "eztv" not in ids
                assert ids["internetarchive"]["enabled"] and not ids["knaben"]["enabled"]
                assert ids["knaben"]["kind"] == "media" and ids["internetarchive"]["kind"] == "builtin"
                # toggle without restart; persisted in the store
                r = await p.r_edit(JReq({"enabled": True}, "knaben"))
                assert json.loads(r.text)["enabled"] and "knaben" in p.enabled_ids()
                assert "knaben" in hub.store.get("torrents.enabled")
                # add (api key moved out of the URL), never echoed
                r = await p.r_add(JReq({"name": " My  JK ", "url": "http://jk:9117/api?apikey=SECRET"}))
                new = json.loads(r.text)
                assert r.status == 201 and new["name"] == "My JK" and new["has_key"] and "SECRET" not in r.text
                assert new["enabled"] and new["url"] == "http://jk:9117/api" and new["kind"] == "torznab"
                tid = new["id"]
                assert "SECRET" not in (await p.r_trackers(None)).text
                assert hub.store.get("torrents.trackers")[0]["key"] == "SECRET"
                # edit keeps the key when the field is empty, replaces it otherwise
                r = await p.r_edit(JReq({"name": "Renamed", "url": "https://jk2/api", "key": ""}, tid))
                assert json.loads(r.text)["name"] == "Renamed" and p.engine.providers[tid].key == "SECRET"
                assert p.engine.providers[tid].url == "https://jk2/api"
                await p.r_edit(JReq({"key": "NEW"}, tid))
                assert hub.store.get("torrents.trackers")[0]["key"] == "NEW"
                await p.r_edit(JReq({"enabled": False}, tid))
                assert tid not in p.enabled_ids()
                # validation
                for bad in ({"name": "x", "url": "ftp://h"}, {"name": "", "url": "http://h"}, {"url": "http://h"}, []):
                    try:
                        await p.r_add(JReq(bad))
                        raise AssertionError(bad)
                    except ValueError:
                        pass
                for fn, body in ((p.r_edit, {"name": "x"}), (p.r_delete, None)):
                    try:
                        await fn(JReq(body, "knaben"))
                        raise AssertionError
                    except ValueError:
                        pass
                try:
                    await p.r_delete(JReq(None, "nope"))
                    raise AssertionError
                except KeyError:
                    pass
                # a fresh plugin (restart) sees the same state
                await p.stop()
                p = TorrentsPlugin(hub, {"providers": ["internetarchive"]})
                await p.start()
                assert tid in p.engine.providers and "knaben" in p.enabled_ids() and tid not in p.enabled_ids()
                await p.r_delete(JReq(None, tid))
                assert tid not in p.engine.providers and hub.store.get("torrents.trackers") == []
                assert tid not in hub.store.get("torrents.enabled")
                # disabling everything gives the "no tracker" message, searches only enabled ones
                for i in list(p.enabled_ids()):
                    await p.r_edit(JReq({"enabled": False}, i))
                out = json.loads((await p.r_search(Req({"q": "ubuntu"}))).text)
                assert out["error"] and out["providers"] == []
                p.engine.providers["f1"] = Fake("f1", lambda: [R("A")])
                p.engine.providers["f2"] = Fake("f2", lambda: [R("B")])
                await p.r_edit(JReq({"enabled": True}, "f1"))
                out = json.loads((await p.r_search(Req({"q": "ubuntu"}))).text)
                assert [s["id"] for s in out["providers"]] == ["f1"] and out["providers"][0]["ms"] is not None
                assert json.loads((await p.r_trackers(None)).text)["trackers"][0] is not None
                return True
            finally:
                await p.stop()
        assert asyncio.run(go())


def test_tracker_test_route_and_draft():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            p = TorrentsPlugin(Hub({"homekit": {"enabled": False}}, Path(d)), {})
            await p.start()
            try:
                p.engine.providers["ok"] = Fake("ok", lambda: [R("x")])
                p.engine.providers["bad"] = Fake("bad", lambda: ProviderError("unavailable", "HTTP 502"))
                ok = json.loads((await p.r_test(JReq({}, "ok"))).text)
                bad = json.loads((await p.r_test(JReq({}, "bad"))).text)
                assert ok["ok"] and ok["count"] == 1 and bad["error"] == "HTTP 502" and not bad["ok"]
                hl = {t["id"]: t["health"] for t in json.loads((await p.r_trackers(None)).text)["trackers"]}
                assert hl["ok"]["ok"] and not hl["bad"]["ok"]
                try:
                    await p.r_test_draft(JReq({"url": "nope"}))
                    raise AssertionError
                except ValueError:
                    pass
                return True
            finally:
                await p.stop()
        assert asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
