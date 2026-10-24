"""One Download button for every library: identify a release and hand it to media / games / music.
Run: python tests/test_router.py"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Hub  # noqa: E402
from hub.plugins.media import PLUGIN as MediaPlugin  # noqa: E402
from hub.plugins.torrents import TorrentsPlugin  # noqa: E402
from hub.plugins.torrents.identify import identify  # noqa: E402


class Req:
    def __init__(self, body=None, query=None):
        self._body, self.query = body or {}, query or {}

    async def json(self):
        return self._body


def test_identify():
    cases = {
        "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX": ("movie", {"query": "dune part two", "year": 2024}),
        "The.Bear.S04E02.ITA.ENG.1080p.WEB-DL-MeM": ("series", {"season": 4, "episode": 2}),
        "Outlander.S04.ITA.ENG.1080p.AMZN.WEBRip": ("series", {"season": 4, "episode": None}),
        "[Anime Time] Naruto Shippuden Complete (001-500 + Movies) [1080p]": ("series", {"query": "naruto shippuden"}),
        "Pink Floyd - The Wall (1979) [FLAC 24bit]": ("music", {"artist": "Pink Floyd", "album": "The Wall", "year": 1979}),
        "Radiohead Discography 1993-2016 [MP3 320]": ("music", {"artist": "Radiohead", "discography": True}),
        "Hollow Knight Silksong v1.0.2 [FitGirl Repack]": ("game", {"title": "Hollow Knight Silksong", "platform": "pc"}),
        "Mario Kart 8 Deluxe NSW NSP": ("game", {"platform": "switch"}),
    }
    for name, (kind, want) in cases.items():
        got = identify(name)
        assert got["kind"] == kind, (name, got)
        for k, v in want.items():
            assert got["guess"].get(k) == v, (name, k, got)


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.metadata = SimpleNamespace(search=self._search)
        self.downloads = SimpleNamespace(rows=lambda active_only=False: [
            {"type": "movie", "title": "Dune", "name": "Dune.2024", "progress": 0.5, "state": "downloading"}])

    async def _search(self, q):
        return [{"id": "tmdb:1", "type": "movie", "title": "Dune: Part Two", "year": 2024, "poster": None},
                {"id": "tmdb:2", "type": "series", "title": "Dune: Prophecy", "year": 2024, "poster": None}]

    async def accept_download(self, result, guess):
        self.calls.append((result, guess))
        return {"hash": "abc", "name": result["title"]}


class FakeGames:
    def __init__(self):
        self.calls = []
        self.finder = SimpleNamespace(add=self._add, listing=lambda: [
            {"title": "Celeste", "progress": 0.2, "state": "downloading"}, {"title": "Old", "state": "done"}])

    async def _add(self, title, platform, magnet, url):
        self.calls.append((title, platform, magnet, url))
        return "def"


def test_route_dispatch():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            hub = Hub({"homekit": {"enabled": False}}, Path(d))
            p = TorrentsPlugin(hub, {"providers": ["internetarchive"]})
            await p.start()
            try:
                media, games = FakeMedia(), FakeGames()
                hub.plugins.update({"media": media, "games": games})
                res = {"title": "Dune.Part.Two.2024.1080p.WEB-DL", "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
                       "size": 5_000_000_000}
                ident = json.loads((await p.r_identify(Req({"title": res["title"]}))).text)
                assert ident["kind"] == "movie" and ident["candidates"][0]["id"] == "tmdb:1", ident
                assert ident["available"] == {"movie": True, "series": True, "game": True, "music": False}
                out = json.loads((await p.r_download(Req({"result": res, "kind": "movie",
                                                           "guess": {"id": "tmdb:1"}}))).text)
                assert out == {"ok": True, "kind": "movie", "hash": "abc"}
                assert media.calls[0][1] == {"id": "tmdb:1", "type": "movie"}

                g = {"title": "Celeste v1.4 GOG", "url": "https://example.org/c.torrent"}
                out = json.loads((await p.r_download(Req({"result": g, "kind": "game",
                                                           "guess": {"title": "Celeste", "platform": "pc"}}))).text)
                assert out["hash"] == "def" and games.calls == [("Celeste", "pc", "", "https://example.org/c.torrent")]

                for bad in ({"result": res, "kind": "music"},                       # music plugin not running
                            {"result": res, "kind": "nope"},
                            {"result": {"title": "x", "magnet": "javascript:alert(1)"}, "kind": "game"}):
                    try:
                        await p.r_download(Req(bad))
                        raise AssertionError(bad)
                    except ValueError:
                        pass

                dls = json.loads((await p.r_downloads(Req())).text)
                assert [(x["kind"], x["title"]) for x in dls] == [("movie", "Dune"), ("game", "Celeste")], dls
            finally:
                await p.stop()

    asyncio.run(go())


def test_media_accept_download():
    async def go():
        with tempfile.TemporaryDirectory() as d:
            hub = Hub({"homekit": {"enabled": False}}, Path(d))
            m = MediaPlugin(hub, {})
            added = []

            async def details(type_, id_):
                return {"type": type_, "id": id_, "title": "Outlander", "year": 2014}

            async def add(item, release, season, episode):
                added.append((item, release, season, episode))
                return {"hash": "h", "name": release["name"]}
            m.metadata = SimpleNamespace(details=details)
            m.downloads = SimpleNamespace(add=add)
            r = {"title": "Outlander.S04.ITA.ENG.1080p.AMZN.WEBRip.x265", "magnet": "magnet:?xt=urn:btih:" + "b" * 40}
            out = await m.accept_download(r, {"type": "series", "id": "tmdb:56570"})
            item, rel, season, episode = added[0]
            assert out["hash"] == "h" and season == 4 and episode is None and rel["pack"] is True
            assert rel["quality"] == "1080p webrip" and rel["magnet"].startswith("magnet:"), rel
            try:
                await m.accept_download(r, {"type": "series"})
                raise AssertionError("no id")
            except ValueError:
                pass

    asyncio.run(go())


if __name__ == "__main__":
    test_identify()
    test_route_dispatch()
    test_media_accept_download()
    print("3 tests passed")
