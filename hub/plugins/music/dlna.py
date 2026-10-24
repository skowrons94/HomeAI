"""UPnP/DLNA renderers (MediaRenderer + AVTransport): the speaker fetches the track from the hub itself.

Used for speakers whose AirPlay 2 receiver accepts pyatv's realtime stream but stays silent (the KEF LSX)
and for DLNA-only speakers. The hub hands the renderer a short-lived signed URL of the cached mp3
(``/api/p/music/stream/...``, see MusicPlugin.r_stream) and follows its transport state until the track ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import os
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import aiohttp

from ...i18n import _
from .player import Output

log = logging.getLogger("hub.music")

SSDP = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
START_TIMEOUT = 20.0     # seconds for the renderer to fetch the file and start
POLL = 1.0


class DlnaError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_description(xml: str, location: str) -> dict | None:
    """description.xml -> {name, manufacturer, model, udn, avt, rc} (absolute control URLs) or None."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    base = next((e.text for e in root.iter() if _local(e.tag) == "URLBase" and e.text), None) or location
    for dev in (e for e in root.iter() if _local(e.tag) == "device"):
        kids = {_local(c.tag): (c.text or "").strip() for c in dev}
        if kids.get("deviceType") != RENDERER:
            continue
        ctrl = {}
        for svc in (e for e in dev.iter() if _local(e.tag) == "service"):
            s = {_local(c.tag): (c.text or "").strip() for c in svc}
            if s.get("serviceType") in (AVT, RC) and s.get("controlURL"):
                ctrl[s["serviceType"]] = urljoin(base, s["controlURL"])
        if AVT not in ctrl:
            return None
        return {"name": kids.get("friendlyName") or "", "manufacturer": kids.get("manufacturer") or "",
                "model": kids.get("modelName") or "", "udn": kids.get("UDN") or "",
                "address": urlparse(location).hostname or "", "avt": ctrl[AVT], "rc": ctrl.get(RC, "")}
    return None


async def discover(session: aiohttp.ClientSession, timeout: float = 3.0) -> list[dict]:
    """SSDP M-SEARCH for media renderers, then their descriptions."""
    loop = asyncio.get_running_loop()
    locations: set[str] = set()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode(errors="ignore"))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    sock.bind(("", 0))
    transport, _p = await loop.create_datagram_endpoint(Proto, sock=sock)
    try:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {RENDERER}\r\n\r\n").encode()
        for _i in range(2):
            transport.sendto(msg, SSDP)
            await asyncio.sleep(timeout / 2)
    finally:
        transport.close()

    async def one(loc):
        if urlparse(loc).scheme != "http":
            return None
        try:
            async with session.get(loc, timeout=aiohttp.ClientTimeout(total=5)) as r:
                return parse_description(await r.text(), loc)
        except Exception as err:
            log.debug("DLNA description %s: %s", loc, err)
            return None
    return [d for d in await asyncio.gather(*(one(x) for x in sorted(locations))) if d]


def didl(url: str, track: dict | None, mime: str = "audio/mpeg") -> str:
    t = track or {}
    e = html.escape
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1">'
            f'<dc:title>{e(str(t.get("title") or "Casa"))}</dc:title>'
            f'<upnp:artist>{e(str(t.get("artist") or ""))}</upnp:artist>'
            f'<upnp:album>{e(str(t.get("album") or ""))}</upnp:album>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:*">{e(url)}</res></item></DIDL-Lite>')


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


class Renderer:
    def __init__(self, session: aiohttp.ClientSession, desc: dict):
        self.session, self.d = session, desc

    async def call(self, svc: str, action: str, **args) -> str:
        url = self.d["avt"] if svc == AVT else self.d["rc"]
        if not url:
            raise DlnaError(_("This speaker can't do that"))
        body = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
        env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{svc}">{body}</u:{action}></s:Body></s:Envelope>')
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{svc}#{action}"'}
        try:
            async with self.session.post(url, data=env.encode(), headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=8)) as r:
                text = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DlnaError(_("{name} is not reachable", name=self.d.get("name") or "?")) from err
        if r.status != 200:
            code = re.search(r"<errorCode>(\d+)</errorCode>", text)
            raise DlnaError(_("{name} refused {action} ({code})", name=self.d.get("name") or "?", action=action,
                              code=code.group(1) if code else r.status))
        return text

    async def state(self) -> str:
        m = re.search(r"<CurrentTransportState>(\w+)<", await self.call(AVT, "GetTransportInfo", InstanceID=0))
        return m.group(1) if m else ""


MIME = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}


class DlnaOutput(Output):
    kind = "dlna"
    can_pause = True

    def __init__(self, plugin, desc: dict):
        self.p, self.desc = plugin, desc
        self.id, self.name = desc["id"], desc["name"]
        self.r = Renderer(plugin.session, desc["dlna"])

    async def prepare(self) -> None:
        k = self.p.hub.plugins.get("kef")      # the KEF switches to Wi-Fi by itself; turn it on if we can
        if k is not None and getattr(k, "host", None) == self.desc.get("address") and hasattr(k, "turn_on"):
            with contextlib.suppress(Exception):
                await k.turn_on("Wifi")

    async def stream(self, path: str, offset: float = 0.0, track: dict | None = None) -> None:
        url = self.p.stream_link(path, self.desc["dlna"]["address"])
        try:
            with contextlib.suppress(DlnaError):
                await self.r.call(AVT, "Stop", InstanceID=0)
            await self.r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url,
                              CurrentURIMetaData=didl(url, track, MIME.get(os.path.splitext(path)[1].lower(), "audio/mpeg")))
            await self.r.call(AVT, "Play", InstanceID=0, Speed=1)
            started, deadline = False, time.monotonic() + START_TIMEOUT
            while True:
                await asyncio.sleep(POLL)
                st = await self.r.state()
                if st == "PLAYING":
                    if not started and offset > 2:
                        with contextlib.suppress(DlnaError):
                            await self.r.call(AVT, "Seek", InstanceID=0, Unit="REL_TIME", Target=hms(offset))
                    started = True
                elif started and st in ("STOPPED", "NO_MEDIA_PRESENT"):
                    return                                  # the track ended
                elif not started and time.monotonic() > deadline:
                    raise DlnaError(_("{name} did not start playing", name=self.name))
        except asyncio.CancelledError:
            raise            # next/stop: the next SetAVTransportURI or close() takes over the speaker
        except DlnaError as err:
            from .outputs import OutputError
            raise OutputError(str(err)) from err

    async def set_volume(self, value: int) -> None:
        try:
            await self.r.call(RC, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=int(value))
        except DlnaError as err:
            from .outputs import OutputError
            raise OutputError(str(err)) from err

    async def pause(self) -> None:
        await self.r.call(AVT, "Pause", InstanceID=0)

    async def resume(self) -> None:
        await self.r.call(AVT, "Play", InstanceID=0, Speed=1)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.r.call(AVT, "Stop", InstanceID=0)
