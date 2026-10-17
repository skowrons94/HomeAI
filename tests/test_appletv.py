"""Apple TV remote: presses answer at once, a dead connection is replaced."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Hub  # noqa: E402
from hub.plugins import appletv  # noqa: E402


class FakeRemote:
    def __init__(self, log, hang=False):
        self.log, self.hang = log, hang

    def __getattr__(self, key):
        async def press():
            if self.hang:
                await asyncio.sleep(60)  # dead socket: never answers
            self.log.append(key)
        return press


class FakeATV:
    def __init__(self, log, hang=False):
        self.remote_control = FakeRemote(log, hang)


def make(tmp):
    hub = Hub({"homekit": {"enabled": False}}, Path(tmp))
    p = appletv.AppleTVPlugin(hub, {})
    p.creds, p.identifier = {"Companion": "x"}, "id"
    hub.add_entity(appletv.Entity(p.eid, "Apple TV", "media_player", "appletv"))
    return hub, p


def test_presses_are_queued_and_sent_in_order():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            hub, p = make(d)
            sent = []
            p.atv = FakeATV(sent)
            worker = asyncio.ensure_future(p._press_worker())
            t = time.monotonic()
            for k in ("down", "down", "select"):
                assert await p.remote(k) == {"queued": True}
            assert time.monotonic() - t < 0.05  # the app never waits for the TV
            await asyncio.sleep(0.05)
            assert sent == ["down", "down", "select"]
            worker.cancel()
    asyncio.run(run())


def test_dead_connection_is_replaced_and_press_retried():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            hub, p = make(d)
            sent = []
            p.atv = FakeATV(sent, hang=True)
            reconnects = []

            async def ensure():
                if p.atv is None:
                    reconnects.append(1)
                    p.atv = FakeATV(sent)

            async def disconnect():
                p.atv = None

            p._ensure_connected, p._disconnect = ensure, disconnect
            appletv.PRESS_TIMEOUT = 0.1
            worker = asyncio.ensure_future(p._press_worker())
            await p.remote("select")
            await asyncio.sleep(0.4)
            assert reconnects == [1] and sent == ["select"]
            worker.cancel()
    asyncio.run(run())


def test_stale_presses_dropped():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            hub, p = make(d)
            sent = []
            p.atv = FakeATV(sent)
            p._presses.put_nowait(("up", time.monotonic() - 60))
            await p.remote("down")
            worker = asyncio.ensure_future(p._press_worker())
            await asyncio.sleep(0.05)
            assert sent == ["down"]
            worker.cancel()
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
