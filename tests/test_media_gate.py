"""transmission.enabled = false: the media plugin never contacts Transmission. No network.
Run: python tests/test_media_gate.py"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media import MediaPlugin  # noqa: E402
from hub.plugins.media.transmission import Transmission, TransmissionError  # noqa: E402
from hub.store import Store  # noqa: E402


class FakeHub:
    def __init__(self, tmp: Path):
        self.store, self.data_dir, self.http, self.plugins = Store(tmp / "hub.db"), tmp, None, {}
        self.tasks: list[str] = []

    def spawn(self, coro, name=""):
        coro.close()
        self.tasks.append(name)

    def every(self, seconds, fn, name="", initial_delay=None):
        self.tasks.append(name)


def _plugin(tmp: Path, enabled) -> tuple[MediaPlugin, FakeHub]:
    hub = FakeHub(tmp)
    tx = {"url": "http://127.0.0.1:9/transmission/rpc"}
    if enabled is not None:
        tx["enabled"] = enabled
    p = MediaPlugin(hub, {"download_dir": str(tmp / "dl"), "movies_dir": str(tmp / "m"),
                          "series_dir": str(tmp / "s"), "transmission": tx})
    return p, hub


def test_client_refuses_when_disabled():
    tr = Transmission({"enabled": False})
    asyncio.run(tr.open())
    assert tr.http is None  # no session, so nothing could be sent
    for coro in (tr.session(), tr.set_download_dir("/x"), tr.add("magnet:?xt=urn:btih:abc"),
                 tr.remove("abc"), tr.torrents()):
        try:
            asyncio.run(coro)
        except TransmissionError as err:
            assert "turned off" in str(err)
        else:
            raise AssertionError("disabled client made a call")
    asyncio.run(tr.close())
    print("ok test_client_refuses_when_disabled")


def test_plugin_start_skips_transmission():
    with tempfile.TemporaryDirectory() as d:
        off, hub = _plugin(Path(d), False)
        off.indexers.open = off.indexers.probe = _noop
        asyncio.run(off.start())
        assert "transmission dir" not in hub.tasks and "transmission poll" not in hub.tasks, hub.tasks
        st = off.status()["transmission"]
        assert st["enabled"] is False and st["ok"] is None
        assert not [a for a in off.attention() if a["id"] == "transmission"]
        asyncio.run(off.stop())
    with tempfile.TemporaryDirectory() as d:
        on, hub = _plugin(Path(d), None)  # default: enabled
        on.indexers.open = on.indexers.probe = _noop
        asyncio.run(on.start())
        assert "transmission dir" in hub.tasks and "transmission poll" in hub.tasks, hub.tasks
        asyncio.run(on.stop())
    print("ok test_plugin_start_skips_transmission")


async def _noop(*a, **k):
    return None


if __name__ == "__main__":
    test_client_refuses_when_disabled()
    test_plugin_start_skips_transmission()
