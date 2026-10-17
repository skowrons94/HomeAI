"""IKEA DIRIGERA hub (local REST API + websocket on port 8443).

Lights become ``light`` entities, outlets ``switch``, VINDSTYRKA / motion /
door sensors ``sensor``. Pair once from Settings → DIRIGERA (press the action
button on the hub when asked); the token is stored in the hub database.

    [plugins.dirigera]
    host = ""              # empty = find it with mDNS (_ihsp._tcp)
    homekit = false        # DIRIGERA can join Apple Home via Matter itself
"""

from __future__ import annotations

import asyncio
import time
import base64
import hashlib
import json
import secrets
import socket
import string

import aiohttp
from aiohttp import web

from ..core import Entity, Plugin
from ..i18n import _

PORT = 8443
REFRESH_EVERY = 300  # full device refresh as a fallback to the websocket
TIMEOUT = aiohttp.ClientTimeout(total=10)
PRESS_MSG = ("Press the action button on the bottom of the DIRIGERA hub now, then tap "
             "Continue. Casa waits up to a minute for the press.")  # shown through _()


def map_device(dev: dict) -> tuple[str, dict, dict] | None:
    """DIRIGERA device JSON -> (kind, state, attrs), or None if not supported."""
    a = dev.get("attributes", {})
    dtype = dev.get("deviceType") or dev.get("type")
    attrs = {"manufacturer": a.get("manufacturer", "IKEA"), "model": a.get("model", dtype),
             "dirigera_type": dtype}
    if dtype == "light":
        state = {"on": bool(a.get("isOn"))}
        caps = dev.get("capabilities", {}).get("canReceive", [])
        if "lightLevel" in caps or "lightLevel" in a:
            state["brightness"] = a.get("lightLevel")
        if "colorTemperature" in caps or "colorTemperature" in a:
            state["color_temp"] = a.get("colorTemperature")
            # IKEA calls the coolest end "Min"; keep (warm, cool) in Kelvin
            lo, hi = sorted((a.get("colorTemperatureMin", 4000), a.get("colorTemperatureMax", 2200)))
            attrs["color_temp_range"] = (lo, hi)
        if "colorHue" in caps:
            # hub convention: hue 0-360, saturation 0-100 (IKEA uses 0-1)
            state["hue"] = round(a.get("colorHue") or 0) % 360
            state["saturation"] = round((a.get("colorSaturation") or 0) * 100)
            attrs["color"] = True
            if a.get("colorMode"):
                state["color_mode"] = a["colorMode"]  # "color" | "temperature"
        return "light", state, attrs
    if dtype == "outlet":
        state = {"on": bool(a.get("isOn"))}
        if "currentActivePower" in a:
            state["power"] = a["currentActivePower"]
        return "switch", state, attrs
    if dtype == "environmentSensor":
        state = {}
        for src, dst in (("currentTemperature", "temperature"), ("currentRH", "humidity"),
                         ("currentPM25", "pm25"), ("vocIndex", "voc")):
            if a.get(src) is not None:
                state[dst] = a[src]
        attrs["measures"] = [k for k in ("temperature", "humidity") if k in state]
        return "sensor", state, attrs
    if dtype == "motionSensor":
        return "sensor", {"motion": bool(a.get("isDetected")),
                          "battery": a.get("batteryPercentage")}, attrs
    if dtype == "openCloseSensor":
        return "sensor", {"open": bool(a.get("isOpen")),
                          "battery": a.get("batteryPercentage")}, attrs
    return None


def eid_for(device_id: str) -> str:
    return "dirigera." + device_id.split("_")[0].replace("-", "")[:20]


class DirigeraPlugin(Plugin):
    name = "dirigera"
    title = "IKEA DIRIGERA"
    description = "IKEA DIRIGERA hub: lights, outlets and sensors over the local API"
    icon = "bulb"
    schema = [
        {"key": "host", "label": "IP address", "type": "text", "help": "Empty = find it with mDNS"},
        {"key": "homekit", "label": "Expose to Apple Home", "type": "bool", "default": False, "help": "DIRIGERA can join Apple Home itself via Matter"},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.host = config.get("host") or hub.store.get("dirigera.host", "")
        self.token = hub.store.get("dirigera.token", "")
        self.devices: dict[str, dict] = {}  # entity id -> device json
        self._pairing: dict = {}
        self._ws_task = None

    # -- lifecycle -------------------------------------------------------------------

    async def start(self):
        # recreate known devices first so HomeKit (if enabled) keeps them
        for dev in self.hub.store.get("dirigera.devices", []):
            self._upsert(dev, available=False)
        if not self.token:
            self.log.info("not paired yet - use Settings → IKEA DIRIGERA")
            return
        self._run()

    def _run(self):
        if self._ws_task:
            return  # already running (e.g. paired again)
        self.hub.every(REFRESH_EVERY, self.refresh, "dirigera refresh", initial_delay=0)
        self._ws_task = self.hub.spawn(self._websocket(), "dirigera websocket")

    def status(self):
        return {"setup": True, "configured": bool(self.token), "host": self.host,
                "devices": len(self.devices)}

    def routes(self):
        return [web.post("/setup", self.setup)]

    # -- HTTP --------------------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"https://{self.host}:{PORT}/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, **kw):
        async with self.hub.http.request(method, self.base + path, ssl=False,
                                         headers=self._headers(), timeout=TIMEOUT, **kw) as r:
            if r.status == 401:
                self.error = _("token rejected - pair again")
                raise PermissionError(self.error)
            r.raise_for_status()
            return await r.json() if r.content_type == "application/json" else None

    async def refresh(self):
        if not self.host:
            self.host = await self._discover()
            if not self.host:
                raise ConnectionError(_("DIRIGERA not found on the network"))
        try:
            devices = await self._request("GET", "/devices")
        except Exception:
            for eid in self.devices:
                self.hub.update(eid, available=False)
            raise
        self.error = None
        for dev in devices:
            self._upsert(dev)
        self.hub.store.set("dirigera.devices", list(self.devices.values()))

    async def _websocket(self):
        delay = 5
        while True:
            try:
                if not self.host:
                    await asyncio.sleep(30)
                    continue
                async with self.hub.http.ws_connect(
                        f"wss://{self.host}:{PORT}/v1", ssl=False, headers=self._headers(),
                        heartbeat=30) as ws:
                    self.log.info("websocket connected")
                    delay = 5
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.handle_event(json.loads(msg.data))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.log.debug("websocket: %s", err)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    # -- devices -----------------------------------------------------------------------

    def _upsert(self, dev: dict, available: bool | None = None) -> None:
        mapped = map_device(dev)
        if mapped is None:
            return
        kind, state, attrs = mapped
        eid = eid_for(dev["id"])
        self.devices[eid] = dev
        avail = dev.get("isReachable", True) if available is None else available
        if eid not in self.hub.entities:
            a = dev.get("attributes", {})
            actions = {}
            if kind in ("light", "switch"):
                actions = {"turn_on": lambda eid=eid, **kw: self.turn_on(eid, **kw),
                           "turn_off": lambda eid=eid: self.turn_off(eid)}
            e = self.hub.add_entity(Entity(
                id=eid, name=a.get("customName") or a.get("model") or _("IKEA device"),
                kind=kind, plugin=self.name, room=((dev.get("room") or {}).get("name") or "").strip(),
                state=state, attrs=attrs, available=avail, actions=actions,
            ))
            if self.config.get("homekit", False):
                self.hub.homekit.expose(e)
        else:
            self.hub.entities[eid].attrs.update(attrs)
            self.hub.update(eid, available=avail, **state)

    def handle_event(self, event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "deviceStateChanged" and data.get("id"):
            eid = eid_for(data["id"])
            dev = self.devices.get(eid)
            if dev is None:
                return
            dev.setdefault("attributes", {}).update(data.get("attributes") or {})
            if "isReachable" in data:
                dev["isReachable"] = data["isReachable"]
            self._upsert(dev)
        elif etype == "deviceAdded" and data.get("id"):
            self._upsert(data)
        elif etype == "deviceRemoved" and data.get("id"):
            eid = eid_for(data["id"])
            self.devices.pop(eid, None)
            self.hub.remove_entity(eid)

    async def _patch(self, eid: str, attributes: dict):
        dev = self.devices[eid]
        await self._request("PATCH", f"/devices/{dev['id']}", json=[{"attributes": attributes}])

    async def turn_on(self, eid: str, brightness=None, color_temp=None, hue=None,
                      saturation=None):
        await self._patch(eid, {"isOn": True})
        update = {"on": True}
        if hue is not None or saturation is not None:
            st = self.hub.entities[eid].state
            update["hue"] = round(float(st.get("hue", 0) if hue is None else hue)) % 360
            update["saturation"] = max(0, min(100, round(float(
                st.get("saturation", 100) if saturation is None else saturation))))
            await self._patch(eid, {"colorHue": update["hue"],
                                    "colorSaturation": update["saturation"] / 100})
            update["color_mode"] = "color"
        if brightness is not None:
            update["brightness"] = max(1, min(100, int(brightness)))
            await self._patch(eid, {"lightLevel": update["brightness"]})
        if color_temp is not None:
            lo, hi = self.hub.entities[eid].attrs.get("color_temp_range", (2200, 4000))
            update["color_temp"] = max(lo, min(hi, int(color_temp)))
            await self._patch(eid, {"colorTemperature": update["color_temp"]})
            if "hue" in self.hub.entities[eid].state:
                update["color_mode"] = "temperature"
        self.hub.update(eid, **update)

    async def turn_off(self, eid: str):
        await self._patch(eid, {"isOn": False})
        self.hub.update(eid, on=False)

    # -- discovery & pairing -------------------------------------------------------------

    async def _discover(self) -> str:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

        found: list[str] = []

        def on_change(zeroconf, service_type, name, state_change):
            found.append(name)

        browser = AsyncServiceBrowser(self.hub.zeroconf.zeroconf, "_ihsp._tcp.local.",
                                      handlers=[on_change])
        await asyncio.sleep(4)
        await browser.async_cancel()
        for name in found:
            info = AsyncServiceInfo("_ihsp._tcp.local.", name)
            if await info.async_request(self.hub.zeroconf.zeroconf, 3000):
                addrs = info.parsed_addresses()
                if addrs:
                    self.hub.store.set("dirigera.host", addrs[0])
                    self.log.info("found DIRIGERA at %s", addrs[0])
                    return addrs[0]
        return ""

    async def setup(self, request):
        body = await request.json() if request.can_read_body else {}
        step, value = body.get("step"), (body.get("value") or "").strip()

        def reply(message, step=None, input=None, done=False, error=None):
            return web.json_response({"message": message, "step": step, "input": input,
                                      "done": done, "error": error, "choices": None})

        if step == "host" and value:
            self.host = value
            self.hub.store.set("dirigera.host", value)
            step = None
        if step is None:
            if not self.host:
                self.host = await self._discover()
            if not self.host:
                return reply(_("DIRIGERA not found on the network. Is it powered on? "
                               "Enter its IP address:"), step="host",
                             input={"label": _("DIRIGERA IP address"), "type": "text"})
            # a request we opened in the last minute is still valid: reuse it (a new
            # one would be refused with 409 "Already one ongoing pairing request")
            if self._pairing and time.monotonic() - self._pairing.get("at", 0) < 55:
                return reply(_(PRESS_MSG), step="token")
            verifier = "".join(secrets.choice(string.ascii_letters + string.digits + "-._~")
                               for _ in range(128))
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            deadline = time.monotonic() + 120
            code = None
            try:
                while code is None:
                    async with self.hub.http.get(  # GET: the hub answers 404 to POST
                            f"{self.base}/oauth/authorize", ssl=False, timeout=TIMEOUT,
                            params={"audience": "homesmart.local", "response_type": "code",
                                    "code_challenge": challenge,
                                    "code_challenge_method": "S256"}) as r:
                        if r.status == 409 and time.monotonic() < deadline:
                            # an earlier attempt is still pending on the hub: wait it out
                            await asyncio.sleep(5)
                            continue
                        r.raise_for_status()
                        code = (await r.json())["code"]
            except Exception as err:
                busy = "409" in str(err)
                return reply(_("DIRIGERA is still busy with an earlier pairing attempt. "
                               "Wait two minutes and try again.") if busy else
                             _("Could not reach DIRIGERA at {host}.", host=self.host),
                             step=None if busy else "host",
                             input=None if busy else {"label": _("DIRIGERA IP address"),
                                                      "type": "text"},
                             error=str(err))
            self._pairing = {"code": code, "verifier": verifier, "at": time.monotonic()}
            return reply(_(PRESS_MSG), step="token")
        if step == "token" and self._pairing:
            for _i in range(30):  # a minute to press the button
                try:
                    async with self.hub.http.post(
                            f"{self.base}/oauth/token", ssl=False, timeout=TIMEOUT,
                            data={"code": self._pairing["code"], "name": socket.gethostname(),
                                  "grant_type": "authorization_code",
                                  "code_verifier": self._pairing["verifier"]}) as r:
                        if r.status == 200:
                            self.token = (await r.json())["access_token"]
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                    self._pairing = {}
                    return reply(_("Lost contact with DIRIGERA at {host} (it may be "
                                   "restarting or updating). Wait until its light is steady, "
                                   "then tap Continue to start again.", host=self.host),
                                 step=None, error=str(err))
                await asyncio.sleep(2)
            if not self.token:
                self._pairing = {}  # expired on the hub too: the next try starts fresh
                return reply(_("The button press wasn't detected in time. Tap Continue to "
                               "start again, then press the button."), step=None, error="timeout")
            self.hub.store.set("dirigera.token", self.token)
            self._pairing = {}
            self.error = None
            self._run()
            return reply(_("DIRIGERA paired. Your lamps will appear in a moment."), done=True)
        return reply(_("Start again."), error="unknown step")


PLUGIN = DirigeraPlugin
