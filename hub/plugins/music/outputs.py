"""AirPlay (RAOP) outputs through pyatv: the KEF LSX, the Apple TV, HomePods and other AirPlay speakers."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from ...i18n import _
from .player import Output

log = logging.getLogger("hub.music")

KEF_WARMUP = 4.0       # seconds the LSX needs after turning on before it accepts a stream


class OutputError(Exception):
    pass


def describe(conf, creds: dict) -> dict:
    """pyatv scan result -> what the page shows."""
    from pyatv.const import PairingRequirement, Protocol

    svc = conf.get_service(Protocol.RAOP)
    pairing = getattr(svc, "pairing", None)
    stored = creds.get(conf.identifier) or {}
    needs = pairing == PairingRequirement.Mandatory and not stored.get("credentials")
    model = ""
    try:
        model = str(conf.device_info.model.name)
    except Exception:
        pass
    return {"id": conf.identifier, "name": conf.name, "address": str(conf.address), "model": model,
            "kind": "airplay", "needs_pairing": bool(needs), "password": bool(getattr(svc, "requires_password", False)),
            "paired": bool(stored.get("credentials")), "has_password": bool(stored.get("password"))}


async def scan(extra_hosts: list[str], timeout: float = 5.0) -> list:
    import pyatv
    from pyatv.const import Protocol

    loop = asyncio.get_running_loop()
    found = {c.identifier: c for c in await pyatv.scan(loop, timeout=timeout, protocol={Protocol.RAOP})}
    hosts = [h for h in extra_hosts if h and h not in {str(c.address) for c in found.values()}]
    if hosts:
        try:
            for c in await pyatv.scan(loop, timeout=timeout, hosts=hosts, protocol={Protocol.RAOP}):
                found.setdefault(c.identifier, c)
        except Exception as err:
            log.info("scan of %s failed: %s", hosts, err)
    return [c for c in found.values() if c.get_service(Protocol.RAOP) is not None]


def trimmed_copy(path: str, offset: float, bitrate_kbps: int = 320) -> str:
    """For a resume in the middle of a CBR mp3: a copy that starts at the wanted byte (decoders resync)."""
    skip = int(offset * bitrate_kbps * 125)
    if skip <= 0 or skip >= os.path.getsize(path):
        return path
    fd, out = tempfile.mkstemp(suffix=".mp3", dir=os.path.dirname(path), prefix=".resume-")
    with os.fdopen(fd, "wb") as dst, open(path, "rb") as src:
        src.seek(skip)
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    return out


class AirPlayOutput(Output):
    kind = "airplay"

    def __init__(self, plugin, desc: dict):
        self.p, self.desc = plugin, desc
        self.id, self.name = desc["id"], desc["name"]
        self.atv = None

    # -- KEF -------------------------------------------------------------------------------------
    def kef(self):
        """The running kef plugin, when this output is the KEF speaker it controls (never a second TCP client)."""
        k = self.p.hub.plugins.get("kef")
        if k is None or not hasattr(k, "turn_on"):
            return None
        if getattr(k, "host", None) == self.desc.get("address") or "kef" in self.name.lower():
            return k
        return None

    async def prepare(self) -> None:
        k = self.kef()
        if k is None:
            return
        src = getattr(k, "src", {}) or {}
        was_on = bool(src.get("on")) and src.get("source") == "Wifi"
        try:
            await k.turn_on("Wifi")
        except Exception as err:       # the speaker may already be on Wifi; try to stream anyway
            log.warning("KEF turn_on failed: %s", err)
            return
        if not was_on:
            await asyncio.sleep(KEF_WARMUP)

    # -- connection ------------------------------------------------------------------------------
    async def _connect(self):
        if self.atv is not None:
            return self.atv
        import pyatv
        from pyatv import conf as pconf
        from pyatv.const import Protocol

        loop = asyncio.get_running_loop()
        confs = await pyatv.scan(loop, timeout=4, identifier=self.id, hosts=[self.desc["address"]],
                                 protocol={Protocol.RAOP})
        if not confs:
            # some speakers (the KEF LSX) only answer multicast mDNS, not a query sent to their address
            confs = [c for c in await pyatv.scan(loop, timeout=5, protocol={Protocol.RAOP})
                     if self._is_me(c)]
        if not confs:
            raise OutputError(_("{name} not found on the network", name=self.name))
        src = confs[0].get_service(Protocol.RAOP)
        cred = self.p.output_creds().get(self.id) or {}
        credentials = cred.get("credentials") or self._borrowed_credentials()
        dev = pconf.AppleTV(confs[0].address, confs[0].name)
        dev.add_service(pconf.RaopService(src.identifier, src.port, credentials=credentials,
                                          password=cred.get("password") or None, properties=src.properties))
        try:
            self.atv = await pyatv.connect(dev, loop)
        except Exception as err:
            text = str(err) or type(err).__name__
            if any(w in text.lower() for w in ("auth", "pair", "password")):
                raise OutputError(_("{name} needs pairing or a password: open the output list and set it up", name=self.name)) from err
            raise OutputError(text) from err
        return self.atv

    def _is_me(self, conf) -> bool:
        plain = lambda v: str(v or "").replace(":", "").upper()
        ids = {plain(i) for i in (getattr(conf, "all_identifiers", None) or [conf.identifier])}
        return plain(self.id) in ids or str(conf.address) == self.desc.get("address")

    def _borrowed_credentials(self) -> str | None:
        """The Apple TV plugin paired AirPlay already: those credentials work for audio too."""
        a = self.p.hub.plugins.get("appletv")
        if a is not None and getattr(a, "identifier", None) == self.id:
            return (getattr(a, "creds", {}) or {}).get("AirPlay")
        return None

    async def stream(self, path: str, offset: float = 0.0, track: dict | None = None) -> None:
        atv = await self._connect()
        use = trimmed_copy(path, offset) if offset > 2 and path.endswith(".mp3") else path
        meta = None
        if track:
            try:
                from pyatv.interface import MediaMetadata
                meta = MediaMetadata(artist=track.get("artist"), title=track.get("title"), album=track.get("album"),
                                     duration=track.get("duration") or None)
            except Exception:
                meta = None
        try:
            if meta:
                await atv.stream.stream_file(use, metadata=meta)
            else:
                await atv.stream.stream_file(use)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            await self.close()
            raise OutputError(str(err) or type(err).__name__) from err
        finally:
            if use != path:
                try:
                    os.unlink(use)
                except OSError:
                    pass

    async def set_volume(self, value: int) -> None:
        k = self.kef()
        if k is not None:
            try:
                await k.set_volume(value)
                return
            except Exception as err:    # its control port can be closed while AirPlay still works
                log.info("KEF volume via its protocol failed (%s): using AirPlay", err)
        temp = self.atv is None
        atv = await self._connect()
        try:
            await atv.audio.set_volume(float(value))
        finally:
            if temp:
                await self.close()

    async def close(self) -> None:
        atv, self.atv = self.atv, None
        if atv is not None:
            try:
                tasks = atv.close()
                if tasks:
                    await asyncio.wait(tasks, timeout=5)
            except Exception:
                pass
