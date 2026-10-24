"""Music plugin: Subsonic client, MusicBrainz, owned matching, import, queue, KEF routing, accept_download.
No network and nothing outside temp dirs: Navidrome is a local fake server, outputs are fakes."""
import asyncio
import hashlib
import io
import sys
import tempfile
from types import SimpleNamespace
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.plugins.games.find import UnpackError  # noqa: E402
from hub.plugins.music import MusicPlugin, discover as D, find as F, musicbrainz as MB, subsonic as S  # noqa: E402
from hub.plugins.music.outputs import AirPlayOutput  # noqa: E402
from hub.plugins.music.player import Output, Player  # noqa: E402


# -- Subsonic -----------------------------------------------------------------------------------

def test_subsonic_auth_and_mapping():
    p = S.auth_params("kuba", "secret", salt="abc")
    assert p["t"] == hashlib.md5(b"secretabc").hexdigest() and p["s"] == "abc"
    assert p["v"] == "1.16.1" and p["c"] == "casa" and p["f"] == "json" and p["u"] == "kuba"
    assert "password" not in "".join(p.values())
    u = S.Subsonic("http://x:1/", "kuba", "secret", None)
    assert u.stream_url("al-1").startswith("http://x:1/rest/stream?") and "format=mp3" in u.stream_url("al-1")
    assert "maxBitRate=320" in u.stream_url("al-1")
    a = S.map_album({"id": 5, "name": "OK Computer", "artist": "Radiohead", "year": 1997, "coverArt": "al-5",
                     "songCount": 12, "musicBrainzId": "m"})
    assert a["id"] == "5" and a["cover"] == "al-5" and a["mbid"] == "m" and a["songs"] == 12
    s = S.map_song({"id": "t1", "title": "Airbag", "track": 1, "suffix": "FLAC", "albumId": "al-5"})
    assert s["suffix"] == "flac" and s["cover"] == "al-5" and s["disc"] == 1
    assert S.valid_id("al-5_x") and not S.valid_id("../x") and not S.valid_id("a b")


class FakeNavidrome:
    def __init__(self):
        self.calls, self.scans = [], 0
        app = web.Application()
        app.router.add_get("/rest/{ep}", self.h)
        self.server = TestServer(app)

    async def h(self, request):
        q = request.query
        ep = request.match_info["ep"]
        self.calls.append((ep, dict(q)))
        if q.get("t") != hashlib.md5(("pw" + q.get("s", "")).encode()).hexdigest():
            return web.json_response({"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "bad"}}})
        ok = {"status": "ok", "version": "1.16.1"}
        if ep == "getAlbumList2":
            ok["albumList2"] = {"album": [{"id": "al1", "name": "Kid A", "artist": "Radiohead", "year": 2000,
                                           "coverArt": "al-al1"}, {"id": "al2", "name": "Low", "artist": "David Bowie"}]}
        elif ep == "getAlbum":
            ok["album"] = {"id": "al1", "name": "Kid A", "artist": "Radiohead", "song": [
                {"id": "s1", "title": "Everything", "track": 1, "duration": 1, "suffix": "flac"},
                {"id": "s2", "title": "Kid A", "track": 2, "duration": 1, "suffix": "flac"}]}
        elif ep == "getGenres":
            ok["genres"] = {"genre": [{"value": "Rock", "albumCount": 3, "songCount": 30},
                                      {"value": "Jazz", "albumCount": 5, "songCount": 40},
                                      {"value": "Empty", "albumCount": 0, "songCount": 0}]}
        elif ep == "download":
            body = b"Z" * 200000 if q["id"].startswith("al") else b"FLACDATA"
            rng = request.headers.get("Range")
            if rng:
                a, b = rng.replace("bytes=", "").split("-")
                part = body[int(a):int(b or len(body) - 1) + 1]
                return web.Response(status=206, body=part, content_type="audio/flac",
                                    headers={"Content-Range": f"bytes {a}-{int(a) + len(part) - 1}/{len(body)}"})
            if q["id"] == "missing":
                return web.json_response({"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "x"}}})
            return web.Response(body=body, content_type="application/zip" if q["id"].startswith("al") else "audio/flac")
        elif ep == "getSong":
            ok["song"] = {"id": q["id"], "title": "T" + q["id"], "artist": "Ré/diohead", "duration": 1, "suffix": "flac",
                          "size": 8}
        elif ep == "startScan":
            self.scans += 1
            ok["scanStatus"] = {"scanning": True}
        elif ep == "getCoverArt":
            return web.Response(body=b"\xff\xd8\xff\xe0JPEG", content_type="image/jpeg")
        elif ep == "stream":
            body = b"0123456789"
            rng = request.headers.get("Range")
            if rng:
                a, b = rng.replace("bytes=", "").split("-")
                part = body[int(a):int(b or len(body) - 1) + 1]
                return web.Response(status=206, body=part, content_type="audio/flac",
                                    headers={"Content-Range": f"bytes {a}-{int(a) + len(part) - 1}/{len(body)}"})
            return web.Response(body=body, content_type="audio/mpeg")
        return web.json_response({"subsonic-response": ok})


# -- MusicBrainz --------------------------------------------------------------------------------

def test_limiter_spaces_requests():
    now = [0.0]
    sleeps = []

    async def sleep(d):
        sleeps.append(round(d, 2))
        now[0] += d

    lim = MB.Limiter(1.0, clock=lambda: now[0], sleep=sleep)

    async def go():
        await asyncio.gather(*(lim.wait() for _i in range(3)))
        now[0] += 5
        await lim.wait()
    asyncio.run(go())
    assert sleeps == [1.0, 1.0], sleeps          # first free, 2nd and 3rd wait, the one after a pause is free


def test_mb_mapping_sorting_and_matching():
    g = MB.map_group({"id": "g1", "title": "Kid A", "primary-type": "Album", "first-release-date": "2000-10-02",
                      "artist-credit": [{"name": "Radiohead", "joinphrase": "", "artist": {"id": "a1"}}]})
    assert g["year"] == 2000 and g["artist"] == "Radiohead" and g["artist_mbid"] == "a1"
    gs = [MB.map_group(x) for x in [
        {"id": "1", "title": "Live", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "1999"},
        {"id": "2", "title": "EP", "primary-type": "EP", "first-release-date": "1991"},
        {"id": "3", "title": "B", "primary-type": "Album", "first-release-date": "2001"},
        {"id": "4", "title": "A", "primary-type": "Album", "first-release-date": "1995"}]]
    assert [x["title"] for x in MB.sort_groups(gs)] == ["A", "B", "EP", "Live"]
    tr = MB.map_tracks([{"media": []}, {"media": [{"position": 1, "tracks": [{"position": 1, "title": "x", "length": 61000}]}]}])
    assert tr == [{"disc": 1, "number": 1, "title": "x", "length": 61}]
    owned = [{"id": "al1", "name": "Kid A (Deluxe Edition)", "artist": "Radiohead", "mbid": ""},
             {"id": "al2", "name": "Sigur Rós", "artist": "Sigur Rós", "mbid": "g9"}]
    assert MB.find_owned(owned, "Radiohead", "Kid A")["id"] == "al1"
    assert MB.find_owned(owned, "radiohead", "KID A [Remastered]")["id"] == "al1"
    assert MB.find_owned(owned, "Radiohead", "Amnesiac") is None
    assert MB.find_owned(owned, "The Radiohead", "Kid A")["id"] == "al1"
    assert MB.find_owned(owned, "Other", "Other", mbid="g9")["id"] == "al2"
    assert MB.find_owned(owned, "Sigur Ros", "Sigur Ros")["id"] == "al2"        # accents
    assert MB.find_owned(owned, "Bowie", "Kid A") is None


# -- import -------------------------------------------------------------------------------------

def mk(path: Path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_import_album_folder_and_cleanup():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        music, inc = d / "music", d / "music" / ".incoming"
        pay = inc / "Radiohead - Kid A [FLAC]"
        mk(pay / "01 - Everything.flac", b"a")
        mk(pay / "02 - Kid A.flac", b"b")
        mk(pay / "folder.jpg", b"img")
        mk(pay / "rip.log", b"log")
        mk(pay / "info.nfo", b"nfo")
        mk(inc / "other-download" / "keep.flac", b"k")
        r = F.import_payload(pay, inc, music, "Radiohead", "Kid A", 2000)
        got = tree(music / "Radiohead")
        assert got == ["Kid A (2000)/01 - Everything.flac", "Kid A (2000)/02 - Kid A.flac", "Kid A (2000)/cover.jpg"], got
        assert r["files"] == 2 and not pay.exists()
        assert (inc / "other-download" / "keep.flac").exists()           # other incoming data untouched


def test_import_zip_slip_and_unsafe_names():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        music, inc = d / "music", d / "music" / ".incoming"
        inc.mkdir(parents=True)
        z = inc / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../../escape.flac", b"x")
            zf.writestr("a.flac", b"y")
        try:
            F.import_payload(z, inc, music, "A", "B", None)
            raise AssertionError("zip-slip accepted")
        except UnpackError:
            pass
        assert not (d / "escape.flac").exists() and not (music / "escape.flac").exists()
        assert not any(p.name.startswith(".unzip") for p in inc.iterdir())
        # a good zip, with a hostile artist/album name
        z2 = inc / "good.zip"
        with zipfile.ZipFile(z2, "w") as zf:
            zf.writestr("Album/01.mp3", b"m")
            zf.writestr("Album/cover.jpg", b"c")
            zf.writestr("Album/readme.txt", b"t")
        r = F.import_payload(z2, inc, music, "../../etc", "../x:y", "1999")
        assert all(Path(a).resolve().is_relative_to(music.resolve()) for a in r["albums"]), r
        assert "readme.txt" not in " ".join(tree(music))
        assert not z2.exists()
        assert F.safe_name("..") == "Unknown" and "/" not in F.safe_name("a/b\\c")


def test_import_refuses_outside_incoming_and_empty():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        music, inc = d / "music", d / "music" / ".incoming"
        inc.mkdir(parents=True)
        mk(d / "elsewhere" / "a.flac")
        for bad in (d / "elsewhere", inc):
            try:
                F.import_payload(bad, inc, music, "A", "B", None)
                raise AssertionError
            except UnpackError:
                pass
        assert (d / "elsewhere" / "a.flac").exists()
        mk(inc / "novideo" / "x.txt")
        try:
            F.import_payload(inc / "novideo", inc, music, "A", "B", None)
            raise AssertionError
        except UnpackError:
            pass


def test_import_discography_and_multidisc():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        music, inc = d / "music", d / "music" / ".incoming"
        pay = inc / "Band Discography"
        mk(pay / "1995 - First" / "01.flac")
        mk(pay / "1998 - Second" / "CD1" / "01.flac")
        mk(pay / "1998 - Second" / "CD2" / "01.flac")
        F.import_payload(pay, inc, music, "Band", "", None, discography=True)
        got = tree(music / "Band")
        assert "First (1995)/01.flac" in got, got
        assert "Second (1998)/CD1/01.flac" in got and "Second (1998)/CD2/01.flac" in got, got


def test_guess_and_hints():
    g = F.guess_from_title("Radiohead - Kid A (2000) [FLAC]")
    assert g == {"artist": "Radiohead", "album": "Kid A", "year": "2000"}
    assert F.hints("Artist - Album [FLAC 24bit]") == ["FLAC", "24-bit"]
    assert "MP3 320" in F.hints("Artist - Album 320 kbps MP3") and "discography" in F.hints("Artist Discography")
    assert F.album_folder("Kid A", 2000) == "Kid A (2000)" and F.album_folder("Kid A", None) == "Kid A"


# -- queue --------------------------------------------------------------------------------------

class FakeOut(Output):
    def __init__(self):
        self.id, self.name = "dev1", "Fake"
        self.log, self.prepared, self.closed, self.vol = [], 0, 0, None
        self.finish = asyncio.Event()

    async def prepare(self):
        self.prepared += 1

    async def stream(self, path, offset=0.0, track=None):
        self.log.append((path, offset))
        self.finish.clear()
        await self.finish.wait()

    async def set_volume(self, v):
        self.vol = v

    async def close(self):
        self.closed += 1


def test_queue_logic():
    async def go():
        out = FakeOut()
        tasks = []

        async def fetch(t):
            return "/tmp/" + t["id"]

        def spawn(c, name=""):
            tasks.append(asyncio.ensure_future(c))
            return tasks[-1]

        pl = Player(out, fetch, spawn)
        tr = [{"id": f"t{i}", "title": str(i), "duration": 100} for i in range(3)]

        async def settle():
            for _i in range(6):
                await asyncio.sleep(0)

        await pl.play(tr)
        await settle()
        assert pl.state == "playing" and pl.track["id"] == "t0" and out.prepared == 1
        out.finish.set()                                    # t0 ends → t1 starts by itself
        await settle()
        assert pl.track["id"] == "t1" and [p for p, _o in out.log][:2] == ["/tmp/t0", "/tmp/t1"]
        await pl.next()
        await settle()
        assert pl.track["id"] == "t2"
        await pl.next()                                     # after the last one: stops
        assert pl.state == "idle" and pl.queue == []
        await pl.play(tr, start=2)
        await settle()
        await pl.previous()                                 # just started: goes to the previous track
        await settle()
        assert pl.track["id"] == "t1"
        await pl.pause()
        assert pl.state == "paused"
        n = len(out.log)
        await pl.resume()
        await settle()
        assert pl.state == "playing" and len(out.log) == n + 1 and out.log[-1][0] == "/tmp/t1"
        await pl.enqueue([{"id": "x"}], next_=True)
        assert pl.queue[pl.index + 1]["id"] == "x"
        await pl.set_volume(150)
        assert out.vol == 100 and pl.volume == 100
        await pl.stop()
        assert pl.state == "idle" and pl.track is None and out.closed >= 1
        await pl.play([tr[0]], shuffle=True)
        await settle()
        out.finish.set()
        await settle()
        assert pl.state == "idle"                           # queue exhausted
        for t in tasks:
            t.cancel()
    asyncio.run(go())


def test_player_error_is_reported():
    async def go():
        class Bad(FakeOut):
            async def stream(self, path, offset=0.0, track=None):
                raise RuntimeError("needs pairing")

        async def fetch(t):
            return "/x"
        pl = Player(Bad(), fetch, lambda c, n="": asyncio.ensure_future(c))
        await pl.play([{"id": "a"}])
        for _i in range(6):
            await asyncio.sleep(0)
        assert pl.state == "idle" and pl.error == "needs pairing"
    asyncio.run(go())


# -- plugin: KEF routing, accept_download, routes ------------------------------------------------

class FakeKef:
    host = "192.168.1.43"
    src = {"on": True, "source": "Wifi"}

    def __init__(self):
        self.calls = []

    async def turn_on(self, source=None):
        self.calls.append(("turn_on", source))

    async def set_volume(self, v):
        self.calls.append(("volume", v))


class FakeTx:
    enabled = True

    def __init__(self):
        self.added = []

    async def add(self, link, download_dir=None, **kw):
        self.added.append((link, download_dir))
        return {"hash": link.split("btih:")[1][:40], "id": 1, "name": "x"}


class FakeMedia:
    def __init__(self, dl):
        self.config = {"download_dir": dl}
        self.transmission = FakeTx()


class FakeTorrents:
    engine = object()

    def enabled_ids(self):
        return {"x"}


def new_plugin(d, nav_url="http://127.0.0.1:1"):
    (Path(d) / "hub").mkdir(exist_ok=True)
    hub = Hub({"homekit": {"enabled": False}}, Path(d) / "hub")
    cfg = {"navidrome": {"url": nav_url, "user": "u", "password": "pw"}, "music_dir": str(Path(d) / "music")}
    return hub, MusicPlugin(hub, cfg)


def test_kef_routing_uses_kef_plugin_only():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub, p = new_plugin(d)
            await p.start()
            kef = FakeKef()
            hub.plugins["kef"] = kef
            out = AirPlayOutput(p, {"id": "kefid", "name": "KEF LSX", "address": "192.168.1.43"})
            other = AirPlayOutput(p, {"id": "tv", "name": "Apple TV", "address": "192.168.1.9"})
            assert out.kef() is kef and other.kef() is None
            await out.prepare()
            await out.set_volume(33)
            assert kef.calls == [("turn_on", "Wifi"), ("volume", 33)], kef.calls
            assert out.atv is None                      # no pyatv connection was opened for control
            await other.prepare()
            assert kef.calls == [("turn_on", "Wifi"), ("volume", 33)]
            await p.stop()
        asyncio.run(go())


def test_accept_download_and_listing():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub, p = new_plugin(d)
            await p.start()
            media = FakeMedia(str(Path(d) / "dl"))
            hub.plugins["media"], hub.plugins["torrents"] = media, FakeTorrents()
            r = await p.accept_download({"title": "Radiohead - Kid A (2000) [FLAC]", "magnet": "magnet:?xt=urn:btih:" + "ab" * 20,
                                         "url": None, "hash": "ab" * 20, "size": 1}, {"artist": "", "album": "", "year": "", "discography": False})
            assert r == {"ok": True, "hash": "ab" * 20}
            assert media.transmission.added[0][1] == str(Path(d) / "dl" / ".incoming")
            lst = p.downloads_listing()
            assert len(lst) == 1 and lst[0]["state"] == "downloading" and lst[0]["title"].startswith("Radiohead")
            assert set(lst[0]) >= {"hash", "title", "progress", "rate", "eta", "state", "error"}
            e = p.finder.entries()[0]
            assert (e["artist"], e["album"], e["year"]) == ("Radiohead", "Kid A", "2000")
            try:
                await p.accept_download({"title": "???", "magnet": "magnet:?xt=urn:btih:" + "cd" * 20}, {})
                raise AssertionError
            except ValueError:
                pass
            r2 = await p.accept_download({"title": "x", "magnet": "magnet:?xt=urn:btih:" + "cd" * 20},
                                         {"artist": "Bowie", "album": "Low", "year": 1977, "discography": True})
            assert r2["ok"]
            es = {e["hash"]: e for e in p.finder.entries()}
            assert es["cd" * 20]["artist"] == "Bowie" and es["cd" * 20]["discography"]
            await p.stop()
        asyncio.run(go())


def test_finish_flow_imports_and_rescans():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            nav = FakeNavidrome()
            await nav.server.start_server()
            hub, p = new_plugin(d, str(nav.server.make_url("")).rstrip("/"))
            await p.start()
            media = FakeMedia(str(Path(d) / "dl"))
            hub.plugins["media"], hub.plugins["torrents"] = media, FakeTorrents()
            inc = Path(d) / "dl" / ".incoming"

            async def remove(h, delete_data=False):
                media.transmission.removed = (h, delete_data)
            media.transmission.remove = remove
            h = await p.finder.add("Radiohead - Kid A [FLAC]", "Radiohead", "Kid A", "2000", "magnet:?xt=urn:btih:" + "ab" * 20)
            mk(inc / "Kid A" / "01.flac")
            p.finder._update(h, state="importing", payload=str(inc / "Kid A"))
            await p.finder._finish(h)
            assert media.transmission.removed == (h, False)
            assert (Path(d) / "music" / "Radiohead" / "Kid A (2000)" / "01.flac").exists()
            assert p.finder.entries()[0]["state"] == "done" and nav.scans == 1
            assert p.finder.entries()[0]["albums"] == [str((Path(d) / "music" / "Radiohead" / "Kid A (2000)").resolve())]
            await p.stop()
            await nav.server.close()
        asyncio.run(go())


def test_routes_against_fake_navidrome():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            nav = FakeNavidrome()
            await nav.server.start_server()
            hub, p = new_plugin(d, str(nav.server.make_url("")).rstrip("/"))
            await p.start()
            fo = FakeOut()
            p.outs["dev1"] = {"id": "dev1", "name": "Fake", "address": "1.2.3.4", "kind": "airplay"}
            from hub.plugins.music.player import Player as P
            p.players["dev1"] = P(fo, lambda t: asyncio.sleep(0, "/tmp/" + t["id"]), p.spawn, p._player_changed)

            @web.middleware
            async def errors(request, handler):
                try:
                    return await handler(request)
                except web.HTTPException:
                    raise
                except KeyError as err:
                    return web.json_response({"error": str(err)}, status=404)
                except (ValueError, TypeError) as err:
                    return web.json_response({"error": str(err)}, status=400)
            app = web.Application(middlewares=[errors])
            for rd in p.routes():
                app.router.add_route(rd.method, "/api/p/music" + rd.path, rd.handler)
            async with TestClient(TestServer(app)) as c:
                al = await (await c.get("/api/p/music/albums?type=newest")).json()
                assert [a["name"] for a in al["albums"]] == ["Kid A", "Low"]
                a = await (await c.get("/api/p/music/album/al1")).json()
                assert len(a["tracks"]) == 2
                r = await c.get("/api/p/music/cover/al-al1?size=200")
                assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg" and (await r.read()).startswith(b"\xff\xd8")
                assert (await c.get("/api/p/music/cover/..%2Fx")).status == 400
                r = await c.get("/api/p/music/stream/s1?suffix=flac", headers={"Range": "bytes=2-5"})
                assert r.status == 206 and await r.read() == b"2345" and "Content-Range" in r.headers
                r = await c.get("/api/p/music/stream/s1?suffix=ogg")        # transcoded
                assert r.status == 200 and await r.read() == b"0123456789"
                tr = [q for ep, q in nav.calls if ep == "stream"][-1]
                assert tr["format"] == "mp3" and tr["maxBitRate"] == "320"
                # credentials never reach the client
                body = await (await c.get("/api/p/music/albums")).text()
                assert "pw" not in body and "&t=" not in body
                r = await c.post("/api/p/music/player/play", json={"output": "Fake", "album": "al1", "start": 1})
                st = await r.json()
                assert r.status == 200 and st["state"] == "playing" and st["track"]["title"] == "Kid A", st
                assert hub.entities["music.player"].state["state"] == "playing"
                assert hub.entities["music.player"].state["title"] == "Kid A"
                st = await (await c.post("/api/p/music/player/pause", json={"output": "dev1"})).json()
                assert st["state"] == "paused"
                st = await (await c.post("/api/p/music/player/volume", json={"output": "dev1", "value": 40})).json()
                assert st["volume"] == 40 and fo.vol == 40
                await hub.call("music.player", "play_pause")
                assert p.players["dev1"].state == "playing"
                st = await (await c.post("/api/p/music/player/stop", json={"output": "dev1"})).json()
                assert st["state"] == "idle"
                assert (await c.post("/api/p/music/player/play", json={"output": "nope", "album": "al1"})).status == 404
                t = await (await c.post("/api/p/music/test", json={})).json()
                assert t["ok"] is True
            # wrong password is reported, not raised
            p.nav.password = "bad"
            try:
                await p.nav.ping()
                raise AssertionError
            except S.SubsonicError:
                pass
            await p.stop()
            await nav.server.close()
        asyncio.run(go())


def test_stop_cancels_streams():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub, p = new_plugin(d)
            await p.start()
            fo = FakeOut()
            p.outs["dev1"] = {"id": "dev1", "name": "Fake", "address": "x", "kind": "airplay"}
            pl = p.players["dev1"] = Player(fo, lambda t: asyncio.sleep(0, "/x"), p.spawn, p._player_changed)
            await pl.play([{"id": "a"}])
            for _i in range(5):
                await asyncio.sleep(0)
            task = pl._task
            assert task and not task.done()
            await p.stop()
            assert task.done() and fo.closed >= 1
        asyncio.run(go())


# -- Discover (Deezer + Apple), fake HTTP -----------------------------------------------------------

def dz_album(i, title, artist, **kw):
    return {"id": i, "title": title, "artist": {"id": 7, "name": artist}, "record_type": kw.get("rt", "album"),
            "cover_big": f"https://cdn-images.dzcdn.net/images/cover/{i}/500x500.jpg", "release_date": kw.get("date", "2020-01-02")}


class FakeDiscover(D.Discover):
    """Discover with the network replaced by canned answers; ``fail`` lists URL fragments that raise."""

    def __init__(self, owned=None, fail=()):
        super().__init__(lambda: None, owned)
        self.calls, self.fail = [], list(fail)

    async def _json(self, url, params=None, retry=False):
        self.calls.append(url)
        if any(f in url for f in self.fail):
            raise D.DiscoverError("down")
        if "marketingtools" in url:
            return {"feed": {"results": [
                {"id": "1", "name": "Fresh", "artistName": "Geolier", "releaseDate": "2999-01-01",
                 "artworkUrl100": "https://is1-ssl.mzstatic.com/x/100x100bb.jpg"},
                {"id": "2", "name": "Old", "artistName": "Someone", "releaseDate": "2001-01-01",
                 "artworkUrl100": "https://is1-ssl.mzstatic.com/y/100x100bb.jpg"}]}}
        if "/chart/0/albums" in url:
            return {"data": [dz_album(10, "Kid A (Deluxe Edition)", "Radiohead"), dz_album(11, "Nope", "Nobody")]}
        if "/chart/0/artists" in url:
            return {"data": [{"id": 5, "name": "Sfera", "picture_big": "https://cdn-images.dzcdn.net/images/artist/z/500.jpg", "nb_fan": 3}]}
        if "/album/10" in url:
            return {"id": 10, "title": "Kid A", "artist": {"id": 3, "name": "Radiohead"}, "label": "XL", "release_date": "2000-10-02",
                    "genres": {"data": [{"name": "Alt"}]}, "cover_xl": "https://cdn-images.dzcdn.net/images/cover/xl.jpg",
                    "tracks": {"data": [{"id": 1, "title": "Everything", "duration": 251, "preview": "https://cdnt-preview.dzcdn.net/a.mp3"},
                                        {"id": 2, "title": "Kid A", "duration": 1, "preview": "javascript:alert(1)"}]}}
        if "/artist/9/top" in url:
            return {"data": [{"id": 1, "title": "T", "preview": "", "artist": {"id": 9, "name": "A"}, "album": {"id": 1, "title": "X"}}]}
        if "/artist/9/albums" in url:
            return {"data": [dz_album(1, "Dup", "A", date="2019-01-01"), dz_album(2, "Dup (Deluxe)", "A", date="2020-01-01"),
                             dz_album(3, "Single", "A", rt="single")]}
        if "/artist/9/related" in url:
            return {"data": [{"id": 8, "name": "B"}]}
        if "/artist/9" in url:
            return {"id": 9, "name": "A", "nb_fan": 10, "picture_xl": "https://cdn-images.dzcdn.net/images/artist/q/1000.jpg"}
        return {"data": []}


def test_discover_parsing_and_ids():
    assert D.valid_id("123") and not D.valid_id("12a") and not D.valid_id("1/../2") and not D.valid_id("")
    a = D.map_apple({"id": 5, "name": "N", "artistName": "A", "releaseDate": "2026-10-15",
                     "artworkUrl100": "https://is1-ssl.mzstatic.com/i/100x100bb.jpg"})
    assert a["cover"].endswith("/500x500bb.jpg") and a["year"] == 2026 and a["id"] == ""
    assert D.map_apple({"name": "N", "artworkUrl100": "javascript:x"})["cover"] == ""
    assert D.map_track({"id": 1, "preview": "javascript:1"})["preview"] == ""
    assert D.map_album({"id": 1, "cover_big": "https://evil.example/x.jpg"})["cover"] == ""

    async def go():
        d = FakeDiscover()
        for bad in ("abc", "1;2", "../x"):
            for fn in (d.album, d.artist, d.genre_albums):
                try:
                    await fn(bad)
                    raise AssertionError(bad)
                except D.DiscoverError:
                    pass
        assert not d.calls                       # nothing reached the network
        al = await d.album("10")
        assert al["label"] == "XL" and al["genres"] == ["Alt"] and al["year"] == 2000 and al["cover"].endswith("xl.jpg")
        assert al["tracks"][0]["preview"].startswith("https://") and al["tracks"][1]["preview"] == ""
    asyncio.run(go())


def test_discover_rails_cache_and_partial_failure():
    async def go():
        d = FakeDiscover()
        r = await d.rails(["top_it", "new", "chart_albums", "chart_artists", "bogus"])
        assert [x["key"] for x in r["rails"]] == ["top_it", "new", "chart_albums", "chart_artists"] and not r["errors"]
        assert [i["title"] for i in next(x for x in r["rails"] if x["key"] == "new")["items"]] == ["Fresh"]
        n = len(d.calls)
        await d.rails(["top_it", "chart_albums"])
        assert len(d.calls) == n                 # served from the cache
        # one source down: the others still answer, the failure is reported per rail
        d2 = FakeDiscover(fail=["marketingtools"])
        r = await d2.rails(["top_it", "chart_albums"])
        assert [x["key"] for x in r["rails"]] == ["chart_albums"] and list(r["errors"]) == ["top_it"]
        # stale answer beats an error
        d3 = FakeDiscover()
        await d3.chart("albums", 0)
        d3._cache["chart:0:albums"] = (0.0, 1, d3._cache["chart:0:albums"][2])
        d3.fail = ["chart"]
        assert len(await d3.chart("albums", 0)) == 2
        # bounded
        d4 = FakeDiscover()
        for i in range(D.MAX_CACHE + 50):
            d4._put(f"k{i}", 100, i)
        assert len(d4._cache) <= D.MAX_CACHE
    asyncio.run(go())


def test_discover_library_matching():
    async def go():
        owned = [{"id": "al1", "name": "Kid A", "artist": "Radiohead"}]

        async def lib():
            return owned
        d = FakeDiscover(lib)
        items = (await d.rails(["chart_albums"]))["rails"][0]["items"]
        by = {i["title"]: i["owned"] for i in items}
        assert by["Kid A (Deluxe Edition)"] == "al1" and by["Nope"] is None     # edition tags ignored
        al = await d.album("10")
        assert al["owned"] == "al1"

        async def dead():
            raise S.SubsonicError("down")
        d2 = FakeDiscover(dead)                  # Navidrome down: no marks, no failure, no retry storm
        items = (await d2.rails(["chart_albums"]))["rails"][0]["items"]
        assert all(i["owned"] is None for i in items)
        assert d2._down_until > 0
    asyncio.run(go())


def test_discover_artist_partial_and_dedupe():
    async def go():
        d = FakeDiscover(fail=["/related"])
        a = await d.artist("9")
        assert a["name"] == "A" and a["picture"].endswith("1000.jpg")
        assert list(a["errors"]) == ["related"] and a["related"] == []
        assert len([x for x in a["albums"] if x["title"].startswith("Dup")]) == 1      # regional editions collapse
        assert a["albums"][-1]["type"] == "single"                                       # albums and EPs first
        assert len(a["top"]) == 1 and a["top"][0]["preview"] == ""
        try:
            await FakeDiscover(fail=["/artist/9"]).artist("9")
            raise AssertionError
        except D.DiscoverError:
            pass
    asyncio.run(go())


def test_discover_routes():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub, p = new_plugin(d)
            await p.start()
            p.disc = FakeDiscover()
            @web.middleware
            async def errors(request, handler):          # the hub's own middleware turns ValueError into a 400
                try:
                    return await handler(request)
                except ValueError as err:
                    return web.json_response({"error": str(err)}, status=400)
            app = web.Application(middlewares=[errors])
            app.router.add_routes([web.RouteDef(r.method, "/api/p/music" + r.path, r.handler, r.kwargs) for r in p.routes()])
            async with TestClient(TestServer(app)) as c:
                j = await (await c.get("/api/p/music/discover?rails=chart_artists")).json()
                assert j["rails"][0]["items"][0]["name"] == "Sfera"
                r = await c.get("/api/p/music/discover/album/x1")
                assert r.status == 400 and "error" in await r.json()
                assert (await c.get("/api/p/music/discover/album/10")).status == 200
            await p.stop()
        asyncio.run(go())


def test_move_across_shares_without_permissions():
    """Download -> Multimedia are two SMB shares: rename fails (EXDEV) and chmod/utime fail (EPERM)."""
    import os
    import shutil
    from unittest import mock
    from hub.plugins.music import find as mf
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src, music = d / "dl" / "01.flac", d / "music"
        src.parent.mkdir()
        src.write_bytes(b"x" * 1000)
        dest = music / "A" / "B (1995)" / "01.flac"
        calls = []

        def fake_replace(a, b):     # the first call is the cross-share rename, the next the .part rename
            calls.append(a)
            if len(calls) == 1:
                raise OSError(18, "Invalid cross-device link")
            os.rename(a, b)
        with mock.patch.object(os, "replace", side_effect=fake_replace), \
                mock.patch.object(shutil, "copystat", side_effect=PermissionError(1, "Operation not permitted")):
            assert mf._move(src, dest, music) is True
        assert dest.read_bytes() == b"x" * 1000 and not src.exists()
        assert not list(dest.parent.glob("*.part"))
        # an earlier interrupted import left the copy behind: the original is removed, no duplicate
        src.write_bytes(b"x" * 1000)
        assert mf._move(src, dest, music) is False and not src.exists()
        assert sorted(x.name for x in dest.parent.iterdir()) == ["01.flac"]


def test_failed_import_keeps_the_download():
    from unittest import mock
    from hub.plugins.music import find as mf
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        inc, music = d / "incoming", d / "music"
        payload = inc / "Radiohead - The Bends [FLAC]"
        payload.mkdir(parents=True)
        for n in ("01.flac", "02.flac"):
            (payload / n).write_bytes(b"x" * 100)
        with mock.patch.object(mf, "copy_file", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch.object(mf.os, "replace", side_effect=OSError(18, "cross-device")):
            try:
                mf.import_payload(payload, inc, music, "Radiohead", "The Bends", 1995)
                raise AssertionError("should fail")
            except PermissionError:
                pass
        assert sorted(p.name for p in payload.iterdir()) == ["01.flac", "02.flac"]   # nothing lost
        out = mf.import_payload(payload, inc, music, "Radiohead", "The Bends", 1995)   # "Try again"
        assert out["files"] == 2 and not payload.exists()


def test_genres_parsing_and_album_list():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            nav = FakeNavidrome()
            await nav.server.start_server()
            hub, p = new_plugin(d, str(nav.server.make_url("")).rstrip("/"))
            await p.start()
            g = await p.nav.genres()
            assert [x["name"] for x in g] == ["Jazz", "Rock"] and g[0]["albums"] == 5      # empty genre dropped, biggest first
            await p.nav.album_list("byGenre", 20, 0, "Jazz  Fusion")
            ep, q = nav.calls[-1]
            assert q["type"] == "byGenre" and q["genre"] == "Jazz Fusion"
            try:
                await p.nav.album_list("byGenre", 20, 0, "")
                raise AssertionError
            except ValueError:
                pass
            await p.stop()
            await nav.server.close()
        asyncio.run(go())


def test_content_disposition_is_safe():
    from hub.plugins.music import content_disposition as cd
    h = cd('Bj\u00f6rk - "Hyper/ballad"\r\nX: y.flac')
    assert "\r" not in h and "\n" not in h and "/" not in h.split("filename*=")[0]
    assert h.startswith("attachment; filename=\"") and "filename*=UTF-8''Bj%C3%B6rk" in h
    assert cd("..").startswith('attachment; filename="music"')


def test_save_proxy_streams_with_range_and_hides_credentials():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            nav = FakeNavidrome()
            await nav.server.start_server()
            hub, p = new_plugin(d, str(nav.server.make_url("")).rstrip("/"))
            await p.start()

            @web.middleware
            async def errors(request, handler):
                try:
                    return await handler(request)
                except KeyError as err:
                    return web.json_response({"error": str(err)}, status=404)
                except (ValueError, TypeError) as err:
                    return web.json_response({"error": str(err)}, status=400)
            app = web.Application(middlewares=[errors])
            app.router.add_routes([web.RouteDef(r.method, "/api/p/music" + r.path, r.handler, r.kwargs) for r in p.routes()])
            async with TestClient(TestServer(app)) as c:
                r = await c.get("/api/p/music/save/s1")
                assert r.status == 200 and await r.read() == b"FLACDATA"
                assert r.headers["Content-Type"] == "audio/flac"
                cdp = r.headers["Content-Disposition"]
                assert cdp.startswith("attachment;") and "Ts1.flac" in cdp and "filename*=UTF-8''R%C3%A9%20diohead%20-%20Ts1.flac" in cdp, cdp
                assert "pw" not in str(dict(r.headers)) and "t=" not in cdp
                ep, q = [x for x in nav.calls if x[0] == "download"][-1]
                assert q["id"] == "s1"
                r = await c.get("/api/p/music/save/s1", headers={"Range": "bytes=2-4"})
                assert r.status == 206 and await r.read() == b"ACD" and "Content-Range" in r.headers
                r = await c.get("/api/p/music/save/al1?kind=album")           # a zip, streamed (200 kB)
                body = await r.read()
                assert r.status == 200 and len(body) == 200000 and r.headers["Content-Type"] == "application/zip"
                assert "Radiohead - Kid A.zip" in r.headers["Content-Disposition"]
                assert (await c.get("/api/p/music/save/..%2Fx")).status == 400
                assert (await c.get("/api/p/music/save/a%20b")).status == 400
                assert (await c.get("/api/p/music/save/s1?kind=evil")).status == 400
                assert (await c.get("/api/p/music/genres")).status == 200
                r = await c.get("/api/p/music/albums?type=byGenre&genre=Rock")
                assert r.status == 200 and nav.calls[-1][1]["genre"] == "Rock"
            await p.stop()
            await nav.server.close()
        asyncio.run(go())


def test_genre_page_marks_owned_and_survives_partial_failure():
    async def go():
        async def owned():
            return [{"id": "n1", "name": "Kid A", "artist": "Radiohead"}]

        class G(FakeDiscover):
            async def _json(self, url, params=None, retry=False):
                if "/genre/5" in url or "/chart/5/artists" in url:
                    raise D.DiscoverError("down")
                if "/chart/5/albums" in url:
                    return {"data": [dz_album(10, "Kid A", "Radiohead")]}
                if "/chart/5/tracks" in url:
                    return {"data": [{"id": 1, "title": "T", "preview": "", "artist": {"id": 3, "name": "Radiohead"},
                                      "album": {"id": 10, "title": "Kid A"}}]}
                if url.endswith("/genre"):
                    return {"data": [{"id": 0, "name": "All"},
                                     {"id": 5, "name": "Rock", "picture_medium": "https://cdn-images.dzcdn.net/images/misc/x/250x250.jpg"}]}
                return await super()._json(url, params, retry)
        g = G(owned)
        gs = await g.genres()
        assert [x["name"] for x in gs] == ["Rock"] and gs[0]["picture"].endswith("250x250.jpg")
        pg = await g.genre_page("5")
        assert pg["albums"][0]["owned"] == "n1" and pg["tracks"][0]["owned"] == "n1" and pg["artists"] == []
        assert "artists" in pg["errors"] and "albums" not in pg["errors"]
        try:
            await g.genre_page("5x")
            raise AssertionError
        except D.DiscoverError:
            pass
    asyncio.run(go())


def test_delete_download_files_and_library_safety():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            nav = FakeNavidrome()
            await nav.server.start_server()
            hub, p = new_plugin(d, str(nav.server.make_url("")).rstrip("/"))
            await p.start()
            media = FakeMedia(str(Path(d) / "dl"))
            hub.plugins["media"], hub.plugins["torrents"] = media, FakeTorrents()
            inc, music, outside = Path(d) / "dl" / ".incoming", Path(d) / "music", Path(d) / "outside"
            fi = p.finder
            mk(inc / "Failed" / "01.flac", b"x" * 50)
            mk(inc / "Keep" / "01.flac")
            mk(outside / "precious.txt")
            (inc / "Link").symlink_to(outside)
            mk(music / "Radiohead" / "Kid A (2000)" / "01.flac", b"y" * 30)
            mk(music / "Radiohead" / "Amnesiac (2001)" / "01.flac")
            mk(music / "Other" / "Album" / "01.flac")
            base = {"title": "t", "artist": "a", "album": "b", "added": 0, "dir": str(inc), "error": None}
            fi._save([
                {**base, "hash": "h1", "state": "error", "error": "x", "payload": str(inc / "Failed")},
                {**base, "hash": "h2", "state": "error", "payload": str(inc)},                  # the incoming root
                {**base, "hash": "h3", "state": "error", "payload": str(inc / "Link")},         # symlink escape
                {**base, "hash": "h4", "state": "error", "payload": str(outside)},              # outside the folder
                {**base, "hash": "h5", "state": "done", "payload": None,
                 "albums": [str(music / "Radiohead" / "Kid A (2000)"), str(music), str(music / "Other"), str(outside)]},
                {**base, "hash": "h6", "state": "error", "payload": str(inc / "Keep")},
            ])
            i = await fi.info("h1")
            assert i["payload"]["size"] == 50 and i["payload"]["path"].endswith("Failed")
            for h in ("h2", "h3", "h4"):
                assert (await fi.info(h))["payload"] is None
            await fi.cancel("h2", delete_files=True)
            await fi.cancel("h3", delete_files=True)
            await fi.cancel("h4", delete_files=True)
            assert inc.exists() and (inc / "Keep" / "01.flac").exists() and (outside / "precious.txt").exists()
            await fi.cancel("h6")                                    # remove from list only: files stay
            assert (inc / "Keep" / "01.flac").exists()
            await fi.cancel("h1", delete_files=True)
            assert not (inc / "Failed").exists() and inc.exists() and not any(e["hash"] == "h1" for e in fi.entries())
            # library: only album folders two levels below the music dir
            ia = await fi.info("h5")
            assert [a["path"] for a in ia["albums"]] == [str(music / "Radiohead" / "Kid A (2000)")]
            await fi.delete_from_library("h5")
            assert not (music / "Radiohead" / "Kid A (2000)").exists()
            assert (music / "Radiohead" / "Amnesiac (2001)" / "01.flac").exists()     # sibling album untouched
            assert (music / "Other" / "Album" / "01.flac").exists() and (outside / "precious.txt").exists()
            assert nav.scans == 1 and not any(e["hash"] == "h5" for e in fi.entries())
            fi._save(fi.entries() + [{**base, "hash": "h7", "state": "error", "albums": [str(music / "Other" / "Album")]}])
            try:
                await fi.delete_from_library("h7")          # only a finished import
                raise AssertionError
            except ValueError:
                pass
            assert (music / "Other" / "Album" / "01.flac").exists()
            await p.stop()
            await nav.server.close()
        asyncio.run(go())


def test_dlna_renderer_plays_until_stopped():
    """The KEF path: SetAVTransportURI + Play, follow GetTransportInfo until the track ends."""
    import aiohttp
    from aiohttp import web
    from hub.plugins.music import dlna

    desc_xml = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
      <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><friendlyName>LSX 1</friendlyName>
      <manufacturer>KEF</manufacturer><modelName>SP3994</modelName><UDN>uuid:abc-841715119ae6</UDN><serviceList>
      <service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/AVTransport/ctrl</controlURL></service>
      <service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/RenderingControl/ctrl</controlURL></service>
      </serviceList></device></root>"""
    d = dlna.parse_description(desc_xml, "http://192.168.1.43:8080/description.xml")
    assert d["avt"] == "http://192.168.1.43:8080/AVTransport/ctrl" and d["manufacturer"] == "KEF"
    assert dlna.parse_description("<x>", "http://a/") is None

    async def go():
        calls, states = [], iter(["TRANSITIONING", "PLAYING", "PLAYING", "STOPPED"])

        async def ctrl(request):
            action = request.headers["SOAPAction"].strip('"').split("#")[1]
            body = await request.text()
            calls.append((action, body))
            if action == "GetTransportInfo":
                st = next(states, "STOPPED")
                return web.Response(text=f"<r><CurrentTransportState>{st}</CurrentTransportState></r>")
            return web.Response(text="<ok/>")
        app = web.Application()
        app.router.add_post("/AVTransport/ctrl", ctrl)
        app.router.add_post("/RenderingControl/ctrl", ctrl)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            r = {**d, "avt": f"http://127.0.0.1:{port}/AVTransport/ctrl",
                 "rc": f"http://127.0.0.1:{port}/RenderingControl/ctrl", "address": "127.0.0.1"}
            plugin = SimpleNamespace(session=session, hub=SimpleNamespace(plugins={}),
                                     stream_link=lambda path, peer: "http://hub/api/p/music/stream/1/s/a&b.mp3")
            out = dlna.DlnaOutput(plugin, {"id": "x", "name": "LSX 1", "address": "127.0.0.1", "dlna": r})
            old, dlna.POLL = dlna.POLL, 0.01
            try:
                await out.stream("/tmp/a.mp3", 0, {"title": "Planet <Telex>", "artist": "Radiohead"})
                await out.set_volume(30)
            finally:
                dlna.POLL = old
        await runner.cleanup()
        names = [a for a, _b in calls]
        assert names[:3] == ["Stop", "SetAVTransportURI", "Play"] and names.count("GetTransportInfo") == 4, names
        uri = calls[1][1]
        assert "a&amp;b.mp3" in uri and "Planet &amp;lt;Telex&amp;gt;" in uri      # URL and DIDL escaped once more inside SOAP
        assert "<DesiredVolume>30</DesiredVolume>" in calls[-1][1]
    asyncio.run(go())


def test_stream_links_are_signed():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            hub = Hub({"homekit": {"enabled": False}, "web": {"port": 8080}}, Path(d))
            from hub.plugins.music import PLUGIN as MusicPlugin
            m = MusicPlugin(hub, {})
            m.tmp = Path(d) / "music-tmp"
            m.tmp.mkdir()
            f = m.tmp / "abc.mp3"
            f.write_bytes(b"ID3" + b"x" * 100)
            url = m.stream_link(str(f), "127.0.0.1")
            assert url.startswith("http://127.0.0.1:8080/api/p/music/speaker/") and url.endswith("/abc.mp3")
            exp, sig, name = url.split("/speaker/")[1].split("/")

            class R:
                def __init__(self, **mi):
                    self.match_info = mi
            resp = await m.r_speaker_stream(R(exp=exp, sig=sig, name=name))
            assert resp.headers["Content-Type"] == "audio/mpeg"
            from aiohttp import web
            for bad in (dict(exp=exp, sig="0" * 64, name=name), dict(exp="1", sig=sig, name=name),
                        dict(exp=exp, sig=sig, name="../hub.db")):
                try:
                    await m.r_speaker_stream(R(**bad))
                    raise AssertionError(bad)
                except web.HTTPNotFound:
                    pass
            try:
                m.stream_link("/etc/passwd.mp3", "127.0.0.1")
                raise AssertionError("outside")
            except ValueError:
                pass
    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
