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


if __name__ == "__main__":
    test_series_pack_move()
    test_movie_copy_single_file_from_pack()
    print("2 tests passed")
