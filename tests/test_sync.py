"""Shared feed: merge, validation/privacy, recipe markdown, and a real-git integration test (no network).
Run: python tests/test_sync.py"""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.recipes.db import Book  # noqa: E402
from hub.plugins.sync import SyncPlugin, feed, recipes_md  # noqa: E402
from hub.plugins.sync.merge import merge  # noqa: E402
from hub.store import Store  # noqa: E402


def ev(user, kind, imdb, ts, id, **kw):
    base = dict(rating=None, on=None, watched_at=None)
    base.update(kw)
    return feed.build_event(user, kind, "movie", imdb, ts=ts, id=id, **base)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError as err:
        return str(err)
    raise AssertionError("expected ValueError")


def test_merge_out_of_order():
    a = ev("mango", "rating", "tt0111161", "2026-10-17T07:00:00.000Z", "01JAAAAAAAAAAAAAAAAAAAAAAA", rating=3)
    b = ev("mango", "rating", "tt0111161", "2026-10-17T08:00:00.000Z", "01JBBBBBBBBBBBBBBBBBBBBBBB", rating=4.5)
    c = ev("mango", "watch", "tt0111161", "2026-10-17T06:00:00.000Z", "01JCCCCCCCCCCCCCCCCCCCCCCC",
           watched_at="2026-10-16")
    for order in ([a, b, c], [c, b, a], [b, c, a]):
        m = merge(order)
        assert m[("mango", "tt0111161", "rating")]["rating"] == 4.5 and len(m) == 2


def test_same_millisecond_tie_break_keeps_both():
    ts = "2026-10-17T07:00:00.123Z"
    x = ev("mango", "rating", "tt0111161", ts, "01JAAAAAAAAAAAAAAAAAAAAAA1", rating=2)
    y = ev("kuba", "rating", "tt0111161", ts, "01JAAAAAAAAAAAAAAAAAAAAAA2", rating=5)
    m = merge([y, x])
    assert len(m) == 2  # different users: both kept
    z = ev("mango", "rating", "tt0111161", ts, "01JAAAAAAAAAAAAAAAAAAAAAA2", rating=1)
    for order in ([x, z], [z, x]):  # same user, same ms: greater id wins
        assert merge(order)[("mango", "tt0111161", "rating")]["rating"] == 1


def test_correction_overrides():
    a = ev("mango", "rating", "tt0111161", "2026-10-17T07:00:00.000Z", feed.ulid(), rating=5)
    b = ev("mango", "rating", "tt0111161", "2026-10-17T07:05:00.000Z", feed.ulid(), rating=None)
    w = merge([a, b])[("mango", "tt0111161", "rating")]
    assert w["rating"] is None
    from hub.plugins.sync.merge import is_active
    assert not is_active(w)


def test_ulid_monotonic():
    ids = [feed.ulid() for _ in range(2000)]
    assert ids == sorted(ids) and len(set(ids)) == 2000 and all(feed.ID_RE.match(i) for i in ids)


def test_validation_and_privacy():
    good = feed.build_event("mango", "rating", "movie", "tt0111161", rating=4.5, note="Bellissimo, ore 20:30")
    assert list(good) == list(feed.KEYS)
    for key in ("path", "host", "ip"):
        bad = dict(good, **{key: "x"})
        assert "not allowed" in raises(feed.validate_event, bad)
    assert raises(feed.validate_event, dict(good, rating=4.2))
    assert raises(feed.validate_event, dict(good, imdb_id="tmdb:1"))
    assert raises(feed.validate_event, dict(good, user="Mango"))
    assert raises(feed.validate_event, dict(good, kind="watchlist"))
    assert raises(feed.validate_event, good, "other")
    for note in ("see 192.168.1.20", "at /mnt/nas/film.mkv", "/Volumes/media", "~/films", r"C:\films", r"\\nas\x",
                 "magnet:?xt=urn:btih:abc", "film.torrent", "https://x.test/a", "www.x.test", "fe80::1",
                 "aa:bb:cc:dd:ee:ff", "2001:db8:0:0:0:0:0:1", "/home/pi/x"):
        assert raises(feed.build_event, "mango", "rating", "movie", "tt0111161", rating=3, note=note), note
    for ok in ("version 2.0", "ore 20:30:15", "bel film, 8.5/10", "ciao!"):
        feed.scan_text(ok)
    assert raises(recipes_md.check_tip, "mango", "guarda http://x.test")
    assert raises(recipes_md.validate_recipe, {"title": "X", "author": "mango", "ingredients": ["10.0.0.1"]})
    assert raises(recipes_md.validate_recipe, {"title": "X", "author": "mango", "source": "http://192.168.1.2/x"})
    tmp = Path(tempfile.mkdtemp()) / "mango.jsonl"
    feed.append_events(tmp, [good, good])
    assert len(tmp.read_text().splitlines()) == 1
    tmp.write_text(tmp.read_text() + '{"v":1,"junk":1}\nnot json\n')
    assert len(feed.read_feed(tmp)) == 1


REC = {"title": "Carbonara", "servings": 2, "time_min": 20, "tags": ["primo", "veloce"], "author": "mango",
       "source": "https://www.giallozafferano.it/x.html", "image": "carbonara.jpg",
       "ingredients": ["200 g spaghetti", "# Per la crema", "3 tuorli"],
       "steps": ["Lessate la pasta.", "# Finale", "Mantecate."],
       "tips": [{"user": "kuba", "text": "Solo pecorino."}]}


def test_recipe_md_roundtrip_and_tips():
    rec = recipes_md.validate_recipe(REC)
    text = recipes_md.render(rec)
    assert "## Ingredienti" in text and "- # Per la crema" in text and "- (kuba) Solo pecorino." in text
    assert recipes_md.parse(text) == rec
    en = text.replace("## Ingredienti", "## Ingredients").replace("## Procedimento", "## Method") \
        .replace("## Trucchi", "## Tips")
    assert recipes_md.parse(en) == rec
    old = [{"user": "kuba", "text": "a"}, {"user": "mango", "text": "b"}]
    new = [{"user": "mango", "text": "b"}, {"user": "lia", "text": "c"}]
    assert recipes_md.merge_tips(old, new) == old + [{"user": "lia", "text": "c"}]
    assert recipes_md.merge_tips(old, []) == old
    assert recipes_md.slugify("Spaghetti alla Carbonara! è") == "spaghetti-alla-carbonara-e"
    ex = {"carbonara": ("Carbonara", "mango"), "carbonara-2": ("Carbonara", "kuba")}
    assert recipes_md.pick_slug("Carbonara", "mango", ex) == "carbonara"
    assert recipes_md.pick_slug("Carbonara", "lia", ex) == "carbonara-3"


# -- integration with real git ---------------------------------------------------------------------


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class FakeMeta:
    DB = {"tmdb:278": ("tt0111161", "Le ali della libertà", 1994), "tmdb:238": ("tt0068646", "Il padrino", 1972)}

    async def details(self, type_, id_):
        imdb, title, year = self.DB.get(id_) or next(v for v in self.DB.values() if v[0] == id_)
        return {"id": id_ if id_.startswith("tmdb:") else "tmdb:1", "imdb_id": imdb, "title": title,
                "original_title": title, "year": year, "poster": None, "type": type_}


def make_hub(remote: str, user: str):
    root = Path(tempfile.mkdtemp())
    store = Store(root / "hub.db")
    book = Book(store, root, {"language": "it"})
    hub = SimpleNamespace(
        store=store, data_dir=root, http=None,
        plugins={"media": SimpleNamespace(metadata=FakeMeta()), "recipes": SimpleNamespace(book=book)},
        spawn=lambda coro, name="": (coro.close(), SimpleNamespace(cancel=lambda: None))[1],
        every=lambda *a, **k: None)
    p = SyncPlugin(hub, {"repo_url": remote, "user": user, "share_watchlist": True})
    p.debounce = 3600
    return p, book


async def integration():
    base = Path(tempfile.mkdtemp())
    bare = base / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=base)
    a, abook = make_hub(str(bare), "mango")
    b, _ = make_hub(str(bare), "kuba")
    await a.start()
    await b.start()
    assert a.configured and a.info()["pending"] == 0

    # A: rating, watch, watchlist (via the media plugin's table), a shared recipe
    await a.add_event("rating", "movie", "tmdb:278", rating=4.5, note="Capolavoro")
    await a.add_event("watch", "movie", "tmdb:278", watched_at="2026-10-01")
    a.hub.store.execute("CREATE TABLE IF NOT EXISTS media_watchlist (key TEXT PRIMARY KEY, id TEXT, type TEXT)")
    a.hub.store.execute("INSERT INTO media_watchlist VALUES('movie:tmdb:238','tmdb:238','movie')")
    rid = abook.add({"title": "Carbonara", "servings": 2, "ingredients": ["200 g spaghetti", "3 tuorli"],
                     "steps": ["Lessate.", "Mantecate."], "tags": ["primo"]})
    slug = a.share_recipe(rid)["slug"]
    assert slug == "carbonara" and a.db.recipe(slug)["tips"] == []  # visible locally before the sync
    assert a.info()["pending"] >= 4
    info = await a.sync()
    assert info["last_error"] is None and info["pending"] == 0, info
    assert a.db.card("tt0111161")["title"] == "Le ali della libertà"
    assert [e["kind"] for e in a.db.feed(user="mango", kind="watchlist")] == ["watchlist"]

    # B pulls A's data, then queues its own events and a tip
    info = await b.sync()
    assert info["last_error"] is None, info
    assert {e["user"] for e in b.db.feed()} == {"mango"} and b.db.recipe("carbonara")["author"] == "mango"
    await b.add_event("rating", "movie", "tt0111161", rating=3)
    await b.add_event("watch", "series", "tmdb:278", watched_at="2026-10-02")
    b.add_tip("carbonara", "Spegnete il fuoco prima delle uova.")
    try:
        b.add_tip("carbonara", "vedi /mnt/nas/x")
        raise AssertionError("tip with a path accepted")
    except ValueError:
        pass
    try:
        await a.add_event("rating", "movie", "tmdb:278", rating=4, note="http://x.test")
        raise AssertionError("note with a URL accepted")
    except ValueError:
        pass

    # A queues more; B pushes while A is mid-cycle, so A's first push is rejected and retried
    a.add_tip("carbonara", "Solo guanciale.")
    await a.add_event("rating", "movie", "tmdb:278", rating=5, note="Rivisto, ancora meglio")
    pushes = {"a": 0}

    from hub.plugins.sync import remote as remote_mod
    real_git = remote_mod.Remote.git

    async def racing_git(self, *args, **kw):
        if args and args[0] == "push" and self.user == "mango":
            pushes["a"] += 1
            if pushes["a"] == 1:
                await b.sync()  # someone pushes meanwhile
        return await real_git(self, *args, **kw)

    remote_mod.Remote.git = racing_git
    try:
        info = await a.sync()
    finally:
        remote_mod.Remote.git = real_git
    assert info["last_error"] is None and pushes["a"] == 2, (info, pushes)
    info = await b.sync()
    assert info["last_error"] is None and info["pending"] == 0

    # remote content, checked from a fresh clone
    check = base / "check"
    git("clone", "-q", str(bare), str(check), cwd=base)
    names = {p.name for p in (check / "feeds").glob("*.jsonl")}
    assert names == {"mango.jsonl", "kuba.jsonl"}, names
    mango = feed.read_feed(check / "feeds" / "mango.jsonl")
    kuba = feed.read_feed(check / "feeds" / "kuba.jsonl")
    assert len(mango) == 4 and len(kuba) == 2, (len(mango), len(kuba))
    md = (check / "recipes" / "carbonara.md").read_text()
    assert "- (kuba) Spegnete il fuoco prima delle uova." in md and "- (mango) Solo guanciale." in md
    assert (check / "SCHEMA.md").exists() and (check / "media" / "tt0111161.json").exists()
    log = git("log", "--format=%an <%ae>", cwd=check).split("\n")
    assert "mango <mango@casa.invalid>" in log and "kuba <kuba@casa.invalid>" in log

    # both mirrors agree
    for hub in (a, b):
        tips = {(t["user"], t["text"]) for t in hub.db.recipe("carbonara")["tips"]}
        assert tips == {("kuba", "Spegnete il fuoco prima delle uova."), ("mango", "Solo guanciale.")}
        assert hub.db.users() == ["kuba", "mango"]
        t = hub.db.title("tt0111161", hub.user)
        other = "kuba" if hub.user == "mango" else "mango"
        assert [o["user"] for o in t["others"]] == [other]
        assert {x["rating"] for x in hub.db.feed(kind="rating") if x["user"] == "mango"} == {5}
    t = b.db.title("tt0111161", "kuba")
    assert t["mine"]["rating"] == 3 and t["others"][0]["rating"] == 5 and t["others"][0]["watch"] == "2026-10-01"
    assert a.db.feed(limit=2)[0]["ts"] >= a.db.feed(limit=2)[1]["ts"]

    # import into B's recipe book
    rid_b = b.import_recipe("carbonara")["id"]
    v = b._book().view(rid_b)
    assert v["title"] == "Carbonara" and "Solo guanciale" in v["notes"]

    # nothing local leaked into the repo
    blob = "".join(p.read_text() for p in (check / "feeds").glob("*.jsonl")) + md
    assert str(base) not in blob and "/tmp" not in blob


def test_integration_git():
    asyncio.run(integration())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
