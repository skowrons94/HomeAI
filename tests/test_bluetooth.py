"""Bluetooth sensor parsers + plugin logic (no radio). Run: python tests/test_bluetooth.py"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Hub  # noqa: E402
from hub.plugins.bluetooth import BluetoothPlugin, eid_for  # noqa: E402
from hub.plugins.bluetooth.parsers import decode_govee as decode  # noqa: E402
from hub.plugins.bluetooth.parsers import parse  # noqa: E402


def uuid16(n):
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


# -- Govee (ported from the old govee plugin) ----------------------------------------------

def test_h5075_packed():
    # 21.5 °C / 45.6 % -> 215456 = 0x0349A0, battery 90
    r = decode("GVH5075_1A2B", {0xEC88: bytes.fromhex("000349a05a00")})
    assert r == {"model": "H5075", "temperature": 21.5, "humidity": 45.6, "battery": 90}


def test_h5075_negative():
    r = decode("GVH5075_1A2B", {0xEC88: bytes.fromhex("0080cd784000")})
    assert r["temperature"] == -5.2 and r["humidity"] == 60.0 and r["battery"] == 64


def test_h5072_without_name():
    r = decode("", {0xEC88: bytes.fromhex("000349a05a00")})
    assert r["model"] == "H5075" and r["temperature"] == 21.5


def test_h5101_h5177_family():
    for name in ("GVH5101_0001", "GVH5177_ABCD", "GVH5102_1234", "GVH5174_9999"):
        r = decode(name, {0x0001: bytes.fromhex("01010349a05a")})
        assert r["model"] == name[2:7]
        assert (r["temperature"], r["humidity"], r["battery"]) == (21.5, 45.6, 90)


def test_0001_needs_govee_name():
    assert decode("", {0x0001: bytes.fromhex("01010349a05a")}) is None
    assert decode("SomePhone", {0x0001: bytes.fromhex("01010349a05a")}) is None


def test_h5074_int16():
    r = decode("Govee_H5074_ABCD", {0xEC88: bytes.fromhex("006608d0115a02")})
    assert r == {"model": "H5074", "temperature": 21.5, "humidity": 45.6, "battery": 90}


def test_h5074_negative():
    r = decode("Govee_H5074_ABCD", {0xEC88: bytes.fromhex("00cafed0115a02")})
    assert r["temperature"] == -3.1


def test_h5051_int16():
    r = decode("Govee_H5051_1111", {0xEC88: bytes.fromhex("006608d0115a")})
    assert r["model"] == "H5051" and r["temperature"] == 21.5 and r["battery"] == 90


def test_h5179():
    r = decode("Govee_H5179_AB12", {0x8801: bytes.fromhex("ec0001016608d0115a")})
    assert r == {"model": "H5179", "temperature": 21.5, "humidity": 45.6, "battery": 90}


def test_unrelated():
    assert decode("AirPods", {0x004C: bytes.fromhex("0719010e2022")}) is None
    assert parse("AirPods", {0x004C: bytes.fromhex("0719010e2022")}, {}) is None


def test_govee_via_parse():
    r = parse("GVH5075_1A2B", {0xEC88: bytes.fromhex("000349a05a00")}, {})
    assert r["manufacturer"] == "Govee" and r["temperature"] == 21.5


# -- other parsers --------------------------------------------------------------------------

def test_bthome():
    # v2, unencrypted: packet id 0x12? no: 00 12 = packet id, 01 5d battery 93,
    # 02 ca08 temp 22.50, 03 bf13 humidity 50.55
    d = bytes.fromhex("40" "0012" "015d" "02ca08" "03bf13")
    r = parse("", {}, {uuid16(0xFCD2): d})
    assert r["model"] == "BTHome"
    assert (r["temperature"], r["humidity"], r["battery"]) == (22.5, 50.55, 93)


def test_bthome_more_objects():
    # pressure 1013.25 hPa (0x018c65), illuminance 55.5 lx (0x0015b3?), co2 800, negative temp
    d = bytes.fromhex("40" "04" "cd8b01" "05" "ae1500" "12" "2003" "02" "9cff")
    r = parse("", {}, {uuid16(0xFCD2): d})
    assert r["pressure"] == 1013.25 and r["co2"] == 800 and r["temperature"] == -1.0
    assert r["illuminance"] == 55.5


def test_bthome_encrypted_or_empty():
    assert parse("", {}, {uuid16(0xFCD2): bytes.fromhex("41" "02ca08")}) is None
    assert parse("", {}, {uuid16(0xFCD2): bytes.fromhex("40" "0012")}) is None  # no measurements


def test_pvvx_custom():
    d = bytes.fromhex("563412 38c1a4".replace(" ", "") + "6608" "d011" "b80b" "5a" "07" "00")
    assert len(d) == 15
    r = parse("ATC_A4C138", {}, {uuid16(0x181A): d})
    assert r["model"] == "LYWSD03MMC"
    assert (r["temperature"], r["humidity"], r["battery"], r["voltage"]) == (21.5, 45.6, 90, 3.0)


def test_atc1441():
    d = bytes.fromhex("a4c138123456" "00d7" "2e" "5a" "0bb8" "07")
    assert len(d) == 13
    r = parse("ATC_123456", {}, {uuid16(0x181A): d})
    assert (r["temperature"], r["humidity"], r["battery"]) == (21.5, 46, 90)


def test_atc_negative():
    d = bytes.fromhex("a4c138123456" "ff9c" "2e" "5a" "0bb8" "07")  # -10.0 C
    assert parse("", {}, {uuid16(0x181A): d})["temperature"] == -10.0


def test_mibeacon():
    mac = "112233445566"
    d = bytes.fromhex("5000" "9800" "01" + mac + "041002e400")
    r = parse("MJ_HT_V1", {}, {uuid16(0xFE95): d})
    assert r["model"] == "LYWSDCGQ" and r["temperature"] == 22.8
    d = bytes.fromhex("5000" "9800" "02" + mac + "0d1004e4001a02")  # temp+humidity 53.8
    r = parse("", {}, {uuid16(0xFE95): d})
    assert (r["temperature"], r["humidity"]) == (22.8, 53.8)
    d = bytes.fromhex("5000" "9800" "03" + mac + "0a100164")
    assert parse("", {}, {uuid16(0xFE95): d})["battery"] == 100


def test_mibeacon_encrypted():
    d = bytes.fromhex("5858" "5b05" "01" "112233445566" "aabbccdd")
    assert parse("", {}, {uuid16(0xFE95): d}) is None


def test_switchbot_service():
    d = bytes.fromhex("54" "00" "e4" "05" "96" "3c")  # Meter 22.5 C 60 % 100 %
    r = parse("", {}, {uuid16(0xFD3D): d})
    assert r["model"] == "SwitchBot Meter"
    assert (r["temperature"], r["humidity"], r["battery"]) == (22.5, 60, 100)
    d = bytes.fromhex("77" "00" "50" "03" "05" "32")  # Outdoor, negative: -(5 + 0.3), 50 %
    r = parse("", {}, {uuid16(0xFD3D): d})
    assert r["model"] == "SwitchBot Outdoor Meter" and r["temperature"] == -5.3
    assert r["humidity"] == 50 and r["battery"] == 80


def test_qingping():
    d = bytes.fromhex("0801" "112233445566" "0104e400c301" "02015a")
    r = parse("Qingping Temp RH", {}, {uuid16(0xFDCD): d})
    assert r["model"] == "CGG1"
    assert (r["temperature"], r["humidity"], r["battery"]) == (22.8, 45.1, 90)


def test_inkbird():
    raw = bytes.fromhex("6608" "d011" "000000" "5a" "00")
    cid = int.from_bytes(raw[:2], "little")
    r = parse("sps", {cid: raw[2:]}, {})
    assert r["model"] == "IBS-TH1"
    assert (r["temperature"], r["humidity"], r["battery"]) == (21.5, 45.6, 90)
    assert parse("other", {cid: raw[2:]}, {}) is None


def test_garbage_does_not_raise():
    assert parse("x", {0xEC88: b""}, {uuid16(0x181A): b"\x01", uuid16(0xFE95): b"\x50"}) is None


# -- plugin logic -----------------------------------------------------------------------------

H5075 = {0xEC88: bytes.fromhex("000349a05a00")}
ADDR = "A4:C1:38:12:34:56"


def _plugin(config=None, store_from=None):
    hub = store_from or Hub({"homekit": {"enabled": False}}, Path(tempfile.mkdtemp()))
    exposed = []
    hub.homekit.expose = lambda e, uid=None: exposed.append(uid or e.id)
    return hub, BluetoothPlugin(hub, config or {}), exposed


def test_auto_add_and_values():
    hub, p, exposed = _plugin()
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    e = hub.entities[eid_for(ADDR)]
    assert e.id == "ble.a4c138123456" and e.kind == "sensor" and e.plugin == "bluetooth"
    assert e.state == {"temperature": 21.5, "humidity": 45.6, "battery": 90} and e.available
    assert e.attrs["units"]["temperature"] == "°C" and e.attrs["rssi"] == -60
    assert "rssi" not in e.state and exposed == [e.id]
    assert hub.store.get("bluetooth.devices")[ADDR]["model"] == "H5075"
    hub.store.close()


def test_auto_add_off_and_manual_add():
    hub, p, _ = _plugin({"auto_add": False})
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    assert eid_for(ADDR) not in hub.entities
    row = p.nearby_list()[0]
    assert row["recognised"] and not row["added"] and row["model"] == "H5075"
    assert p._add(ADDR)
    assert eid_for(ADDR) in hub.entities
    assert p._remove(ADDR) and eid_for(ADDR) not in hub.entities
    assert hub.store.get("bluetooth.devices") == {}
    hub.store.close()


def test_unknown_listed_not_added():
    hub, p, _ = _plugin()
    p.feed("11:22:33:44:55:66", "AirPods", -50, {0x004C: b"\x07\x19"}, {})
    p.feed("77:22:33:44:55:66", "", -50, {}, {})  # anonymous: ignored
    rows = p.nearby_list()
    assert len(rows) == 1 and rows[0]["model"] == "unknown" and not rows[0]["recognised"]
    assert not [e for e in hub.entities.values() if e.plugin == "bluetooth"]
    hub.store.close()


def test_persistence_across_restart():
    hub, p, _ = _plugin()
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    p2 = BluetoothPlugin(hub, {})
    hub.remove_entity(eid_for(ADDR))
    asyncio.run(_start_only(p2))
    e = hub.entities[eid_for(ADDR)]
    assert not e.available and e.attrs["model"] == "H5075"
    hub.store.close()


async def _start_only(p):
    for addr in p.known:
        p._ensure_entity(addr)


def test_ignore_blocks_auto_add():
    hub, p, _ = _plugin()
    p.ignored.add(ADDR)
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    assert eid_for(ADDR) not in hub.entities and p.nearby_list()[0]["ignored"]
    hub.store.close()


def test_stale_unavailable_then_back():
    hub, p, _ = _plugin({"stale_minutes": 10})
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    eid = eid_for(ADDR)
    asyncio.run(p._check_stale())
    assert hub.entities[eid].available
    p.last_seen[eid] = time.time() - 11 * 60
    asyncio.run(p._check_stale())
    assert not hub.entities[eid].available
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    assert hub.entities[eid].available
    hub.store.close()


def test_homekit_flags():
    hub, p, exposed = _plugin({"homekit": False})
    p.feed(ADDR, "GVH5075_3456", -60, H5075, {})
    assert exposed == []
    hub.remove_entity(eid_for(ADDR))
    p._add(ADDR, homekit=True)  # per-device flag wins over the plugin default
    assert exposed == [eid_for(ADDR)]
    # a sensor without temperature/humidity is never exposed
    hub2, p2, ex2 = _plugin()
    p2.feed("AA:BB:CC:DD:EE:FF", "", -70, {}, {uuid16(0xFCD2): bytes.fromhex("40" "05" "ae1500")})
    assert eid_for("AA:BB:CC:DD:EE:FF") in hub2.entities and ex2 == []
    hub.store.close()
    hub2.store.close()


def test_legacy_sensors_config():
    hub, p, _ = _plugin({"sensors": {"a4:c1:38:12:34:56": {"name": "Bagno", "room": "Bagno"}}})
    asyncio.run(_start_only(p))
    e = hub.entities[eid_for(ADDR)]
    assert e.name == "Bagno" and e.room == "Bagno"
    hub.store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
