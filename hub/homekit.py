"""One HomeKit bridge for the whole hub (HAP-python, sharing the hub's loop).

Plugins either register a custom accessory with ``homekit.add(uid, factory)``
or call ``homekit.expose(entity)`` to get a generic accessory for sensor /
light / switch entities that follows the entity's state automatically.

Accessory IDs (AIDs) are persisted per ``uid`` in data/homekit.aids.json, so
the Home app keeps rooms and automations even when plugins are added, removed
or start in a different order.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Callable

from pyhap import util as hap_util
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB, CATEGORY_SENSOR, CATEGORY_SWITCH
from zeroconf import InterfaceChoice

log = logging.getLogger("hub.homekit")

Factory = Callable[[AccessoryDriver], Accessory]


class HomeKit:
    def __init__(self, hub, cfg: dict, data_dir: Path):
        self.hub = hub
        self.enabled = cfg.get("enabled", True)
        self.port = int(cfg.get("port", 51826))
        self.name = cfg.get("name", "Casa")
        self.state_file = data_dir / "homekit.state"
        self.pairing_file = data_dir / "homekit.pairing.json"
        self.aids_file = data_dir / "homekit.aids.json"
        self.aids: dict[str, int] = (
            json.loads(self.aids_file.read_text()) if self.aids_file.exists() else {}
        )
        self.pending: list[tuple[str, Factory]] = []
        self.driver: AccessoryDriver | None = None
        self.bridge: Bridge | None = None
        self.uids: set[str] = set()
        self.owners: dict[str, str] = {}  # uid -> plugin that registered it

    # -- registration -------------------------------------------------------------

    def _aid(self, uid: str) -> int:
        if uid not in self.aids:
            used = set(self.aids.values())
            # HAP-python skips 7 (iOS bug, issue #61); keep doing the same
            self.aids[uid] = next(a for a in range(2, 10_000) if a not in used and a != 7)
            self.aids_file.write_text(json.dumps(self.aids, indent=2))
        return self.aids[uid]

    def add(self, uid: str, factory: Factory) -> None:
        """Register an accessory. Before start it's queued; afterwards it is
        added live and the Home app is told the bridge changed."""
        if not self.enabled or uid in self.uids:
            return
        from .core import current_plugin

        self.uids.add(uid)
        if current_plugin.get():
            self.owners[uid] = current_plugin.get()
        if self.driver is None:
            self.pending.append((uid, factory))
            return
        self._attach(uid, factory)
        self.driver.config_changed()

    def expose(self, entity, uid: str | None = None) -> None:
        """Generic accessory for sensor / light / switch entities."""
        maker = {"sensor": SensorAccessory, "light": LightAccessory,
                 "switch": SwitchAccessory}.get(entity.kind)
        if maker is None:
            log.warning("no generic HomeKit mapping for %s (%s)", entity.id, entity.kind)
            return
        self.add(uid or entity.id, lambda driver: maker(driver, self.hub, entity.id))

    def remove(self, uid: str) -> None:
        """Take one accessory off the bridge (its AID stays reserved)."""
        if uid in self.uids:
            self._detach([uid])

    def remove_owned(self, plugin: str) -> None:
        """Take a disabled plugin's accessories off the bridge. Their AIDs stay
        reserved, so re-enabling the plugin restores rooms and automations."""
        uids = [u for u, owner in self.owners.items() if owner == plugin]
        if uids:
            self._detach(uids)

    def _detach(self, uids: list[str]) -> None:
        for uid in uids:
            self.uids.discard(uid)
            self.owners.pop(uid, None)
            self.pending = [p for p in self.pending if p[0] != uid]
            if self.bridge is not None:
                acc = self.bridge.accessories.pop(self.aids.get(uid), None)
                if acc is not None:
                    self.hub.listeners[:] = [cb for cb in self.hub.listeners
                                             if getattr(cb, "__self__", None) is not acc]
        if self.driver is not None:
            self.driver.config_changed()

    def _attach(self, uid: str, factory: Factory) -> None:
        try:
            acc = factory(self.driver)
        except Exception:
            log.exception("could not create HomeKit accessory %s", uid)
            return
        acc.aid = self._aid(uid)
        self.bridge.add_accessory(acc)

    # -- lifecycle -------------------------------------------------------------

    def _pairing(self) -> dict:
        if self.pairing_file.exists():
            return json.loads(self.pairing_file.read_text())
        pairing = {
            "pincode": hap_util.generate_pincode().decode(),
            "setup_id": hap_util.generate_setup_id(),
        }
        self.pairing_file.write_text(json.dumps(pairing, indent=2))
        self.pairing_file.chmod(0o600)
        return pairing

    async def start(self) -> None:
        if not self.enabled:
            return
        pairing = self._pairing()
        self.driver = AccessoryDriver(
            port=self.port,
            persist_file=str(self.state_file),
            pincode=pairing["pincode"].encode(),
            loop=asyncio.get_running_loop(),
            interface_choice=InterfaceChoice.Default,  # not VPN / docker interfaces
        )
        # HAP-python makes a new setup id at every start; keep it stable so a
        # printed QR code stays valid
        self.driver.state.setup_id = pairing["setup_id"]
        self.bridge = Bridge(self.driver, self.name)
        self.bridge.set_info_service(manufacturer="Casa", model="Home hub bridge")
        # attach in AID order so the bridge layout is stable
        for uid, factory in sorted(self.pending, key=lambda p: self._aid(p[0])):
            self._attach(uid, factory)
        self.pending.clear()
        self.driver.add_accessory(self.bridge)
        await self.driver.async_start()
        log.info("HomeKit bridge '%s' on port %s with %d accessories, setup code %s",
                 self.name, self.port, len(self.bridge.accessories),
                 self.driver.state.pincode.decode())

    async def stop(self) -> None:
        if self.driver and self.driver.aio_stop_event:
            await self.driver.async_stop()

    def info(self) -> dict:
        if not self.driver:
            return {"enabled": self.enabled, "running": False}
        return {
            "enabled": True,
            "running": True,
            "name": self.name,
            "paired": self.driver.state.paired,
            "pincode": self.driver.state.pincode.decode(),
            "setup_uri": self.bridge.xhm_uri() if self.bridge else "",
            "accessories": sorted(a.display_name for a in self.bridge.accessories.values()),
        }

    def qr_svg(self) -> str:
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(self.bridge.xhm_uri(), image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode()


# -- generic accessories ------------------------------------------------------------


class _EntityAccessory(Accessory):
    """Mirrors one hub entity; ``sync`` runs on every entity change."""

    def __init__(self, driver, hub, entity_id: str):
        self.hub = hub
        self.entity_id = entity_id
        e = hub.entities[entity_id]
        super().__init__(driver, e.name[:64])
        self.set_info_service(
            manufacturer=e.attrs.get("manufacturer", e.plugin.title()),
            model=e.attrs.get("model", e.kind),
            serial_number=e.id[:64],
        )
        self.build(e)
        hub.listeners.append(self._on_change)
        self.sync(e)

    def _on_change(self, e) -> None:
        if e.id == self.entity_id:
            self.sync(e)

    def build(self, e) -> None:
        raise NotImplementedError

    def sync(self, e) -> None:
        raise NotImplementedError

    def call(self, action: str, **params) -> None:
        self.driver.async_add_job(self.hub.call, self.entity_id, action, **params)


class SensorAccessory(_EntityAccessory):
    category = CATEGORY_SENSOR

    def build(self, e):
        self.c_temp = self.c_hum = self.c_batt = self.c_low = None
        if "temperature" in e.state or "temperature" in e.attrs.get("measures", []):
            s = self.add_preload_service("TemperatureSensor")
            self.c_temp = s.configure_char("CurrentTemperature",
                                           properties={"minValue": -40, "maxValue": 100})
        if "humidity" in e.state or "humidity" in e.attrs.get("measures", []):
            s = self.add_preload_service("HumiditySensor")
            self.c_hum = s.get_characteristic("CurrentRelativeHumidity")
        if "battery" in e.state or "battery" in e.attrs.get("measures", []):
            s = self.add_preload_service("BatteryService", chars=["BatteryLevel", "StatusLowBattery"])
            self.c_batt = s.get_characteristic("BatteryLevel")
            self.c_low = s.get_characteristic("StatusLowBattery")

    def sync(self, e):
        st = e.state
        if self.c_temp is not None and isinstance(st.get("temperature"), (int, float)):
            self.c_temp.set_value(round(st["temperature"], 1))
        if self.c_hum is not None and isinstance(st.get("humidity"), (int, float)):
            self.c_hum.set_value(max(0, min(100, round(st["humidity"], 1))))
        if self.c_batt is not None and isinstance(st.get("battery"), (int, float)):
            self.c_batt.set_value(int(max(0, min(100, st["battery"]))))
            self.c_low.set_value(1 if st["battery"] < 15 else 0)


class LightAccessory(_EntityAccessory):
    category = CATEGORY_LIGHTBULB

    def build(self, e):
        chars = ["On"]
        if "brightness" in e.state:
            chars.append("Brightness")
        if "color_temp" in e.state:
            chars.append("ColorTemperature")
        s = self.add_preload_service("Lightbulb", chars=chars)
        self.c_on = s.configure_char("On", setter_callback=self._set_on)
        self.c_bri = (s.configure_char("Brightness", setter_callback=self._set_bri)
                      if "brightness" in e.state else None)
        self.c_ct = None
        if "color_temp" in e.state:
            lo, hi = e.attrs.get("color_temp_range", (2200, 4000))
            # HomeKit uses mireds
            self.c_ct = s.configure_char(
                "ColorTemperature",
                properties={"minValue": int(1e6 / hi), "maxValue": int(1e6 / lo)},
                setter_callback=self._set_ct,
            )

    def sync(self, e):
        self.c_on.set_value(bool(e.state.get("on")))
        if self.c_bri is not None and e.state.get("brightness") is not None:
            self.c_bri.set_value(int(e.state["brightness"]))
        if self.c_ct is not None and e.state.get("color_temp"):
            p = self.c_ct.properties
            self.c_ct.set_value(max(p["minValue"], min(p["maxValue"],
                                                       int(1e6 / e.state["color_temp"]))))

    def _set_on(self, v):
        self.call("turn_on" if v else "turn_off")

    def _set_bri(self, v):
        self.call("turn_on", brightness=int(v))

    def _set_ct(self, v):
        self.call("turn_on", color_temp=int(1e6 / v))


class SwitchAccessory(_EntityAccessory):
    category = CATEGORY_SWITCH

    def build(self, e):
        s = self.add_preload_service("Switch")
        self.c_on = s.configure_char("On", setter_callback=self._set_on)

    def sync(self, e):
        self.c_on.set_value(bool(e.state.get("on")))

    def _set_on(self, v):
        self.call("turn_on" if v else "turn_off")
