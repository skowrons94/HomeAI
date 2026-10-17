"""Core tests: entities, overrides, history throttling, stable HomeKit AIDs."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Entity, Hub  # noqa: E402
from hub.homekit import HomeKit  # noqa: E402


def make_hub(tmp: Path) -> Hub:
    return Hub({"homekit": {"enabled": False}}, tmp)


def test_update_and_listeners():
    with tempfile.TemporaryDirectory() as d:
        hub = make_hub(Path(d))
        seen = []
        hub.listeners.append(lambda e: seen.append(dict(e.state)))
        q = hub.subscribe()
        hub.add_entity(Entity("t.s", "Sensor", "sensor", "t", state={"temperature": 20.0}))
        hub.update("t.s", temperature=20.0)  # unchanged -> no event
        hub.update("t.s", temperature=21.5)
        assert seen == [{"temperature": 20.0}, {"temperature": 21.5}]
        assert q.qsize() == 2
        hub.update("missing", temperature=1)  # ignored, no crash


def test_overrides_persist():
    with tempfile.TemporaryDirectory() as d:
        hub = make_hub(Path(d))
        hub.add_entity(Entity("t.l", "Lamp", "light", "t", room="A"))
        hub.set_override("t.l", name="Lampada", room="Soggiorno", hidden=True)
        hub2 = Hub({"homekit": {"enabled": False}}, Path(d))
        hub2.store = hub.store
        e = hub2.add_entity(Entity("t.l", "Lamp", "light", "t", room="A"))
        assert (e.name, e.room, e.attrs.get("hidden")) == ("Lampada", "Soggiorno", True)


def test_history_throttled():
    with tempfile.TemporaryDirectory() as d:
        hub = make_hub(Path(d))
        hub.add_entity(Entity("t.s", "S", "sensor", "t", state={"humidity": 50}))
        for v in (51, 52, 53):  # rapid changes: at most one sample a minute
            hub.update("t.s", humidity=v)
        assert len(hub.store.history("t.s", "humidity", 0)) == 1


def test_action_call():
    with tempfile.TemporaryDirectory() as d:
        hub = make_hub(Path(d))
        calls = []

        async def turn_on(brightness=None):
            calls.append(brightness)

        hub.add_entity(Entity("t.l", "L", "light", "t", actions={"turn_on": turn_on}))
        asyncio.run(hub.call("t.l", "turn_on", brightness=40))
        assert calls == [40]


def test_homekit_aids_stable():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "homekit.aids.json").write_text(json.dumps({"ariston.zone1": 2, "ariston.dhw": 3}))
        hk = HomeKit(None, {}, d)
        assert hk._aid("ariston.dhw") == 3
        assert hk._aid("govee.a") == 4
        assert hk._aid("x") == 5 and hk._aid("y") == 6 and hk._aid("z") == 8  # 7 is skipped
        assert HomeKit(None, {}, d)._aid("govee.a") == 4  # persisted


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
