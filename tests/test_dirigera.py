"""DIRIGERA device mapping + websocket events. Run: python tests/test_dirigera.py"""

import asyncio
import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Hub  # noqa: E402
from hub.plugins.dirigera import DirigeraPlugin, eid_for, map_device  # noqa: E402

LIGHT = {
    "id": "5c5d1e6a-6bf4-4b0a-9c2b-7a0e2d1b9a11_1",
    "type": "light", "deviceType": "light", "isReachable": True,
    "attributes": {
        "customName": "Lampada divano", "model": "TRADFRI bulb E27 WS globe 1055lm",
        "manufacturer": "IKEA of Sweden", "isOn": True, "lightLevel": 62,
        "colorTemperature": 2710, "colorTemperatureMin": 4000, "colorTemperatureMax": 2202,
        "colorMode": "temperature",
    },
    "capabilities": {"canSend": [],
                     "canReceive": ["customName", "isOn", "lightLevel", "colorTemperature"]},
    "room": {"id": "r1", "name": "Soggiorno"},
}
OUTLET = {
    "id": "7f1c0c2e-1111-2222-3333-444455556666_1",
    "type": "outlet", "deviceType": "outlet", "isReachable": True,
    "attributes": {"customName": "Presa TV", "model": "TRETAKT Smart plug",
                   "manufacturer": "IKEA of Sweden", "isOn": False},
    "capabilities": {"canReceive": ["isOn"]},
    "room": {"id": "r1", "name": "Soggiorno"},
}
VINDSTYRKA = {
    "id": "aa11bb22-cc33-dd44-ee55-ff6677889900_1",
    "type": "sensor", "deviceType": "environmentSensor", "isReachable": True,
    "attributes": {"customName": "Aria camera", "model": "VINDSTYRKA",
                   "manufacturer": "IKEA of Sweden", "currentTemperature": 21,
                   "currentRH": 48, "currentPM25": 3, "vocIndex": 98},
    "room": {"id": "r2", "name": "Camera"},
}
REMOTE = {"id": "x_1", "type": "controller", "deviceType": "lightController",
          "attributes": {"customName": "Remote"}}


def test_mapping():
    kind, state, attrs = map_device(LIGHT)
    assert kind == "light"
    assert state == {"on": True, "brightness": 62, "color_temp": 2710}
    assert attrs["color_temp_range"] == (2202, 4000)
    assert map_device(OUTLET)[:2] == ("switch", {"on": False})
    kind, state, attrs = map_device(VINDSTYRKA)
    assert kind == "sensor" and state["temperature"] == 21 and state["humidity"] == 48
    assert state["pm25"] == 3
    assert map_device(REMOTE) is None


def _hub():
    root = Path(tempfile.mkdtemp())
    return Hub({"homekit": {"enabled": False}}, root)


def test_entities_and_events():
    hub = _hub()
    p = DirigeraPlugin(hub, {})
    for dev in (LIGHT, OUTLET, VINDSTYRKA, REMOTE):
        p._upsert(copy.deepcopy(dev))
    light = hub.entities[eid_for(LIGHT["id"])]
    assert light.name == "Lampada divano" and light.room == "Soggiorno"
    assert set(light.actions) == {"turn_on", "turn_off"}
    assert len([e for e in hub.entities.values() if e.plugin == "dirigera"]) == 3

    p.handle_event({"type": "deviceStateChanged",
                    "data": {"id": LIGHT["id"], "attributes": {"isOn": False, "lightLevel": 10}}})
    assert light.state["on"] is False and light.state["brightness"] == 10

    p.handle_event({"type": "deviceStateChanged",
                    "data": {"id": OUTLET["id"], "isReachable": False, "attributes": {}}})
    assert hub.entities[eid_for(OUTLET["id"])].available is False

    p.handle_event({"type": "deviceRemoved", "data": {"id": OUTLET["id"]}})
    assert eid_for(OUTLET["id"]) not in hub.entities
    hub.store.close()


def test_turn_on_patches():
    hub = _hub()
    p = DirigeraPlugin(hub, {})
    p._upsert(copy.deepcopy(LIGHT))
    sent = []

    async def fake_patch(eid, attributes):
        sent.append(attributes)

    p._patch = fake_patch
    eid = eid_for(LIGHT["id"])
    asyncio.run(hub.call(eid, "turn_on", brightness=150, color_temp=9000))
    assert sent == [{"isOn": True}, {"lightLevel": 100}, {"colorTemperature": 4000}]
    asyncio.run(hub.call(eid, "turn_off"))
    assert sent[-1] == {"isOn": False} and hub.entities[eid].state["on"] is False
    hub.store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
