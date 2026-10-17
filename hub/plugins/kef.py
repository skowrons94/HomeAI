"""KEF LSX / LS50 Wireless (first generation) over the TCP 50001 protocol.

Messages are 3-4 bytes: ``G <reg> 0x80`` reads a register, ``S <reg> 0x81 <v>``
writes it. Replies look like ``R <reg> 0x81 <value> <checksum>``; a write is
acknowledged with ``R 0x11 0xff``.

The source register packs source, standby time, speaker orientation and power:
bit 7 = standby, bit 6 = R/L, bits 4-5 = standby time (20 min / 60 min / never),
bits 0-3 = source.

    [plugins.kef]
    host = "192.168.1.43"
    name = "KEF LSX"
    room = "Soggiorno"
    poll_interval = 10
"""

from __future__ import annotations

import asyncio

from ..core import Entity, Plugin

PORT = 50001
TIMEOUT = 3.0
REG_VOLUME, REG_SOURCE, REG_CONTROL = ord("%"), ord("0"), ord("1")
OK = bytes([82, 17, 255])

SOURCES = {"Wifi": 2, "Bluetooth": 9, "Aux": 10, "Opt": 11, "Usb": 12}
SOURCE_NAMES = {v: k for k, v in SOURCES.items()} | {15: "Bluetooth"}  # 15 = BT paired
STANDBY = [20, 60, None]  # minutes; index = bits 4-5


def decode_source(value: int) -> dict:
    """Source register -> {on, source, standby_index, rl}."""
    on = value < 128
    code = value % 128
    if code == 48:  # speakers report this for Wifi / R-L / 60 min
        code = 82
    return {
        "on": on,
        "source": SOURCE_NAMES.get(code & 15, "Wifi"),
        "standby_index": min((code & 63) // 16, 2),
        "rl": bool(code & 64),
    }


def encode_source(source: str, standby_index: int, rl: bool, on: bool) -> int:
    code = SOURCES[source] + 16 * standby_index + (64 if rl else 0)
    return code if on else code + 128


class KefClient:
    """One request at a time over a short-lived connection."""

    def __init__(self, host: str):
        self.host = host
        self.lock = asyncio.Lock()
        self._reader = self._writer = None

    async def _connect(self):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, PORT), TIMEOUT)

    async def close(self):
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    async def _send(self, msg: bytes) -> bytes:
        async with self.lock:
            for attempt in range(2):
                try:
                    if self._writer is None:
                        await self._connect()
                    self._writer.write(msg)
                    await self._writer.drain()
                    data = await asyncio.wait_for(self._reader.read(100), TIMEOUT)
                    if not data:
                        raise ConnectionResetError("connection closed")
                    return data
                except (OSError, asyncio.TimeoutError):
                    await self.close()
                    if attempt:
                        raise
            raise ConnectionError("unreachable")

    async def get(self, reg: int) -> int:
        reply = await self._send(bytes([ord("G"), reg, 0x80]))
        # several replies can arrive glued together; pick the one for reg
        for i in range(len(reply) - 3):
            if reply[i] == 82 and reply[i + 1] == reg and reply[i + 2] == 0x81:
                return reply[i + 3]
        raise ConnectionError(f"unexpected reply {list(reply)}")

    async def set(self, reg: int, value: int) -> None:
        reply = await self._send(bytes([ord("S"), reg, 0x81, value]))
        if OK not in reply:
            raise ConnectionError(f"speaker refused write: {list(reply)}")


class KefPlugin(Plugin):
    name = "kef"
    title = "KEF"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.host = config.get("host", "")
        self.eid = f"kef.{config.get('id', 'speaker')}"
        self.client = KefClient(self.host)
        self.src: dict = {"on": False, "source": "Wifi", "standby_index": 1, "rl": False}
        self.volume = 0
        self.muted = False

    async def start(self):
        if not self.host:
            raise ValueError("host missing in [plugins.kef]")
        self.hub.add_entity(Entity(
            id=self.eid, name=self.config.get("name", "KEF LSX"), kind="speaker",
            plugin=self.name, room=self.config.get("room", ""), available=False,
            attrs={"sources": list(SOURCES), "manufacturer": "KEF",
                   "model": self.config.get("model", "LSX"), "host": self.host},
            actions={
                "turn_on": self.turn_on, "turn_off": self.turn_off,
                "set_volume": self.set_volume, "mute": self.mute,
                "set_source": self.set_source, "play_pause": self.play_pause,
                "next": self.next, "previous": self.previous,
            },
        ))
        try:
            await self.refresh()
        except Exception as err:
            self.log.warning("KEF %s not reachable: %s", self.host, err)
        self.hub.every(float(self.config.get("poll_interval", 10)), self.refresh, "kef poll")

    async def stop(self):
        await self.client.close()

    def status(self):
        return {"host": self.host,
                "standby": STANDBY[self.src["standby_index"]]}

    async def refresh(self):
        try:
            self.src = decode_source(await self.client.get(REG_SOURCE))
            vol = await self.client.get(REG_VOLUME)
            self.volume, self.muted = vol % 128, vol >= 128
            playing = None
            if self.src["on"] and self.src["source"] in ("Wifi", "Bluetooth"):
                playing = (await self.client.get(REG_CONTROL)) == 129
        except Exception:
            self.hub.update(self.eid, available=False)
            raise
        self.hub.update(self.eid, available=True, on=self.src["on"],
                        source=self.src["source"], volume=self.volume,
                        muted=self.muted, playing=bool(playing))

    # -- actions ------------------------------------------------------------------

    async def _write_source(self, source: str, on: bool):
        if source not in SOURCES:
            raise ValueError(f"unknown source {source}; use one of {list(SOURCES)}")
        await self.client.set(REG_SOURCE, encode_source(
            source, self.src["standby_index"], self.src["rl"], on))
        self.src.update(source=source, on=on)
        self.hub.update(self.eid, on=on, source=source)

    async def turn_on(self, source: str | None = None):
        await self._write_source(source or self.src["source"], True)

    async def turn_off(self):
        await self._write_source(self.src["source"], False)

    async def set_source(self, source: str):
        await self._write_source(source, True)

    async def set_volume(self, value):
        v = max(0, min(100, int(float(value))))
        await self.client.set(REG_VOLUME, v + (128 if self.muted else 0))
        self.volume = v
        self.hub.update(self.eid, volume=v)

    async def mute(self, value=True):
        value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on")
        await self.client.set(REG_VOLUME, self.volume + (128 if value else 0))
        self.muted = value
        self.hub.update(self.eid, muted=value)

    async def play_pause(self):
        await self.client.set(REG_CONTROL, 129)

    async def next(self):
        await self.client.set(REG_CONTROL, 130)

    async def previous(self):
        await self.client.set(REG_CONTROL, 131)


PLUGIN = KefPlugin
