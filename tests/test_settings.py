"""Settings (config.toml edited from the app), live plugin reload, history rollups."""

import asyncio
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from hub import settings  # noqa: E402
from hub.core import Entity, Hub, Plugin  # noqa: E402
from hub.settings import SECRET, Config  # noqa: E402
from hub.store import Store  # noqa: E402
from hub.web import WebServer  # noqa: E402

SCHEMA = [
    {"key": "host", "label": "Host", "type": "text", "default": ""},
    {"key": "password", "label": "Password", "type": "password"},
    {"key": "interval", "label": "Interval", "type": "number", "default": 10, "min": 5},
    {"key": "proxy.url", "label": "Proxy", "type": "text"},
    {"key": "langs", "label": "Languages", "type": "list"},
]


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        path.write_text('name = "Casa"\n[plugins.demo]\nhost = "a"\npassword = "pw"\n'
                        'paired_id = "keep-me"\n')
        cfg = Config(path)
        vals = settings.values_for(SCHEMA, cfg.plugin("demo"))
        assert vals["password"] == SECRET and vals["interval"] == 10
        cfg.set_plugin("demo", SCHEMA, {"host": "b", "password": SECRET, "interval": "2",
                                        "proxy.url": "socks5://x", "langs": "it, en"})
        again = Config(path).plugin("demo")
        assert again == {"host": "b", "password": "pw", "interval": 5, "paired_id": "keep-me",
                         "proxy": {"url": "socks5://x"}, "langs": ["it", "en"]}
        cfg.set_plugin("demo", SCHEMA, {"host": ""}, enabled=False)
        assert "host" not in Config(path).plugin("demo")
        assert not Config(path).plugin_enabled("demo")
        assert (Path(d) / "config.toml.bak").exists()
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_rollups_and_ranges():
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "h.db")
        now = time.time()
        for i in range(120):  # two hours, a sample a minute
            st.add_history("s.t", "temperature", 20 + (i % 2), now - 7200 + i * 60)
        day = st.history("s.t", "temperature", now - 86400)
        assert len(day) >= 119 and len(day[0]) == 4
        year = st.history("s.t", "temperature", now - 365 * 86400)  # hourly table
        assert 2 <= len(year) <= 3
        _, avg, lo, hi = year[0]
        assert lo == 20 and hi == 21 and 20 < avg < 21
        assert st.series()[0]["entity"] == "s.t"
        assert st.history("nope", "x", 0) == []


def test_migrates_old_history():
    with tempfile.TemporaryDirectory() as d:
        db = sqlite3.connect(Path(d) / "h.db")
        db.execute("CREATE TABLE history (entity TEXT, key TEXT, ts REAL, value REAL)")
        db.execute("INSERT INTO history VALUES ('a', 'humidity', ?, 55)", (time.time() - 60,))
        db.commit()
        db.close()
        st = Store(Path(d) / "h.db")
        assert st.history("a", "humidity", time.time() - 3600)[0][1] == 55
        assert not st.query("SELECT name FROM sqlite_master WHERE name='history'")


class Demo(Plugin):
    name = "demo"
    schema = SCHEMA
    starts = 0

    async def start(self):
        Demo.starts += 1
        self.hub.add_entity(Entity("demo.s", "S", "sensor", "demo",
                                   state={"temperature": 1.0, "rssi": -60}))
        self.ticks = 0
        self.hub.every(0.01, self.tick, initial_delay=0)

    async def tick(self):
        self.ticks += 1

    def routes(self):
        async def hello(request):
            return web.json_response({"host": self.config.get("host"),
                                      "id": request.match_info["id"], "q": request.query.get("q")})
        return [web.get("/hello/{id}", hello)]


def test_live_reload_and_routes():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.toml").write_text('[homekit]\nenabled = false\n[plugins.demo]\nhost = "a"\n')
            sys.modules["hub.plugins.demo"] = types.SimpleNamespace(PLUGIN=Demo)
            hub = Hub(Config(d / "config.toml"), d)
            await hub.start()
            assert Demo.starts == 1 and "demo.s" in hub.entities
            # every numeric key of a sensor is recorded
            assert {r["key"] for r in hub.store.series()} == {"temperature", "rssi"}
            srv = WebServer(hub, {}, d)
            client = TestClient(TestServer(srv.build()))
            await client.start_server()
            try:
                r = await (await client.get("/api/p/demo/hello/7?q=x")).json()
                assert r == {"host": "a", "id": "7", "q": "x"}
                s = await (await client.get("/api/settings")).json()
                demo = next(p for p in s["plugins"] if p["name"] == "demo")
                assert demo["running"] and demo["values"]["host"] == "a"
                old = hub.plugins["demo"]
                await asyncio.sleep(0.05)
                resp = await client.put("/api/settings/plugins/demo", json={"values": {"host": "b"}})
                assert (await resp.json())["running"]
                assert Demo.starts == 2 and hub.plugins["demo"] is not old
                ticks = old.ticks
                await asyncio.sleep(0.05)
                assert old.ticks == ticks, "old plugin's tasks must be cancelled"
                r = await (await client.get("/api/p/demo/hello/1")).json()
                assert r["host"] == "b"
                await client.put("/api/settings/plugins/demo", json={"enabled": False})
                assert "demo" not in hub.plugins and "demo.s" not in hub.entities
                assert (await client.get("/api/p/demo/hello/1")).status == 404
                r = await client.put("/api/settings/core/notify",
                                     json={"values": {"ntfy_url": "https://ntfy.sh/x"}})
                assert not (await r.json())["restart"] and "ntfy" in hub.notify.channels
                assert 'ntfy_url = "https://ntfy.sh/x"' in (d / "config.toml").read_text()
            finally:
                await client.close()
                await hub.stop()
                del sys.modules["hub.plugins.demo"]

    asyncio.run(run())


def test_attention():
    with tempfile.TemporaryDirectory() as d:
        hub = Hub({"homekit": {"enabled": False}}, Path(d))

        class Broken(Plugin):
            name, title = "broken", "Broken"

        class Unpaired(Plugin):
            name, title = "tv", "TV"

            def status(self):
                return {"setup": True, "configured": False}

            def attention(self):
                return [{"id": "key", "title": "Add a key", "level": "info"}]

        b = Broken(hub, {})
        b.error = "boom"
        hub.plugins = {"broken": b, "tv": Unpaired(hub, {})}
        items = hub.attention()
        assert [i["id"] for i in items][:2] == ["broken.error", "tv.setup"]
        assert {"tv.key", "notify.phone"} <= {i["id"] for i in items}
        hub.dismiss_attention("tv.key")
        hub.dismiss_attention("broken.error")  # errors stay visible
        ids = {i["id"] for i in hub.attention()}
        assert "tv.key" not in ids and "broken.error" in ids
        assert "tv.key" in {i["id"] for i in hub.attention(include_dismissed=True)}


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
