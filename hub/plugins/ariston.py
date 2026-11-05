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
    gas_price = 1.10          # EUR per standard m3 of gas: put your own tariff
    gas_kwh_per_m3 = 10.69    # energy of 1 Smc (used to convert kWh <-> m3)
    energy_interval = 45      # minutes between reads of the boiler's consumption data
    latitude = 45.4           # for outdoor temperature (Open-Meteo); both empty = boiler probe only
    longitude = 11.9
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
from aiohttp import web

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_THERMOSTAT

from ..core import Entity, Plugin
from ..heating import GAS_KWH_PER_M3, Heating
from ..i18n import _

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
    description = "Ariston NET boiler: heating zones and domestic hot water"
    icon = "flame"
    schema = [
        {"key": "username", "label": "Ariston NET email", "type": "text", "required": True, "help": "Account e-mail for ariston.net"},
        {"key": "password", "label": "Ariston NET password", "type": "password", "required": True, "link": {"url": "https://www.ariston-net.remotethermo.com", "label": "Ariston NET"}},
        {"key": "gateway", "label": "Gateway ID", "type": "text", "placeholder": "empty = first boiler on the account", "advanced": True},
        {"key": "poll_interval", "label": "Poll interval", "type": "number", "default": 120, "min": 60, "unit": "s", "advanced": True},
        {"key": "expose_hot_water", "label": "Expose hot water", "type": "bool", "default": True, "help": "Show domestic hot water as a thermostat"},
        {"key": "hot_water_name", "label": "Hot water name", "type": "text", "default": "Hot Water", "advanced": True},
        {"key": "room", "label": "Room", "type": "text"},
        {"key": "gas_price", "label": "Gas price", "type": "number", "default": 1.10, "min": 0, "step": 0.01,
         "unit": "€/Smc", "help": "Your tariff per standard cubic metre of gas, used for the cost estimates in Heating"},
        {"key": "gas_kwh_per_m3", "label": "Gas energy content", "type": "number", "default": 10.69, "min": 5,
         "max": 15, "step": 0.01, "unit": "kWh/Smc", "advanced": True,
         "help": "Converts the boiler's kWh into cubic metres; see your gas bill (PCS)"},
        {"key": "energy_interval", "label": "Consumption read interval", "type": "number", "default": 45,
         "min": 30, "unit": "min", "advanced": True, "help": "How often the gas use is read from the Ariston cloud"},
        {"key": "latitude", "label": "Latitude", "type": "number", "step": 0.01, "advanced": True,
         "help": "Home location for the outdoor temperature and degree-days (Open-Meteo, no key). Empty = use the boiler's outdoor probe"},
        {"key": "longitude", "label": "Longitude", "type": "number", "step": 0.01, "advanced": True},
    ]

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
        self.heat = Heating(hub.store, _num(config.get("gas_price"), 1.10),
                            _num(config.get("gas_kwh_per_m3"), GAS_KWH_PER_M3) or GAS_KWH_PER_M3)
        self.energy_interval = max(30, int(_num(config.get("energy_interval"), 45)))

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
            self.error = _("Offline: {err}", err=err)
        self._create_entities()
        self.hub.every(self.poll_interval, self.refresh, "ariston poll")
        self.hub.every(self.energy_interval * 60, self.refresh_energy, "ariston energy", initial_delay=45)
        self.hub.every(3 * 3600, self.refresh_weather, "ariston weather", initial_delay=20)

    async def _connect(self):
        from ariston import Ariston, AristonGalevoDevice, AristonVelisBaseDevice

        api = Ariston()
        if not await api.async_connect(self.config["username"], self.config["password"]):
            raise PermissionError(_("Ariston login failed - check username/password"))
        devices = await api.async_discover()
        if not devices:
            raise LookupError(_("No devices on this Ariston NET account"))
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
            name = names.get(str(z)) or (_("Heating") if len(zones) == 1 else _("Heating Zone {z}", z=z))
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
                id="ariston.dhw", name=self.config.get("hot_water_name") or _("Hot Water"),
                kind="water_heater", plugin=self.name, room=room,
                attrs={"min": dhw["min"], "max": dhw["max"], "step": dhw["step"],
                       "modes": ["off", "heat"] if dhw["velis"] else ["heat"],
                       "manufacturer": "Ariston", "model": "Ariston NET hot water"},
                actions={"set_temperature": self.set_dhw_temp, "set_mode": self.set_dhw_mode},
            ))
            self.hub.homekit.add("ariston.dhw",
                                 lambda d: AristonThermostat(d, self.hub, "ariston.dhw"))
        self.hub.add_entity(Entity(
            id="ariston.energy", name=_("Heating use"), kind="sensor", plugin=self.name, room=room,
            attrs={"hidden": True, "group": _("Heating"), "icon": "flame",
                   "units": {"heating_kwh_today": "kWh", "dhw_kwh_today": "kWh", "heating_hours_today": "h",
                             "burner_hours_today": "h", "outdoor_temp": "°C"}},
        ))
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
            except Exception as err:
                for eid in self._entity_ids():
                    self.hub.update(eid, available=False)
                self.heat.error, self.heat.error_ts = str(err), time.time()
                raise
        self.heat.error = None
        self._push_state()
        self._observe()

    # -- heating log and consumption ---------------------------------------------------------

    def _observe(self):
        """Feed the heating log with what the boiler just reported."""
        dev = self.device
        if dev is None or not self.layout.get("zones"):
            return
        try:
            zones = [self.hub.entities.get(f"ariston.zone{z['zone']}") for z in self.layout["zones"]]
            zones = [e for e in zones if e is not None]
            if not zones:
                return
            st = zones[0].state

            def prop(name):
                try:
                    v = getattr(dev, name)
                except Exception:
                    return None
                return None if v is None else bool(v)

            outdoor = None
            try:
                outdoor = _num(dev.outside_temp_value, None)
            except Exception:
                pass
            now = time.time()
            self.heat.observe(
                now, heat_req=any(e.state.get("heating") for e in zones), flame=prop("is_flame_on_value"),
                pump=prop("is_heating_pump_on_value"), temp=st.get("current_temperature"),
                target=st.get("target_temperature"), mode=st.get("mode"), outdoor=outdoor)
            if outdoor is not None:
                self.heat.add_boiler_outdoor(now, outdoor)
            self._push_energy()
        except Exception as err:
            self.log.warning("heating log failed: %s", err)

    def _push_energy(self):
        if "ariston.energy" not in self.hub.entities:
            return
        t = self.heat.today()
        h = t["hours"]
        state = {"heating_hours_today": round(h["heat"], 2),
                 "burner_hours_today": round(h["heat"] + h["dhw"] + h["other"], 2)}
        if t["heat_kwh"] is not None:
            state["heating_kwh_today"] = t["heat_kwh"]
        if t["dhw_kwh"] is not None:
            state["dhw_kwh_today"] = t["dhw_kwh"]
        if t["out_c"] is not None:
            state["outdoor_temp"] = t["out_c"]
        self.hub.update("ariston.energy", available=True, **state)

    async def refresh_energy(self):
        """Gas use per day / month from the cloud (rate-limited: one read per energy_interval)."""
        dev = self.device
        if dev is None or not hasattr(dev, "async_update_energy"):
            return
        if time.time() - (self.heat.energy_ts or 0) < self.energy_interval * 60 * 0.9:
            return  # restarted recently: the stored figures are fresh enough
        async with self.lock:
            try:
                await dev.async_update_energy()
            except Exception as err:
                self.heat.error, self.heat.error_ts = str(err), time.time()
                raise
        seqs = list(getattr(dev, "consumptions_sequences", None) or [])
        if not seqs:
            self.heat.error, self.heat.error_ts = _("The boiler returned no consumption data yet"), time.time()
            return
        try:
            unit = dev.gas_energy_unit
            self.heat.gas_type = dev.gas_type
        except Exception:
            unit = None
        self.hub.store.set("ariston.heat.raw", [{"k": q.get("k"), "p": q.get("p"), "v": q.get("v")} for q in seqs])
        self.heat.ingest_energy(seqs, unit)
        self.heat.error = None
        self._push_energy()

    async def refresh_weather(self):
        """Hourly outdoor temperature from Open-Meteo (free, no key)."""
        lat, lon = _num(self.config.get("latitude"), None), _num(self.config.get("longitude"), None)
        if lat is None or lon is None:
            return
        first = not self.hub.store.get("ariston.heat.weather_ok")
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f&hourly=temperature_2m"
               "&past_days=%d&forecast_days=1&timezone=UTC&timeformat=unixtime" % (lat, lon, 60 if first else 3))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                data = await r.json()
        hourly = data.get("hourly") or {}
        pts = list(zip(hourly.get("time") or [], hourly.get("temperature_2m") or []))
        self.heat.add_outdoor([(t, v) for t, v in pts if t <= time.time() + 3600], "om")
        self.hub.store.set("ariston.heat.weather_ok", True)

    # -- routes ---------------------------------------------------------------------------------

    def routes(self):
        return [web.get("/today", self.r_today), web.get("/summary", self.r_summary),
                web.get("/usage", self.r_usage), web.get("/sessions", self.r_sessions)]

    @staticmethod
    def _days(request, default):
        try:
            return max(1, min(int(request.query.get("days", default)), 800))
        except ValueError:
            return default

    async def r_today(self, request):
        return web.json_response(self.heat.today())

    async def r_summary(self, request):
        return web.json_response(self.heat.summary())

    async def r_usage(self, request):
        return web.json_response(self.heat.usage(self._days(request, 30)))

    async def r_sessions(self, request):
        return web.json_response(self.heat.sessions(self._days(request, 3)))

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
            raise ConnectionError(_("Ariston is offline"))
        async with self.lock:
            await coro_fn(*args)
            self._last_write = time.monotonic()
            self.log.info("%s%s ok", coro_fn.__name__, args)

    async def set_zone_temp(self, zone: int, value):
        await self._write(self.device.async_set_comfort_temp, float(value), zone)
        self.heat.note_change("target", float(value))
        self.hub.update(f"ariston.zone{zone}", target_temperature=float(value))

    async def set_zone_mode(self, zone: int, mode: str):
        from ariston.const import PlantMode, ZoneMode

        dev = self.device
        if dev is None:
            raise ConnectionError(_("Ariston is offline"))
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
        self.heat.note_change("mode", mode)
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
