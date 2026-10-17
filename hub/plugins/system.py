"""This hub: CPU, memory, disk, network traffic, Wi-Fi signal and internet latency of the
machine Casa runs on, recorded as sensors so the History page can chart them.

Only the hub's own traffic is visible here: per-device Wi-Fi usage of other devices would
need a router integration.

    [plugins.system]
    interval = 15
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time

from ..core import Entity, Plugin
from ..i18n import _

LATENCY_EVERY = 60  # seconds between internet checks


def counter_rate(prev: int | None, cur: int, dt: float) -> float | None:
    """Bytes/s from two readings of a monotonic byte counter, or None if unusable.

    A 32-bit counter that wrapped (32-bit kernels, old drivers) is corrected; any other
    decrease is a reset (interface restart) and yields None.
    """
    if prev is None or dt <= 0:
        return None
    if cur >= prev:
        return (cur - prev) / dt
    if prev >= 2**31 and prev < 2**32 and cur < 2**31:
        return (cur + 2**32 - prev) / dt
    return None


def mbit(bytes_per_s: float | None) -> float | None:
    return None if bytes_per_s is None else round(bytes_per_s * 8 / 1e6, 3)


def dbm_quality(level: float | None, quality: float | None = None) -> tuple[float | None, int | None]:
    """-> (dBm, link quality 0-100). Derives quality from dBm when not given."""
    if level is None:
        return None, None
    if quality is None:
        quality = 2 * (level + 100)  # common approximation: -100 dBm = 0 %, -50 dBm = 100 %
    return level, max(0, min(100, round(quality)))


def parse_proc_wireless(text: str, iface: str) -> tuple[float, int] | None:
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        f = rest.split()
        if len(f) < 3:
            return None
        link, level = float(f[1].rstrip(".")), float(f[2].rstrip("."))
        if level > 0:  # some drivers report 0..255 instead of dBm
            level -= 256
        dbm, q = dbm_quality(level, link / 70 * 100 if link else None)
        return dbm, q
    return None


def parse_iw_link(text: str) -> tuple[float, int] | None:
    m = re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", text)
    if not m:
        return None
    dbm, q = dbm_quality(float(m.group(1)))
    return dbm, q


def default_interface() -> str | None:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/route") as f:
                for line in f.read().splitlines()[1:]:
                    p = line.split()
                    if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                        return p[0]
        else:
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def cpu_temperature(psutil) -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass
    try:
        temps = psutil.sensors_temperatures()  # Linux only
        for entries in temps.values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return None


class SystemPlugin(Plugin):
    name = "system"
    title = "This hub"
    icon = "cpu"
    description = ("CPU, temperature, memory, disk, network speed, Wi-Fi signal and internet latency "
                   "of the hub itself, recorded in History. Traffic of other devices on your Wi-Fi "
                   "isn't visible to the hub (that needs a router integration).")
    schema = [
        {"key": "interval", "label": "Update every", "type": "number", "default": 15,
         "min": 5, "max": 3600, "unit": "s"},
        {"key": "interface", "label": "Network interface", "type": "text", "default": "",
         "placeholder": "auto", "advanced": True,
         "help": "e.g. wlan0 or eth0. Empty: the interface of the default route."},
        {"key": "latency_host", "label": "Internet check host", "type": "text",
         "default": "1.1.1.1", "advanced": True,
         "help": "Latency is the TCP connect time to this host on port 443, once a minute."},
    ]

    async def start(self) -> None:
        import psutil

        self.psutil = psutil
        self.iface: str | None = self.config.get("interface") or None
        self.iface_checked = 0.0
        self.prev_net: tuple[float, int, int] | None = None
        self.last_latency = 0.0
        self.data_dir = str(getattr(self.hub, "data_dir", "/"))
        psutil.cpu_percent(None)  # prime
        self.hub.add_entity(Entity(
            "system.cpu", _("CPU"), "sensor", self.name, "",
            attrs={"units": {"usage": "%", "temperature": "°C", "load": ""},
                   "history": ["usage", "temperature", "load"], "primary": "temperature", "group": _("This hub"), "icon": "cpu"}))
        self.hub.add_entity(Entity(
            "system.memory", _("Memory"), "sensor", self.name, "",
            attrs={"units": {"used": "%", "free": "MB"}, "history": ["used"], "primary": "used", "group": _("This hub"), "icon": "cpu"}))
        self.hub.add_entity(Entity(
            "system.disk", _("Disk"), "sensor", self.name, "",
            attrs={"units": {"free": "GB", "used": "%"}, "history": ["free"], "primary": "free", "group": _("This hub"), "icon": "storage"}))
        self.hub.add_entity(Entity(
            "system.network", _("Network"), "sensor", self.name, "",
            attrs={"units": {"download": "Mbit/s", "upload": "Mbit/s", "signal": "dBm", "quality": "%"},
                   "history": ["download", "upload", "signal", "quality"], "primary": "download", "group": _("This hub"), "icon": "wifi"}))
        self.hub.add_entity(Entity(
            "system.internet", _("Internet"), "sensor", self.name, "",
            attrs={"units": {"latency": "ms"}, "history": ["latency"], "primary": "latency", "group": _("This hub"), "icon": "wifi"}))
        interval = max(5, float(self.config.get("interval", 15) or 15))
        self.hub.every(interval, self.poll, "system", initial_delay=1)

    async def poll(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.sample)
        if time.time() - self.last_latency >= LATENCY_EVERY:
            self.last_latency = time.time()
            await self.check_internet()

    # -- sampling (blocking, runs in a thread) ---------------------------------------

    def sample(self) -> None:
        ps, hub = self.psutil, self.hub
        st: dict = {"usage": round(ps.cpu_percent(None), 1)}
        t = cpu_temperature(ps)
        if t is not None:
            st["temperature"] = t
        try:
            st["load"] = round(ps.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        st["cores"] = ps.cpu_count() or 0
        st["uptime"] = int(time.time() - ps.boot_time())
        hub.update("system.cpu", True, **st)

        vm = ps.virtual_memory()
        hub.update("system.memory", True, used=round(vm.percent, 1),
                   free=round(vm.available / 1e6), total=round(vm.total / 1e6))
        try:
            du = ps.disk_usage(self.data_dir)
            hub.update("system.disk", True, free=round(du.free / 1e9, 1),
                       used=round(du.percent, 1), total=round(du.total / 1e9, 1))
        except OSError:
            pass
        self.sample_network()

    def sample_network(self) -> None:
        ps = self.psutil
        now = time.time()
        if not self.config.get("interface") and now - self.iface_checked > 60:
            self.iface_checked = now
            found = default_interface()
            if found != self.iface:
                self.iface, self.prev_net = found, None
        if not self.iface:
            return
        counters = ps.net_io_counters(pernic=True).get(self.iface)
        if counters is None:
            self.hub.update("system.network", False)
            return
        st: dict = {"interface": self.iface}
        if self.prev_net:
            t0, rx0, tx0 = self.prev_net
            dt = now - t0
            down = mbit(counter_rate(rx0, counters.bytes_recv, dt))
            up = mbit(counter_rate(tx0, counters.bytes_sent, dt))
            if down is not None:
                st["download"] = down
            if up is not None:
                st["upload"] = up
        self.prev_net = (now, counters.bytes_recv, counters.bytes_sent)
        wifi = self.wifi_signal(self.iface)
        if wifi:
            st["signal"], st["quality"] = wifi
        self.hub.update("system.network", True, **st)

    @staticmethod
    def wifi_signal(iface: str) -> tuple[float, int] | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            with open("/proc/net/wireless") as f:
                r = parse_proc_wireless(f.read(), iface)
            if r:
                return r
        except OSError:
            return None
        try:
            out = subprocess.run(["iw", "dev", iface, "link"], capture_output=True,
                                 text=True, timeout=3).stdout
            return parse_iw_link(out)
        except Exception:
            return None

    async def check_internet(self) -> None:
        host = self.config.get("latency_host") or "1.1.1.1"
        t0 = time.perf_counter()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, 443), 5)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            w.close()
            self.hub.update("system.internet", True, latency=ms, online=True)
        except Exception:
            self.hub.update("system.internet", True, online=False)


PLUGIN = SystemPlugin
