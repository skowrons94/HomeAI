"""Genre parsing (TMDB + Cinemeta fixtures) and the media plugin's attention hints. No network.
Run: python tests/test_media_genres.py"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media import MediaPlugin  # noqa: E402
from hub.plugins.media.metadata import (  # noqa: E402
    Metadata, parse_cinemeta_genres, parse_tmdb_genres)

TMDB = {"genres": [{"id": 35, "name": "Commedia"}, {"id": 28, "name": "Azione"}, {"id": None, "name": "x"}]}
MANIFEST = {"catalogs": [
    {"type": "movie", "id": "top", "extra": [{"name": "genre", "options": ["Action", "Comedy"]}, {"name": "skip"}]},
    {"type": "series", "id": "top", "extra": [{"name": "genre", "options": ["Drama"]}]},
    {"type": "movie", "id": "year", "extra": [{"name": "genre", "options": ["2024"]}]}]}


def test_parsing():
    g = parse_tmdb_genres(TMDB)
    assert g == [{"id": "28", "name": "Azione"}, {"id": "35", "name": "Commedia"}], g
    assert parse_cinemeta_genres(MANIFEST, "movie") == [{"id": "Action", "name": "Action"},
                                                         {"id": "Comedy", "name": "Comedy"}]
    assert parse_cinemeta_genres(MANIFEST, "series") == [{"id": "Drama", "name": "Drama"}]
    assert parse_cinemeta_genres({}, "movie") == []


def test_metadata_discover():
    calls = []
    md = Metadata.__new__(Metadata)
    md.key, md.language, md.region = "k", "it-IT", "IT"

    async def tmdb(path, ttl=0, persist=False, **params):
        calls.append((path, params))
        if path.startswith("/genre"):
            return TMDB
        return {"results": [{"id": 1, "title": "Film", "release_date": "2020-01-01"}], "total_pages": 3}
    md._tmdb = tmdb
    r = asyncio.run(md.discover("movie", "35", 2))
    assert r["items"][0]["id"] == "tmdb:1" and r["has_more"] and r["page"] == 2
    assert calls[0][0] == "/discover/movie" and calls[0][1]["with_genres"] == "35"
    assert "Azione" in [g["name"] for g in asyncio.run(md.genres("series"))] and calls[-1][0] == "/genre/tv/list"

    md.key = ""
    urls = []

    async def get(url, params=None, ttl=0, persist=False):
        urls.append(url)
        return MANIFEST if url.endswith("manifest.json") else {"metas": [{"imdb_id": "tt1", "name": "A"}] * 50}
    md._get = get
    r = asyncio.run(md.discover("movie", "Action", 3))
    assert urls[-1].endswith("/catalog/movie/top/genre=Action&skip=100.json"), urls
    assert r["items"][0]["id"] == "tt1" and r["has_more"]
    assert [x["id"] for x in asyncio.run(md.genres("movie"))] == ["Action", "Comedy"]


def plugin(key, movies, series, tx_ok, with_storage=False):
    p = MediaPlugin.__new__(MediaPlugin)
    p.metadata = SimpleNamespace(key=key)
    p.downloads = SimpleNamespace(ok=tx_ok, error="Transmission not reachable")
    p.library = SimpleNamespace(movies_dir=movies, series_dir=series)
    p.hub = SimpleNamespace(plugins={"storage": 1} if with_storage else {})
    return p


def test_attention():
    with tempfile.TemporaryDirectory() as d:
        ids = lambda p: {i["id"]: i for i in p.attention()}
        assert ids(plugin("k", d, d, True)) == {}
        a = ids(plugin("", d, d, None))
        assert list(a) == ["tmdb"] and a["tmdb"]["level"] == "info"
        a = ids(plugin("k", d, d, False))
        assert a["transmission"]["level"] == "warning" and "reachable" in a["transmission"]["detail"]
        a = ids(plugin("k", d + "/nope", d, True, with_storage=True))
        assert a["library"]["action"]["plugin"] == "storage"
        a = ids(plugin("k", d + "/nope", d, True))
        assert a["library"]["action"]["plugin"] == "media"
        a = ids(plugin("k", "", d, True))
        assert "not set" in a["library"]["detail"] and a["library"]["action"]["plugin"] == "media"


if __name__ == "__main__":
    test_parsing(); test_metadata_discover(); test_attention()
    print("media genres/attention OK")
