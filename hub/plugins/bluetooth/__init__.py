"""Bluetooth LE sensors: listens for advertisements (nothing is paired or connected),
lists what it hears and records the values of the sensors you add.

    [plugins.bluetooth]
    auto_add = true        # add every recognised sensor heard
    homekit = true         # expose temperature/humidity sensors to the Home app
    stale_minutes = 30     # unavailable after this long without packets
    adapter = "hci0"       # Linux only, optional
    sensors = { "A4:C1:38:12:34:56" = { name = "Bagno", room = "Bagno" } }   # optional, legacy
"""

from __future__ import annotations

import asyncio
import re
import sys
import time

from aiohttp import web

from ...core import Entity, Plugin
from ...i18n import _
from .parsers import UNITS, parse, values

PUSH_EVERY = 60  # re-send unchanged values at most this often
START_TIMEOUT = 15
NEARBY_KEEP = 600  # forget unrecognised devices not heard for this long (s)
NEARBY_MAX = 400
RECOGNISED_KEEP = 3600


def eid_for(address: str) -> str:
    return "ble." + re.sub(r"[^0-9a-z]", "", address.lower())


class BluetoothPlugin(Plugin):
    name = "bluetooth"
    title = "Bluetooth sensors"
    description = "Detects nearby Bluetooth LE sensors (Govee, Xiaomi, SwitchBot, BTHome...) and records their values."
    icon = "bluetooth"
    ui = True
    schema = [
        {"key": "auto_add", "label": "Add sensors automatically", "type": "bool", "default": True,
         "help": "Add every recognised sensor that is heard. Turn off to pick them from the list."},
        {"key": "homekit", "label": "Expose to HomeKit", "type": "bool", "default": True,
         "help": "Temperature and humidity sensors show up in the Home app."},
        {"key": "stale_minutes", "label": "Unavailable after", "type": "number", "default": 30,
         "min": 1, "max": 1440, "unit": "min", "help": "Minutes without a packet before a sensor is marked unavailable."},
        {"key": "adapter", "label": "Adapter", "type": "text", "default": "", "advanced": True,
         "placeholder": "hci0", "help": "Linux only. Leave empty for the default adapter."},
        {"key": "active_scan", "label": "Active scan", "type": "bool", "default": False,
         "advanced": True, "help": "Use active scanning instead of passive (Linux)."},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.known: dict[str, dict] = {k: dict(v) for k, v in
                                       (hub.store.get("bluetooth.devices", {}) or {}).items()}
        for addr, cfg in (config.get("sensors") or {}).items():  # legacy [plugins.govee] style
            self.known.setdefault(addr.upper(), {"model": "", "name": cfg.get("name", ""),
                                                 "room": cfg.get("room", "")})
        self.ignored: set[str] = set(hub.store.get("bluetooth.ignored", []) or [])
        self.nearby: dict[str, dict] = {}
        self.last_push: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self.scanner = None
        self.scanning = False
        self.mode = ""
        self.adverts = 0

    # -- lifecycle -------------------------------------------------------------------

    async def start(self):
        # re-create remembered sensors now so HomeKit sees them before it starts
        for addr in self.known:
            self._ensure_entity(addr)
        self.hub.spawn(self._scan_forever(), "bluetooth scanner")
        self.hub.every(60, self._check_stale, "bluetooth stale check")

    async def stop(self):
        sc, self.scanner = self.scanner, None
        if sc is not None and self.scanning:
            try:
                await sc.stop()
            except Exception:
                pass
        self.scanning = False

    def status(self):
        return {"scanning": self.scanning, "mode": self.mode,
                "adapter": self.config.get("adapter") or "default",
                "devices_seen": len(self.nearby), "added": len(self.known),
                "adverts_seen": self.adverts}

    # -- entities ----------------------------------------------------------------------

    def _wants_homekit(self, addr: str, measures) -> bool:
        flag = self.known.get(addr, {}).get("homekit")
        if flag is None:
            flag = self.config.get("homekit", True)
        return bool(flag) and any(m in measures for m in ("temperature", "humidity"))

    def _ensure_entity(self, addr: str) -> str:
        eid = eid_for(addr)
        if eid in self.hub.entities:
            return eid
        k = self.known[addr]
        measures = k.get("measures") or ["temperature", "humidity", "battery"]
        tail = re.sub(r"[^0-9A-Za-z]", "", addr)[-4:]
        name = k.get("name") or f"{k.get('model') or 'Bluetooth sensor'} {tail}"
        e = self.hub.add_entity(Entity(
            id=eid, name=name, kind="sensor", plugin=self.name, room=k.get("room", ""),
            available=False,
            attrs={"model": k.get("model") or "", "manufacturer": k.get("manufacturer", ""),
                   "address": addr, "measures": list(measures),
                   "units": {m: UNITS[m] for m in measures if m in UNITS}}))
        if self._wants_homekit(addr, measures):
            self.hub.homekit.expose(e, eid)
        return eid

    def _save(self):
        self.hub.store.set("bluetooth.devices", self.known)

    def _add(self, addr: str, homekit: bool | None = None) -> bool:
        n = self.nearby.get(addr)
        if not n or not n["values"]:
            return False
        if addr not in self.known:
            self.known[addr] = {"model": n["model"], "name": ""}
            self.log.info("new sensor %s at %s", n["model"], addr)
        k = self.known[addr]
        k.update(model=n["model"], manufacturer=n["manufacturer"],
                 measures=sorted(n["values"]))
        if homekit is not None:
            k["homekit"] = bool(homekit)
        if addr in self.ignored:
            self.ignored.discard(addr)
            self.hub.store.set("bluetooth.ignored", sorted(self.ignored))
        self._save()
        eid = self._ensure_entity(addr)
        self._push(addr, n["values"], n.get("rssi"), force=True)
        return eid in self.hub.entities

    def _remove(self, addr: str) -> bool:
        if self.known.pop(addr, None) is None:
            return False
        self._save()
        self.hub.remove_entity(eid_for(addr))
        self.hub.homekit.remove(eid_for(addr))
        self.last_seen.pop(eid_for(addr), None)
        return True

    # -- advertisements --------------------------------------------------------------

    def _on_advert(self, device, adv):
        """bleak callback."""
        self.feed(device.address, adv.local_name or device.name or "", adv.rssi,
                  adv.manufacturer_data, adv.service_data)

    def feed(self, address, name, rssi, manufacturer_data=None, service_data=None):
        self.adverts += 1
        addr = address.upper()
        now = time.time()
        reading = parse(name, manufacturer_data, service_data)
        n = self.nearby.get(addr)
        if n is None:
            if reading is None and not name and addr not in self.known:
                return  # anonymous noise: not worth listing
            n = self.nearby[addr] = {"address": addr, "name": name, "model": "unknown",
                                     "manufacturer": "", "values": {}}
        n.update(rssi=rssi, last_seen=now)
        if name:
            n["name"] = name
        if reading is None:
            return
        vals = values(reading)
        n.update(model=reading["model"], manufacturer=reading.get("manufacturer", ""))
        n["values"] = {**n["values"], **vals}  # frames may carry different subsets
        if addr in self.ignored and addr not in self.known:
            return
        if addr not in self.known:
            if not self.config.get("auto_add", True):
                return
            self._add(addr)
            return
        k = self.known[addr]
        if k.get("model") != reading["model"] or set(vals) - set(k.get("measures") or []):
            k["model"] = reading["model"]
            k["manufacturer"] = reading.get("manufacturer", "")
            k["measures"] = sorted(set(k.get("measures") or []) | set(vals))
            self._save()
            e = self.hub.entities.get(eid_for(addr))
            if e:
                e.attrs.update(model=k["model"], manufacturer=k["manufacturer"],
                               measures=k["measures"],
                               units={m: UNITS[m] for m in k["measures"] if m in UNITS})
        self._ensure_entity(addr)
        self._push(addr, vals, rssi)

    def _push(self, addr, vals, rssi, force=False):
        eid = eid_for(addr)
        e = self.hub.entities.get(eid)
        if e is None:
            return
        now = time.time()
        self.last_seen[eid] = now
        e.attrs["rssi"] = rssi
        changed = any(e.state.get(k) != v for k, v in vals.items())
        if force or changed or not e.available or now - self.last_push.get(eid, 0) > PUSH_EVERY:
            self.last_push[eid] = now
            self.hub.update(eid, available=True, **vals)

    async def _check_stale(self):
        now = time.time()
        limit = float(self.config.get("stale_minutes", 30)) * 60
        for eid, e in self.hub.entities.items():
            if e.plugin == self.name and e.available and now - self.last_seen.get(eid, 0) > limit:
                self.hub.update(eid, available=False)
        for addr, n in list(self.nearby.items()):
            age = now - n["last_seen"]
            if addr not in self.known and age > (RECOGNISED_KEEP if n["values"] else NEARBY_KEEP):
                del self.nearby[addr]
        if len(self.nearby) > NEARBY_MAX:  # keep the strongest
            keep = sorted(self.nearby.values(), key=lambda n: n.get("rssi") or -999, reverse=True)
            for n in keep[NEARBY_MAX:]:
                if n["address"] not in self.known:
                    self.nearby.pop(n["address"], None)

    # -- scanning -----------------------------------------------------------------------

    def _make_scanner(self, BleakScanner, passive: bool):
        kw: dict = {}
        if self.config.get("adapter"):
            kw["adapter"] = self.config["adapter"]
        if passive:
            from bleak.assigned_numbers import AdvertisementDataType as T
            from bleak.backends.bluezdbus.advertisement_monitor import OrPattern
            kw["scanning_mode"] = "passive"
            kw["bluez"] = {"or_patterns": [OrPattern(0, T.FLAGS, bytes([f]))
                                           for f in (0x01, 0x02, 0x04, 0x05, 0x06, 0x1A, 0x18)]}
        return BleakScanner(self._on_advert, **kw)

    def _friendly(self, err: Exception) -> str:
        msg = str(err).strip()
        low = msg.lower()
        if sys.platform == "darwin" and any(w in low for w in
                                            ("authoriz", "permission", "denied", "not allowed")):
            return _("Bluetooth permission denied: allow this app (or your terminal) in "
                     "System Settings > Privacy & Security > Bluetooth, then reload the plugin.")
        if "turned off" in low or "powered off" in low:
            return _("Bluetooth is turned off.")
        return _("Bluetooth unavailable: {msg}", msg=msg) if msg else \
            _("Bluetooth unavailable (no adapter or no permission)")

    async def _scan_forever(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self.error = _("bleak is not installed (pip install bleak)")
            return
        delay = 15
        passive_ok = sys.platform.startswith("linux") and not self.config.get("active_scan", False)
        while True:
            passive = passive_ok
            self.error = None
            try:
                try:
                    self.scanner = self._make_scanner(BleakScanner, passive)
                    await asyncio.wait_for(self.scanner.start(), START_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    if not passive:
                        raise
                    self.log.info("passive scan not available (%s), using active", err)
                    passive_ok = passive = False
                    self.scanner = self._make_scanner(BleakScanner, False)
                    await asyncio.wait_for(self.scanner.start(), START_TIMEOUT)
                self.scanning, delay = True, 15
                self.mode = "passive" if passive else "active"
                self.log.info("Bluetooth scanning started (%s)", self.mode)
                # restart if the scanner silently stops delivering adverts
                last = self.adverts
                while True:
                    await asyncio.sleep(300 if passive else 600)
                    if self.adverts == last:
                        self.log.info("no Bluetooth adverts, restarting scanner")
                        passive_ok = False  # passive may be what is failing
                        break
                    last = self.adverts
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self.error = _("Bluetooth did not start (timeout; permission prompt pending?)")
            except Exception as err:
                self.error = self._friendly(err)
            if self.error:
                self.log.warning("%s; retrying in %d s", self.error, delay)
            self.scanning = False
            try:
                if self.scanner:
                    await self.scanner.stop()
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    # -- HTTP -----------------------------------------------------------------------------

    def routes(self):
        return [web.get("/nearby", self.r_nearby), web.post("/add", self.r_add),
                web.post("/remove", self.r_remove), web.post("/ignore", self.r_ignore)]

    def nearby_list(self) -> list[dict]:
        now = time.time()
        out = []
        for addr, n in self.nearby.items():
            out.append({
                "address": addr, "name": n["name"], "model": n["model"],
                "manufacturer": n["manufacturer"], "rssi": n.get("rssi"),
                "last_seen": n["last_seen"], "age": round(now - n["last_seen"], 1),
                "values": n["values"], "units": {k: UNITS[k] for k in n["values"] if k in UNITS},
                "recognised": bool(n["values"]), "added": addr in self.known,
                "ignored": addr in self.ignored,
                "entity": eid_for(addr) if addr in self.known else None,
                "homekit": addr in self.known and self._wants_homekit(
                    addr, self.known[addr].get("measures") or ["temperature"]),
            })
        for addr, k in self.known.items():  # added but not heard since start
            if addr not in self.nearby:
                out.append({"address": addr, "name": k.get("name", ""),
                            "model": k.get("model") or "unknown",
                            "manufacturer": k.get("manufacturer", ""), "rssi": None,
                            "last_seen": None, "age": None, "values": {}, "units": {},
                            "recognised": True, "added": True, "ignored": False,
                            "entity": eid_for(addr), "homekit": False})
        out.sort(key=lambda d: (d["ignored"], not d["recognised"],
                                -(d["rssi"] if d["rssi"] is not None else -999)))
        return out

    async def r_nearby(self, request):
        return web.json_response({"scanning": self.scanning, "mode": self.mode,
                                  "error": self.error, "devices": self.nearby_list()})

    async def _addr(self, request) -> tuple[str, dict]:
        try:
            body = await request.json()
            return str(body["address"]).upper(), body
        except Exception:
            raise ValueError("address required")

    async def r_add(self, request):
        addr, body = await self._addr(request)
        if not self._add(addr, body.get("homekit")):
            raise ValueError("device not recognised (no readings heard yet)")
        return web.json_response({"ok": True, "entity": eid_for(addr)})

    async def r_remove(self, request):
        addr, _ = await self._addr(request)
        if not self._remove(addr):
            raise ValueError("not added")
        return web.json_response({"ok": True})

    async def r_ignore(self, request):
        addr, body = await self._addr(request)
        if body.get("ignore", True):
            self.ignored.add(addr)
        else:
            self.ignored.discard(addr)
        self.hub.store.set("bluetooth.ignored", sorted(self.ignored))
        return web.json_response({"ok": True})


PLUGIN = BluetoothPlugin
