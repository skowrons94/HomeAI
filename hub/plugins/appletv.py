"""Apple TV (pyatv): now playing, remote control, power and app launch (Infuse).

Pair once from Settings → Apple TV: a PIN appears on the TV for each protocol
(AirPlay for now-playing, Companion for apps/power). If pairing says
"disabled", on the Apple TV open Settings → AirPlay and HomeKit → Allow Access
and choose "Everyone on the Same Network" (you can switch it back afterwards).

    [plugins.appletv]
    identifier = ""        # empty = the device chosen during setup
    host = ""              # optional, speeds up connecting
    name = "Apple TV"
    room = "Soggiorno"
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from aiohttp import web

from ..core import Entity, Plugin

APPS = {
    "infuse": "com.firecore.infuse",
    "youtube": "com.google.ios.youtube",
    "netflix": "com.netflix.Netflix",
    "primevideo": "com.amazon.aiv.AIVApp",
    "disney+": "com.disney.disneyplus",
    "raiplay": "it.rai.raiplay",
    "tv": "com.apple.TVWatchList",
    "music": "com.apple.TVMusic",
    "settings": "com.apple.TVSettings",
}
KEYS = ("up", "down", "left", "right", "select", "menu", "home", "play_pause",
        "volume_up", "volume_down", "next", "previous", "top_menu")
PAIR_ORDER = ("AirPlay", "Companion")


class AppleTVPlugin(Plugin):
    name = "appletv"
    title = "Apple TV"
    description = "Apple TV: now playing, remote control, power and app launch"
    icon = "appletv"
    schema = [
        {"key": "host", "label": "IP address", "type": "text", "advanced": True, "help": "Optional, speeds up connecting"},
        {"key": "identifier", "label": "Device identifier", "type": "text", "advanced": True, "help": "Set by pairing"},
        {"key": "name", "label": "Name", "type": "text", "default": "Apple TV"},
        {"key": "room", "label": "Room", "type": "text"},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.identifier = config.get("identifier") or hub.store.get("appletv.identifier", "")
        self.creds: dict = hub.store.get("appletv.credentials", {})  # protocol -> creds
        self.eid = "appletv.main"
        self.atv = None
        self.conf = None
        self.artwork: dict[str, tuple[bytes, str]] = {}
        self._artwork_id = None
        self._pairing = None
        self._pair_queue: list[str] = []
        self._connecting = asyncio.Lock()

    async def start(self):
        import pyatv  # noqa: F401  (fail early if missing)

        self.hub.add_entity(Entity(
            id=self.eid, name=self.config.get("name", "Apple TV"), kind="media_player",
            plugin=self.name, room=self.config.get("room", ""), available=False,
            state={"on": False, "state": "off"},
            attrs={"manufacturer": "Apple", "model": "Apple TV", "apps": sorted(APPS),
                   "keys": list(KEYS)},
            actions={
                "turn_on": self.turn_on, "turn_off": self.turn_off,
                "play_pause": lambda: self.remote("play_pause"),
                "next": lambda: self.remote("next"),
                "previous": lambda: self.remote("previous"),
                "remote": self.remote, "launch_app": self.launch_app,
            },
        ))
        self.hub.every(30, self._ensure_connected, "appletv connect", initial_delay=0)

    async def stop(self):
        await self._disconnect()

    def status(self):
        return {"setup": True, "configured": bool(self.creds and self.identifier),
                "identifier": self.identifier, "connected": self.atv is not None,
                "paired_protocols": sorted(self.creds)}

    def routes(self):
        return [web.post("/setup", self.setup),
                web.get("/artwork/{id}", self.get_artwork)]

    # -- connection ---------------------------------------------------------------------

    async def _scan(self, timeout=5):
        import pyatv

        kw = {"timeout": timeout}
        if self.identifier:
            kw["identifier"] = self.identifier
        if self.config.get("host"):
            kw["hosts"] = [self.config["host"]]
        return await pyatv.scan(asyncio.get_running_loop(), **kw)

    async def _ensure_connected(self):
        if self.atv is not None or not self.identifier or not self.creds:
            return
        import pyatv
        from pyatv.const import Protocol

        async with self._connecting:
            confs = await self._scan()
            if not confs:
                self.hub.update(self.eid, available=False)
                raise ConnectionError("Apple TV not found on the network")
            conf = confs[0]
            for proto, cred in self.creds.items():
                conf.set_credentials(Protocol[proto], cred)
            self.atv = await pyatv.connect(conf, asyncio.get_running_loop())
            self.conf = conf
            self.error = None
            self.log.info("connected to %s (%s)", conf.name, conf.address)
            self.hub.entities[self.eid].attrs["model"] = str(conf.device_info.model)

            self.atv.listener = _DeviceListener(self)
            self.atv.push_updater.listener = _PushListener(self)
            self.atv.push_updater.start()
            try:
                self.atv.power.listener = _PowerListener(self)
            except Exception:
                pass
            await self._update_from(await self.atv.metadata.playing())

    async def _disconnect(self):
        if self.atv is not None:
            atv, self.atv = self.atv, None
            try:
                atv.push_updater.stop()
                tasks = atv.close()
                if tasks:
                    await asyncio.wait(tasks, timeout=5)
            except Exception:
                pass

    def _connection_lost(self):
        self.log.info("connection to Apple TV lost; will reconnect")
        self.atv = None
        self.hub.update(self.eid, available=False)

    # -- state ----------------------------------------------------------------------------

    def _power_on(self) -> bool:
        from pyatv.const import PowerState

        try:
            return self.atv.power.power_state == PowerState.On
        except Exception:
            return True

    async def _update_from(self, playing) -> None:
        if self.atv is None:
            return
        state = playing.device_state.name.lower()  # idle/loading/paused/playing/seeking/stopped
        app = None
        try:
            app = self.atv.metadata.app
        except Exception:
            pass
        on = self._power_on()
        update = {
            "on": on,
            "state": "off" if not on else ("playing" if state == "playing" else
                                           "paused" if state in ("paused", "loading", "seeking")
                                           else "idle"),
            "title": playing.title or "",
            "artist": playing.artist or playing.series_name or "",
            "series": playing.series_name or "",
            "season": playing.season_number,
            "episode": playing.episode_number,
            "app": app.name if app else "",
            "app_id": app.identifier if app else "",
            "position": playing.position,
            "duration": playing.total_time,
            "position_at": time.time(),
            "media_type": playing.media_type.name.lower(),
        }
        art_id = None
        try:
            art_id = self.atv.metadata.artwork_id
        except Exception:
            pass
        if art_id and art_id != self._artwork_id and playing.title:
            self._artwork_id = art_id
            try:
                art = await self.atv.metadata.artwork(width=512, height=None)
                if art and art.bytes:
                    key = hashlib.sha1(art.bytes).hexdigest()[:16]
                    self.artwork = {key: (art.bytes, art.mimetype or "image/jpeg")}
                    update["artwork"] = f"/api/p/appletv/artwork/{key}"
            except Exception as err:
                self.log.debug("artwork failed: %s", err)
        if not playing.title:
            update["artwork"] = ""
            self._artwork_id = None
        self.hub.update(self.eid, available=True, **update)

    async def get_artwork(self, request):
        item = self.artwork.get(request.match_info["id"])
        if not item:
            raise web.HTTPNotFound()
        return web.Response(body=item[0], content_type=item[1],
                            headers={"Cache-Control": "max-age=86400"})

    # -- actions ----------------------------------------------------------------------------

    def _need(self):
        if self.atv is None:
            raise ConnectionError("Apple TV not connected" +
                                  ("" if self.creds else " - pair it in Settings"))
        return self.atv

    async def turn_on(self):
        await self._need().power.turn_on()
        self.hub.update(self.eid, on=True)

    async def turn_off(self):
        await self._need().power.turn_off()
        self.hub.update(self.eid, on=False, state="off")

    async def remote(self, key: str):
        if key not in KEYS:
            raise ValueError(f"unknown key {key}; use one of {KEYS}")
        await getattr(self._need().remote_control, key)()

    async def launch_app(self, app: str):
        bundle = APPS.get(app.lower(), app)
        await self._need().apps.launch_app(bundle)

    # -- pairing (generic setup flow) ---------------------------------------------------------

    async def setup(self, request):
        from pyatv.const import OperatingSystem, PairingRequirement, Protocol

        body = await request.json() if request.can_read_body else {}
        step, value = body.get("step"), (body.get("value") or "").strip()

        def reply(message, step=None, input=None, done=False, error=None, choices=None):
            return web.json_response({"message": message, "step": step, "input": input,
                                      "done": done, "error": error, "choices": choices})

        if step is None:
            import pyatv

            confs = await pyatv.scan(asyncio.get_running_loop(), timeout=5)
            tvs = [c for c in confs if c.device_info.operating_system == OperatingSystem.TvOS
                   and "homepod" not in c.device_info.model.name.lower()]
            if not tvs:
                return reply("No Apple TV found on the network.", error="not found")
            return reply("Which Apple TV?", step="device", choices=[
                {"label": f"{c.name} ({c.address})", "value": c.identifier} for c in tvs])

        if step == "device":
            self.identifier = value
            confs = await self._scan()
            if not confs:
                return reply("That Apple TV disappeared from the network.", error="not found")
            self.conf = confs[0]
            self._pair_queue = []
            for proto in PAIR_ORDER:
                svc = self.conf.get_service(Protocol[proto])
                if svc is None or svc.pairing == PairingRequirement.NotNeeded:
                    continue
                if svc.pairing == PairingRequirement.Disabled:
                    return reply(
                        "Pairing is disabled on the Apple TV. On the TV open Settings → "
                        "AirPlay and HomeKit → Allow Access and choose \"Everyone on the "
                        "Same Network\", then start again.", error="pairing disabled")
                self._pair_queue.append(proto)
            return await self._next_pairing(reply)

        if step and step.startswith("pin:") and self._pairing is not None:
            proto = step[4:]
            try:
                self._pairing.pin(int(value))
                await self._pairing.finish()
                ok = self._pairing.has_paired
                if ok:
                    self.creds[proto] = self._pairing.service.credentials
            finally:
                await self._pairing.close()
                self._pairing = None
            if not ok:
                return reply("Wrong PIN or pairing timed out. Start again.", error="failed")
            return await self._next_pairing(reply)

        return reply("Start again.", error="unknown step")

    async def _next_pairing(self, reply):
        import pyatv
        from pyatv.const import Protocol

        if not self._pair_queue:
            self.hub.store.set("appletv.identifier", self.identifier)
            self.hub.store.set("appletv.credentials", self.creds)
            await self._disconnect()
            self.hub.spawn(self._ensure_connected(), "appletv connect")
            return reply(f"Paired with {self.conf.name}.", done=True)
        proto = self._pair_queue.pop(0)
        self._pairing = await pyatv.pair(self.conf, Protocol[proto], asyncio.get_running_loop(),
                                         name="Casa")
        await self._pairing.begin()
        return reply(f"Enter the PIN shown on the TV ({proto}).", step=f"pin:{proto}",
                     input={"label": "PIN", "type": "number"})


class _PushListener:
    def __init__(self, plugin: AppleTVPlugin):
        self.p = plugin

    def playstatus_update(self, updater, playstatus):
        self.p.hub.spawn(self.p._update_from(playstatus), "appletv update")

    def playstatus_error(self, updater, exception):
        self.p.log.debug("push error: %s", exception)


class _DeviceListener:
    def __init__(self, plugin: AppleTVPlugin):
        self.p = plugin

    def connection_lost(self, exception):
        self.p._connection_lost()

    def connection_closed(self):
        self.p._connection_lost()


class _PowerListener:
    def __init__(self, plugin: AppleTVPlugin):
        self.p = plugin

    def powerstate_update(self, old_state, new_state):
        from pyatv.const import PowerState

        on = new_state == PowerState.On
        self.p.hub.update(self.p.eid, on=on, **({} if on else {"state": "off"}))


PLUGIN = AppleTVPlugin
