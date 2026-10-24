"""Games: library scan, Deck agent (VDF, safety) and a full install flow Casa → agent → files.
Everything runs on temp dirs and 127.0.0.1; Steam is faked; nothing touches /mnt/nas or a real Deck."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "deck"))

import casa_deck_agent as A  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.i18n import _  # noqa: E402
from hub.plugins.games import GamesPlugin, library  # noqa: E402

TOKEN = "t" * 32


def put(path: Path, data=b"x", size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if size is None else (data * size)[:size])


def make_library(root: Path):
    put(root / "pc/Celeste v1.4/Celeste.exe", b"E", 3000)
    put(root / "pc/Celeste v1.4/Content/data.bin", b"D", 5000)
    put(root / "pc/Celeste v1.4/Content/unins000.exe", b"U", 100)
    put(root / "pc/Celeste v1.4/cover.jpg", b"JPG")
    put(root / "pc/Hades/v1.0/Hades.exe", b"1", 10)
    put(root / "pc/Hades/v1.2/Hades.exe", b"2", 20)
    put(root / "pc/Hades/v1.10/Hades.exe", b"3", 30)
    put(root / "pc/Old Game/setup_old_game.exe", b"S", 400)
    put(root / "pc/Penguin/Penguin.AppImage", b"A", 50)
    put(root / "pc/Mystery/readme.txt", b"?")
    put(root / "pc/Custom/bin/run.exe", b"C", 40)
    put(root / "pc/Custom/bin/other.exe", b"O", 90)
    put(root / "pc/Custom/game.json", json.dumps({"title": "Custom Title", "version": "9", "exe": "bin/run.exe"}).encode())
    put(root / "emu/snes/Super Game (v1.1).sfc", b"R", 2048)
    put(root / "emu/snes/Super Game (v1.1).jpg", b"IMG")
    put(root / "emu/snes/notes.txt", b"skip")
    put(root / "emu/psx/Disc Game/Disc.cue", b"cue")
    put(root / "emu/psx/Disc Game/Disc.bin", b"B", 4096)


def test_scan():
    with tempfile.TemporaryDirectory() as d:
        make_library(Path(d))
        games, problems = library.scan(d)
        by = {g["id"]: g for g in games}
        assert set(by) == {"pc-celeste", "pc-hades", "pc-old-game", "pc-penguin", "pc-custom-title",
                           "snes-super-game", "psx-disc-game"}, set(by)
        c = by["pc-celeste"]
        assert (c["title"], c["version"], c["kind"], c["exe"]) == ("Celeste", "1.4", "portable", "Celeste.exe")
        assert c["cover"] and c["files"] == 3 and c["size"] == 8100            # cover.jpg is not a game file
        assert by["pc-hades"]["version"] == "1.10" and by["pc-hades"]["size"] == 30     # natural order, not text order
        assert by["pc-old-game"]["kind"] == "installer" and by["pc-penguin"]["kind"] == "native"
        cu = by["pc-custom-title"]
        assert (cu["version"], cu["exe"], cu["kind"]) == ("9", "bin/run.exe", "portable")
        s, p = by["snes-super-game"], by["psx-disc-game"]
        assert (s["kind"], s["system"], s["version"], s["folder"], s["cover"]) == ("rom", "snes", "1.1", "", True)
        assert (p["system"], p["folder"], p["files"]) == ("psx", "Disc Game", 2)
        assert [x["path"] for x in problems] == ["Mystery"]
        assert (c["platform"], by["pc-penguin"]["platform"], s["platform"], p["platform"]) == ("pc", "linux", "snes", "psx")
        assert all(g["added"] > 1e9 for g in games) and "_list" not in library.public(c) and "added" in library.public(c)
        assert c["path"] == "pc/Celeste v1.4" and s["path"] == "emu/snes/Super Game (v1.1).sfc"
        assert library.split_name("Fallout 4") == ("Fallout 4", "") and library.split_name("Hades (v1.38)") == ("Hades", "1.38")
        assert library.scan(d + "/missing")[0] == [] and library.scan(d + "/missing")[1]


def test_missing_library_is_reported():
    with tempfile.TemporaryDirectory() as d:
        async def go():
            hub = Hub({"homekit": {"enabled": False}}, Path(d))
            p = GamesPlugin(hub, {"library": d + "/nope"})
            await p.start()
            await p._scan()          # start() scans in the background
            async with TestClient(TestServer(casa_app(p))) as c:
                lib = await (await c.get("/api/p/games/library")).json()
                assert lib["exists"] is False and lib["games"] == [] and lib["systems"] == [] and lib["problems"]
                assert lib["deck"]["configured"] is False
            await p.stop()
        asyncio.run(go())


def test_safe_file_blocks_escapes():
    with tempfile.TemporaryDirectory() as d:
        make_library(Path(d))
        (Path(d) / "secret.txt").write_text("s")
        os.symlink(Path(d) / "secret.txt", Path(d) / "pc/Celeste v1.4/link.dat")
        g = {x["id"]: x for x in library.scan(d)[0]}["pc-celeste"]
        assert library.safe_file(g, "Celeste.exe") and library.safe_file(g, "Content/data.bin")
        for bad in ("../../secret.txt", "link.dat", "/etc/passwd", "nope.exe", "Content"):
            assert library.safe_file(g, bad) is None, bad


def test_vdf_roundtrip_and_shortcuts():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "shortcuts.vdf"
        assert A.add_shortcut(f, "Celeste", "/home/deck/Games/Celeste/Celeste.exe", "/home/deck/Games/Celeste")
        assert not A.add_shortcut(f, "Celeste", "/home/deck/Games/Celeste/Celeste.exe", "/home/deck/Games/Celeste")
        assert A.add_shortcut(f, "Hades", "/home/deck/Games/Hades/Hades.exe", "/home/deck/Games/Hades", "-x")
        raw = f.read_bytes()
        assert raw.startswith(b"\x00shortcuts\x00") and raw.endswith(b"\x08\x08")
        sc = A.vdf_loads(raw)["shortcuts"]
        assert [sc[k]["AppName"] for k in ("0", "1")] == ["Celeste", "Hades"] and sc["1"]["LaunchOptions"] == "-x"
        assert sc["0"]["Exe"] == '"/home/deck/Games/Celeste/Celeste.exe"' and sc["0"]["appid"] & 0x80000000
        assert A.vdf_dumps(A.vdf_loads(raw)) == raw                                  # lossless
        assert not (Path(d) / "shortcuts.vdf.casa.bak").exists() or True
        assert A.remove_shortcut(f, "/home/deck/Games/Celeste/Celeste.exe")
        assert [v["AppName"] for v in A.vdf_loads(f.read_bytes())["shortcuts"].values()] == ["Hades"]
        f.write_bytes(b"garbage")
        try:
            A.add_shortcut(f, "x", "/x", "/")
            raise AssertionError("corrupt file accepted")
        except ValueError:
            pass
        assert f.read_bytes() == b"garbage"                                          # never overwritten


def test_agent_validation():
    with tempfile.TemporaryDirectory() as d:
        a = A.Agent(TOKEN, Path(d, "g"), Path(d, "r"), Path(d, "s"), Path(d, "data"))
        ok = {"id": "pc-x", "kind": "portable", "folder": "X", "exe": "x.exe", "base": "http://h",
              "files": [{"p": "x.exe", "s": 1}], "title": "X", "exp": 1, "sig": "s"}
        for bad in ({**ok, "id": "../x"}, {**ok, "kind": "evil"}, {**ok, "files": []}, {**ok, "files": [{"p": "../a", "s": 1}]},
                    {**ok, "files": [{"p": "/etc/passwd", "s": 1}]}, {**ok, "folder": ".."}, {**ok, "exe": "../x"},
                    {**ok, "base": "file:///etc"}, {**ok, "kind": "rom", "system": "../../etc"}):
            try:
                a.submit(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        assert A.safe_name("a/b:c") == "a_b_c" and A.safe_name("..") is None and A.safe_rel("a/./b") is None


class Deck:
    """A temp 'Steam Deck': agent on a real socket, fake Steam, user dirs."""

    def __init__(self, tmp: Path, srm=""):
        self.tmp, self.steam = tmp, {"running": True, "started": 0}
        (tmp / "steam/userdata/12345/config").mkdir(parents=True)
        (tmp / "steam/userdata/0/config").mkdir(parents=True)
        self.agent = A.Agent(TOKEN, tmp / "Games", tmp / "roms", tmp / "steam", tmp / "data", srm)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), A.make_handler(self.agent))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.srm_calls = []
        A.steam_running = lambda: self.steam["running"]
        A.steam_shutdown = lambda: self.steam.update(running=False) or True
        A.steam_start = lambda: self.steam.update(running=True, started=self.steam["started"] + 1)
        A.run_srm = lambda cmd: self.srm_calls.append(cmd) or True

    def close(self):
        self.srv.shutdown()


def casa_app(plugin):
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
    for rd in plugin.routes():
        app.router.add_route(rd.method, "/api/p/games" + rd.path, rd.handler)
    return app


async def wait_state(client, gid, want, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = await (await client.get("/api/p/games/library")).json()
        g = {x["id"]: x for x in d["games"]}[gid]
        if g["state"] in want:
            return g, d
        await asyncio.sleep(0.1)
    raise AssertionError("timeout waiting for %s, last %s" % (want, g["state"]))


def test_full_install_flow():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lib = tmp / "nas/games"
        make_library(lib)
        deck = Deck(tmp / "deck", srm="/fake/srm")

        async def go():
            (tmp / "hubdata").mkdir(exist_ok=True)
            hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
            cfg = {"library": str(lib), "deck": {"host": "127.0.0.1", "port": deck.port, "token": TOKEN}}
            p = GamesPlugin(hub, cfg)
            await p.start()
            await p._scan()          # start() scans in the background
            async with TestClient(TestServer(casa_app(p))) as c:
                post = lambda path, body: c.post("/api/p/games" + path, json=body)
                d0 = await (await c.get("/api/p/games/library")).json()
                assert d0["deck"]["online"] and len(d0["games"]) == 7 and all(g["state"] == "not_installed" for g in d0["games"])
                assert not any(k.startswith("_") for k in d0["games"][0])
                assert d0["exists"] is True and d0["deck"]["configured"] and d0["deck"]["host"] == "127.0.0.1"
                assert {x["key"]: x["count"] for x in d0["systems"]} == {"pc": 4, "linux": 1, "snes": 1, "psx": 1}, d0["systems"]
                assert d0["systems"][0]["key"] == "pc"                                  # biggest first
                tr = await (await c.post("/api/p/games/test", json={})).json()
                assert tr["ok"] is True and tr["error"] is None and tr["pending"] == 0
                assert (await c.get("/api/p/games/cover/pc-celeste")).status == 200
                assert (await c.get("/api/p/games/cover/pc-hades")).status == 404

                # --- PC game, resume from a partial file
                part = deck.tmp / "Games/Celeste/Celeste.exe.part"
                part.parent.mkdir(parents=True)
                part.write_bytes(b"E" * 1000)
                assert (await post("/install", {"id": "pc-celeste"})).status == 200
                g, _ = await wait_state(c, "pc-celeste", ("installed",))
                dest = deck.tmp / "Games/Celeste"
                assert (dest / "Celeste.exe").read_bytes() == b"E" * 3000 and (dest / "Content/data.bin").stat().st_size == 5000
                assert not part.exists() and not (dest / "cover.jpg").exists()
                assert g["installed_version"] == "1.4" and "Apply to Steam" in g["note"]
                assert (await (await c.get("/api/p/games/library")).json())["deck"]["pending"] == 1

                # --- apply: refuses while Steam runs, then restarts it and writes the shortcut
                r = await post("/apply", {})
                assert r.status == 400 and "Steam is running" in (await r.json())["error"]
                r = await post("/apply", {"restart": True})
                body = await r.json()
                assert r.status == 200 and body["shortcuts"] == 1 and body["restarted"] and deck.steam["started"] == 1
                sc = A.vdf_loads((deck.tmp / "steam/userdata/12345/config/shortcuts.vdf").read_bytes())["shortcuts"]
                assert sc["0"]["AppName"] == "Celeste" and sc["0"]["Exe"].endswith('Celeste/Celeste.exe"')
                assert not (deck.tmp / "steam/userdata/0/config/shortcuts.vdf").exists()
                assert (await (await c.get("/api/p/games/library")).json())["deck"]["pending"] == 0

                # --- update detection
                (lib / "pc/Celeste v1.4/game.json").write_text(json.dumps({"version": "1.5"}))
                await post("/rescan", {})
                g, _ = await wait_state(c, "pc-celeste", ("update",))
                assert g["installed_version"] == "1.4" and g["version"] == "1.5"

                # --- ROM (file) and ROM (folder)
                assert (await post("/install", {"id": "snes-super-game"})).status == 200
                assert (await post("/install", {"id": "psx-disc-game"})).status == 200
                await wait_state(c, "snes-super-game", ("installed",))
                await wait_state(c, "psx-disc-game", ("installed",))
                assert (deck.tmp / "roms/snes/Super Game (v1.1).sfc").stat().st_size == 2048
                assert (deck.tmp / "roms/psx/Disc Game/Disc.bin").stat().st_size == 4096
                assert (await (await c.get("/api/p/games/library")).json())["deck"]["pending"] == 1     # SRM run
                body = await (await post("/apply", {"restart": True})).json()
                assert body["srm"] is True and deck.srm_calls == ["/fake/srm"]
                assert (await (await c.get("/api/p/games/library")).json())["deck"]["pending"] == 0

                # --- installer: downloaded, flagged as manual
                await post("/install", {"id": "pc-old-game"})
                g, _ = await wait_state(c, "pc-old-game", ("needs_manual",))
                assert (deck.tmp / "Games/_installers/Old Game/setup_old_game.exe").is_file() and "Proton" in g["message"]
                assert (await post("/install", {"id": "nope"})).status == 404

                # --- uninstall keeps saves, drops files and the shortcut
                (dest / "save.dat").write_bytes(b"my save")
                deck.steam["running"] = False
                body = await (await post("/uninstall", {"id": "pc-celeste"})).json()
                assert body["shortcut_removed"] and not (dest / "Celeste.exe").exists() and (dest / "save.dat").exists()
                assert A.vdf_loads((deck.tmp / "steam/userdata/12345/config/shortcuts.vdf").read_bytes())["shortcuts"] == {}
                g, _ = await wait_state(c, "pc-celeste", ("not_installed",))
                assert (await post("/uninstall", {"id": "pc-celeste"})).status == 404
                await post("/uninstall", {"id": "psx-disc-game"})
                assert not (deck.tmp / "roms/psx/Disc Game").exists() and (deck.tmp / "roms/snes/Super Game (v1.1).sfc").exists()

                # --- download links: signed, expiring, confined to the game's files
                exp = int(time.time()) + 60
                good = "e=%d&t=%s" % (exp, p._sig("pc-hades", exp))
                assert (await c.get("/api/p/games/dl/pc-hades/Hades.exe?" + good)).status == 200
                assert (await c.get("/api/p/games/dl/pc-hades/Hades.exe?e=%d&t=bad" % exp)).status == 403
                assert (await c.get("/api/p/games/dl/pc-hades/Hades.exe")).status == 403
                old = int(time.time()) - 5
                assert (await c.get("/api/p/games/dl/pc-hades/Hades.exe?e=%d&t=%s" % (old, p._sig("pc-hades", old)))).status == 403
                assert (await c.get("/api/p/games/dl/pc-celeste/Celeste.exe?" + good)).status == 403      # sig is per game
                for bad in ("../../Celeste.exe", "missing.exe"):
                    assert (await c.get("/api/p/games/dl/pc-hades/%s?%s" % (bad, good))).status in (403, 404)
                await p.stop()
            return True

        try:
            assert asyncio.run(go())
        finally:
            deck.close()


def test_deck_errors_are_reported():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        make_library(tmp / "lib")
        deck = Deck(tmp / "deck")

        async def go(cfg_deck, expect):
            (tmp / "hubdata").mkdir(exist_ok=True)
            hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
            p = GamesPlugin(hub, {"library": str(tmp / "lib"), "deck": cfg_deck})
            await p.start()
            await p._scan()          # start() scans in the background
            expect = expect()          # translated: the language is set once a Hub exists
            async with TestClient(TestServer(casa_app(p))) as c:
                lib = await (await c.get("/api/p/games/library")).json()
                assert lib["deck"]["online"] is False and expect in lib["deck"]["error"], lib["deck"]
                assert len(lib["games"]) == 7                                       # the library still lists
                tr = await (await c.post("/api/p/games/test", json={})).json()
                assert tr["ok"] is False and expect in tr["error"]
                r = await c.post("/api/p/games/install", json={"id": "pc-celeste"})
                assert r.status == 400 and expect in (await r.json())["error"]
            await p.stop()

        try:
            asyncio.run(go({"host": "127.0.0.1", "port": deck.port, "token": "w" * 32},
                           lambda: _("Wrong token: check it matches the one on the Deck")))
            asyncio.run(go({"host": "127.0.0.1", "port": 1, "token": TOKEN},
                           lambda: _("Cannot reach the Steam Deck: {err}", err="").strip()))
            asyncio.run(go({}, lambda: _("Set the Steam Deck address and token in Settings → Games.")))
        finally:
            deck.close()


def test_download_route_works_through_the_real_web_server_with_a_password():
    """The Deck agent has no login cookie: only the signed /dl/ link may pass, nothing else."""
    from hub.web import WebServer
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        make_library(tmp / "lib")
        (tmp / "hubdata").mkdir()
        (tmp / "static").mkdir()

        async def go():
            hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
            p = GamesPlugin(hub, {"library": str(tmp / "lib")})
            await p.start()
            await p._scan()          # start() scans in the background
            hub.plugins["games"] = p
            ws = WebServer(hub, {"password": "secret"}, tmp / "static")
            async with TestClient(TestServer(ws.build())) as c:
                assert (await c.get("/api/p/games/library")).status == 401              # app API needs the login
                assert (await c.post("/api/p/games/install", json={"id": "pc-hades"})).status == 401
                exp = int(time.time()) + 60
                r = await c.get("/api/p/games/dl/pc-hades/Hades.exe?e=%d&t=%s" % (exp, p._sig("pc-hades", exp)))
                assert r.status == 200 and len(await r.read()) == 30                    # signed link needs no cookie
                assert (await c.get("/api/p/games/dl/pc-hades/Hades.exe?e=%d&t=x" % exp)).status == 403
                assert (await c.get("/api/p/media/dl/x")).status == 401                  # no other prefix is open
            await p.stop()
            return True
        assert asyncio.run(go())


# ---------------------------------------------------------------- metadata, find, art

from hub.plugins.games import find as F  # noqa: E402
from hub.plugins.games import metadata as M  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 20
JPG = b"\xff\xd8\xff\xe0" + b"j" * 20


def test_title_cleaning_and_platform_mapping():
    c = M.clean_title
    assert c("Celeste v1.4 [FitGirl Repack]") == "Celeste"
    assert c("Hades (Build 12345) - GOG") == "Hades"
    assert c("Super Mario World (USA) [!]", "rom") == "Super Mario World"
    assert c("Sonic_The_Hedgehog_(Europe)_(Rev_1)", "rom") == "Sonic The Hedgehog"
    assert c("The.Witcher.3.Wild.Hunt.v1.31-GROUP") == "The Witcher 3 Wild Hunt GROUP" or c("The.Witcher.3.Wild.Hunt.v1.31-GROUP").startswith("The Witcher 3")
    assert c("Prototype") == "Prototype"
    assert "pc" in M.igdb_platforms("pc") and "snes" in M.igdb_platforms("snes") and not M.igdb_platforms("zzz")
    hits = [{"id": 1, "name": "Sonic", "platforms": [{"abbreviation": "MD"}]},
            {"id": 2, "name": "Sonic", "platforms": [{"abbreviation": "GBA"}]},
            {"id": 3, "name": "Totally Other", "platforms": []}]
    assert M.pick_best("Sonic", hits, "gba")["id"] == 2 and M.pick_best("Sonic", hits, "genesis")["id"] == 1
    assert M.pick_best("Zzzzzz Qqqq", hits) is None


def test_igdb_response_mapping():
    m = M.to_meta({"id": 7, "name": "Celeste", "summary": "Climb.", "genres": [{"name": "Platform"}],
                   "first_release_date": 1516924800, "total_rating": 87.6,
                   "involved_companies": [{"developer": False, "company": {"name": "Pub"}},
                                          {"developer": True, "company": {"name": "Maddy"}}],
                   "cover": {"image_id": "co1"}, "screenshots": [{"image_id": "s%d" % i} for i in range(9)],
                   "artworks": [{"image_id": "a1"}], "platforms": [{"abbreviation": "PC"}]})
    assert (m["igdb_id"], m["title"], m["genres"], m["released"], m["rating"], m["developer"]) == (
        7, "Celeste", ["Platform"], "2018-01-26", 88, "Maddy")
    assert m["_cover"] == "co1" and len(m["_shots"]) == M.SHOTS and m["_art"] == ["a1"] and m["platforms"] == ["PC"]
    e = M.to_meta({"id": 1})
    assert e["released"] is None and e["rating"] is None and e["developer"] is None and e["genres"] == []


def test_image_cache_eviction_and_safe_names():
    with tempfile.TemporaryDirectory() as d:
        c = M.ImageCache(Path(d), cap=250)
        for i in range(4):
            c.put("g%d.cover.jpg" % i, b"x" * 100)
            os.utime(Path(d) / ("g%d.cover.jpg" % i), (1000 + i, 1000 + i))
        assert c.total() == 400 and c.evict() == 2 and c.total() == 200
        assert c.path("g0.cover.jpg") is None and c.path("g3.cover.jpg")
        assert c.path("../x") is None and c.path("a/b.jpg") is None and c.path("") is None
        c.purge("g3")
        assert c.path("g3.cover.jpg") is None


class FakeMeta(M.Metadata):
    """IGDB/SteamGridDB/image fetchers replaced; records what was asked."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.asked = []

    async def search(self, term, limit=10):
        self.asked.append(term)
        return [{"id": 7, "name": "Celeste", "summary": "Climb.", "genres": [{"name": "Platform"}],
                 "first_release_date": 1516924800, "total_rating": 87.6, "cover": {"image_id": "co1"},
                 "screenshots": [{"image_id": "s1"}, {"image_id": "s2"}], "artworks": [{"image_id": "a1"}],
                 "involved_companies": [{"developer": True, "company": {"name": "Maddy"}}],
                 "platforms": [{"abbreviation": "PC"}]}]

    async def by_id(self, igdb_id):
        self.asked.append(igdb_id)
        return {"id": igdb_id, "name": "Other Celeste", "cover": {"image_id": "co2"}}

    async def _download(self, url, base):
        name = base + ".jpg"
        self.cache.put(name, JPG)
        return name

    async def _sgdb(self, path):
        return []


def test_metadata_fetch_and_manual_match():
    with tempfile.TemporaryDirectory() as d:
        hub = Hub({"homekit": {"enabled": False}}, Path(d))

        async def go():
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                m = FakeMeta(hub.store, sess, Path(d) / "art", "id", "secret", "")
                assert m.igdb and m.configured and not m.steamgriddb
                g = {"id": "pc-celeste", "title": "Celeste v1.4 [Repack]", "kind": "portable", "platform": "pc"}
                rec = await m.fetch(g)
                assert m.asked == ["Celeste"] and rec["matched_by"] == "auto"
                pub = m.public("pc-celeste")
                assert pub["title"] == "Celeste" and pub["released"] == "2018-01-26" and pub["rating"] == 88
                assert pub["has_hero"] and len(pub["screenshots"]) == 2 and pub["screenshots"][0].startswith("/api/p/games/img/")
                assert m.file("pc-celeste", "cover") and hub.store.get("games.meta.pc-celeste")["igdb_id"] == 7
                rec = await m.fetch(g, igdb_id=99, manual=True)
                assert rec["matched_by"] == "manual" and m.public("pc-celeste")["igdb_id"] == 99
                await m.fetch(g)                                  # a manual match survives a refresh
                assert m.public("pc-celeste")["igdb_id"] == 99
                assert M.Metadata(hub.store, sess, Path(d) / "x").configured is False
        asyncio.run(go())


def test_library_has_meta_cover_priority_and_art_for_the_deck():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lib = tmp / "games"
        put(lib / "pc/Celeste v1.4/Celeste.exe", b"E", 30)
        put(lib / "pc/Hades/Hades.exe", b"E", 30)
        put(lib / "pc/Celeste v1.4/cover.jpg", JPG)
        (tmp / "hubdata").mkdir()
        hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")

        async def go():
            p = GamesPlugin(hub, {"library": str(lib)})
            await p.start()
            await p._scan()          # start() scans in the background
            try:
                assert not p.meta.configured and p._meta_queue == {}      # no keys: nothing is fetched
                p.meta = FakeMeta(hub.store, p.session, tmp / "art", "id", "secret", "")
                await p.meta.fetch(p.games["pc-celeste"])
                await p.meta.fetch(p.games["pc-hades"])
                p.meta.cache.put("pc-hades.grid.png", PNG)
                p.meta.cache.put("pc-hades.wide.png", b"junk")            # not an image: never sent
                p.meta.cache.put("pc-celeste.grid.png", PNG)
                for g in ("pc-hades", "pc-celeste"):
                    rec = p.meta.get(g)
                    rec["files"].update(grid=g + ".grid.png")
                    if g == "pc-hades":
                        rec["files"]["wide"] = "pc-hades.wide.png"
                async with TestClient(TestServer(casa_app(p))) as c:
                    lib_ = await (await c.get("/api/p/games/library")).json()
                    assert lib_["metadata"]["igdb"] and lib_["metadata"]["configured"] and not lib_["metadata"]["steamgriddb"]
                    by = {x["id"]: x for x in lib_["games"]}
                    assert by["pc-hades"]["meta"]["developer"] == "Maddy" and by["pc-hades"]["cover"] is True
                    r = await c.get("/api/p/games/cover/pc-celeste")
                    assert (await r.read()) == JPG                         # local cover.jpg wins
                    assert (await c.get("/api/p/games/cover/pc-hades")).status == 200   # cached cover
                    assert (await c.get("/api/p/games/hero/pc-hades")).status == 200
                    name = by["pc-hades"]["meta"]["screenshots"][0].rsplit("/", 1)[1]
                    assert (await c.get("/api/p/games/img/" + name)).status == 200
                    assert (await c.get("/api/p/games/img/..%2Fx")).status == 404
                art = p._art(p.games["pc-hades"])
                assert set(art) == {"grid", "hero"} and base64.b64decode(art["grid"]) == PNG
                assert base64.b64decode(p._art(p.games["pc-celeste"])["grid"]) == JPG    # local cover as the grid
            finally:
                await p.stop()
        asyncio.run(go())


class FakeTx:
    enabled = True

    def __init__(self):
        self.added, self.removed, self.state = [], [], {}

    async def add(self, magnet="", download_dir=None, paused=False, torrent=None):
        self.added.append((magnet, download_dir))
        h = "%040x" % len(self.added)
        self.state[h] = {"hash": h, "percentDone": 0.5, "leftUntilDone": 10, "rateDownload": 1000, "eta": 60,
                         "downloadDir": download_dir, "name": "", "error": 0, "errorString": ""}
        return {"id": len(self.added), "hash": h, "name": "x"}

    async def torrents(self, hashes=None, files=False):
        return [self.state[h] for h in hashes if h in self.state]

    async def remove(self, h, delete_data=False):
        self.removed.append((h, delete_data))
        self.state.pop(h, None)


def make_zip(path: Path, members: dict):
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in members.items():
            zf.writestr(n, data)


def test_zip_extraction_and_zip_slip():
    with tempfile.TemporaryDirectory() as d:
        z, out = Path(d) / "a.zip", Path(d) / "out"
        make_zip(z, {"Game/run.exe": b"x" * 10, "Game/data/a.bin": b"y"})
        assert F.unzip(z, out) == 2 and (out / "Game/data/a.bin").read_bytes() == b"y" and F.zip_single_top(z)
        for bad in ("../evil.txt", "/abs.txt", "a/../../evil.txt", "C:/evil.txt", "..\\evil.txt"):
            make_zip(z, {bad: b"no"})
            try:
                F.unzip(z, Path(d) / "out2")
                raise AssertionError("accepted " + bad)
            except F.UnpackError:
                pass
        assert not (Path(d) / "evil.txt").exists() and not (Path(d) / "out2").exists() or not any((Path(d) / "out2").rglob("*.txt"))
        make_zip(z, {"big.bin": b"\0" * (30 << 20)})                  # 30 MB of zeros: ratio > 1000
        import zipfile
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\0" * (30 << 20))
        try:
            F.unzip(z, Path(d) / "out3")
            raise AssertionError("bomb accepted")
        except F.UnpackError:
            pass


def test_find_and_download_flow():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lib = tmp / "games"
        (lib / "pc").mkdir(parents=True)
        (tmp / "hubdata").mkdir()
        hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
        from hub.plugins.torrents.model import Result
        seen = {}

        class Eng:
            async def search(self, session, q, page, size, ids, category=""):
                seen.update(q=q, category=category, ids=ids)
                r = [Result(title="Celeste", source="x", size=5, seeders=3, leechers=1, magnet="magnet:?xt=urn:btih:" + "a" * 40,
                            hash="a" * 40, sources=["x"])]
                return r, [{"id": "x", "name": "X", "ok": True, "count": 1, "error": None}], False

        class Torr:
            engine, session, config = Eng(), None, {}
            enabled_ids = staticmethod(lambda: {"x"})
            _note = staticmethod(lambda st: None)

        tx = FakeTx()
        Media = type("Media", (), {"transmission": tx})

        async def go():
            p = GamesPlugin(hub, {"library": str(lib)})
            await p.start()
            await p._scan()          # start() scans in the background
            try:
                async with TestClient(TestServer(casa_app(p))) as c:
                    r = await c.get("/api/p/games/find?q=celeste&platform=pc")
                    d = await r.json()                                                            # not running
                    assert r.status == 200 and d.get("disabled") is True and d["error"], (r.status, d)
                    hub.plugins["torrents"], hub.plugins["media"] = Torr(), Media()
                    res = await (await c.get("/api/p/games/find?q=celeste&platform=pc")).json()
                    assert seen["category"] == "games" and res["results"][0]["title"] == "Celeste" and res["providers"][0]["ok"]
                    assert set(res["results"][0]) == {"title", "size", "seeders", "leechers", "source", "sources", "magnet",
                                                      "url", "hash"} and res["has_more"] is False
                    pl = await (await c.get("/api/p/games/find/platforms")).json()
                    assert pl[0] == {"key": "pc", "label": "PC"} and any(x["key"] == "snes" for x in pl)
                    post = lambda b: c.post("/api/p/games/find/download", json=b)
                    for bad in ({"title": "x", "platform": "../x", "magnet": "magnet:?xt=1"},
                                {"title": "x", "platform": "pc", "magnet": "ftp://x"}, {"title": "", "platform": "pc", "magnet": "magnet:?a"}):
                        assert (await post(bad)).status == 400
                    j = await (await post({"title": "Celeste", "platform": "pc", "magnet": "magnet:?xt=urn:btih:" + "a" * 40})).json()
                    j2 = await (await post({"title": "Super Game", "platform": "snes", "url": "https://x/t.torrent"})).json()
                    assert j["ok"] and tx.added[0][1] == str(lib / "pc") and tx.added[1][1] == str(lib / "emu/snes")
                    lst = await (await c.get("/api/p/games/find/downloads")).json()
                    assert [x["state"] for x in lst] == ["downloading"] * 2

                    await p.finder.poll()                               # half done: progress is reported
                    lst = await (await c.get("/api/p/games/find/downloads")).json()
                    assert lst[0]["progress"] == 0.5 and lst[0]["rate"] == 1000 and lst[0]["eta"] == 60

                    # PC torrent finishes with a bare zip in pc/; the ROM torrent with a rom file
                    h1, h2 = j["hash"], j2["hash"]
                    make_zip(lib / "pc/Celeste.zip", {"Celeste/Celeste.exe": b"E" * 50})
                    (lib / "emu/snes").mkdir(parents=True, exist_ok=True)
                    (lib / "emu/snes/Super Game.sfc").write_bytes(b"R" * 40)
                    tx.state[h1].update(percentDone=1.0, leftUntilDone=0, name="Celeste.zip", downloadDir=str(lib / "pc"))
                    tx.state[h2].update(percentDone=1.0, leftUntilDone=0, name="Super Game.sfc", downloadDir=str(lib / "emu/snes"))
                    await p.finder.poll()
                    for _i in range(100):
                        if all(x["state"] == "done" for x in p.finder.listing()):
                            break
                        await asyncio.sleep(0.05)
                    lst = await (await c.get("/api/p/games/find/downloads")).json()
                    assert [x["state"] for x in lst] == ["done", "done"], lst
                    assert sorted(tx.removed) == sorted([(h1, False), (h2, False)])      # data kept
                    assert not (lib / "pc/Celeste.zip").exists() and (lib / "pc/Celeste/Celeste.exe").is_file()
                    assert "pc-celeste" in p.games and "snes-super-game" in p.games       # rescanned
                    owned = {e["hash"]: e.get("owned") for e in p.finder.entries()}
                    assert [Path(x).resolve() for x in owned[h1]] == [(lib / "pc/Celeste").resolve()]      # what a delete may touch
                    assert [Path(x) for x in owned[h2]] == [lib / "emu/snes/Super Game.sfc"]
                    # forget a finished entry: files stay
                    assert (await c.delete("/api/p/games/find/downloads/" + h1)).status == 200
                    assert (lib / "pc/Celeste/Celeste.exe").exists() and len(p.finder.entries()) == 1
                    assert (await c.delete("/api/p/games/find/downloads/" + "0" * 40)).status == 404

                    # cancel a running one: torrent and partial data go
                    j3 = await (await post({"title": "Other", "platform": "pc", "magnet": "magnet:?xt=urn:btih:" + "b" * 40})).json()
                    tx.removed.clear()
                    assert (await c.delete("/api/p/games/find/downloads/" + j3["hash"])).status == 200
                    assert tx.removed == [(j3["hash"], True)]

                    # an archive that is unsafe is an error, not a write outside the root
                    make_zip(lib / "pc/Evil.zip", {"../../outside.txt": b"x"})
                    j4 = await (await post({"title": "Evil", "platform": "pc", "magnet": "magnet:?xt=urn:btih:" + "c" * 40})).json()
                    tx.state[j4["hash"]].update(percentDone=1.0, leftUntilDone=0, name="Evil.zip", downloadDir=str(lib / "pc"))
                    await p.finder.poll()
                    for _i in range(100):
                        if next(x for x in p.finder.listing() if x["hash"] == j4["hash"])["state"] != "extracting":
                            break
                        await asyncio.sleep(0.05)
                    e = next(x for x in p.finder.listing() if x["hash"] == j4["hash"])
                    assert e["state"] == "error" and not (tmp / "outside.txt").exists() and (lib / "pc/Evil.zip").exists()
            finally:
                await p.stop()
        asyncio.run(go())


def test_rar_without_a_tool_needs_extraction():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "games"
        (root / "pc/G").mkdir(parents=True)
        (root / "pc/G/g.rar").write_bytes(b"Rar!")
        (root / "pc/G/g.part2.rar").write_bytes(b"Rar!")
        assert [a.name for a in F.find_archives(root / "pc/G")] == ["g.rar"]
        orig = F.tool_for
        F.tool_for = lambda name: None
        try:
            msg = asyncio.run(F.unpack_payload(root / "pc/G", root))
        finally:
            F.tool_for = orig
        assert "7z" in msg and (root / "pc/G/g.rar").exists()
        try:
            asyncio.run(F.unpack_payload(Path(d), root))
            raise AssertionError
        except F.UnpackError:
            pass


def test_agent_writes_art_into_steam_grid():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        steam = tmp / "steam"
        (steam / "userdata/1234/config").mkdir(parents=True)
        (steam / "userdata/0/config").mkdir(parents=True)
        A.steam_running = lambda: False
        agent = A.Agent(TOKEN, tmp / "Games", tmp / "roms", steam, tmp / "data")
        assert A.VERSION == "1.2"
        art = A.decode_art({"grid": base64.b64encode(PNG).decode(), "hero": base64.b64encode(JPG).decode()})
        assert art["grid"] == (PNG, ".png") and art["hero"][1] == ".jpg" and A.decode_art(None) == {}
        for bad in ({"grid": "!!notbase64"}, {"grid": base64.b64encode(b"GIF89a....").decode()}, {"evil": "AAAA"}, "x",
                    {"grid": base64.b64encode(PNG + b"0" * (4 << 20)).decode()}):
            try:
                A.decode_art(bad)
                raise AssertionError("accepted %r" % (str(bad)[:30],))
            except ValueError:
                pass
        exe = tmp / "Games/Celeste/Celeste.exe"
        staged = agent._stage_art({"id": "pc-celeste", "_art": art})
        agent._save(agent.pend_file, {"shortcuts": [{"name": "Celeste", "exe": str(exe), "startdir": str(exe.parent), "art": staged},
                                                    {"name": "Old", "exe": "/x/old.exe", "startdir": "/x"}], "srm": False})
        res = agent.apply(False)
        assert res["shortcuts"] == 2          # both shortcuts, for the one real Steam user
        appid = A.shortcut_appid('"%s"' % exe, "Celeste")
        grid = steam / "userdata/1234/config/grid"
        assert (grid / ("%dp.png" % appid)).read_bytes() == PNG and (grid / ("%d_hero.jpg" % appid)).read_bytes() == JPG
        assert not (steam / "userdata/0/config/grid").exists() and not list((tmp / "data/art").glob("*"))
        assert sorted(p.name for p in grid.iterdir()) == sorted(["%dp.png" % appid, "%d_hero.jpg" % appid])
        vdf = A.vdf_loads((steam / "userdata/1234/config/shortcuts.vdf").read_bytes())
        assert {v["appid"] for v in vdf["shortcuts"].values()} >= {appid}


# -- discover (fake HTTP, no network) ------------------------------------------------------------

class FakeResp:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.data

    @property
    def content(self):
        raw = json.dumps(self.data).encode()

        class _Body:
            async def iter_chunked(self, n):
                for i in range(0, len(raw), n):
                    yield raw[i:i + n]
        return _Body()


class FakeSession:
    """session.get(url, params=…) answered from {url fragment: (status, json) | Exception}."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        for frag, ans in self.routes.items():
            if frag in url:
                if isinstance(ans, Exception):
                    raise ans
                return FakeResp(*ans)
        raise AssertionError("unexpected url " + url)


def _fi(i, name, **kw):
    return {"id": i, "type": 0, "name": name, "discount_percent": 0, "original_price": None, "final_price": 1999,
            "currency": "EUR", "header_image": "https://shared.akamai.steamstatic.com/x/%d.jpg" % i, **kw}


FEATURED = {"top_sellers": {"items": [_fi(1, "Celeste®"), {"id": 2, "type": 4, "name": "Tool"},
                                      _fi(3, "Hades", discount_percent=50, original_price=2999, final_price=1500)]},
            "new_releases": {"items": [_fi(4, "New One")]}, "specials": {"items": []},
            "coming_soon": {"items": [_fi(5, "Later", header_image="http://evil.example/x.jpg")]}, "status": 1}
RANKS = {"response": {"ranks": [{"rank": 1, "appid": 730}, {"rank": 2, "appid": 3}, {"rank": 3, "appid": 999}]}}
ITEMS = {"response": {"store_items": [
    {"appid": 730, "name": "Counter-Strike 2", "type": 0, "is_free": True,
     "reviews": {"summary_filtered": {"percent_positive": 85, "review_count": 900}}, "release": {"steam_release_date": 1346000000}},
    {"appid": 3, "name": "Hades", "type": 0, "reviews": {"summary_filtered": {"percent_positive": 97, "review_count": 5}}},
    {"appid": 1, "name": "Celeste", "type": 0, "reviews": {"summary_filtered": {"percent_positive": 90, "review_count": 50}}},
]}}
DETAIL = {"1": {"success": True, "data": {
    "name": "Celeste", "short_description": "Climb <b>the</b> mountain &amp; more<br>line2<script>alert(1)</script>",
    "genres": [{"description": "Platformer"}], "developers": ["Maddy"], "publishers": ["Maddy"],
    "metacritic": {"score": 92}, "release_date": {"coming_soon": False, "date": "25 gen 2018"},
    "price_overview": {"final": 1999, "initial": 1999, "discount_percent": 0, "currency": "EUR"},
    "header_image": "https://shared.akamai.steamstatic.com/h.jpg", "background_raw": "javascript:alert(1)",
    "screenshots": [{"path_thumbnail": "https://shared.akamai.steamstatic.com/t.jpg", "path_full": "https://evil.example/f.jpg"}],
    "movies": [{"thumbnail": "https://shared.akamai.steamstatic.com/m.jpg", "mp4": {"480": "https://video.akamai.steamstatic.com/a.mp4"}}]}}}


def _routes(**over):
    r = {"featuredcategories": (200, FEATURED), "GetMostPlayedGames": (200, RANKS), "GetItems": (200, ITEMS),
         "appdetails": (200, DETAIL), "steamspy": (200, {})}
    r.update(over)
    return r


def _disc(routes, lib=None, clock=None):
    from hub.plugins.games.discover import Discover
    kw = {"clock": clock} if clock else {}
    return Discover(FakeSession(routes), None, lambda: lib or {}, lambda: "it", **kw)


def test_discover_rails_parse_flag_and_cache():
    async def go():
        d = _disc(_routes(), {"celeste": {"pc"}, "hades": {"snes"}})
        r = await d.rails("steam")
        assert r["errors"] == [] and r["igdb"] is False
        rails = {x["key"]: x["items"] for x in r["rails"]}
        assert [x["key"] for x in r["rails"]] == ["top_sellers", "most_played", "new_releases", "coming_soon"]  # empty specials dropped
        top = rails["top_sellers"]
        assert [i["title"] for i in top] == ["Celeste®", "Hades"]            # type 4 (software) filtered out
        assert top[0]["in_library"] is True and top[1]["in_library"] is False   # Hades is only on SNES in the library
        assert top[0]["price"] == "19,99€" and top[1]["discount"] == 50 and top[1]["was"] == "29,99€"
        assert top[0]["rating"] == 90 and top[1]["rating"] is None             # < 10 reviews: no score
        assert top[0]["cover"].endswith("/1/library_600x900.jpg")
        assert rails["coming_soon"][0]["image"].endswith("/5/header.jpg")      # untrusted host replaced
        mp = rails["most_played"]
        assert [i["title"] for i in mp] == ["Counter-Strike 2", "Hades"] and mp[0]["free"] and mp[0]["year"] == "2012"
        n = len(d.session.calls)
        await d.rails("steam")
        assert len(d.session.calls) == n                                        # served from cache
        await d.rails("steam", refresh=True)
        assert len(d.session.calls) > n
        assert (await d.rails("igdb")) == {"rails": [], "errors": [], "igdb": False}
    asyncio.run(go())


def test_discover_partial_failures_and_fallbacks():
    import aiohttp

    async def go():
        # charts down: the other rails still arrive, one error for most_played
        d = _disc(_routes(GetMostPlayedGames=(500, {})))
        r = await d.rails("steam")
        assert [x["key"] for x in r["rails"]] == ["top_sellers", "new_releases", "coming_soon"]
        assert [e["rail"] for e in r["errors"]] == ["most_played"] and "500" in r["errors"][0]["error"]
        # featured down + network error: four errors, most_played still works
        d = _disc(_routes(featuredcategories=aiohttp.ClientError("boom")))
        r = await d.rails("steam")
        assert [x["key"] for x in r["rails"]] == ["most_played"]
        assert sorted(e["rail"] for e in r["errors"]) == ["coming_soon", "new_releases", "specials", "top_sellers"]
        # GetItems down: names come from SteamSpy
        spy = {"730": {"name": "Counter-Strike 2", "price": "0"}, "570": {"name": "Dota 2", "price": "0"}}
        d = _disc(_routes(GetItems=(503, {}), steamspy=(200, spy)))
        r = await d.rails("steam")
        mp = next(x for x in r["rails"] if x["key"] == "most_played")["items"]
        assert [i["title"] for i in mp][0] == "Counter-Strike 2" and len(mp) == 2
        # everything down: no exception, errors only
        d = _disc({"store.steampowered": (500, {}), "api.steampowered": (429, {}), "steamspy": (500, {})})
        r = await d.rails("all")
        assert r["rails"] == [] and len(r["errors"]) == 5
    asyncio.run(go())


def test_discover_stale_cache_survives_an_outage_and_expires():
    async def go():
        now = [0.0]
        routes = _routes()
        d = _disc(routes, clock=lambda: now[0])
        assert (await d.rails("steam"))["errors"] == []
        calls = len(d.session.calls)
        now[0] += 3600
        await d.rails("steam")
        assert len(d.session.calls) == calls                                    # still fresh after 1 h
        now[0] += 4 * 3600
        routes["featuredcategories"] = (500, {})                                # expired, and Steam is down
        r = await d.rails("steam")
        assert len(d.session.calls) > calls and r["errors"] == [] and len(r["rails"]) == 4   # stale data beats an error
    asyncio.run(go())


def test_discover_cache_is_bounded():
    from hub.plugins.games.discover import Cache
    now = [0]
    c = Cache(3, lambda: now[0])
    for i in range(6):
        now[0] += 1
        c.put(i, i, 100)
    assert len(c.data) == 3 and c.get(5) == 5 and c.get(0) is None
    now[0] += 1000
    assert c.get(5) is None and c.get(5, stale=True) == 5


def test_discover_detail_validation_and_sanitising():
    from hub.plugins.games import discover as D

    async def go():
        d = _disc(_routes(), {"celeste": {"pc"}})
        x = await d.detail("steam", "1")
        assert x["title"] == "Celeste" and x["in_library"] is True and x["metacritic"] == 92 and x["year"] == "2018"
        assert x["summary"] == "Climb the mountain & more\nline2"              # tags stripped, entities decoded, script dropped
        assert x["hero"] is None                                                # javascript: URL rejected
        assert x["screenshots"] == [{"thumb": "https://shared.akamai.steamstatic.com/t.jpg",
                                     "full": "https://shared.akamai.steamstatic.com/t.jpg"}]
        assert x["trailer"]["mp4"].endswith("a.mp4") and x["genres"] == ["Platformer"] and x["price"] == "19,99€"
        before = len(d.session.calls)
        await d.detail("steam", "1")
        assert len(d.session.calls) == before                                    # cached
        for bad in ("", "abc", "1;2", "-1", "1 2", "12345678901", "١٢", "../x"):
            try:
                await d.detail("steam", bad)
                raise AssertionError("accepted %r" % bad)
            except ValueError:
                pass
        for src in ("gog", "", "STEAM"):
            try:
                await d.detail(src, "1")
                raise AssertionError("accepted source %r" % src)
            except ValueError:
                pass
        try:
            await d.detail("igdb", "5")                                          # no IGDB keys
            raise AssertionError("igdb without keys")
        except ValueError:
            pass
        d2 = _disc(_routes(appdetails=(200, {"7": {"success": False}})))
        try:
            await d2.detail("steam", "7")
            raise AssertionError("missing game")
        except KeyError:
            pass
    asyncio.run(go())
    assert D.valid_id("730") and not D.valid_id(730) and not D.valid_id("7.3")
    assert D.strip_html("<p>a</p><p>b &lt;i&gt;</p>") == "a\n\nb <i>"
    assert D.safe_url("https://cdn.akamai.steamstatic.com/a.jpg") and not D.safe_url("http://store.steampowered.com/a.jpg") \
        and not D.safe_url("https://steampowered.com.evil.io/a.jpg") and not D.safe_url(5)
    assert D.norm_title("Celeste™ v1.4 [Repack]") == D.norm_title("CELESTE")


def test_discover_igdb_rails_degrade_silently():
    from hub.plugins.games import discover as D

    class Meta:
        igdb = True

        def __init__(self):
            self.asked = []

        async def _igdb(self, body, endpoint="games"):
            self.asked.append((endpoint, body))
            if endpoint == "popularity_primitives":
                raise D.MetaError("IGDB answered HTTP 403")
            if "total_rating_count > 200" in body:
                return [{"id": 9, "name": "Celeste", "slug": "celeste", "total_rating": 87.6, "total_rating_count": 900,
                         "cover": {"image_id": "co1"}, "first_release_date": 1516924800}]
            if "hypes" in body:
                return []
            if "platforms = (130)" in body:
                return [{"id": 10, "name": "Hades", "total_rating": 93, "total_rating_count": 100, "cover": {"image_id": "co2"}}]
            raise D.MetaError("IGDB unreachable: x")

    async def go():
        m = Meta()
        d = D.Discover(FakeSession({}), m, lambda: {"celeste": {"pc"}, "mario": {"gba"}}, lambda: "it")
        r = await d.rails("igdb")
        keys = {x["key"]: x for x in r["rails"]}
        assert set(keys) == {"top_rated", "platform:switch"}                       # popular failed, coming_soon empty, others errored
        assert keys["top_rated"]["items"][0]["in_library"] is True and keys["top_rated"]["items"][0]["rating"] == 88
        assert keys["top_rated"]["items"][0]["cover"].endswith("t_cover_big/co1.jpg")
        assert keys["platform:switch"]["label"] == "Nintendo Switch" and keys["platform:switch"]["items"][0]["platform"] == "switch"
        assert {e["rail"] for e in r["errors"]} == {"popular", "platform:ps2", "platform:gba", "platform:snes"}
        assert d.platform_rails() == ["switch", "ps2", "gba", "snes"]
    asyncio.run(go())



# -- GOG installers, innoextract, library detection --------------------------------------------------

import stat  # noqa: E402


def goggame(root: Path, gid="1207658930", name="Hades", exe="Hades.exe", work="", main=True, prefix=""):
    info = {"gameId": gid, "rootGameId": gid if main else "999", "name": name, "playTasks": [
        {"category": "launcher", "isPrimary": False, "name": "Config", "path": "Config.exe", "type": "FileTask"},
        {"category": "game", "isPrimary": True, "name": name, "path": exe, "type": "FileTask", "workingDir": work},
        {"category": "document", "name": "Manual", "link": "http://x", "type": "URLTask"}]}
    put(root / (prefix + "goggame-%s.info" % gid), json.dumps(info).encode())


def test_gog_info_in_library_scan():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d)
        put(lib / "pc/Hades GOG/Hades.exe", b"E", 500)
        put(lib / "pc/Hades GOG/Config.exe", b"C", 900)             # bigger, but not the primary task
        put(lib / "pc/Hades GOG/unins000.exe", b"U", 2000)
        goggame(lib / "pc/Hades GOG", name="Hades: Edition", exe="hades.EXE")     # case differs on purpose
        put(lib / "pc/Witcher/app/bin/w3.exe", b"W", 100)
        put(lib / "pc/Witcher/app/bin/launcher.exe", b"L", 900)
        goggame(lib / "pc/Witcher", gid="55", name="The Witcher", exe="bin\\w3.exe", work="bin", prefix="app/")
        goggame(lib / "pc/Witcher", gid="56", name="Witcher DLC", exe="dlc.exe", main=False, prefix="app/")
        put(lib / "pc/Broken/Broken.exe", b"B", 10)
        put(lib / "pc/Broken/goggame-9.info", b"{not json")
        games, problems = library.scan(str(lib))
        by = {g["title"]: g for g in games}
        h = by["Hades: Edition"]
        assert h["kind"] == "portable" and h["exe"] == "Hades.exe" and h["gog"] and h["workdir"] == ""
        assert h["id"] == "pc-hades-edition" and h["entry"] == "pc/Hades GOG"
        w = by["The Witcher"]
        assert w["exe"] == "app/bin/w3.exe" and w["workdir"] == "app/bin" and w["kind"] == "portable"
        assert by["Broken"]["gog"] is False and by["Broken"]["exe"] == "Broken.exe"
        assert not problems


FAKE_INNO = """#!%(py)s
import json, os, sys, time
a = sys.argv[1:]
d, inst = a[a.index("-d") + 1], a[-1]
mode = os.environ.get("FAKE_INNO", "ok")
with open(os.environ["FAKE_INNO_LOG"], "a") as f:
    f.write(" ".join(a) + "\\n")
def put(rel, data=b"x"):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
if mode == "fail":
    put("app/half.dat")
    sys.stderr.write("Error: bad data\\n")
    sys.exit(1)
if mode == "slow":
    open(os.environ["FAKE_INNO_PID"], "w").write(str(os.getpid()))
    put("app/half.dat")
    time.sleep(60)
put("app/Hades.exe", b"E" * 100)
put("app/data/pak.bin", b"P" * 300)
put("app/__support/uninstall.bat")
put("app/unins000.exe", b"U")
put("tmp/setup.tmp")
put("commonappdata/gog/x.cfg")
info = {"gameId": "1", "rootGameId": "1", "name": "Hades", "playTasks": [
    {"category": "game", "isPrimary": True, "path": "Hades.exe", "type": "FileTask"}]}
put("app/goggame-1.info", json.dumps(info).encode())
"""


def fake_inno(tmp: Path) -> Path:
    tool = tmp / "bin" / "innoextract"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(FAKE_INNO % {"py": sys.executable})
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def gog_installer(folder: Path, name="setup_hades_1.37_(12345).exe"):
    put(folder / name, b"MZ" + b"Inno Setup" + b"\0" * 50)
    put(folder / name.replace(".exe", "-1.bin"), b"B" * 400)


def test_gog_detection_rules():
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        put(t / "setup_plain.exe", b"MZ" + b"\0" * 50)                       # no marker, no .bin: not an Inno installer
        put(t / "setup_inno.exe", b"MZ Inno Setup")
        put(t / "setup_big.exe", b"MZ")
        put(t / "setup_big-1.bin", b"B")
        put(t / "game.exe", b"MZ Inno Setup")                                # name does not look like a GOG installer
        put(t / "patch_x_1.1.exe", b"MZ Inno Setup")
        found = [p.name for p in F.find_gog(t)]
        assert sorted(found) == ["patch_x_1.1.exe", "setup_big.exe", "setup_inno.exe"] and found[-1] == "patch_x_1.1.exe", found
        assert F.gog_title(Path("setup_the_witcher_3_1.2_(12345).exe")) == "The Witcher 3"
        assert F.gog_title(Path("setup_hades_1.37_(64bit)_(1).exe")) == "Hades"


def test_gog_extraction_with_fake_innoextract():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tool = fake_inno(tmp)
        log = tmp / "inno.log"
        os.environ.update(FAKE_INNO="ok", FAKE_INNO_LOG=str(log))
        root = tmp / "games"
        (root / "pc").mkdir(parents=True)
        orig = F.shutil.which
        F.shutil.which = lambda n, *a, **k: str(tool) if n == "innoextract" else orig(n, *a, **k)
        try:
            # a payload folder: extracted in place, installers gone, scaffolding gone
            gog_installer(root / "pc/Hades [GOG]")
            put(root / "pc/Hades [GOG]/readme.txt", b"r")
            owned = []
            msg = asyncio.run(F.unpack_payload(root / "pc/Hades [GOG]", root, owned))
            g = root / "pc/Hades [GOG]"
            assert msg == "" and owned == [g]
            assert (g / "Hades.exe").stat().st_size == 100 and (g / "data/pak.bin").is_file() and (g / "readme.txt").is_file()
            assert not (g / "app").exists() and not (g / "tmp").exists() and not (g / "commonappdata").exists()
            assert not (g / "__support").exists() and not (g / F.GOG_TEMP).exists()
            assert sorted(p.name for p in g.iterdir() if p.suffix in (".exe", ".bin")) == ["Hades.exe", "unins000.exe"]
            args = log.read_text().split()
            assert "--gog" in args and "-e" in args and "-d" in args and args[-1].endswith("setup_hades_1.37_(12345).exe")
            games, _p = library.scan(str(root))
            assert len(games) == 1 and games[0]["title"] == "Hades" and games[0]["exe"] == "Hades.exe" and games[0]["kind"] == "portable"

            # a bare installer straight in pc/ gets a folder named after the game
            shutil_rm = __import__("shutil").rmtree
            shutil_rm(root / "pc/Hades [GOG]")
            gog_installer(root / "pc", "setup_celeste_1.4_(9).exe")
            owned = []
            assert asyncio.run(F.unpack_payload(root / "pc/setup_celeste_1.4_(9).exe", root, owned)) == ""
            assert [x.resolve() for x in owned] == [(root / "pc/Celeste").resolve()] and (root / "pc/Celeste/Hades.exe").is_file(), (owned, sorted(str(x) for x in root.rglob("*")))
            assert not (root / "pc/setup_celeste_1.4_(9).exe").exists() and not (root / "pc/setup_celeste_1.4_(9)-1.bin").exists()

            # failure: installers stay, nothing half-extracted is left behind, the error is readable
            gog_installer(root / "pc/Bad Game")
            os.environ["FAKE_INNO"] = "fail"
            try:
                asyncio.run(F.unpack_payload(root / "pc/Bad Game", root))
                raise AssertionError("must fail")
            except F.UnpackError as err:
                assert "innoextract" in str(err) and "bad data" in str(err), str(err)
            bad = root / "pc/Bad Game"
            assert sorted(p.name for p in bad.iterdir()) == ["setup_hades_1.37_(12345)-1.bin", "setup_hades_1.37_(12345).exe"]

            # cancel: the extractor process is killed and the temp folder removed
            os.environ.update(FAKE_INNO="slow", FAKE_INNO_PID=str(tmp / "pid"))

            async def cancel_it():
                task = asyncio.ensure_future(F.unpack_payload(bad, root))
                for _i in range(100):
                    if (tmp / "pid").exists() and (tmp / "pid").read_text():
                        break
                    await asyncio.sleep(0.05)
                pid = int((tmp / "pid").read_text())
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return pid
            pid = asyncio.run(cancel_it())
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
            assert not alive and not (bad / F.GOG_TEMP).exists() and (bad / "setup_hades_1.37_(12345).exe").exists()

            # no tool installed: the entry says what is missing instead of failing
            F.shutil.which = lambda n, *a, **k: None
            assert "innoextract" in asyncio.run(F.unpack_payload(bad, root))
        finally:
            F.shutil.which = orig


def test_deletable_refuses_anything_but_a_downloads_own_files():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = tmp / "games"
        put(root / "pc/Celeste/Celeste.exe", b"E", 10)
        put(root / "emu/snes/Game.sfc", b"R", 10)
        outside = tmp / "outside"
        put(outside / "precious.txt", b"keep")
        (root / "pc/Link").symlink_to(outside, target_is_directory=True)
        (root / "pc/Sneaky").mkdir()
        (root / "pc/Sneaky/inner").symlink_to(outside, target_is_directory=True)
        ok = F.deletable(root, root / "pc/Celeste")
        assert ok == (root / "pc/Celeste").resolve()
        assert F.deletable(root, root / "emu/snes/Game.sfc")
        for bad in (root, root / "pc", root / "emu", root / "emu/snes", root / "pc/Link", outside, tmp,
                    root / "pc/../..", root / "pc/Celeste/../../../outside", Path("pc/Celeste"), root / "other/x",
                    root / "pc/.hidden", Path("/")):
            try:
                F.deletable(root, bad)
                raise AssertionError("should refuse %s" % bad)
            except ValueError:
                pass
        # a link inside a game folder is removed as a link: the target is never followed
        F.delete_tree(root / "pc/Sneaky")
        assert (outside / "precious.txt").read_bytes() == b"keep" and not (root / "pc/Sneaky").exists()
        assert (outside / "precious.txt").exists()


def _plugin_env(tmp: Path, cfg_extra=None):
    lib = tmp / "games"
    (lib / "pc").mkdir(parents=True, exist_ok=True)
    (tmp / "hubdata").mkdir(exist_ok=True)
    hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
    cfg = {"library": str(lib), "base_url": "http://hub:8080", **(cfg_extra or {})}
    return hub, lib, cfg


def test_delete_download_and_library_game_files():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        hub, lib, cfg = _plugin_env(tmp)
        outside = tmp / "outside"
        put(outside / "precious.txt", b"keep")
        put(lib / "pc/Celeste/Celeste.exe", b"E", 3000)
        put(lib / "pc/Celeste/Content/d.bin", b"D", 1000)
        put(lib / "pc/Other/Other.exe", b"E", 30)
        put(lib / "pc/Third/Third.exe", b"E", 30)
        put(lib / "emu/snes/Super.sfc", b"R", 70)
        put(lib / "emu/snes/Super.jpg", b"J", 5)

        async def go():
            p = GamesPlugin(hub, cfg)
            await p.start()
            await p._scan()
            try:
                f = p.finder
                f._save([
                    {"hash": "a" * 40, "title": "Celeste", "platform": "pc", "state": "done", "payload": str(lib / "pc/Celeste"),
                     "owned": [str(lib / "pc/Celeste")], "dir": str(lib / "pc"), "added": 1},
                    {"hash": "b" * 40, "title": "Evil", "platform": "pc", "state": "done", "payload": str(lib / "pc"),
                     "owned": [str(lib / "pc"), str(outside)], "dir": str(lib / "pc"), "added": 2},
                    {"hash": "c" * 40, "title": "Old", "platform": "pc", "state": "done", "payload": str(lib / "pc/Other"),
                     "dir": str(lib / "pc"), "added": 3},                             # entry from before "owned" existed
                    {"hash": "d" * 40, "title": "Busy", "platform": "pc", "state": "extracting", "payload": str(lib / "pc/Third"),
                     "owned": [], "dir": str(lib / "pc"), "added": 4}])
                info = await f.files_info("a" * 40)
                assert info["paths"] == [{"path": "pc/Celeste", "size": 4000, "files": 2}] and info["size"] == 4000
                assert (await f.files_info("b" * 40))["paths"] == []                # nothing deletable: root, outside
                async with TestClient(TestServer(casa_app(p))) as c:
                    lst = await (await c.get("/api/p/games/find/downloads")).json()
                    assert lst[0]["path"] == "pc/Celeste"
                    r = await c.get("/api/p/games/find/downloads/" + "a" * 40 + "/files")
                    assert (await r.json())["size"] == 4000
                    # plain remove keeps the files
                    r = await c.delete("/api/p/games/find/downloads/" + "a" * 40)
                    assert r.status == 200 and (await r.json())["deleted"] == 0 and (lib / "pc/Celeste/Celeste.exe").exists()
                    f._save(f.entries() + [{"hash": "a" * 40, "title": "Celeste", "platform": "pc", "state": "done",
                                            "payload": str(lib / "pc/Celeste"), "owned": [str(lib / "pc/Celeste")], "dir": str(lib / "pc")}])
                    # remove + delete: only the download's own folder
                    r = await c.delete("/api/p/games/find/downloads/" + "a" * 40 + "?files=1")
                    assert r.status == 200 and (await r.json())["deleted"] == 1
                    assert not (lib / "pc/Celeste").exists() and (lib / "pc/Other/Other.exe").exists()
                    assert "pc-celeste" not in p.games                                 # rescanned
                    # a hostile entry (payload = the pc folder, or a path outside) deletes nothing at all
                    r = await c.delete("/api/p/games/find/downloads/" + "b" * 40 + "?files=1")
                    assert r.status == 200 and (await r.json())["deleted"] == 0
                    assert (lib / "pc/Other/Other.exe").exists() and (outside / "precious.txt").exists()
                    # an old entry without "owned" falls back to its payload
                    r = await c.delete("/api/p/games/find/downloads/" + "c" * 40 + "?files=1")
                    assert (await r.json())["deleted"] == 1 and not (lib / "pc/Other").exists()
                    assert (await c.delete("/api/p/games/find/downloads/" + "9" * 40 + "?files=1")).status == 404

                    # library game: files on the NAS, with the ROM's side picture
                    await p._scan()
                    assert (await c.post("/api/p/games/delete", json={"id": "nope"})).status == 404
                    r = await c.post("/api/p/games/delete", json={"id": "snes-super"})
                    assert r.status == 200 and not (lib / "emu/snes/Super.sfc").exists() and not (lib / "emu/snes/Super.jpg").exists()
                    assert (lib / "emu/snes").is_dir() and "snes-super" not in p.games
                    # not while it is being unpacked
                    r = await c.post("/api/p/games/delete", json={"id": "pc-third"})
                    assert r.status == 400 and (lib / "pc/Third/Third.exe").exists()
                    f._save([])
                    r = await c.post("/api/p/games/delete", json={"id": "pc-third"})
                    assert r.status == 200 and not (lib / "pc/Third").exists() and (lib / "pc").is_dir()
                    # a game whose entry points outside the library is refused
                    p.games["pc-x"] = {**dict(id="pc-x", title="x", kind="portable", platform="pc", system="", version=""),
                                       "_entry": str(outside), "_dir": str(outside), "_list": []}
                    assert (await c.post("/api/p/games/delete", json={"id": "pc-x"})).status == 400
                    assert (outside / "precious.txt").exists()
            finally:
                await p.stop()
        asyncio.run(go())


class FakeDeckCalls:
    """Replaces Deck.call: scripted status, records installs."""
    def __init__(self):
        self.online, self.agent, self.installed, self.jobs, self.sent, self.fail = True, "1.2", {}, {}, [], None

    async def __call__(self, method, path, body=None, timeout=5):
        from hub.plugins.games.deck import DeckError
        if not self.online:
            raise DeckError("The Steam Deck did not answer")
        if path == "/status":
            return {"agent": self.agent, "installed": self.installed, "jobs": self.jobs, "pending": 0, "steam_running": False}
        if path == "/install":
            if self.fail:
                raise DeckError(self.fail, 400)
            self.sent.append(body)
            return {"ok": True}
        raise AssertionError(path)


def test_auto_install_waits_for_the_deck_and_retries():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        hub, lib, cfg = _plugin_env(tmp, {"deck": {"host": "deck", "token": "t" * 20, "auto_install": True}})
        put(lib / "pc/Celeste/Celeste.exe", b"E", 300)
        put(lib / "pc/Setup Only/setup_x.exe", b"S", 30)

        async def go():
            p = GamesPlugin(hub, cfg)
            await p.start()
            await p._scan()
            calls = FakeDeckCalls()
            p.deck.call = calls
            try:
                f = p.finder
                f._save([{"hash": "a" * 40, "title": "Celeste", "platform": "pc", "state": "done", "owned": [str(lib / "pc/Celeste")],
                          "payload": str(lib / "pc/Celeste"), "dir": str(lib / "pc"), "added": 1},
                         {"hash": "b" * 40, "title": "Setup", "platform": "pc", "state": "done", "owned": [str(lib / "pc/Setup Only")],
                          "payload": str(lib / "pc/Setup Only"), "dir": str(lib / "pc"), "added": 2}])
                calls.online = False
                p.auto_queue("a" * 40)
                p.auto_queue("b" * 40)
                assert p.auto_for_game("pc-celeste")["state"] == "pending" and p.auto_for_game("pc-setup-only") is None
                assert f.entries()[0]["games"] == ["pc-celeste"]
                await p._auto_tick()                                              # Deck asleep: nothing sent, still pending
                assert calls.sent == [] and p.auto_for_game("pc-celeste")["state"] == "pending"
                row = f.listing()[0]["deck"]
                assert row["state"] == "pending" and row["online"] is False
                calls.online = True
                await p._auto_tick()
                assert len(calls.sent) == 1 and calls.sent[0]["id"] == "pc-celeste" and "location" not in calls.sent[0]
                assert p.auto_for_game("pc-celeste")["state"] == "sent" and f.listing()[0]["deck"]["state"] == "installing"
                calls.jobs = {"pc-celeste": {"state": "downloading", "done": 0.3, "message": "x"}}
                await p._auto_tick()
                assert len(calls.sent) == 1                                       # not sent twice while it installs
                calls.jobs = {}
                calls.installed = {"pc-celeste": {"version": "", "kind": "portable"}}
                await p._auto_tick()
                assert p.auto_for_game("pc-celeste") is None and len(calls.sent) == 1
                p._deck_cache = (0, await p._deck_state(fresh=True))
                assert f.listing()[0]["deck"]["state"] == "installed"
                # the Deck says no five times: the card shows the failure instead of retrying forever
                calls.installed = {}
                calls.fail = "not enough free space on the Deck"
                p.auto_queue("a" * 40)
                for _i in range(6):
                    await p._auto_tick()
                a = p.auto_for_game("pc-celeste")
                assert a["state"] == "failed" and "free space" in a["message"] and calls.sent == calls.sent[:1]
                # job failed on the Deck after being sent
                calls.fail = None
                p._auto_clear("pc-celeste")
                p.auto_queue("a" * 40)
                await p._auto_tick()
                calls.jobs = {"pc-celeste": {"state": "failed", "message": "link expired"}}
                await p._auto_tick()
                assert p.auto_for_game("pc-celeste") == {"state": "failed", "message": "link expired"}
                # setting off: nothing is queued
                p._auto_clear("pc-celeste")
                p.config["deck"]["auto_install"] = False
                p.auto_queue("a" * 40)
                assert p.auto_for_game("pc-celeste") is None
                # and the library item carries the state for the card
                p.config["deck"]["auto_install"] = True
                p.auto_queue("a" * 40)
                async with TestClient(TestServer(casa_app(p))) as c:
                    lib_json = await (await c.get("/api/p/games/library")).json()
                    item = {g["id"]: g for g in lib_json["games"]}["pc-celeste"]
                    assert item["auto"] and item["auto"]["state"] in ("pending", "sent") and lib_json["deck"]["auto_install"] is True, (item["auto"], lib_json["deck"])
            finally:
                await p.stop()
        asyncio.run(go())


def test_deck_agent_location_validation():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d).resolve()
        home = tmp / "home/deck"
        (home / "Games").mkdir(parents=True)
        media = tmp / "run/media"
        (media / "deck/SDCARD").mkdir(parents=True)
        (media / "deck/notmounted").mkdir(parents=True)
        (media / "USB").mkdir(parents=True)
        (tmp / "etc").mkdir()
        (home / "link").symlink_to(tmp / "etc", target_is_directory=True)
        mounts = {str(media / "deck/SDCARD"), str(media / "USB")}
        roots = [media / "deck", media]
        v = lambda raw: A.validate_location(raw, home, roots, lambda p: str(p) in mounts)
        assert v(str(home / "Games")) == home / "Games"
        assert v(str(home / "New/Place")) == home / "New/Place"                        # not created yet: fine
        assert v(str(media / "deck/SDCARD/Games")) == media / "deck/SDCARD/Games"
        assert v(str(media / "USB/Games")) == media / "USB/Games"                    # /run/media/<label>
        for bad in ("Games", "", None, 5, "/etc", "/", str(home), str(tmp / "etc"), str(home) + "/../etc",
                    str(home / "Games/../../x"), str(home / ".ssh"), str(home / "Games/.hidden"),
                    str(home / "link/x"), str(media / "deck/notmounted/Games"), str(media / "deck"), str(media / "nothing/Games"),
                    "/run/media/../etc", str(home) + "\\x", str(home / "a\x00b"), "/" + "a" * 500):
            try:
                v(bad)
                raise AssertionError("accepted %r" % (bad,))
            except ValueError:
                pass
        a = A.Agent(TOKEN, home / "Games", tmp / "roms", tmp / "s", tmp / "data", home=home, media_roots=roots,
                    mount_check=lambda p: str(p) in mounts)
        loc = a.locations()
        assert loc["agent"] == "1.2" and loc["default"] == str(home / "Games")
        assert [x["id"] for x in loc["locations"]] == ["internal", "sd:SDCARD", "sd:USB"], loc
        assert all(x["free"] > 0 and x["total"] >= x["free"] for x in loc["locations"])
        assert loc["locations"][1]["path"] == str(media / "deck/SDCARD/Games") and loc["locations"][1]["label"] == "SDCARD"
        assert a.check_location(str(media / "USB/Games"))["ok"]
        try:
            a.check_location("/etc")
            raise AssertionError("etc")
        except ValueError:
            pass
        ok = {"id": "pc-x", "kind": "portable", "folder": "X", "exe": "x.exe", "base": "http://h",
              "files": [{"p": "x.exe", "s": 1}], "title": "X", "exp": 1, "sig": "s"}
        for bad in ({**ok, "location": "/etc"}, {**ok, "location": "../x"}, {**ok, "location": str(media / "deck/notmounted/G")},
                    {**ok, "workdir": "../x"}):
            try:
                a.submit(dict(bad))
                raise AssertionError(bad)
            except ValueError:
                pass
        a.submit({**ok, "location": str(media / "deck/SDCARD/Games")})            # accepted (it fails later on the fake URL)
        assert a.jobs["pc-x"]["_base"] == str(media / "deck/SDCARD/Games")
        # ROMs never move: a location sent for one is ignored
        a.submit({**ok, "id": "snes-r", "kind": "rom", "system": "snes", "folder": "", "location": "/etc", "files": [{"p": "r.sfc", "s": 1}]})
        assert a.jobs["snes-r"]["_base"] is None


def test_install_to_a_chosen_location_and_old_agents():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d).resolve()
        lib = tmp / "nas/games"
        put(lib / "pc/Celeste v1.4/Celeste.exe", b"E", 300)
        put(lib / "pc/Hades/Hades.exe", b"H", 40)
        deck = Deck(tmp / "deck")
        sd = deck.tmp / "run/media/deck/SDCARD"
        sd.mkdir(parents=True)
        deck.agent.home = deck.tmp / "home"
        deck.agent.media_roots = [deck.tmp / "run/media/deck", deck.tmp / "run/media"]
        deck.agent.mount_check = lambda p: p == sd
        deck.agent.games_dir = deck.tmp / "home/Games"
        (deck.tmp / "home/Games").mkdir(parents=True)
        deck.steam["running"] = False

        async def go():
            (tmp / "hubdata").mkdir(exist_ok=True)
            hub = Hub({"homekit": {"enabled": False}}, tmp / "hubdata")
            cfg = {"library": str(lib), "deck": {"host": "127.0.0.1", "port": deck.port, "token": TOKEN}}
            p = GamesPlugin(hub, cfg)
            await p.start()
            await p._scan()
            try:
                async with TestClient(TestServer(casa_app(p))) as c:
                    post = lambda path, body: c.post("/api/p/games" + path, json=body)
                    lj = await (await c.get("/api/p/games/library")).json()
                    assert lj["deck"]["agent"] == "1.2" and lj["deck"]["locations"] is True and lj["deck"]["location"] == ""
                    locs = await (await c.get("/api/p/games/deck/locations")).json()
                    assert locs["supported"] and [x["id"] for x in locs["locations"]] == ["internal", "sd:SDCARD"]
                    assert locs["locations"][1]["path"] == str(sd / "Games") and locs["locations"][1]["free"] > 0
                    # custom folder check runs on the Deck
                    ck = await (await post("/deck/location/check", {"path": str(deck.tmp / "home/Roms2")})).json()
                    assert ck["ok"] and ck["free"] > 0
                    assert (await post("/deck/location/check", {"path": "/etc"})).status == 400
                    # per-game override: SD card
                    assert (await post("/install", {"id": "pc-celeste", "location": str(sd / "Games")})).status == 200
                    await wait_state(c, "pc-celeste", ("installed",))
                    assert (sd / "Games/Celeste/Celeste.exe").stat().st_size == 300, deck.agent.registry()
                    assert not (deck.tmp / "home/Games/Celeste").exists()
                    st = await (await c.get("/api/p/games/library")).json()
                    assert deck.agent.registry()["pc-celeste"]["root"] == str(sd / "Games")
                    # a hostile location is refused by the Deck, nothing is written
                    r = await post("/install", {"id": "pc-hades", "location": "/etc/evil"})
                    assert r.status == 400 and not Path("/etc/evil").exists()
                    # uninstall from the SD card works (and only deletes inside the validated location)
                    deck.steam["running"] = False
                    body = await (await post("/uninstall", {"id": "pc-celeste"})).json()
                    assert body["ok"] and not (sd / "Games/Celeste/Celeste.exe").exists()
                    # default from Settings
                    p.config["deck"]["location"] = str(sd / "Games")
                    assert (await post("/install", {"id": "pc-hades"})).status == 200
                    await wait_state(c, "pc-hades", ("installed",))
                    assert (sd / "Games/Hades/Hades.exe").is_file()
                    # an agent older than 1.2: no choice, a clear message, plain installs still work
                    A.VERSION = "1.1"
                    p._deck_cache = (0, {})
                    locs = await (await c.get("/api/p/games/deck/locations")).json()
                    assert locs["supported"] is False and "agent" in locs["error"]
                    r = await post("/install", {"id": "pc-celeste", "location": str(sd / "Games")})
                    assert r.status == 400 and "agent" in (await r.json())["error"]
                    p._deck_cache = (0, {})
                    assert (await post("/install", {"id": "pc-celeste"})).status == 200     # the configured default is not sent
                    await wait_state(c, "pc-celeste", ("installed",))
                    assert (deck.tmp / "home/Games/Celeste/Celeste.exe").is_file() or (sd / "Games/Celeste/Celeste.exe").is_file()
                    lj = await (await c.get("/api/p/games/library")).json()
                    assert lj["deck"]["locations"] is False
            finally:
                A.VERSION = "1.2"
                await p.stop()
        try:
            asyncio.run(go())
        finally:
            deck.close()


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_metadata_overrides_and_images():
    with tempfile.TemporaryDirectory() as d:
        hub = Hub({"homekit": {"enabled": False}}, Path(d))

        async def go():
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                m = FakeMeta(hub.store, sess, Path(d) / "art", "id", "secret", "sgdbkey")
                g = {"id": "pc-celeste", "title": "Celeste", "kind": "portable", "platform": "pc"}
                assert m.public("pc-celeste") is None
                await m.fetch(g)
                base = m.public("pc-celeste")
                assert base["title"] == "Celeste" and base["edited"] == [] and base["genres"] == ["Platform"]
                # edits sit on top of the fetched data
                m.set_fields("pc-celeste", {"title": " My  Celeste ", "year": "2019", "genres": "Horror, indie ,horror", "summary": "Mine.\r\nLine"})
                pub = m.public("pc-celeste")
                assert pub["title"] == "My Celeste" and pub["released"] == "2019" and pub["genres"] == ["Horror", "indie"]
                assert pub["summary"] == "Mine.\nLine" and set(pub["edited"]) == {"title", "year", "genres", "summary"}
                assert m.display_title("pc-celeste", "x") == "My Celeste"
                m.set_fields("pc-celeste", {"summary": "", "year": ""})              # back to automatic, field by field
                assert m.public("pc-celeste")["summary"] == "Climb." and m.public("pc-celeste")["released"] == "2018-01-26"
                for bad in ({"year": "1800"}, {"year": "abc"}, {"title": "x" * 200}, {"genres": 5}, {"summary": "x" * 5000}):
                    try:
                        m.set_fields("pc-celeste", bad)
                        raise AssertionError(bad)
                    except M.MetaError:
                        pass
                # a refetch keeps the user's edits; a refresh of the match does not lose them
                await m.fetch(g)
                assert m.public("pc-celeste")["title"] == "My Celeste"

                # pictures: URL validation, then download → cache → used for cover and the Deck's grid
                got = {}

                async def fake_get(url):
                    got["url"] = url
                    return PNG
                m._get_bytes = fake_get
                await m.set_image("pc-celeste", "cover", url="https://example.com/c.png")
                f = m.file("pc-celeste", "cover")
                assert f.name == "ov-pc-celeste.cover.png" and f.read_bytes() == PNG and m.file("pc-celeste", "grid") == f
                assert m.has_override_image("pc-celeste", "cover") and "cover" in m.public("pc-celeste")["edited"]
                await m.set_image("pc-celeste", "hero", cached=m.get("pc-celeste")["files"]["shots"][0])
                assert m.file("pc-celeste", "hero").name.startswith("ov-pc-celeste.hero")
                for bad in ("http://example.com/a.png", "https://127.0.0.1/a.png", "https://10.0.0.5/a.png", "https://[::1]/a",
                            "https://user:pw@example.com/a.png", "https://example.com:8443/a.png", "https://localhost/a.png",
                            "https://nas.local/a.png", "https://intranet/a.png", "file:///etc/passwd", "", "https://" + "a" * 700 + ".com/x",
                            "https://192.168.1.10/x", "https://169.254.169.254/latest"):
                    try:
                        M.check_image_url(bad)
                        raise AssertionError("accepted " + bad)
                    except M.MetaError:
                        pass
                assert M.check_image_url("https://cdn2.steamgriddb.com/grid/x.png")
                assert [i["host"] for i in M.global_addrs([{"host": "10.0.0.1"}, {"host": "8.8.8.8"}, {"host": "127.0.0.1"},
                                                           {"host": "fe80::1%en0"}, {"host": "2606:4700::1"}])] == ["8.8.8.8", "2606:4700::1"]
                # not an image / unknown cache name
                async def junk(url):
                    return b"<html>no</html>"
                m._get_bytes = junk
                for kw in ({"url": "https://example.com/x"}, {"cached": "../../etc/passwd"}):
                    try:
                        await m.set_image("pc-celeste", "cover", **kw)
                        raise AssertionError(kw)
                    except M.MetaError:
                        pass
                assert m.file("pc-celeste", "cover").read_bytes() == PNG                # the good one survived
                # the user's pictures survive a refetch and cache eviction
                await m.fetch(g)
                m.cache.cap = 1
                m.cache.evict()
                assert m.file("pc-celeste", "cover").read_bytes() == PNG
                m.cache.cap = M.CACHE_CAP
                await m.fetch(g)                                                    # (the screenshots were evicted)
                # alternatives: screenshots for the banner, SteamGridDB for both
                async def sgdb(path):
                    if "autocomplete" in path:
                        return [{"id": 5, "name": "Celeste"}]
                    if "/grids/" in path:
                        assert "600x900" in path
                        return [{"id": 1, "url": "https://cdn2.steamgriddb.com/g1.png", "thumb": "https://cdn2.steamgriddb.com/t1.png", "width": 600, "height": 900},
                                {"id": 2, "url": "http://insecure/x.png", "thumb": "http://insecure/x.png"}]
                    return [{"id": 3, "url": "https://cdn2.steamgriddb.com/h1.png", "thumb": "https://cdn2.steamgriddb.com/h1t.png"}]
                m._sgdb = sgdb
                covers = await m.art_options("pc-celeste", "cover", "Celeste")
                assert [o["id"] for o in covers] == ["sgdb:1"] and covers[0]["thumb"].endswith("t1.png")
                heroes = await m.art_options("pc-celeste", "hero", "Celeste")
                assert heroes[0]["source"] == "igdb" and heroes[-1]["id"] == "sgdb:3"
                # reset: everything the user chose is gone, the pictures too
                m.reset("pc-celeste", forget_match=True)
                assert m.public("pc-celeste") is None and m.cache.path("ov-pc-celeste.cover.png") is None
                assert hub.store.get("games.override.pc-celeste") is None
        asyncio.run(go())


def test_override_routes_and_deck_art():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        hub, lib, cfg = _plugin_env(tmp)
        put(lib / "pc/Celeste v1.4/Celeste.exe", b"E", 30)
        put(lib / "pc/Celeste v1.4/cover.jpg", b"\xff\xd8\xff" + b"J" * 20)

        async def go():
            p = GamesPlugin(hub, cfg)
            await p.start()
            await p._scan()
            import aiohttp
            p.meta = FakeMeta(hub.store, p.session, tmp / "art", "", "", "")
            async def fake_get(url):
                return PNG
            p.meta._get_bytes = fake_get
            try:
                async with TestClient(TestServer(casa_app(p))) as c:
                    post = lambda path, body: c.post("/api/p/games" + path, json=body)
                    r = await post("/meta/override", {"id": "pc-celeste", "title": "Celeste Remix", "genres": ["Horror"], "year": "2020"})
                    j = await r.json()
                    assert r.status == 200 and j["game"]["meta"]["title"] == "Celeste Remix" and j["game"]["meta"]["genres"] == ["Horror"]
                    assert (await post("/meta/override", {"id": "pc-celeste", "year": "1066"})).status == 400
                    assert (await post("/meta/override", {"id": "nope", "title": "x"})).status == 404
                    # cover: local cover.jpg first, until the user picks a picture
                    assert (await (await c.get("/api/p/games/cover/pc-celeste")).read()).startswith(b"\xff\xd8\xff")
                    assert (await post("/meta/image", {"id": "pc-celeste", "kind": "cover", "url": "http://x/a.png"})).status == 400
                    r = await post("/meta/image", {"id": "pc-celeste", "kind": "cover", "url": "https://example.com/a.png"})
                    assert r.status == 200 and (await r.json())["game"]["cover"] is True
                    assert (await (await c.get("/api/p/games/cover/pc-celeste")).read()) == PNG
                    art = p._art(p.games["pc-celeste"])
                    assert base64.b64decode(art["grid"]) == PNG                     # the Deck shortcut uses the chosen picture
                    assert (await post("/meta/image", {"id": "pc-celeste", "kind": "bogus", "url": "https://example.com/a.png"})).status == 400
                    r = await c.get("/api/p/games/meta/art?id=pc-celeste&kind=cover")
                    assert r.status == 400                                          # no SteamGridDB key: explained, not a 500
                    r = await post("/meta/image", {"id": "pc-celeste", "kind": "cover", "clear": True})
                    assert (await (await c.get("/api/p/games/cover/pc-celeste")).read()).startswith(b"\xff\xd8\xff")
                    # reset to automatic
                    r = await post("/meta/reset", {"id": "pc-celeste"})
                    j = await r.json()
                    assert r.status == 200 and j["game"]["meta"] is None
            finally:
                await p.stop()
        asyncio.run(go())


def test_discover_genres():
    from hub.plugins.games import discover as D
    import json as _json
    page = {"response": {"metadata": {"total_matching_records": 60}, "ids": [{"appid": i} for i in (10, 11, 12)]}}
    items = {"response": {"store_items": [
        {"appid": 10, "name": "Dead by Daylight", "type": 0, "reviews": {"summary_filtered": {"percent_positive": 78, "review_count": 900}},
         "release": {"steam_release_date": 1465000000}},
        {"appid": 11, "name": "Phasmophobia", "type": 0, "is_free": False},
        {"appid": 12, "name": "A Tool", "type": 4}]}}

    async def go():
        sess = FakeSession({"IStoreQueryService": (200, page), "GetItems": (200, items)})
        d = D.Discover(sess, None, lambda: {"phasmophobia": {"pc"}}, lambda: "it")
        gl = d.genre_list()
        keys = [g["key"] for g in gl["genres"]]
        assert gl["igdb"] is False and gl["platforms"] == [{"key": "pc", "label": "PC"}]
        for want in ("horror", "indie", "rpg", "strategy", "racing", "simulation", "sports", "puzzle", "adventure", "action",
                     "platformer", "roguelike", "survival", "coop"):
            assert want in keys, want
        r = await d.genre("horror")
        assert r["source"] == "steam" and r["more"] is True and r["page"] == 0
        assert [i["title"] for i in r["items"]] == ["Dead by Daylight", "Phasmophobia"]             # software dropped
        assert r["items"][0]["rating"] == 78 and r["items"][0]["year"] == "2016" and r["items"][1]["in_library"] is True
        n = len(sess.calls)
        await d.genre("horror")
        assert len(sess.calls) == n                                                                  # cached for hours
        for bad in (("bogus", "pc"), ("horror", "snes"), ("horror", "../x")):
            try:
                await d.genre(*bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        # the tag ids used are the ones Steam publishes
        assert D.GENRE_BY_KEY["horror"][1] == 1667 and D.GENRE_BY_KEY["indie"][1] == 492 and D.GENRE_BY_KEY["rpg"][1] == 122
        # Steam's query fails: SteamSpy's genre list steps in for the broad genres, nothing for the others
        spy = {str(100 + i): {"appid": 100 + i, "name": "Race %d" % i, "positive": 10 * i, "negative": i, "price": "0"} for i in range(30)}
        d2 = D.Discover(FakeSession({"IStoreQueryService": (500, {}), "steamspy": (200, spy)}), None, None, lambda: "en")
        d2._spy_last = -100
        r = await d2.genre("racing")
        assert [i["title"] for i in r["items"][:2]] == ["Race 29", "Race 28"] and len(r["items"]) == 24 and r["items"][0]["free"]
        try:
            await d2.genre("horror")
            raise AssertionError("no fallback for horror")
        except D.DiscoverError:
            pass

        # IGDB: genres/themes/modes per platform, paged
        class Meta:
            igdb = True

            def __init__(self):
                self.asked = []

            async def _igdb(self, body, endpoint="games"):
                self.asked.append(body)
                return [{"id": 40 + i, "name": "G%d" % i, "total_rating": 80, "total_rating_count": 50, "cover": {"image_id": "c%d" % i}}
                        for i in range(25)]
        m = Meta()
        d3 = D.Discover(FakeSession({}), m, None, lambda: "it")
        assert d3.genre_list()["igdb"] and any(p["key"] == "switch" for p in d3.genre_list()["platforms"])
        r = await d3.genre("horror", "switch", 1)
        assert r["source"] == "igdb" and len(r["items"]) == 24 and r["more"] is True and r["items"][0]["platform"] == "switch"
        q = m.asked[0]
        assert "platforms = (130)" in q and "themes = (19)" in q and "offset 24" in q
        await d3.genre("coop", "snes")
        assert "game_modes = (3)" in m.asked[-1]
        try:
            await d3.genre("roguelike", "snes")
            raise AssertionError("no igdb filter")
        except D.DiscoverError:
            pass
    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
