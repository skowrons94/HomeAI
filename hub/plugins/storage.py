"""Storage & NAS: find a NAS on the network, mount it, suggest library folders, track free space.

    [plugins.storage]
    auto_mount = true
    [plugins.storage.nas]
    host = "NAS.local"
    share = "Multimedia"
    username = ""
    password = ""
    mount_point = "/mnt/nas"      # macOS default: ~/Casa/NAS
    version = "auto"              # SMB version: auto | 1.0 | 2.0 | 2.1 | 3.0 (QNAP TS-419P II: 2.0)

Linux mounts go through the root helper ``casa-mount`` (deploy/casa-mount) via sudo;
macOS mounts with ``mount_smbfs`` as the current user.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ..core import Entity, Plugin
from ..settings import SECRET

MAC = sys.platform == "darwin"
DEFAULT_MOUNT = str(Path.home() / "Casa" / "NAS") if MAC else "/mnt/nas"
HELPER = "/usr/local/sbin/casa-mount"
VERSIONS = ("auto", "1.0", "2.0", "2.1", "3.0")

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,251}$")
SHARE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._$-]{0,79}$")
USER_RE = re.compile(r"^[^\x00-\x1f:/\\]{0,64}$")

MOVIE_NAMES = {"movies", "movie", "film", "films", "filme", "cinema"}
SERIES_NAMES = {"series", "serie", "serie tv", "tv", "tv shows", "tvshows", "tv series",
                "telefilm", "shows", "serietv"}
DOWNLOAD_NAMES = {"downloads", "download", "incoming", "torrent", "torrents", "scaricati"}
SKIP = {"lost+found", "$recycle.bin", "system volume information"}
YEAR_DIR = re.compile(r"[\(\[](19|20)\d\d[\)\]]")
SEASON_DIR = re.compile(r"^(season|stagione|serie|s)\s*0*\d{1,2}\b", re.I)
EPISODE = re.compile(r"s\d{1,2}e\d{1,3}", re.I)
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}


def schema() -> list[dict]:
    return [
        {"key": "nas.host", "label": "NAS host", "type": "text", "placeholder": "NAS.local",
         "help": "Name or IP of the NAS. Use the panel below to find it."},
        {"key": "nas.share", "label": "Share", "type": "text", "placeholder": "Multimedia",
         "help": "SMB share name on the NAS."},
        {"key": "nas.username", "label": "Username", "type": "text",
         "help": "Leave empty for guest access."},
        {"key": "nas.password", "label": "Password", "type": "password"},
        {"key": "nas.mount_point", "label": "Mount point", "type": "path",
         "default": DEFAULT_MOUNT,
         "help": "Folder where the NAS appears. On Linux: /mnt/<name> or /media/<name>."},
        {"key": "nas.version", "label": "SMB version", "type": "select", "default": "auto",
         "options": list(VERSIONS), "advanced": True,
         "help": "Old NAS firmware needs a fixed version (QNAP TS-419P II: 2.0)."},
        {"key": "auto_mount", "label": "Mount automatically", "type": "bool", "default": True,
         "help": "Mount the NAS at start-up and re-mount it if it drops."},
    ]


# -- pure helpers (tested) ------------------------------------------------------------------


def valid_host(s: str) -> bool:
    return bool(HOST_RE.match(s or ""))


def valid_share(s: str) -> bool:
    return bool(SHARE_RE.match(s or ""))


def valid_user(s: str) -> bool:
    return bool(USER_RE.match(s or ""))


def valid_mount_point(p: str) -> bool:
    """Linux root helper only accepts /mnt/<name> or /media/<name>; mirror that here."""
    if MAC:
        return bool(p) and os.path.isabs(os.path.expanduser(p))
    return bool(re.fullmatch(r"/(mnt|media)/[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", p or ""))


def parse_smbclient(out: str) -> list[str]:
    """smbclient -L -g: 'Disk|name|comment' lines."""
    shares = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            shares.append(parts[1])
    return shares


def parse_smbutil(out: str) -> list[str]:
    shares = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}Disk\b", line)
        if m and not m.group(1).strip().endswith("$"):
            shares.append(m.group(1).strip())
    return shares


def _subdirs(path: str, limit: int = 400) -> list[str]:
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                n = e.name
                if n.startswith((".", "@", "#")) or n.lower() in SKIP:
                    continue
                try:
                    if e.is_dir():
                        out.append(n)
                except OSError:
                    continue
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return sorted(out)


def _content_scores(path: str) -> tuple[float, float]:
    """(movie-likeness, series-likeness) in 0..1 from the children of a folder."""
    kids = _subdirs(path)[:30]
    if not kids:
        return 0.0, 0.0
    movie = series = 0
    for k in kids:
        kp = os.path.join(path, k)
        subs = _subdirs(kp, 40)
        if any(SEASON_DIR.match(s) for s in subs):
            series += 1
            continue
        try:
            files = [f for f in os.listdir(kp)][:60]
        except OSError:
            files = []
        if any(EPISODE.search(f) for f in files):
            series += 1
        elif YEAR_DIR.search(k) or any(Path(f).suffix.lower() in VIDEO for f in files):
            movie += 1
    return movie / len(kids), series / len(kids)


def suggest_folders(root: str) -> dict:
    """Look two levels deep under ``root`` for movie / series / download folders."""
    cands: list[tuple[str, str]] = []  # (path, lowercase name)
    for a in _subdirs(root):
        pa = os.path.join(root, a)
        cands.append((pa, a.lower()))
        for b in _subdirs(pa, 60):
            cands.append((os.path.join(pa, b), b.lower()))
    best: dict[str, tuple[float, str]] = {}

    def offer(role, score, path):
        if score > best.get(role, (0, ""))[0]:
            best[role] = (score, path)

    for path, name in cands:
        depth_penalty = 0.05 * (path[len(root):].count("/") - 1)
        if name in MOVIE_NAMES:
            offer("movies_dir", 1.0 - depth_penalty, path)
        if name in SERIES_NAMES:
            offer("series_dir", 1.0 - depth_penalty, path)
        if name in DOWNLOAD_NAMES:
            offer("download_dir", 1.0 - depth_penalty, path)
        m, s = _content_scores(path)
        if m >= 0.5:
            offer("movies_dir", 0.5 * m - depth_penalty, path)
        if s >= 0.5:
            offer("series_dir", 0.5 * s - depth_penalty, path)
    out = {k: v[1] for k, v in best.items()}
    if "download_dir" in out:
        inc = os.path.join(out["download_dir"], "incomplete")
        if os.path.isdir(inc):
            out["download_dir"] = inc
    return out


def usage(path: str) -> dict | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    gb = 1024 ** 3
    return {"free_gb": round(u.free / gb, 1), "total_gb": round(u.total / gb, 1),
            "used_pct": round(100 * (u.total - u.free) / u.total, 1) if u.total else 0.0}


def is_mounted(path: str) -> bool:
    try:
        return bool(path) and os.path.ismount(path)
    except OSError:
        return False


async def run(cmd: list[str], stdin: str | None = None, env: dict | None = None,
              timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})})
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout:.0f}s")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class StoragePlugin(Plugin):
    name = "storage"
    title = "Storage & NAS"
    description = "Find your NAS, mount it and choose where downloads and libraries live."
    icon = "nas"
    ui = True
    schema = schema()

    def __init__(self, hub, config):
        super().__init__(hub, config)
        nas = config.get("nas") or {}
        self.host = str(nas.get("host") or "").strip()
        self.share = str(nas.get("share") or "").strip()
        self.user = str(nas.get("username") or "")
        self.password = str(nas.get("password") or "")
        self.mount_point = os.path.expanduser(str(nas.get("mount_point") or DEFAULT_MOUNT))
        self.version = str(nas.get("version") or "auto")
        self.auto_mount = bool(config.get("auto_mount", True))
        self.servers: dict[str, dict] = {}  # mDNS SMB servers by service name
        self.browser = None
        self.busy = False
        self.mount_error: str | None = None
        self._entities: set[str] = set()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self):
        if self.hub.zeroconf:
            from zeroconf.asyncio import AsyncServiceBrowser

            self.browser = AsyncServiceBrowser(self.hub.zeroconf.zeroconf, ["_smb._tcp.local."],
                                               handlers=[self._on_service])
        self.hub.every(120, self._refresh, "storage usage", initial_delay=1)
        if self.auto_mount and self.host and self.share:
            self.hub.every(300, self._ensure_mounted, "nas auto-mount", initial_delay=2)

    async def stop(self):
        if self.browser:
            await self.browser.async_cancel()

    def status(self):
        return {"mounted": is_mounted(self.mount_point), "mount_point": self.mount_point,
                "host": self.host, "share": self.share, "busy": self.busy,
                "error": self.mount_error, "platform": "mac" if MAC else "linux",
                "helper": MAC or os.path.exists(HELPER) or os.geteuid() == 0}

    # -- mDNS --------------------------------------------------------------------------------

    def _on_service(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            self.servers.pop(name, None)
            return
        self.hub.spawn(self._resolve(service_type, name), "smb resolve")

    async def _resolve(self, service_type, name):
        from zeroconf.asyncio import AsyncServiceInfo

        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self.hub.zeroconf.zeroconf, 3000):
            return
        ips = [a for a in info.parsed_addresses() if ":" not in a and not a.startswith("127.")]
        if not ips:
            return
        host = (info.server or "").rstrip(".") or ips[0]
        self.servers[name] = {"name": name.removesuffix("." + service_type).strip(),
                              "host": host, "ip": ips[0], "source": "mdns"}

    def discover(self) -> list[dict]:
        found = {d["ip"]: dict(d) for d in self.servers.values()}
        net = self.hub.plugins.get("network")
        for d in (getattr(net, "devices", None) or {}).values():
            if "_smb._tcp" in d.get("services", []) or d.get("kind") == "nas":
                if not d.get("online", True):
                    continue
                cur = found.setdefault(d["ip"], {"ip": d["ip"], "source": "network"})
                cur.setdefault("name", d.get("name") or d.get("hostname") or d["ip"])
                cur.setdefault("host", d.get("hostname") or d["ip"])
                cur["vendor"] = d.get("manufacturer") or d.get("vendor") or ""
        return sorted(found.values(), key=lambda d: (d.get("name") or "").lower())

    # -- shares ------------------------------------------------------------------------------

    async def list_shares(self, host: str, user: str, password: str, version: str = "auto"):
        if not valid_host(host):
            raise ValueError("invalid host")
        if not valid_user(user):
            raise ValueError("invalid username")
        if MAC:
            auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user and password \
                else (f"{quote(user, safe='')}@" if user else "")
            cmd = ["smbutil", "view"] + ([] if user else ["-G"]) + [f"//{auth}{host}"]
            rc, out, err = await run(cmd, timeout=15)
            parse = parse_smbutil
        else:
            if not shutil.which("smbclient"):
                raise RuntimeError("smbclient is not installed (sudo apt install smbclient)")
            cmd = ["smbclient", "-L", f"//{host}", "-g"]
            cmd += ["-U", user] if user else ["-N"]
            proto = {"1.0": "NT1", "2.0": "SMB2", "2.1": "SMB2", "3.0": "SMB3"}.get(version)
            if proto:
                cmd += ["-m", proto]
            rc, out, err = await run(cmd, env={"PASSWD": password} if user else None, timeout=15)
            parse = parse_smbclient
        shares = parse(out)
        if not shares and rc != 0:
            msg = (err or out).strip().splitlines()
            raise RuntimeError(msg[-1] if msg else "could not list shares")
        return shares

    # -- mount / unmount ---------------------------------------------------------------------

    async def mount(self, host=None, share=None, user=None, password=None,
                    mount_point=None, version=None):
        host, share = host or self.host, share or self.share
        user = self.user if user is None else user
        password = self.password if password in (None, SECRET) else password
        mp = os.path.expanduser(mount_point or self.mount_point)
        version = version or self.version
        if not valid_host(host):
            raise ValueError("invalid host")
        if not valid_share(share):
            raise ValueError("invalid share name")
        if not valid_user(user):
            raise ValueError("invalid username")
        if not valid_mount_point(mp):
            raise ValueError("mount point must be under /mnt or /media")
        if version not in VERSIONS:
            raise ValueError("invalid SMB version")
        if is_mounted(mp):
            return {"mounted": True, "mount_point": mp, "already": True}
        self.busy = True
        try:
            if MAC:
                os.makedirs(mp, exist_ok=True)
                auth = f"{quote(user or 'guest', safe='')}" + \
                    (f":{quote(password, safe='')}" if password else "") + "@"
                rc, out, err = await run(
                    ["mount_smbfs", f"//{auth}{host}/{quote(share, safe='')}", mp], timeout=40)
            else:
                cmd = [HELPER, "mount", host, share, mp, version]
                if os.geteuid() != 0:
                    cmd = ["sudo", "-n"] + cmd
                rc, out, err = await run(cmd, stdin=f"{user}\n{password}\n", timeout=60)
            if rc != 0 or not is_mounted(mp):
                msg = (err or out).strip().splitlines()
                self.mount_error = msg[-1] if msg else f"mount failed (exit {rc})"
                raise RuntimeError(self.mount_error)
            self.mount_error = None
            self.log.info("mounted //%s/%s on %s", host, share, mp)
            await self._refresh()
            return {"mounted": True, "mount_point": mp}
        finally:
            self.busy = False

    async def unmount(self, mount_point=None):
        mp = os.path.expanduser(mount_point or self.mount_point)
        if not valid_mount_point(mp):
            raise ValueError("invalid mount point")
        if not is_mounted(mp):
            return {"mounted": False}
        self.busy = True
        try:
            if MAC:
                cmd = ["umount", mp]
            else:
                cmd = [HELPER, "umount", mp]
                if os.geteuid() != 0:
                    cmd = ["sudo", "-n"] + cmd
            rc, out, err = await run(cmd, timeout=30)
            if rc != 0 and is_mounted(mp):
                msg = (err or out).strip().splitlines()
                raise RuntimeError(msg[-1] if msg else "umount failed (busy?)")
            await self._refresh()
            return {"mounted": False}
        finally:
            self.busy = False

    async def _ensure_mounted(self):
        if self.busy or is_mounted(self.mount_point):
            return
        try:
            await self.mount()
        except Exception as err:
            self.mount_error = str(err)
            self.log.warning("auto-mount of //%s/%s failed: %s", self.host, self.share, err)

    # -- usage entities ----------------------------------------------------------------------

    def folders(self) -> dict[str, str]:
        media = (self.hub.config.get("plugins") or {}).get("media") or {}
        out = {}
        for key in ("download_dir", "movies_dir", "series_dir"):
            if media.get(key):
                out[key] = str(media[key])
        out["nas"] = self.mount_point if self.host else ""
        return {k: v for k, v in out.items() if v}

    async def _refresh(self):
        labels = {"download_dir": "Downloads", "movies_dir": "Movies", "series_dir": "Series",
                  "nas": "NAS"}
        for key, path in self.folders().items():
            eid = f"storage.{key}"
            if eid not in self._entities:
                self._entities.add(eid)
                self.hub.add_entity(Entity(
                    id=eid, name=f"{labels[key]} space", kind="sensor", plugin=self.name,
                    attrs={"units": {"free_gb": "GB", "total_gb": "GB", "used_pct": "%"},
                           "path": path, "history": ["free_gb", "used_pct"]}))
            ent = self.hub.entities[eid]
            ent.attrs["path"] = path
            try:
                u = await asyncio.wait_for(asyncio.to_thread(usage, path), 8)
            except asyncio.TimeoutError:
                u = None
            if u is None:
                self.hub.update(eid, available=False)
            else:
                self.hub.update(eid, available=True, **u)

    async def info(self) -> dict:
        res = self.status()
        folders = {}
        for key, path in self.folders().items():
            u = await asyncio.to_thread(usage, path)
            folders[key] = {"path": path, "exists": os.path.isdir(path), **(u or {})}
        res["folders"] = folders
        return res

    # -- routes ------------------------------------------------------------------------------

    def routes(self):
        return [
            web.get("/discover", self.r_discover),
            web.post("/shares", self.r_shares),
            web.post("/mount", self.r_mount),
            web.post("/unmount", self.r_unmount),
            web.get("/status", self.r_status),
            web.get("/suggest", self.r_suggest),
        ]

    @staticmethod
    def _err(err, status=400):
        return web.json_response({"error": str(err)}, status=status)

    async def r_discover(self, request):
        return web.json_response(self.discover())

    async def r_status(self, request):
        return web.json_response(await self.info())

    async def r_shares(self, request):
        b = await request.json()
        host = (b.get("host") or self.host).strip()
        user = b.get("username", self.user)
        pw = b.get("password", "")
        if pw in ("", SECRET) and host == self.host and user == self.user:
            pw = self.password
        try:
            shares = await self.list_shares(host, user, pw, b.get("version") or self.version)
        except ValueError as err:
            return self._err(err)
        except Exception as err:
            return self._err(err, 502)
        return web.json_response({"shares": shares})

    async def r_mount(self, request):
        b = await request.json() if request.can_read_body else {}
        try:
            return web.json_response(await self.mount(
                b.get("host"), b.get("share"), b.get("username"), b.get("password"),
                b.get("mount_point"), b.get("version")))
        except ValueError as err:
            return self._err(err)
        except Exception as err:
            return self._err(err, 502)

    async def r_unmount(self, request):
        try:
            return web.json_response(await self.unmount())
        except ValueError as err:
            return self._err(err)
        except Exception as err:
            return self._err(err, 502)

    async def r_suggest(self, request):
        if not is_mounted(self.mount_point):
            return self._err("NAS is not mounted", 409)
        found = await asyncio.to_thread(suggest_folders, self.mount_point)
        return web.json_response({"root": self.mount_point, "suggestions": found,
                                  "missing": [k for k in ("movies_dir", "series_dir", "download_dir")
                                              if k not in found]})


PLUGIN = StoragePlugin
