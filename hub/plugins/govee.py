"""Govee Bluetooth thermo-hygrometers (H5075, H5072, H5101, H5102, H5174,
H5177, H5100, H5104, H5105, H5110, H5074, H5051, H5052, H5179 ...).

The sensors broadcast their readings in BLE advertisements, so nothing is
paired or connected: the hub just listens. Sensors appear the first time they
are heard and are remembered, so they're back in HomeKit after a restart even
before they broadcast again.

    [plugins.govee]
    auto_add = true          # add every Govee sensor heard
    homekit = true           # expose them to the Home app
    adapter = "hci0"         # Linux only, optional
    sensors = { "A4:C1:38:12:34:56" = { name = "Bagno", room = "Bagno" } }
"""

from __future__ import annotations

import asyncio
import re
import time

from ..core import Entity, Plugin

MFR_EC88 = 0xEC88  # H5072/H5075 (packed) and H5074/H5051/H5052 (int16)
MFR_0001 = 0x0001  # H5100/H5101/H5102/H5104/H5105/H5110/H5174/H5177
MFR_8801 = 0x8801  # H5179

PUSH_EVERY = 60       # re-send unchanged values at most this often
STALE_AFTER = 15 * 60  # unavailable when not heard for this long
START_TIMEOUT = 15


def _packed(raw: int) -> tuple[float, float]:
    """3-byte packed reading: temperature*10000 + humidity*10, bit 23 = negative."""
    neg = bool(raw & 0x800000)
    raw &= 0x7FFFFF
    temp = (raw // 1000) / 10
    return (-temp if neg else temp), (raw % 1000) / 10


def _model_from_name(name: str) -> str:
    m = re.search(r"H\d{4}", name or "")
    return m.group(0) if m else ""


def decode(name: str, manufacturer_data: dict[int, bytes]) -> dict | None:
    """Return {model, temperature, humidity, battery} or None if not a Govee reading."""
    model = _model_from_name(name)
    if MFR_8801 in manufacturer_data:
        d = manufacturer_data[MFR_8801]
        if len(d) >= 9:  # H5179
            return {"model": model or "H5179",
                    "temperature": int.from_bytes(d[4:6], "little", signed=True) / 100,
                    "humidity": int.from_bytes(d[6:8], "little") / 100,
                    "battery": d[8]}
    if MFR_EC88 in manufacturer_data:
        d = manufacturer_data[MFR_EC88]
        int16 = model in ("H5074", "H5051", "H5052", "H5071") or (not model and len(d) == 7)
        if int16 and len(d) >= 6:
            return {"model": model or "H5074",
                    "temperature": int.from_bytes(d[1:3], "little", signed=True) / 100,
                    "humidity": int.from_bytes(d[3:5], "little") / 100,
                    "battery": d[5]}
        if len(d) >= 5:  # H5072 / H5075
            t, h = _packed(int.from_bytes(d[1:4], "big"))
            return {"model": model or "H5075", "temperature": t, "humidity": h,
                    "battery": d[4] & 0x7F}
    if MFR_0001 in manufacturer_data:
        d = manufacturer_data[MFR_0001]
        # 0x0001 is a generic id; only trust it for named Govee devices
        if len(d) >= 6 and (model or (name or "").startswith(("GV", "Govee"))):
            t, h = _packed(int.from_bytes(d[2:5], "big"))
            return {"model": model or "H5101", "temperature": t, "humidity": h,
                    "battery": d[5] & 0x7F}
    return None


def _valid(r: dict) -> bool:
    return -40 <= r["temperature"] <= 80 and 0 <= r["humidity"] <= 100


class GoveePlugin(Plugin):
    name = "govee"
    title = "Govee sensors"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.configured = {k.upper(): v for k, v in (config.get("sensors") or {}).items()}
        self.known: dict = hub.store.get("govee.sensors", {})
        self.last_push: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self.scanner = None
        self.scanning = False
        self.adverts = 0

    @staticmethod
    def _eid(address: str) -> str:
        return "govee." + re.sub(r"[^0-9a-z]", "", address.lower())[-12:]

    async def start(self):
        # re-create remembered sensors now so HomeKit sees them before it starts
        for addr in set(self.known) | set(self.configured):
            self._ensure_entity(addr, self.known.get(addr, {}).get("model", ""))
        self.hub.spawn(self._scan_forever(), "govee scanner")
        self.hub.every(60, self._check_stale, "govee stale check")

    async def stop(self):
        if self.scanner and self.scanning:
            try:
                await self.scanner.stop()
            except Exception:
                pass

    def status(self):
        return {"scanning": self.scanning, "sensors": len(self.known),
                "adverts_seen": self.adverts}

    def _ensure_entity(self, addr: str, model: str) -> str:
        eid = self._eid(addr)
        if eid in self.hub.entities:
            return eid
        cfg = self.configured.get(addr, {})
        name = cfg.get("name") or self.known.get(addr, {}).get("name") or \
            f"Govee {model or 'sensor'} {addr.replace(':', '')[-4:]}"
        e = self.hub.add_entity(Entity(
            id=eid, name=name, kind="sensor", plugin=self.name, room=cfg.get("room", ""),
            available=False,
            attrs={"model": model or "Govee", "manufacturer": "Govee", "address": addr,
                   "measures": ["temperature", "humidity", "battery"]},
        ))
        if self.config.get("homekit", True):
            self.hub.homekit.expose(e)
        return eid

    # -- scanning ---------------------------------------------------------------------

    async def _scan_forever(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self.error = "bleak not installed"
            return
        delay = 30
        while True:
            kwargs = {}
            if self.config.get("adapter"):
                kwargs["adapter"] = self.config["adapter"]
            try:
                self.scanner = BleakScanner(self._on_advert, **kwargs)
                await asyncio.wait_for(self.scanner.start(), START_TIMEOUT)
                self.scanning, self.error, delay = True, None, 30
                self.log.info("Bluetooth scanning started")
                # restart the scanner if it silently stops delivering adverts
                last = -1
                while True:
                    await asyncio.sleep(600)
                    if self.adverts == last:
                        self.log.info("no Bluetooth adverts in 10 min, restarting scanner")
                        break
                    last = self.adverts
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.error = (f"Bluetooth unavailable: {err}" if str(err)
                              else "Bluetooth unavailable (no adapter or no permission)")
                self.log.warning("%s; retrying in %d s", self.error, delay)
            self.scanning = False
            try:
                await self.scanner.stop()
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)

    def _on_advert(self, device, adv):
        self.adverts += 1
        name = adv.local_name or device.name or ""
        if not adv.manufacturer_data:
            return
        reading = decode(name, adv.manufacturer_data)
        if reading is None or not _valid(reading):
            return
        addr = device.address.upper()
        if addr not in self.known and addr not in self.configured:
            if not self.config.get("auto_add", True):
                return
            self.log.info("new Govee %s at %s", reading["model"], addr)
        if self.known.get(addr, {}).get("model") != reading["model"]:
            self.known[addr] = {"model": reading["model"],
                                "name": self.known.get(addr, {}).get("name", "")}
            self.hub.store.set("govee.sensors", self.known)
        eid = self._ensure_entity(addr, reading["model"])
        now = time.time()
        self.last_seen[eid] = now
        e = self.hub.entities[eid]
        e.attrs["rssi"] = adv.rssi
        changed = any(e.state.get(k) != reading[k] for k in ("temperature", "humidity", "battery"))
        if changed or not e.available or now - self.last_push.get(eid, 0) > PUSH_EVERY:
            self.last_push[eid] = now
            self.hub.update(eid, available=True, temperature=reading["temperature"],
                            humidity=reading["humidity"], battery=reading["battery"])

    async def _check_stale(self):
        now = time.time()
        for eid, e in self.hub.entities.items():
            if e.plugin == self.name and e.available and \
                    now - self.last_seen.get(eid, 0) > STALE_AFTER:
                self.hub.update(eid, available=False)


PLUGIN = GoveePlugin
