"""Ariston NET boilers (cloud API through the ``ariston`` library).

Entities: one ``climate`` per heating zone (off / heat / auto = time program)
and one ``water_heater`` for domestic hot water. Both are exposed to HomeKit as
thermostats, exactly like the original standalone bridge.

    [plugins.ariston]
    username = "..."
    password = "..."
    gateway = ""              # empty = first boiler on the account
    poll_interval = 120       # seconds, >= 60 (Ariston rate-limits)
    expose_hot_water = true
    zone_names = { "1" = "Heating" }
    room = "Casa"
"""

from __future__ import annotations

import asyncio
import time

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_THERMOSTAT

from ..core import Entity, Plugin

HK_OFF, HK_HEAT, HK_COOL, HK_AUTO = 0, 1, 2, 3
MODE_TO_HK = {"off": HK_OFF, "heat": HK_HEAT, "auto": HK_AUTO}
HK_TO_MODE = {v: k for k, v in MODE_TO_HK.items()}


def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AristonPlugin(Plugin):
    name = "ariston"
    title = "Ariston"

    # the cloud needs time to push a change to the boiler and back; ignore
    # polls for a while after a write so the UI doesn't flip back
    WRITE_GRACE = 60

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.device = None
        self.lock = asyncio.Lock()
        self._last_write = 0.0
        self.poll_interval = max(60, int(config.get("poll_interval", 120)))
        self.layout: dict = hub.store.get("ariston.layout") or {}

    # -- lifecycle -------------------------------------------------------------

    async def start(self):
        if not self.config.get("username") or not self.config.get("password"):
            raise ValueError("username/password missing in [plugins.ariston]")
        try:
            await self._connect()
        except Exception as err:
            if not self.layout:
                raise
            # use the cached layout so HomeKit keeps the accessories
            self.log.warning("Ariston offline (%s); using cached layout, will retry", err)
            self.error = f"offline: {err}"
        self._create_entities()
        self.hub.every(self.poll_interval, self.refresh, "ariston poll")

    async def _connect(self):
        from ariston import Ariston, AristonGalevoDevice, AristonVelisBaseDevice

        api = Ariston()
        if not await api.async_connect(self.config["username"], self.config["password"]):
            raise PermissionError("Ariston login failed - check username/password")
        devices = await api.async_discover()
        if not devices:
            raise LookupError("no devices on this Ariston NET account")
        gw = self.config.get("gateway") or devices[0]["gw"]
        device = await api.async_hello(gw, is_metric=True)
        if device is None:
            raise LookupError(f"device {gw} is not supported by the ariston library")
        await device.async_get_features()
        await device.async_update_state()
        self.device = device
        self.error = None

        layout = {"gateway": device.gateway, "zones": [], "dhw": None,
                  "galevo": isinstance(device, AristonGalevoDevice)}
        if layout["galevo"]:
            for z in device.zone_numbers:
                layout["zones"].append({
                    "zone": z,
                    "min": _num(device.get_comfort_temp_min(z), 10),
                    "max": _num(device.get_comfort_temp_max(z), 30),
                    "step": _num(device.get_comfort_temp_step(z), 0.5) or 0.5,
                })
        else:
            self.log.warning("device type %s: heating zones not supported, hot water only",
                             type(device).__name__)
        velis = isinstance(device, AristonVelisBaseDevice)
        if device.has_dhw or velis:
            layout["dhw"] = {
                "min": _num(device.water_heater_minimum_temperature, 35),
                "max": _num(device.water_heater_maximum_temperature, 65),
                "step": _num(device.water_heater_temperature_step, 1) or 1,
                "velis": velis,
            }
        if layout != self.layout:
            self.layout = layout
            self.hub.store.set("ariston.layout", layout)
        self.log.info("connected to gateway %s (%d zones, hot water: %s)", gw,
                      len(layout["zones"]), bool(layout["dhw"]))

    def _create_entities(self):
        room = self.config.get("room", "")
        zones = self.layout.get("zones", [])
        names = self.config.get("zone_names", {})
        for zl in zones:
            z = zl["zone"]
            eid = f"ariston.zone{z}"
            name = names.get(str(z)) or ("Heating" if len(zones) == 1 else f"Heating Zone {z}")
            self.hub.add_entity(Entity(
                id=eid, name=name, kind="climate", plugin=self.name, room=room,
                attrs={"min": zl["min"], "max": zl["max"], "step": zl["step"],
                       "modes": ["off", "heat", "auto"], "manufacturer": "Ariston",
                       "model": "Ariston NET zone"},
                actions={
                    "set_temperature": lambda value, z=z: self.set_zone_temp(z, value),
                    "set_mode": lambda mode, z=z: self.set_zone_mode(z, mode),
                },
            ))
            self.hub.homekit.add(eid, lambda d, eid=eid: AristonThermostat(d, self.hub, eid))

        dhw = self.layout.get("dhw")
        if dhw and self.config.get("expose_hot_water", True):
            self.hub.add_entity(Entity(
                id="ariston.dhw", name=self.config.get("hot_water_name", "Hot Water"),
                kind="water_heater", plugin=self.name, room=room,
                attrs={"min": dhw["min"], "max": dhw["max"], "step": dhw["step"],
                       "modes": ["off", "heat"] if dhw["velis"] else ["heat"],
                       "manufacturer": "Ariston", "model": "Ariston NET hot water"},
                actions={"set_temperature": self.set_dhw_temp, "set_mode": self.set_dhw_mode},
            ))
            self.hub.homekit.add("ariston.dhw",
                                 lambda d: AristonThermostat(d, self.hub, "ariston.dhw"))
        self._push_state()

    def status(self):
        return {"gateway": self.layout.get("gateway"), "poll_interval": self.poll_interval,
                "online": self.device is not None}

    # -- polling ----------------------------------------------------------------

    async def refresh(self):
        if self.device is None:
            await self._connect()  # raises -> logged by hub.every, retried next time
        if time.monotonic() - self._last_write < self.WRITE_GRACE:
            return
        async with self.lock:
            try:
                await self.device.async_update_state()
            except Exception:
                for eid in self._entity_ids():
                    self.hub.update(eid, available=False)
                raise
        self._push_state()

    def _entity_ids(self):
        return [f"ariston.zone{z['zone']}" for z in self.layout.get("zones", [])] + ["ariston.dhw"]

    def _push_state(self):
        dev = self.device
        if dev is None:
            for eid in self._entity_ids():
                self.hub.update(eid, available=False)
            return
        from ariston.const import ZoneMode

        for zl in self.layout.get("zones", []):
            z = zl["zone"]
            if not dev.is_plant_in_heat_mode or dev.get_zone_mode(z) == ZoneMode.OFF:
                mode = "off"
            elif dev.is_zone_in_time_program_mode(z):
                mode = "auto"
            else:
                mode = "heat"
            self.hub.update(
                f"ariston.zone{z}", available=True,
                current_temperature=_num(dev.get_measured_temp_value(z), None),
                target_temperature=_num(dev.get_comfort_temp_value(z), None),
                mode=mode,
                heating=mode != "off" and bool(dev.get_zone_heat_request_value(z)),
            )

        dhw = self.layout.get("dhw")
        if dhw:
            on = bool(dev.water_heater_power_value) if dhw["velis"] else True
            heating = getattr(dev, "is_heating", None)
            if heating is None or self.layout.get("galevo"):
                heating = False  # flame flag can't tell DHW from heating; stay quiet
            self.hub.update(
                "ariston.dhw", available=True,
                current_temperature=_num(dev.water_heater_current_temperature, None),
                target_temperature=_num(dev.water_heater_target_temperature, None),
                mode="heat" if on else "off",
                heating=on and bool(heating),
            )

    # -- writes -----------------------------------------------------------------

    async def _write(self, coro_fn, *args):
        if self.device is None:
            raise ConnectionError("Ariston is offline")
        async with self.lock:
            await coro_fn(*args)
            self._last_write = time.monotonic()
            self.log.info("%s%s ok", coro_fn.__name__, args)

    async def set_zone_temp(self, zone: int, value):
        await self._write(self.device.async_set_comfort_temp, float(value), zone)
        self.hub.update(f"ariston.zone{zone}", target_temperature=float(value))

    async def set_zone_mode(self, zone: int, mode: str):
        from ariston.const import PlantMode, ZoneMode

        dev = self.device
        if dev is None:
            raise ConnectionError("Ariston is offline")
        plant_opts = dev.plant_mode_options or []
        zone_opts = dev.get_zone_mode_options(zone) or []

        if mode == "off":
            if ZoneMode.OFF.value in zone_opts and len(dev.zone_numbers) > 1:
                # several zones: switch only this one off
                await self._write(dev.async_set_zone_mode, ZoneMode.OFF, zone)
            elif PlantMode.SUMMER.value in plant_opts:
                # heating off, hot water stays on
                await self._write(dev.async_set_plant_mode, PlantMode.SUMMER)
            elif PlantMode.OFF.value in plant_opts:
                await self._write(dev.async_set_plant_mode, PlantMode.OFF)
            elif ZoneMode.OFF.value in zone_opts:
                await self._write(dev.async_set_zone_mode, ZoneMode.OFF, zone)
        else:
            if not dev.is_plant_in_heat_mode:
                for pm in (PlantMode.WINTER, PlantMode.HEATING_ONLY):
                    if pm.value in plant_opts:
                        await self._write(dev.async_set_plant_mode, pm)
                        break
            want = ZoneMode.TIME_PROGRAM if mode == "auto" else ZoneMode.MANUAL
            if want.value in zone_opts and dev.get_zone_mode(zone) != want:
                await self._write(dev.async_set_zone_mode, want, zone)
        self.hub.update(f"ariston.zone{zone}", mode=mode)

    async def set_dhw_temp(self, value):
        await self._write(self.device.async_set_water_heater_temperature, float(value))
        self.hub.update("ariston.dhw", target_temperature=float(value))

    async def set_dhw_mode(self, mode: str):
        if not self.layout.get("dhw", {}).get("velis"):
            return  # combi boilers: hot water can't be switched off on its own
        await self._write(self.device.async_set_power, mode != "off")
        self.hub.update("ariston.dhw", mode=mode)


class AristonThermostat(Accessory):
    """HomeKit thermostat mirroring an Ariston climate / water_heater entity."""

    category = CATEGORY_THERMOSTAT

    def __init__(self, driver, hub, entity_id: str):
        self.hub, self.entity_id = hub, entity_id
        e = hub.entities[entity_id]
        super().__init__(driver, e.name)
        suffix = entity_id.split(".")[1].replace("zone", "z")  # same serials as the old bridge
        self.set_info_service(manufacturer="Ariston", model=e.attrs["model"],
                              serial_number=f"{hub.plugins['ariston'].layout.get('gateway')}-{suffix}")
        hot_water = e.kind == "water_heater"
        valid = {m.title(): MODE_TO_HK[m] for m in e.attrs["modes"]}
        svc = self.add_preload_service("Thermostat", chars=["Name"])
        svc.configure_char("Name", value=e.name)
        self.c_cur_state = svc.configure_char(
            "CurrentHeatingCoolingState", valid_values={"Off": 0, "Heat": 1})
        if "off" not in e.attrs["modes"]:
            # default is Off, which isn't valid for heat-only; set it first
            svc.get_characteristic("TargetHeatingCoolingState").value = HK_HEAT
        self.c_tgt_state = svc.configure_char(
            "TargetHeatingCoolingState", valid_values=valid, setter_callback=self._set_mode)
        self.c_cur_temp = svc.configure_char(
            "CurrentTemperature",
            properties={"minValue": 0 if hot_water else -20, "maxValue": 100 if hot_water else 60})
        self.c_tgt_temp = svc.configure_char(
            "TargetTemperature",
            properties={"minValue": e.attrs["min"], "maxValue": e.attrs["max"],
                        "minStep": e.attrs["step"]},
            setter_callback=self._set_temp)
        svc.configure_char("TemperatureDisplayUnits", value=0)
        hub.listeners.append(lambda ent: ent.id == entity_id and self.sync(ent))
        self.sync(e)

    def sync(self, e):
        st = e.state
        if st.get("current_temperature") is not None:
            self.c_cur_temp.set_value(st["current_temperature"])
        if st.get("target_temperature") is not None:
            p = self.c_tgt_temp.properties
            self.c_tgt_temp.set_value(min(max(st["target_temperature"], p["minValue"]),
                                          p["maxValue"]))
        if st.get("mode") in e.attrs["modes"]:
            self.c_tgt_state.set_value(MODE_TO_HK[st["mode"]])
        self.c_cur_state.set_value(HK_HEAT if st.get("heating") else HK_OFF)

    def _set_temp(self, value):
        self.driver.async_add_job(self.hub.call, self.entity_id, "set_temperature",
                                  value=float(value))

    def _set_mode(self, value):
        self.driver.async_add_job(self.hub.call, self.entity_id, "set_mode",
                                  mode=HK_TO_MODE[value])


PLUGIN = AristonPlugin
