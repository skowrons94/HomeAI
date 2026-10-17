"""Govee advertisement decoding. Run: python tests/test_govee.py (or pytest)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.govee import decode  # noqa: E402


def test_h5075_packed():
    # 21.5 °C / 45.6 % -> 215456 = 0x0349A0, battery 90
    r = decode("GVH5075_1A2B", {0xEC88: bytes.fromhex("000349a05a00")})
    assert r == {"model": "H5075", "temperature": 21.5, "humidity": 45.6, "battery": 90}


def test_h5075_negative():
    # -5.2 °C / 60.0 % -> 52600 | 0x800000, battery 64
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
    # 0x0001 is a generic company id: unnamed adverts must be ignored
    assert decode("", {0x0001: bytes.fromhex("01010349a05a")}) is None
    assert decode("SomePhone", {0x0001: bytes.fromhex("01010349a05a")}) is None


def test_h5074_int16():
    # 21.50 °C (0x0866) / 45.60 % (0x11D0), battery 90
    r = decode("Govee_H5074_ABCD", {0xEC88: bytes.fromhex("006608d0115a02")})
    assert r == {"model": "H5074", "temperature": 21.5, "humidity": 45.6, "battery": 90}


def test_h5074_negative():
    # -3.10 °C -> 0xFECA little-endian
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
