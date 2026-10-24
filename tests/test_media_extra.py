"""Media: genre filters, library file download route, deletion safety. Temp dirs only, no network.
Run: python tests/test_media_extra.py"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from hub.plugins.media import MediaPlugin  # noqa: E402
from hub.plugins.media.downloads import Downloads  # noqa: E402
from hub.plugins.media.files import (check_deletable, content_disposition, delete_paths,  # noqa: E402
                                     hid, inside)
from hub.plugins.media.library import Library  # noqa: E402
from hub.plugins.media.metadata import Metadata, year_range  # noqa: E402
from hub.store import Store  # noqa: E402

BODY = bytes(range(256)) * 40  # 10240 bytes


def make_library(d: str):
    movies, series = Path(d, "movies"), Path(d, "series")
    m = movies / "Scary Film (2020)"
    m.mkdir(parents=True)
    (m / "Scary Film (2020) 1080p.mkv").write_bytes(BODY)
    (m / "Scary Film (2020) 1080p.it.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nCiao\n")
    s = series / "Show (2019)" / "Season 1"
    s.mkdir(parents=True)
    (s / "Show - S01E01 - Pilot.mkv").write_bytes(BODY[:2000])
    Path(d, "secret.txt").write_text("top secret")
    os.symlink(Path(d, "secret.txt"), movies / "Evil (2021).mkv")  # symlink escaping the library
    store = Store(Path(d, "t.db"))
    plugin = SimpleNamespace(hub=SimpleNamespace(store=store))
    lib = Library(plugin, {"movies_dir": str(movies), "series_dir": str(series)})
    asyncio.run(lib.scan())
    return lib, movies, series


def app_for(lib):
    p = MediaPlugin.__new__(MediaPlugin)
    p.library = lib
    p.downloads = SimpleNamespace()
    app = web.Application()
    app.add_routes([web.get("/library/file/{id}", p.r_lib_file),
                    web.get("/library/file/{id}/subtitle/{n}", p.r_lib_file),
                    web.get("/library/file/{id}/info", p.r_lib_file_info),
                    web.post("/library/file/{id}/delete", p.r_lib_delete)])
    return app


def test_file_route():
    with tempfile.TemporaryDirectory() as d:
        lib, movies, series = make_library(d)
        rows = lib.browse("movies")
        by = {r["title"]: r for r in rows}
        assert "path" not in by["Scary Film"]
        fid = by["Scary Film"]["file_id"]

        async def run():
            async with TestClient(TestServer(app_for(lib))) as c:
                r = await c.get(f"/library/file/{fid}")
                assert r.status == 200 and await r.read() == BODY
                cd = r.headers["Content-Disposition"]
                assert cd.startswith("attachment;") and "filename*=UTF-8''Scary%20Film" in cd, cd
                assert r.headers["Accept-Ranges"] == "bytes"
                # Range
                r = await c.get(f"/library/file/{fid}", headers={"Range": "bytes=10-19"})
                assert r.status == 206 and await r.read() == BODY[10:20]
                assert r.headers["Content-Range"] == f"bytes 10-19/{len(BODY)}"
                r = await c.get(f"/library/file/{fid}", headers={"Range": "bytes=10000-"})
                assert r.status == 206 and await r.read() == BODY[10000:]
                r = await c.get(f"/library/file/{fid}", headers={"Range": "bytes=999999-"})
                assert r.status == 416
                # info: size + subtitle, then the subtitle file
                info = await (await c.get(f"/library/file/{fid}/info")).json()
                assert info["size"] == len(BODY) and info["subtitles"][0]["name"].endswith(".it.srt")
                r = await c.get(f"/library/file/{fid}/subtitle/0")
                assert r.status == 200 and b"Ciao" in await r.read()
                assert (await c.get(f"/library/file/{fid}/subtitle/5")).status == 404
                # unknown / malformed / traversal-looking ids never reach the file system
                for bad in ("m-" + "0" * 20, "x-abc", "../../etc/passwd", "m-..%2f..%2fetc", "e-" + hid("/etc/passwd")):
                    r = await c.get(f"/library/file/{bad}")
                    assert r.status == 404, (bad, r.status)
                # a symlink inside the library pointing outside is refused
                evil = by["Evil"]["file_id"]
                r = await c.get(f"/library/file/{evil}")
                assert r.status == 403, r.status
        asyncio.run(run())


def test_disposition_and_paths():
    cd = content_disposition("Perché è così (2020).mkv")
    assert 'filename="Perch' in cd and "filename*=UTF-8''Perch%C3%A9%20%C3%A8" in cd
    nasty = content_disposition('a"b\r\nc;.mkv')
    assert "\n" not in nasty and "\r" not in nasty and nasty.count('"') == 2, nasty
    with tempfile.TemporaryDirectory() as d:
        root = Path(d, "lib")
        (root / "a").mkdir(parents=True)
        f = root / "a" / "x.mkv"
        f.write_text("x")
        assert inside(f, [str(root)]) == f.resolve()
        assert inside(root, [str(root)]) is None              # a root itself
        assert inside(root / ".." / "lib" / "a", [str(root)]) is not None
        assert inside(root / ".." / "other", [str(root)]) is None
        assert inside(f, [""]) is None and inside("/", ["/"]) is None


def test_delete_safety():
    with tempfile.TemporaryDirectory() as d:
        lib, movies, series = make_library(d)
        roots = lib.roots
        outside = Path(d, "secret.txt")
        for bad in (str(movies), str(series), str(outside), str(movies / "Evil (2021).mkv"),
                    str(movies / ".." / "secret.txt"), "/etc/hosts"):
            try:
                check_deletable([bad], roots)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"should refuse {bad}")
        # a symlinked folder inside the library is never walked through
        os.symlink(Path(d), movies / "Back")
        try:
            check_deletable([str(movies / "Back" / "secret.txt")], roots)
        except PermissionError:
            pass
        else:
            raise AssertionError("symlinked dir")
        assert outside.exists()
        # deleting an episode removes it and prunes the emptied Season / show folders, never the root
        ep = next(r for r in lib.store.query("SELECT path FROM media_lib_episodes"))["path"]
        fid = "e-" + hid(ep)
        res = lib.delete_entry(fid)
        assert res["deleted"] == 1 and not os.path.exists(ep)
        assert not Path(series, "Show (2019)").exists() and series.is_dir()
        # movie: video + subtitle go, folder pruned
        mid = next(r["file_id"] for r in lib.browse("movies") if r["title"] == "Scary Film")
        res = lib.delete_entry(mid)
        assert res["deleted"] == 2 and not Path(movies, "Scary Film (2020)").exists() and movies.is_dir()
        # delete route needs an explicit confirmation
        async def run():
            async with TestClient(TestServer(app_for(lib))) as c:
                r = await c.post(f"/library/file/{mid}/delete", json={})
                assert r.status == 400
        asyncio.run(run())
        delete_paths([], roots)


def test_downloads_remove_files():
    with tempfile.TemporaryDirectory() as d:
        dl = Path(d, "dl")
        (dl / "Some.Release").mkdir(parents=True)
        (dl / "Some.Release" / "a.mkv").write_bytes(b"x" * 100)
        store = Store(Path(d, "t.db"))
        removed = []

        class TR:
            enabled = True
            async def torrents(self, hashes=None, files=False):
                return []
            async def remove(self, h, delete_data=False):
                removed.append((h, delete_data))
        plugin = SimpleNamespace(hub=SimpleNamespace(store=store, publish=lambda *a, **k: None),
                                 transmission=TR(), library=SimpleNamespace())
        dls = Downloads(plugin, {"download_dir": str(dl), "movies_dir": d + "/m", "series_dir": d + "/s"})
        store.execute("INSERT INTO media_downloads (hash,name,key,type,title,state,added,data) VALUES "
                      "('h1','Some.Release','movie:1','movie','T','completed',0,'{}')")
        info = asyncio.run(dls.info("h1"))
        assert info["files"]["size"] == 100 and info["files"]["path"].endswith("Some.Release")
        asyncio.run(dls.action("h1", "remove_files"))
        assert not (dl / "Some.Release").exists() and dl.is_dir() and removed == [("h1", True)]
        assert not store.query("SELECT * FROM media_downloads")
        # a name that tries to climb out of the download folder is ignored
        store.execute("INSERT INTO media_downloads (hash,name,key,type,title,state,added,data) VALUES "
                      "('h2','../../etc','movie:1','movie','T','failed',0,'{}')")
        assert asyncio.run(dls.info("h2"))["files"] is None


def test_genres_and_params():
    assert year_range("2019") == ("2019-01-01", "2019-12-31")
    assert year_range("2010s") == ("2010-01-01", "2019-12-31")
    assert year_range("20x") == ("", "") and year_range("") == ("", "")
    md = Metadata.__new__(Metadata)
    md.key, md.language, md.region = "k", "it-IT", "IT"
    calls = []

    async def tmdb(path, ttl=0, persist=False, **params):
        calls.append((path, params))
        if path == "/genre/tv/list":
            return {"genres": [{"id": 18, "name": "Dramma"}, {"id": 16, "name": "Animazione"}]}
        if path == "/search/keyword":
            return {"results": [{"id": 111, "name": "Horror Comedy"}, {"id": 315058, "name": params["query"]}]}
        return {"results": [{"id": 1, "name": "S", "backdrop_path": "/b.jpg", "first_air_date": "2020-05-05"}],
                "total_pages": 2}
    md._tmdb = tmdb
    g = asyncio.run(md.genres("series"))
    ids = [x["id"] for x in g]
    assert "v:horror" in ids and "v:thriller" in ids and "v:anime" in ids and "18" in ids
    r = asyncio.run(md.discover("series", "v:horror", 1, "rated"))
    path, p = calls[-1]
    assert path == "/discover/tv" and p["with_keywords"].split("|")[0] == "315058", p
    assert p["sort_by"] == "vote_average.desc" and p["vote_count.gte"] == 200 and r["has_more"]
    asyncio.run(md.discover("series", "v:anime", 1))
    assert calls[-1][1]["with_genres"] == "16" and calls[-1][1]["with_original_language"] == "ja"
    asyncio.run(md.discover("movie", "27", 1, "recent", "2010s"))
    p = calls[-1][1]
    assert p["sort_by"] == "primary_release_date.desc" and p["primary_release_date.gte"] == "2010-01-01"
    assert p["primary_release_date.lte"] == "2019-12-31" and p["with_genres"] == "27"
    asyncio.run(md.discover("series", "18", 1, "recent"))
    p = calls[-1][1]
    assert p["sort_by"] == "first_air_date.desc" and "first_air_date.lte" in p
    try:
        asyncio.run(md.discover("series", "v:nope"))
    except ValueError:
        pass
    else:
        raise AssertionError
    try:
        asyncio.run(md.discover("series", "1;drop"))
    except ValueError:
        pass
    else:
        raise AssertionError
    imgs = asyncio.run(md.genre_images("series"))
    assert imgs["18"].endswith("/b.jpg") and "/w780/" in imgs["18"]


def test_library_genre_filter():
    with tempfile.TemporaryDirectory() as d:
        lib, movies, series = make_library(d)
        lib.store.execute("UPDATE media_lib_movies SET genres='[\"27\",\"53\"]' WHERE title='Scary Film'")
        assert [r["title"] for r in lib.browse("movies", "27")] == ["Scary Film"]
        assert lib.browse("movies", "35") == []
        asyncio.run(lib.scan())  # genres survive a rescan
        assert lib.browse("movies", "53")[0]["title"] == "Scary Film"


if __name__ == "__main__":
    test_file_route(); test_disposition_and_paths(); test_delete_safety()
    test_downloads_remove_files(); test_genres_and_params(); test_library_genre_filter()
    print("media extra OK")
