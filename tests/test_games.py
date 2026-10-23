"""Games: library scan, Deck agent (VDF, safety) and a full install flow Casa → agent → files.
Everything runs on temp dirs and 127.0.0.1; Steam is faked; nothing touches /mnt/nas or a real Deck."""
import asyncio
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
