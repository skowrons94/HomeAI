"""System plugin. Run: python tests/test_system.py (or pytest)."""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins import system  # noqa: E402


def test_rate_basic():
    assert system.counter_rate(1000, 3000, 2) == 1000
    assert system.mbit(125000) == 1.0
    assert system.counter_rate(None, 5, 1) is None
    assert system.counter_rate(10, 20, 0) is None


def test_rate_wrap_and_reset():
    # 32-bit wrap: 4294967000 -> 296 is 592 bytes
    assert system.counter_rate(2**32 - 296, 296, 1) == 592
    # reset after interface restart / reboot: unusable
    assert system.counter_rate(5000, 100, 1) is None


def test_wireless_parsing():
    txt = ("Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |\n"
           " wlan0: 0000   58.  -52.  -256 0 0 0 0 0 0\n")
    dbm, q = system.parse_proc_wireless(txt, "wlan0")
    assert dbm == -52 and 0 < q <= 100
    assert system.parse_proc_wireless(txt, "wlan1") is None
    assert system.parse_iw_link("\tsignal: -61 dBm\n")[0] == -61


class FakeHub:
    def __init__(self, tmp):
        self.entities, self.data_dir, self.tasks = {}, Path(tmp), []

    def add_entity(self, e):
        self.entities[e.id] = e
        return e

    def update(self, eid, available=None, **st):
        self.entities[eid].state.update(st)

    def every(self, *a, **k):
        self.tasks.append((a, k))


def test_plugin_start_and_sample():
    with tempfile.TemporaryDirectory() as tmp:
        hub = FakeHub(tmp)
        p = system.PLUGIN(hub, {"interval": 5})
        asyncio.run(p.start())
        assert hub.tasks
        for eid in ("system.cpu", "system.memory", "system.disk", "system.network", "system.internet"):
            e = hub.entities[eid]
            assert e.kind == "sensor" and e.plugin == "system" and e.room == ""
            assert all(k in e.attrs["units"] for k in e.attrs["history"])
        p.sample()
        p.sample()
        cpu = hub.entities["system.cpu"].state
        assert 0 <= cpu["usage"] <= 100 and hub.entities["system.memory"].state["used"] > 0
        assert hub.entities["system.disk"].state["free"] > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
