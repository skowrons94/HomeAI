"""Bluetooth LE sensor advertisement decoders (pure functions, no I/O).

``parse(name, manufacturer_data, service_data)`` returns the first decoder's
result: a dict ``{model, manufacturer, temperature?, humidity?, battery?, ...}`` or
None. ``manufacturer_data`` is bleak's ``{company_id: bytes}``; ``service_data`` is
``{uuid: bytes}`` with 128-bit uuid strings (16-bit ints are accepted too).

Supported: BTHome v2 (unencrypted), pvvx / ATC custom firmware for Xiaomi
LYWSD03MMC, Xiaomi MiBeacon (unencrypted), SwitchBot Meter / Meter Plus / Outdoor,
Qingping, Govee thermo-hygrometers, Inkbird IBS-TH1/TH2 (best effort).
"""

from __future__ import annotations

import re

UNITS = {
    "temperature": "°C", "humidity": "%", "battery": "%", "pressure": "hPa",
    "illuminance": "lx", "co2": "ppm", "moisture": "%", "voltage": "V",
    "pm2_5": "µg/m³", "pm10": "µg/m³", "tvoc": "µg/m³", "dew_point": "°C",
    "power": "W", "energy": "kWh", "conductivity": "µS/cm",
}
# metadata keys of a reading (everything else is a numeric measurement)
META = ("model", "manufacturer")

_BASE = "-0000-1000-8000-00805f9b34fb"


def values(reading: dict) -> dict:
    """The numeric measurements of a parse() result."""
    return {k: v for k, v in reading.items()
            if k not in META and isinstance(v, (int, float)) and not isinstance(v, bool)}


def _sd(service_data: dict | None) -> dict[int, bytes]:
    """Normalise service data keys to 16-bit ints (Bluetooth base uuids only)."""
    out: dict[int, bytes] = {}
    for k, v in (service_data or {}).items():
        if isinstance(k, int):
            out[k] = bytes(v)
            continue
        k = str(k).lower()
        if k.endswith(_BASE) and k.startswith("0000"):
            out[int(k[4:8], 16)] = bytes(v)
        elif re.fullmatch(r"[0-9a-f]{4}", k):
            out[int(k, 16)] = bytes(v)
    return out


def _s16(d: bytes, i: int, order="little") -> int:
    return int.from_bytes(d[i:i + 2], order, signed=True)


def _u(d: bytes, i: int, n: int, order="little") -> int:
    return int.from_bytes(d[i:i + n], order)


def _r(x: float, n: int = 2) -> float:
    return round(x, n)


def _sane(r: dict) -> bool:
    t, h = r.get("temperature"), r.get("humidity")
    return (t is None or -60 <= t <= 130) and (h is None or 0 <= h <= 100)


def _finish(r: dict) -> dict | None:
    return r if values(r) and _sane(r) else None


# -- BTHome v2 -------------------------------------------------------------------------

# object id -> (key, bytes, signed, factor)
_BTHOME = {
    0x01: ("battery", 1, False, 1), 0x02: ("temperature", 2, True, 0.01),
    0x03: ("humidity", 2, False, 0.01), 0x04: ("pressure", 3, False, 0.01),
    0x05: ("illuminance", 3, False, 0.01), 0x08: ("dew_point", 2, True, 0.01),
    0x0A: ("energy", 3, False, 0.001), 0x0B: ("power", 3, False, 0.01),
    0x0C: ("voltage", 2, False, 0.001), 0x0D: ("pm2_5", 2, False, 1),
    0x0E: ("pm10", 2, False, 1), 0x12: ("co2", 2, False, 1), 0x13: ("tvoc", 2, False, 1),
    0x14: ("moisture", 2, False, 0.01), 0x2E: ("humidity", 1, False, 1),
    0x2F: ("moisture", 1, False, 1), 0x45: ("temperature", 2, True, 0.1),
}
# objects we skip over (id -> length): packet id, counts, binary sensors, ...
_BTHOME_SKIP = {0x00: 1, 0x09: 1, 0x3D: 2, 0x3E: 4, 0x0F: 1, 0x10: 1, 0x11: 1,
                0x2C: 1, 0x2D: 1, 0x3F: 2, **{i: 1 for i in range(0x15, 0x23)}}


def parse_bthome(name, mfr, svc) -> dict | None:
    d = _sd(svc).get(0xFCD2)
    if not d or len(d) < 2:
        return None
    if d[0] & 1 or (d[0] >> 5) != 2:  # encrypted or not v2
        return None
    out: dict = {"model": "BTHome", "manufacturer": "BTHome"}
    i = 1
    while i < len(d):
        oid = d[i]
        i += 1
        if oid in _BTHOME:
            key, n, signed, f = _BTHOME[oid]
            if i + n > len(d):
                break
            raw = int.from_bytes(d[i:i + n], "little", signed=signed)
            out[key] = _r(raw * f, 3) if f != 1 else raw
            i += n
        elif oid in _BTHOME_SKIP:
            i += _BTHOME_SKIP[oid]
        else:
            break  # unknown object: its length is unknown
    for k in ("temperature", "humidity", "pressure"):
        if k in out:
            out[k] = _r(out[k], 2)
    return _finish(out)


# -- pvvx / ATC custom firmware (Xiaomi LYWSD03MMC and friends) --------------------------

def parse_pvvx(name, mfr, svc) -> dict | None:
    d = _sd(svc).get(0x181A)
    if not d:
        return None
    if len(d) == 15:  # pvvx custom format: mac(6, reversed) t/100 h/100 mV batt% cnt flags
        r = {"temperature": _s16(d, 6) / 100, "humidity": _u(d, 8, 2) / 100,
             "voltage": _u(d, 10, 2) / 1000, "battery": d[12]}
    elif len(d) == 13:  # ATC1441: mac(6) t/10 (BE) h% batt% mV(BE) cnt
        r = {"temperature": _s16(d, 6, "big") / 10, "humidity": d[8], "battery": d[9],
             "voltage": _u(d, 10, 2, "big") / 1000}
    else:
        return None
    r = {"model": "LYWSD03MMC", "manufacturer": "Xiaomi (custom firmware)", **r}
    return _finish(r)


# -- Xiaomi MiBeacon (0xFE95) -------------------------------------------------------------

_MI_PRODUCTS = {0x0098: "LYWSDCGQ", 0x01AA: "LYWSDCGQ/01ZM", 0x045B: "LYWSD02",
                0x055B: "LYWSD03MMC", 0x0347: "CGG1", 0x0387: "MHO-C401",
                0x06D3: "MHO-C303", 0x2542: "LYWSD02MMC", 0x03DD: "MUE4094RT",
                0x0153: "HHCCJCY01", 0x015D: "HHCCPOT002", 0x066F: "CGDK2"}


def parse_mibeacon(name, mfr, svc) -> dict | None:
    d = _sd(svc).get(0xFE95)
    if not d or len(d) < 5:
        return None
    fc = _u(d, 0, 2)
    if fc & 0x0008 or not fc & 0x0040:  # encrypted / no object
        return None
    product = _u(d, 2, 2)
    i = 5
    if fc & 0x0010:
        i += 6  # mac
    if fc & 0x0020:  # capability byte (+1 if IO bit)
        if i < len(d):
            i += 2 if d[i] & 0x20 else 1
    if i + 3 > len(d):
        return None
    otype, n = _u(d, i, 2), d[i + 2]
    obj = d[i + 3:i + 3 + n]
    if len(obj) < n:
        return None
    out: dict = {"model": _MI_PRODUCTS.get(product, "Xiaomi sensor"), "manufacturer": "Xiaomi"}
    if otype == 0x1004 and n == 2:
        out["temperature"] = _s16(obj, 0) / 10
    elif otype == 0x1006 and n == 2:
        out["humidity"] = _u(obj, 0, 2) / 10
    elif otype == 0x100D and n == 4:
        out["temperature"], out["humidity"] = _s16(obj, 0) / 10, _u(obj, 2, 2) / 10
    elif otype == 0x1007 and n == 3:
        out["illuminance"] = _u(obj, 0, 3)
    elif otype == 0x1008 and n == 1:
        out["moisture"] = obj[0]
    elif otype == 0x1009 and n == 2:
        out["conductivity"] = _u(obj, 0, 2)
    elif otype == 0x100A and n == 1:
        out["battery"] = obj[0]
    else:
        return None
    return _finish(out)


# -- SwitchBot ------------------------------------------------------------------------------

_SB_MODELS = {0x54: "Meter", 0x69: "Meter Plus", 0x77: "Outdoor Meter"}


def _sb_values(decimal: int, integer: int, hum: int) -> dict:
    t = (integer & 0x7F) + (decimal & 0x0F) / 10
    return {"temperature": _r(t if integer & 0x80 else -t, 1), "humidity": hum & 0x7F}


def parse_switchbot(name, mfr, svc) -> dict | None:
    d = _sd(svc).get(0xFD3D)
    if d is None:  # older firmware: 128-bit cba20d00 service
        for k, v in (svc or {}).items():
            if isinstance(k, str) and k.lower().startswith("00000d00"):
                d = bytes(v)
    if d and len(d) >= 6 and (d[0] & 0x7F) in _SB_MODELS:
        r = {"model": "SwitchBot " + _SB_MODELS[d[0] & 0x7F], "manufacturer": "SwitchBot",
             "battery": d[2] & 0x7F, **_sb_values(d[3], d[4], d[5])}
        return _finish(r)
    m = (mfr or {}).get(0x0969)
    if m and len(m) >= 12 and (name or "").startswith(("WoSensorTH", "WoIOSensor")):
        r = {"model": "SwitchBot Meter", "manufacturer": "SwitchBot",
             **_sb_values(m[9], m[10], m[11])}
        return _finish(r)
    return None


# -- Qingping (0xFDCD) ------------------------------------------------------------------------

def parse_qingping(name, mfr, svc) -> dict | None:
    d = _sd(svc).get(0xFDCD)
    if not d or len(d) < 10:
        return None
    out: dict = {"model": "CGG1" if d[1] == 0x01 else "Qingping sensor",
                 "manufacturer": "Qingping"}
    i = 8
    while i + 2 <= len(d):
        t, n = d[i], d[i + 1]
        p = d[i + 2:i + 2 + n]
        if len(p) < n:
            break
        if t == 0x01 and n == 4:
            out["temperature"], out["humidity"] = _s16(p, 0) / 10, _u(p, 2, 2) / 10
        elif t == 0x02 and n == 1:
            out["battery"] = p[0]
        elif t == 0x07 and n == 2:
            out["pressure"] = _u(p, 0, 2) / 10
        elif t == 0x12 and n == 2:
            out["co2"] = _u(p, 0, 2)
        elif t == 0x13 and n == 2:
            out["pm2_5"] = _u(p, 0, 2)
        elif t == 0x14 and n == 2:
            out["pm10"] = _u(p, 0, 2)
        i += 2 + n
    return _finish(out)


# -- Govee -----------------------------------------------------------------------------------

MFR_EC88 = 0xEC88  # H5072/H5075 (packed) and H5074/H5051/H5052 (int16)
MFR_0001 = 0x0001  # H5100/H5101/H5102/H5104/H5105/H5110/H5174/H5177
MFR_8801 = 0x8801  # H5179


def _packed(raw: int) -> tuple[float, float]:
    """3-byte packed reading: temperature*10000 + humidity*10, bit 23 = negative."""
    neg = bool(raw & 0x800000)
    raw &= 0x7FFFFF
    temp = (raw // 1000) / 10
    return (-temp if neg else temp), (raw % 1000) / 10


def _govee(name: str, mfr: dict[int, bytes]) -> dict | None:
    m = re.search(r"H\d{4}", name or "")
    model = m.group(0) if m else ""
    if MFR_8801 in mfr:
        d = mfr[MFR_8801]
        if len(d) >= 9:  # H5179
            return {"model": model or "H5179",
                    "temperature": int.from_bytes(d[4:6], "little", signed=True) / 100,
                    "humidity": int.from_bytes(d[6:8], "little") / 100, "battery": d[8]}
    if MFR_EC88 in mfr:
        d = mfr[MFR_EC88]
        int16 = model in ("H5074", "H5051", "H5052", "H5071") or (not model and len(d) == 7)
        if int16 and len(d) >= 6:
            return {"model": model or "H5074",
                    "temperature": int.from_bytes(d[1:3], "little", signed=True) / 100,
                    "humidity": int.from_bytes(d[3:5], "little") / 100, "battery": d[5]}
        if len(d) >= 5:  # H5072 / H5075
            t, h = _packed(int.from_bytes(d[1:4], "big"))
            return {"model": model or "H5075", "temperature": t, "humidity": h,
                    "battery": d[4] & 0x7F}
    if MFR_0001 in mfr:
        d = mfr[MFR_0001]
        # 0x0001 is a generic id; only trust it for named Govee devices
        if len(d) >= 6 and (model or (name or "").startswith(("GV", "Govee"))):
            t, h = _packed(int.from_bytes(d[2:5], "big"))
            return {"model": model or "H5101", "temperature": t, "humidity": h,
                    "battery": d[5] & 0x7F}
    return None


def decode_govee(name: str, manufacturer_data: dict[int, bytes]) -> dict | None:
    """Return {model, temperature, humidity, battery} or None (no manufacturer field)."""
    return _govee(name, manufacturer_data or {})


def parse_govee(name, mfr, svc) -> dict | None:
    r = _govee(name, mfr or {})
    if r is None or not (-40 <= r["temperature"] <= 80 and 0 <= r["humidity"] <= 100):
        return None
    return {**r, "manufacturer": "Govee"}


# -- Inkbird IBS-TH1 / TH2 (best effort) ---------------------------------------------------------

def parse_inkbird(name, mfr, svc) -> dict | None:
    if name not in ("sps", "tps") or not mfr:
        return None
    cid, data = next(iter(mfr.items()))
    raw = cid.to_bytes(2, "little") + bytes(data)  # the first two bytes land in the company id
    if len(raw) != 9:
        return None
    r = {"model": "IBS-TH1" if name == "sps" else "IBS-TH2", "manufacturer": "Inkbird",
         "temperature": _s16(raw, 0) / 100, "humidity": _u(raw, 2, 2) / 100,
         "battery": raw[7]}
    if r["battery"] > 100:
        del r["battery"]
    return _finish(r)


PARSERS = (parse_bthome, parse_pvvx, parse_mibeacon, parse_switchbot, parse_qingping,
           parse_govee, parse_inkbird)


def parse(name: str, manufacturer_data: dict | None = None,
          service_data: dict | None = None) -> dict | None:
    for fn in PARSERS:
        try:
            r = fn(name or "", manufacturer_data or {}, service_data or {})
        except (IndexError, ValueError, TypeError):
            r = None
        if r is not None:
            return r
    return None
