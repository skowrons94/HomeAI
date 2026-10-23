"""Post-processing (import into the library) with a fake Transmission torrent.
Run: python tests/test_media_import.py"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media.downloads import Downloads  # noqa: E402
from hub.store import Store  # noqa: E402


class FakeLibrary:
    def __init__(self, shows):
        self.shows = shows

    def find_show(self, titles):
        return self.shows.get(titles[0])


class FakeMeta:
    async def episodes(self, type_, id_, season):
        return [{"episode": 1, "title": "Groundhogs"}, {"episode": 2, "title": "Soubise"}]


def make(tmp, mode, shows=None):
    hub = SimpleNamespace(store=Store(Path(tmp) / "t.db"), publish=lambda *a: None)
    plugin = SimpleNamespace(hub=hub, transmission=None, library=FakeLibrary(shows or {}),
                             metadata=FakeMeta())
    cfg = {"download_dir": f"{tmp}/dl", "movies_dir": f"{tmp}/movies",
           "series_dir": f"{tmp}/series", "post_process": mode}
    d = Downloads(plugin, cfg)

    async def no_remove(h, delete_data):
        pass
    d._remove_torrent = no_remove
    return d


def touch(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def test_series_pack_move():
    with tempfile.TemporaryDirectory() as tmp:
        pack = "The.Bear.S04.ITA.ENG.1080p.WEB-DL-MeM"
        files = [f"{pack}/The.Bear.S04E01.ITA.ENG.1080p.WEB-DL-MeM.mkv",
                 f"{pack}/The.Bear.S04E02.ITA.ENG.1080p.WEB-DL-MeM.mkv",
                 f"{pack}/The.Bear.S04E02.ITA.ENG.1080p.WEB-DL-MeM.ita.srt",
                 f"{pack}/Sample/sample.mkv", f"{pack}/info.nfo"]
        for f in files:
            touch(f"{tmp}/dl/{f}", 30_000_000 if f.endswith(".mkv") and "ample" not in f else 100)
        existing = f"{tmp}/series/The Bear"
        os.makedirs(existing)
        d = make(tmp, "move", {"The Bear": {"path": existing}})
        row = {"hash": "x", "type": "series", "title": "The Bear", "year": 2022, "season": 4,
               "episode": None, "file_idx": None, "quality": "1080p",
               "data": {"titles": ["The Bear"], "id": "tt14452776"}}
        t = {"downloadDir": f"{tmp}/dl",
             "files": [{"name": f, "length": os.path.getsize(f"{tmp}/dl/{f}")} for f in files]}
        out = asyncio.run(d._do_import(row, t))
        got = sorted(os.listdir(f"{existing}/Season 4"))
        assert got == ["The Bear - S04E01 - Groundhogs WEB-DL 1080p.mkv",
                       "The Bear - S04E02 - Soubise WEB-DL 1080p.it.srt",
                       "The Bear - S04E02 - Soubise WEB-DL 1080p.mkv"], got
        assert len(out) == 2
        assert not os.path.exists(f"{tmp}/dl/{pack}")      # cleaned up inside download_dir
        assert os.path.isdir(f"{tmp}/dl")                  # but never the download dir itself


def test_movie_copy_single_file_from_pack():
    with tempfile.TemporaryDirectory() as tmp:
        files = ["Conclave (2024) 1080p ITA ENG.mkv", "Conclave (2024) 1080p ITA ENG.it.srt"]
        for f in files:
            touch(f"{tmp}/dl/{f}", 40_000_000 if f.endswith("mkv") else 50)
        d = make(tmp, "copy")
        row = {"hash": "y", "type": "movie", "title": "Conclave", "year": 2024, "season": None,
               "episode": None, "file_idx": None, "quality": "1080p bluray", "data": {}}
        t = {"downloadDir": f"{tmp}/dl",
             "files": [{"name": f, "length": os.path.getsize(f"{tmp}/dl/{f}")} for f in files]}
        asyncio.run(d._do_import(row, t))
        got = sorted(os.listdir(f"{tmp}/movies/Conclave (2024)"))
        assert got == ["Conclave (2024) 1080p bluray.it.srt", "Conclave (2024) 1080p bluray.mkv"], got
        assert os.path.exists(f"{tmp}/dl/{files[0]}")      # copy keeps the seed


class AnimeMeta:
    """TMDB-style: seasons of 2 and 3 episodes, numbered absolutely (season 2 = 3-5)."""
    async def details(self, type_, id_):
        return {"seasons": [{"season": 1}, {"season": 2}]}

    async def episodes(self, type_, id_, season):
        return [{"episode": n, "title": f"Ep {n}"} for n in ([1, 2] if season == 1 else [3, 4, 5])]


def test_anime_absolute_numbers():
    from hub.plugins.media.ranking import absolute_episode
    assert absolute_episode("[Anime Time] Naruto Shippuden - 033 - The Rasengan.mkv") == 33
    assert absolute_episode("Naruto Shippuden Episode 414 On the Brink of Death.mkv") == 414
    assert absolute_episode("[Anime Time] The Last - Naruto The Movie 07 (2014).mkv") is None
    assert absolute_episode("Show - 2014 - Special.mkv") is None
    with tempfile.TemporaryDirectory() as tmp:
        pack = "[Anime Time] Show Complete (001-005 + Movies) [1080p]"
        files = [f"{pack}/Season 01/[Anime Time] Show - 002 - Two.mkv",
                 f"{pack}/Season 02/[Anime Time] Show - 004 - Four.mkv",
                 f"{pack}/Movies/[Anime Time] Show The Movie (2010).mkv"]
        for f in files:
            touch(f"{tmp}/dl/{f}", 30_000_000)
        d = make(tmp, "copy")
        d.plugin.metadata = AnimeMeta()
        row = {"hash": "z", "type": "series", "title": "Show", "year": 2007, "season": None,
               "episode": None, "file_idx": None, "quality": "1080p",
               "data": {"titles": ["Show"], "id": "tmdb:1"}}
        t = {"downloadDir": f"{tmp}/dl",
             "files": [{"name": f, "length": 30_000_000} for f in files]}
        out = asyncio.run(d._do_import(row, t))
        assert sorted(os.path.relpath(p, f"{tmp}/series") for p in out) == [
            "Show/Season 1/Show - S01E02 - Ep 2.mkv",
            "Show/Season 2/Show - S02E04 - Ep 4.mkv"], out


if __name__ == "__main__":
    test_series_pack_move()
    test_movie_copy_single_file_from_pack()
    test_anime_absolute_numbers()
    print("3 tests passed")
