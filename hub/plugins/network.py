"""Network discovery: everything the hub can see on the LAN.

Listens to mDNS (Bonjour) announcements for many service types, merges them
per IP address into devices, guesses what each one is and which plugin would
control it, and adds hosts from the ARP table that don't speak mDNS.

    [plugins.network]
    arp = true          # also list devices from the ARP table
"""

from __future__ import annotations

import asyncio
import re
import sys
import time

from aiohttp import web

from ..core import Plugin

SERVICE_TYPES = [
    "_hap._tcp", "_airplay._tcp", "_raop._tcp", "_companion-link._tcp", "_smb._tcp",
    "_afpovertcp._tcp", "_qdiscover._tcp", "_http._tcp", "_googlecast._tcp",
    "_spotify-connect._tcp", "_ihsp._tcp", "_matter._tcp", "_matterc._udp", "_ipp._tcp",
    "_printer._tcp", "_sonos._tcp", "_hue._tcp", "_esphomelib._tcp", "_ssh._tcp",
    "_workstation._tcp", "_device-info._tcp", "_mediaremotetv._tcp", "_home-assistant._tcp",
    "_daap._tcp", "_nfs._tcp", "_sftp-ssh._tcp", "_miio._udp", "_amzn-wplay._tcp",
]
OFFLINE_AFTER = 10 * 60
# whose instance name wins when several services describe the same host
NAME_RANK = ["_airplay._tcp", "_googlecast._tcp", "_qdiscover._tcp", "_ihsp._tcp",
             "_hap._tcp", "_companion-link._tcp", "_smb._tcp", "_spotify-connect._tcp"]

# MAC prefix -> vendor (a few common in homes; not exhaustive)
VENDORS = {
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
    "d8:3a:dd": "Raspberry Pi", "2c:cf:67": "Raspberry Pi",
    "84:17:15": "KEF", "00:08:9b": "QNAP", "24:5e:be": "QNAP",
    "70:b8:f6": "Espressif", "24:0a:c4": "Espressif", "30:ae:a4": "Espressif",
    "a4:cf:12": "Espressif", "84:f3:eb": "Espressif", "ec:fa:bc": "Espressif",
    "7c:9e:bd": "Espressif", "3c:71:bf": "Espressif", "24:6f:28": "Espressif",
    "08:3a:f2": "Espressif", "c8:c9:a3": "Espressif", "34:85:18": "Espressif",
    "48:3f:da": "Espressif", "40:22:d8": "Espressif", "bc:ff:4d": "Espressif",
    "44:91:60": "Murata", "b0:be:76": "TP-Link", "50:c7:bf": "TP-Link",
    "00:17:88": "Philips Hue", "ec:b5:fa": "Philips Hue", "68:ec:8a": "IKEA",
    "f8:2e:0c": "IKEA",
}



def _short(name: str, service_type: str) -> str:
    inst = name[: -len(service_type) - 1] if name.endswith(service_type) else name
    inst = inst.split("@", 1)[-1]  # raop: "MAC@Name"
    return inst.replace("\\032", " ").strip()


def _props(info) -> dict:
    out = {}
    for k, v in (info.properties or {}).items():
        try:
            out[k.decode()] = v.decode() if isinstance(v, bytes) else v
        except Exception:
            pass
    return out


def classify(dev: dict) -> tuple[str, str]:
    """-> (kind, suggested plugin)."""
    svc = dev["services"]
    model = (dev.get("model") or "").lower()
    maker = (dev.get("manufacturer") or "").lower()
    if model.startswith("appletv"):
        return "apple_tv", "appletv"
    if model.startswith("audioaccessory") or "homepod" in model:
        return "homepod", ""
    if "kef" in maker or dev.get("vendor") == "KEF":
        return "speaker", "kef"
    if "hisense" in maker:
        return "tv", "hisense"
    if any(k in (dev.get("name") or "").lower() for k in ("freebox", "iliadbox", "fritz")):
        return "router", ""
    if model.startswith(("mac", "imac")):
        return "computer", ""
    if "_ihsp._tcp" in svc:
        return "ikea_hub", "dirigera"
    if "_qdiscover._tcp" in svc or "nas" in (dev.get("name") or "").lower():
        return "nas", "media"
    if "_googlecast._tcp" in svc:
        return "cast", ""
    if "_ipp._tcp" in svc or "_printer._tcp" in svc:
        return "printer", ""
    if "_hue._tcp" in svc:
        return "hub", ""
    if "_home-assistant._tcp" in svc or "hass" in (dev.get("name") or "").lower():
        return "home_assistant", ""
    if "_hap._tcp" in svc:
        cat = dev.get("hap_category")
        return {"31": "tv", "2": "hub", "5": "light", "10": "sensor"}.get(cat, "homekit"), ""
    if "_sonos._tcp" in svc or "_spotify-connect._tcp" in svc:
        return "speaker", ""
    if "_workstation._tcp" in svc:
        return "computer", ""
    if "_esphomelib._tcp" in svc or dev.get("vendor") == "Espressif":
        return "iot", ""
    if dev.get("vendor") == "Raspberry Pi":
        return "computer", ""
    if "_smb._tcp" in svc or "_afpovertcp._tcp" in svc:
        return "computer", ""
    return "device", ""


class NetworkPlugin(Plugin):
    name = "network"
    title = "Network"

    def __init__(self, hub, config):
        super().__init__(hub, config)
        self.devices: dict[str, dict] = hub.store.get("network.devices", {})
        self.browsers = []
        self._dirty = False

    async def start(self):
        from zeroconf.asyncio import AsyncServiceBrowser

        zc = self.hub.zeroconf.zeroconf
        types = [f"{t}.local." for t in SERVICE_TYPES]
        self.browsers.append(AsyncServiceBrowser(zc, types, handlers=[self._on_service]))
        if self.config.get("arp", True):
            self.hub.every(60, self._read_arp, "network arp", initial_delay=5)
        self.hub.every(60, self._housekeeping, "network housekeeping")

    async def stop(self):
        for b in self.browsers:
            await b.async_cancel()
        self._save()

    def status(self):
        return {"devices": len(self.devices),
                "online": sum(1 for d in self.devices.values() if d.get("online"))}

    def routes(self):
        return [web.get("/devices", self.list_devices)]

    async def list_devices(self, request):
        # the Ariston gateway id is its MAC address
        ariston = self.hub.plugins.get("ariston")
        gw = (ariston.layout.get("gateway") or "").lower() if ariston else ""
        for d in self.devices.values():
            d["configured"] = bool(d.get("suggested_plugin")) and \
                d["suggested_plugin"] in self.hub.plugins
            if gw and d.get("mac", "").replace(":", "") == gw:
                d.update(name=d.get("name") or "Ariston boiler", kind="boiler",
                         manufacturer="Ariston", suggested_plugin="ariston", configured=True)
        return web.json_response(sorted(self.devices.values(),
                                        key=lambda d: (not d.get("online"), d.get("name") or "")))

    # -- mDNS ------------------------------------------------------------------------------

    def _on_service(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            return  # liveness is handled by last_seen; records also expire quietly
        self.hub.spawn(self._resolve(service_type, name), "network resolve")

    async def _resolve(self, service_type: str, name: str):
        from zeroconf.asyncio import AsyncServiceInfo

        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self.hub.zeroconf.zeroconf, 3000):
            return
        ips = [a for a in info.parsed_addresses() if ":" not in a and not a.startswith("127.")]
        if not ips:
            return
        props = _props(info)
        stype = service_type.removesuffix(".local.")
        if stype == "_qdiscover._tcp":
            # QNAP packs everything in one "a=b,c=d,..." TXT string
            flat = ",".join(f"{k}={v}" for k, v in props.items())
            props = dict(p.split("=", 1) for p in flat.split(",") if "=" in p)
        short = _short(name, service_type)
        d = self._device(ips[0])
        d["services"] = sorted(set(d["services"]) | {stype})
        if info.server:
            d["hostname"] = info.server.rstrip(".")
        # friendliest names first
        rank = NAME_RANK.index(stype) if stype in NAME_RANK else len(NAME_RANK)
        if rank <= d.get("name_rank", 99):
            if stype == "_hap._tcp":
                short = re.sub(r"(\s+[0-9A-F]{4,6})+$", "", short)
            d["name"], d["name_rank"] = short, rank
        if props.get("displayModel"):
            d["model"] = props["displayModel"]
        elif props.get("model") and (stype != "_device-info._tcp" or not d.get("model")):
            d["model"] = props["model"]
        elif props.get("md") and not d.get("model"):
            d["model"] = props["md"]
        if props.get("manufacturer") or props.get("integrator"):
            d["manufacturer"] = props.get("manufacturer") or props.get("integrator")
        if stype == "_hap._tcp" and props.get("ci"):
            d["hap_category"] = props["ci"]
        if stype == "_qdiscover._tcp":
            d["manufacturer"] = "QNAP"
        self._touch(d)

    # -- ARP ---------------------------------------------------------------------------------

    async def _read_arp(self):
        cmd = ["arp", "-an"] if sys.platform == "darwin" else ["ip", "neigh"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        for line in out.decode(errors="ignore").splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)\D.*?(([0-9a-f]{1,2}:){5}[0-9a-f]{1,2})", line, re.I)
            if not m or "FAILED" in line or "incomplete" in line:
                continue
            ip = m.group(1)
            if ip.endswith(".255") or ip.startswith(("224.", "239.", "169.254.")):
                continue
            mac = ":".join(p.zfill(2) for p in m.group(2).lower().split(":"))
            d = self._device(ip)
            d["mac"] = mac
            if int(mac[:2], 16) & 2:
                d["vendor"] = "private MAC"
            elif mac[:8] in VENDORS:
                d["vendor"] = VENDORS[mac[:8]]
            self._touch(d)

    # -- bookkeeping ----------------------------------------------------------------------------

    def _device(self, ip: str) -> dict:
        d = self.devices.get(ip)
        if d is None:
            now = time.time()
            d = self.devices[ip] = {"id": ip, "ip": ip, "name": "", "services": [],
                                    "first_seen": now, "last_seen": now, "online": True}
        return d

    def _touch(self, d: dict):
        d["last_seen"] = time.time()
        d["online"] = True
        kind, plugin = classify(d)
        d["kind"] = kind
        d["suggested_plugin"] = plugin or ""
        d["configured"] = bool(plugin) and plugin in self.hub.plugins
        self._dirty = True
        self.hub.publish("network", d)

    async def _housekeeping(self):
        now = time.time()
        for d in self.devices.values():
            online = now - d.get("last_seen", 0) < OFFLINE_AFTER
            if online != d.get("online"):
                d["online"] = online
                self._dirty = True
                self.hub.publish("network", d)
        # forget devices not seen for 30 days
        for ip in [ip for ip, d in self.devices.items() if now - d["last_seen"] > 30 * 86400]:
            del self.devices[ip]
        self._save()

    def _save(self):
        if self._dirty:
            self.hub.store.set("network.devices", self.devices)
            self._dirty = False


PLUGIN = NetworkPlugin
