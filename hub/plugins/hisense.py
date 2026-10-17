"""Hisense VIDAA TV: power on with Wake-on-LAN, on/off detection, and
EXPERIMENTAL remote control over the TV's MQTT broker (port 36669).

Recent VIDAA firmware only accepts MQTT clients presenting the certificate of
the Hisense RemoteNOW app, so remote keys / volume work only when ``certfile``
and ``keyfile`` are configured. Power on (WoL) and status work without them.
(The TV is also natively in Apple Home and AirPlay.)

    [plugins.hisense]
    host = "192.168.1.136"
    mac = "aa:bb:cc:dd:ee:ff"     # for Wake-on-LAN
    name = "TV Soggiorno"
    room = "Soggiorno"
    # certfile = "data/hisense/rcm_certchain_pem.cer"   # experimental MQTT
    # keyfile = "data/hisense/rcm_pem_privkey.pkcs8"
"""

from __future__ import annotations

import asyncio
import socket
import ssl

from ..core import Entity, Plugin
from ..i18n import _

MQTT_PORT = 36669
KEYS = {
    "power": "KEY_POWER", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT",
    "right": "KEY_RIGHT", "select": "KEY_OK", "back": "KEY_RETURNS", "home": "KEY_HOME",
    "menu": "KEY_MENU", "volume_up": "KEY_VOLUMEUP", "volume_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE", "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "channel_up": "KEY_CHANNELUP", "channel_down": "KEY_CHANNELDOWN",
}


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"bad MAC address {mac}")
    return b"\xff" * 6 + raw * 16


class HisensePlugin(Plugin):
    name = "hisense"
    title = "Hisense TV"
    description = "Hisense VIDAA TV: power on with Wake-on-LAN, status and remote keys"
    icon = "tv"
    schema = [
        {"key": "host", "label": "IP address", "type": "text", "required": True, "placeholder": "192.168.1.136"},
        {"key": "mac", "label": "MAC address", "type": "text", "placeholder": "aa:bb:cc:dd:ee:ff", "help": "Needed for Wake-on-LAN"},
        {"key": "name", "label": "Name", "type": "text", "default": "Hisense TV"},
        {"key": "room", "label": "Room", "type": "text"},
        {"key": "certfile", "label": "Client certificate", "type": "text", "advanced": True, "help": "RemoteNOW certificate chain, for experimental remote keys"},
        {"key": "keyfile", "label": "Client key", "type": "text", "advanced": True, "help": "RemoteNOW private key, for experimental remote keys"},
        {"key": "model", "label": "Model", "type": "text", "default": "VIDAA", "advanced": True},
        {"key": "client_id", "label": "MQTT client id", "type": "text", "default": "CasaHub", "advanced": True},
        {"key": "username", "label": "MQTT username", "type": "text", "default": "hisenseservice", "advanced": True},
        {"key": "password", "label": "MQTT password", "type": "password", "default": "multimqttservice", "advanced": True},
    ]

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.host = config.get("host", "")
        self.mac = config.get("mac", "")
        self.eid = "hisense.tv"
        self.client_id = config.get("client_id", "CasaHub")
        self.mqtt = None
        self.mqtt_ok = False

    async def start(self):
        if not self.host:
            raise ValueError("host missing in [plugins.hisense]")
        actions = {"turn_on": self.turn_on}
        if self.config.get("certfile"):
            actions.update(turn_off=self.turn_off, remote=self.remote,
                           set_volume=self.set_volume)
        self.hub.add_entity(Entity(
            id=self.eid, name=self.config.get("name", "Hisense TV"), kind="tv",
            plugin=self.name, room=self.config.get("room", ""),
            state={"on": False},
            attrs={"manufacturer": "Hisense", "model": self.config.get("model", "VIDAA"),
                   "keys": list(KEYS) if self.config.get("certfile") else [],
                   "experimental": bool(self.config.get("certfile"))},
            actions=actions,
        ))
        if self.config.get("certfile"):
            await asyncio.to_thread(self._mqtt_connect)
        self.hub.every(15, self.refresh, "hisense poll", initial_delay=0)

    async def stop(self):
        if self.mqtt:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()

    def status(self):
        return {"host": self.host, "wol": bool(self.mac),
                "mqtt": "connected" if self.mqtt_ok else
                        ("not configured" if not self.config.get("certfile") else "disconnected")}

    async def _port_open(self, port: int) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(self.host, port), 2)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def refresh(self):
        # the MQTT broker only runs while the TV is on (or in fast standby)
        on = await self._port_open(MQTT_PORT)
        self.hub.update(self.eid, available=True, on=on)

    # -- actions --------------------------------------------------------------------------

    async def turn_on(self):
        if not self.mac:
            raise ValueError("mac missing in [plugins.hisense] - needed for Wake-on-LAN")
        pkt = magic_packet(self.mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            subnet_bcast = ".".join(self.host.split(".")[:3] + ["255"])
            for addr in ("255.255.255.255", subnet_bcast, self.host):
                for port in (7, 9):
                    try:
                        s.sendto(pkt, (addr, port))
                    except OSError:
                        pass
        self.log.info("Wake-on-LAN sent to %s", self.mac)

    async def turn_off(self):
        await self.remote("power")
        self.hub.update(self.eid, on=False)

    async def remote(self, key: str):
        code = KEYS.get(key, key)
        self._publish(f"/remoteapp/tv/remote_service/{self.client_id}/actions/sendkey", code)

    async def set_volume(self, value):
        v = max(0, min(100, int(float(value))))
        self._publish(f"/remoteapp/tv/platform_service/{self.client_id}/actions/changevolume",
                      str(v))
        self.hub.update(self.eid, volume=v)

    # -- experimental MQTT ------------------------------------------------------------------

    def _publish(self, topic: str, payload: str):
        if not self.mqtt_ok:
            raise ConnectionError(_("TV MQTT not connected (experimental; needs certificates)"))
        self.mqtt.publish(topic, payload)

    def _mqtt_connect(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id,
                        protocol=mqtt.MQTTv311)
        c.username_pw_set(self.config.get("username", "hisenseservice"),
                          self.config.get("password", "multimqttservice"))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        root = self.hub.root
        ctx.load_cert_chain(root / self.config["certfile"], root / self.config["keyfile"])
        c.tls_set_context(ctx)

        def on_connect(client, userdata, flags, rc, props=None):
            self.mqtt_ok = not rc.is_failure
            self.log.info("TV MQTT connect: %s", rc)
            if self.mqtt_ok:
                client.subscribe(f"/remoteapp/mobile/{self.client_id}/#")
                client.subscribe("/remoteapp/mobile/broadcast/#")

        def on_disconnect(client, userdata, flags, rc, props=None):
            self.mqtt_ok = False

        c.on_connect, c.on_disconnect = on_connect, on_disconnect
        c.reconnect_delay_set(5, 300)
        try:
            c.connect_async(self.host, MQTT_PORT, 30)
            c.loop_start()  # paho's own thread, reconnects by itself
            self.mqtt = c
        except Exception as err:
            self.error = f"MQTT: {err}"


PLUGIN = HisensePlugin
