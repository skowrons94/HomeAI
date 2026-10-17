"""Storage plugin: validation, share parsing, library suggestions, schema, Transmission session-set.
Run: python tests/test_storage.py"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from hub.plugins import storage as st  # noqa: E402
from hub.plugins.media import SCHEMA as MEDIA_SCHEMA  # noqa: E402
from hub.plugins.media.transmission import Transmission  # noqa: E402
from hub.settings import coerce  # noqa: E402

TYPES = {"text", "password", "number", "bool", "select", "list", "path"}


def test_validation():
    assert st.valid_host("NAS.local") and st.valid_host("192.168.1.5")
    for bad in ("", "-x", "a b", "a;rm", "a/b", "$(x)", "a" * 300):
        assert not st.valid_host(bad), bad
    assert st.valid_share("Multimedia") and st.valid_share("My Share") and st.valid_share("c$")
    for bad in ("", "../x", "a/b", "a;b", "a`b`", "-x"):
        assert not st.valid_share(bad), bad
    assert st.valid_user("") and st.valid_user("kuba") and not st.valid_user("a:b")
    if not st.MAC:
        assert st.valid_mount_point("/mnt/nas") and st.valid_mount_point("/media/pi/nas")
        for bad in ("/etc", "/mnt", "/mnt/../etc", "/mnt/", "relative", "/mnt/a b", "/home/x"):
            assert not st.valid_mount_point(bad), bad


def test_parsers():
    out = "Disk|Multimedia|QNAP share\nDisk|IPC$|IPC\nPrinter|lp|x\nDisk|Public|\nWorkgroup|W|x\n"
    assert st.parse_smbclient(out) == ["Multimedia", "Public"]
    mac = ("Share                      Type    Comments\n------\n"
           "Multimedia                 Disk    \nMy Files                   Disk    x\nIPC$   Pipe  IPC\n")
    assert st.parse_smbutil(mac) == ["Multimedia", "My Files"]


def touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_suggest():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "downloads" / "incomplete").mkdir(parents=True)
        (r / "@Recycle").mkdir()
        touch(r / "Multimedia" / "Film" / "Heat (1995)" / "Heat (1995).mkv")
        touch(r / "Multimedia" / "Telefilm" / "Lost" / "Season 1" / "Lost.S01E01.mkv")
        s = st.suggest_folders(d)
        assert s["movies_dir"] == str(r / "Multimedia" / "Film"), s
        assert s["series_dir"] == str(r / "Multimedia" / "Telefilm"), s
        assert s["download_dir"] == str(r / "downloads" / "incomplete"), s
    with tempfile.TemporaryDirectory() as d:  # content heuristic, odd names
        r = Path(d)
        touch(r / "stuff" / "Alien (1979)" / "a.mkv")
        touch(r / "stuff" / "Heat (1995)" / "h.mkv")
        touch(r / "other" / "Show" / "Stagione 1" / "x.mkv")
        s = st.suggest_folders(d)
        assert s == {"movies_dir": str(r / "stuff"), "series_dir": str(r / "other")}, s
    with tempfile.TemporaryDirectory() as d:
        assert st.suggest_folders(d) == {}
    assert st.suggest_folders("/nonexistent/zzz") == {}


def test_schema():
    for sch in (st.schema(), MEDIA_SCHEMA):
        keys = [f["key"] for f in sch]
        assert len(keys) == len(set(keys))
        for f in sch:
            assert f["type"] in TYPES and f["label"], f
            if f["type"] == "select":
                assert f["options"]
                if "default" in f:
                    assert f["default"] in f["options"]
            coerce(f, f.get("default"))
    assert next(f for f in MEDIA_SCHEMA if f["key"] == "tmdb_api_key")["link"]["url"].startswith("https://")
    assert st.StoragePlugin.ui and st.StoragePlugin.icon == "nas" and st.PLUGIN is st.StoragePlugin
    assert {"nas.host", "nas.share", "nas.username", "nas.password", "nas.mount_point",
            "nas.version", "auto_mount"} <= {f["key"] for f in st.schema()}


async def test_transmission_session_set():
    calls = []
    state = {"download-dir": "/old"}

    async def rpc(request):
        if request.headers.get("X-Transmission-Session-Id") != "sid":
            return web.Response(status=409, headers={"X-Transmission-Session-Id": "sid"})
        body = await request.json()
        calls.append(body)
        if body["method"] == "session-set":
            state.update(body["arguments"])
            return web.json_response({"result": "success", "arguments": {}})
        return web.json_response({"result": "success", "arguments": dict(state)})

    app = web.Application()
    app.router.add_post("/transmission/rpc", rpc)
    srv = TestServer(app)
    await srv.start_server()
    tr = Transmission({"url": str(srv.make_url("/transmission/rpc"))})
    await tr.open()
    try:
        assert await tr.set_download_dir("/mnt/nas/dl") is True
        assert state["download-dir"] == "/mnt/nas/dl"
        n = len(calls)
        assert await tr.set_download_dir("/mnt/nas/dl") is False
        assert [c["method"] for c in calls[n:]] == ["session-get"]
    finally:
        await tr.close()
        await srv.close()
    tr = Transmission({"url": "http://127.0.0.1:1/transmission/rpc"})  # unreachable
    await tr.open()
    try:
        await tr.set_download_dir("/x")
        raise AssertionError("expected error")
    except Exception as err:
        assert "not reachable" in str(err)
    finally:
        await tr.close()


if __name__ == "__main__":
    test_validation()
    test_parsers()
    test_suggest()
    test_schema()
    asyncio.run(test_transmission_session_set())
    print("storage ok")
