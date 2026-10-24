"""Find games on the trackers enabled in Torrent search and download them with Transmission.

Downloads land in ``<games root>/pc`` or ``<games root>/emu/<system>``. When one finishes the torrent
is removed from Transmission (the data stays), archives are unpacked in place, and the library is
rescanned. Our downloads are kept in the store key ``games.downloads``; the Media plugin's own
download table is never involved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from ...i18n import _

log = logging.getLogger("hub.games")

STORE = "games.downloads"
SYSTEM = re.compile(r"^[a-z0-9_-]{1,32}$")
ACTIVE = ("downloading", "extracting")
MAX_UNPACKED = 80 * 1024 ** 3        # refuse archives that claim more than this
MAX_RATIO = 1000                     # …or that compress a big file more than this (zip bomb)
MAX_ENTRIES = 50000
TOOL_TIMEOUT = 3600

PLATFORMS = [
    ("pc", "PC"), ("nes", "NES"), ("snes", "Super Nintendo"), ("n64", "Nintendo 64"), ("gb", "Game Boy"),
    ("gbc", "Game Boy Color"), ("gba", "Game Boy Advance"), ("nds", "Nintendo DS"), ("n3ds", "Nintendo 3DS"),
    ("gc", "GameCube"), ("wii", "Wii"), ("wiiu", "Wii U"), ("switch", "Nintendo Switch"),
    ("psx", "PlayStation"), ("ps2", "PlayStation 2"), ("psp", "PlayStation Portable"), ("psvita", "PlayStation Vita"),
    ("genesis", "Sega Genesis / Mega Drive"), ("mastersystem", "Sega Master System"),
    ("gamegear", "Sega Game Gear"), ("saturn", "Sega Saturn"), ("dreamcast", "Sega Dreamcast"),
    ("arcade", "Arcade"), ("neogeo", "Neo Geo"), ("atari2600", "Atari 2600"), ("pcengine", "PC Engine"),
]


def platforms() -> list[dict]:
    return [{"key": k, "label": v} for k, v in PLATFORMS]


def valid_platform(p) -> bool:
    return isinstance(p, str) and (p == "pc" or bool(SYSTEM.match(p)))


# -- archives -----------------------------------------------------------------------------------

class UnpackError(Exception):
    pass


def _inside(base: Path, target: Path) -> bool:
    return target == base or base in target.parents


def zip_plan(zf: zipfile.ZipFile, dest: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    """Validate every member (zip-slip, symlinks, absurd sizes) → [(member, target path)]."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise UnpackError(_("too many files in the archive"))
    base = dest.resolve()
    plan, total = [], 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or "\x00" in name:
            raise UnpackError(_("unsafe path in the archive: {name}", name=info.filename))
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise UnpackError(_("unsafe path in the archive: {name}", name=info.filename))
        if not parts:
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:      # symlink member
            continue
        target = (base.joinpath(*parts)).resolve()
        if not _inside(base, target):
            raise UnpackError(_("unsafe path in the archive: {name}", name=info.filename))
        if not info.is_dir():
            total += info.file_size
            if info.file_size > (1 << 20) and info.file_size > max(info.compress_size, 1) * MAX_RATIO:
                raise UnpackError(_("the archive looks like a zip bomb"))
        plan.append((info, target))
    if total > MAX_UNPACKED:
        raise UnpackError(_("the archive is too large"))
    return plan


def unzip(archive: Path, dest: Path) -> int:
    """Extract ``archive`` into ``dest`` (created). Returns files written. Raises UnpackError."""
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as err:
        raise UnpackError(_("not a valid zip: {err}", err=err))
    with zf:
        plan = zip_plan(zf, dest)
        need = sum(i.file_size for i, _t in plan if not i.is_dir())
        dest.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(dest).free < need * 1.02 + (100 << 20):
            raise UnpackError(_("not enough free space to unpack"))
        n = 0
        for info, target in plan:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:          # the header lied
                            raise UnpackError(_("the archive looks like a zip bomb"))
                        out.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, OSError) as err:
                target.unlink(missing_ok=True)
                raise UnpackError(_("could not unpack {name}: {err}", name=info.filename, err=err))
            except UnpackError:
                target.unlink(missing_ok=True)
                raise
            n += 1
    return n


def zip_single_top(archive: Path) -> bool:
    """True when every member sits under one top-level folder."""
    try:
        with zipfile.ZipFile(archive) as zf:
            tops = {n.replace("\\", "/").lstrip("/").split("/")[0] for n in zf.namelist() if n.strip("/")}
            return len(tops) == 1 and any("/" in n.strip("/") for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


RAR_PART = re.compile(r"\.part0*(\d+)\.rar$", re.I)


def find_archives(payload: Path) -> list[Path]:
    """Archives in a payload (a file or a folder), without the follow-up parts of a split set."""
    cands = [payload] if payload.is_file() else sorted(p for p in payload.rglob("*") if p.is_file())
    out = []
    for p in cands:
        n = p.name.lower()
        m = RAR_PART.search(n)
        if m and m.group(1) != "1":
            continue
        if n.endswith((".zip", ".rar", ".7z")):
            out.append(p)
    return out


def tool_for(name: str) -> list[str] | None:
    """Command prefix to extract a .rar/.7z, or None when no tool is installed."""
    ext = name.lower().rsplit(".", 1)[-1]
    for tool in ("7z", "7zz", "7za"):
        if shutil.which(tool):
            return [shutil.which(tool), "x", "-y"]
    if ext == "rar" and shutil.which("unrar"):
        return [shutil.which("unrar"), "x", "-y"]
    if shutil.which("unar"):
        return [shutil.which("unar"), "-q", "-f"]
    return None


async def run_tool(cmd: list[str], archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = Path(cmd[0]).name
    if base.startswith("7z"):
        argv = [*cmd, "-o" + str(dest), str(archive)]
    elif base == "unrar":
        argv = [*cmd, str(archive), str(dest) + os.sep]
    else:
        argv = [*cmd, "-o", str(dest), str(archive)]
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(proc.wait(), TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise UnpackError(_("unpacking took too long"))
    except asyncio.CancelledError:   # plugin reload: don't leave a second extractor running
        proc.kill()
        raise
    if code != 0:
        raise UnpackError(_("{tool} failed (exit {code})", tool=base, code=code))


_UNPACKING: set[str] = set()   # archives being unpacked (process-wide: survives a plugin reload)


def _unzip_marked(arc: Path, dest: Path, key: str) -> None:
    try:
        unzip(arc, dest)
    finally:            # in the worker thread: cleared when the unzip really ends, even after a cancel
        _UNPACKING.discard(key)


async def unpack_payload(payload: Path, root: Path) -> str:
    """Unpack every archive of a finished download in place. → "" when done, else a reason it needs a hand."""
    root = root.resolve()
    if not _inside(root, payload.resolve()) or payload.resolve() == root:
        raise UnpackError(_("outside the games folder"))
    if _inside(root / "emu", payload.resolve()):
        return ""   # ROMs: emulators read .zip/.7z directly, and arcade/Neo Geo sets must stay zipped
    loop = asyncio.get_running_loop()
    for arc in await loop.run_in_executor(None, find_archives, payload):   # NAS walk: off the event loop
        if not _inside(root, arc.resolve()):
            continue
        key = str(arc.resolve())
        while key in _UNPACKING:        # the same archive from before a reload: let it finish
            await asyncio.sleep(2)
        if not await loop.run_in_executor(None, arc.exists):
            continue
        in_pc = arc.parent.resolve() == root / "pc"        # a bare archive straight in pc/
        name = arc.name.lower()
        if name.endswith(".zip"):
            if in_pc:
                single = await loop.run_in_executor(None, zip_single_top, arc)
                dest = arc.parent if single else arc.parent / arc.stem
            else:
                dest = arc.parent
            _UNPACKING.add(key)
            await loop.run_in_executor(None, _unzip_marked, arc, dest, key)
        else:
            cmd = tool_for(name)
            if not cmd:
                return _("Archive {name} needs 7z, unar or unrar on the hub to unpack", name=arc.name)
            dest = arc.parent / arc.stem if in_pc else arc.parent
            _UNPACKING.add(key)
            try:
                await run_tool(cmd, arc, dest)
            finally:
                _UNPACKING.discard(key)
        await loop.run_in_executor(None, arc.unlink)
    return ""


# -- the service --------------------------------------------------------------------------------

class Finder:
    """Search + download bookkeeping for the Games plugin (``plugin`` gives hub, root and rescan)."""

    def __init__(self, plugin):
        self.p = plugin
        self.live: dict[str, dict] = {}       # hash → progress numbers from the last poll
        self._busy: set[str] = set()

    # -- storage
    def entries(self) -> list[dict]:
        v = self.p.hub.store.get(STORE, [])
        return [e for e in v if isinstance(e, dict) and e.get("hash")] if isinstance(v, list) else []

    def _save(self, entries: list[dict]) -> None:
        self.p.hub.store.set(STORE, entries)

    def _update(self, h: str, **kw) -> None:
        es = self.entries()
        for e in es:
            if e["hash"] == h:
                e.update(kw)
        self._save(es)

    # -- plugins we lean on
    def _torrents(self):
        t = self.p.hub.plugins.get("torrents")
        if t is None or not getattr(t, "engine", None) or not t.enabled_ids():
            raise ValueError(_("Turn on Torrent search and enable some trackers"))
        return t

    def _tx(self):
        m = self.p.hub.plugins.get("media")
        tx = getattr(m, "transmission", None)
        if tx is None or not getattr(tx, "enabled", False):
            raise ValueError(_("Downloads need the Media plugin with Transmission turned on"))
        return tx

    def target_dir(self, platform: str) -> str:
        root = Path(os.path.expanduser(self.p.root))
        return str(root / "pc") if platform == "pc" else str(root / "emu" / platform)

    # -- search
    async def search(self, q: str, platform: str, page: int) -> dict:
        q = " ".join((q or "").split())
        if len(q) < 2:
            raise ValueError(_("Type at least 2 characters"))
        if len(q) > 100:
            raise ValueError(_("The search is too long (max {n} characters)", n=100))
        if not valid_platform(platform):
            raise ValueError(_("Unknown platform"))
        try:
            t = self._torrents()
        except ValueError as err:  # torrent search off: the page shows a "turn it on" card
            return {"disabled": True, "error": str(err), "results": [], "providers": [], "has_more": False}
        size = min(max(int(t.config.get("page_size", 20) or 20), 5), 50)
        merged, statuses, more = await t.engine.search(t.session, q, max(page, 1), size, sorted(t.enabled_ids()),
                                                       category="games")
        for st in statuses:
            t._note(st)
        from ..torrents.engine import refine
        out = [{"title": r.title, "size": r.size, "seeders": r.seeders, "leechers": r.leechers, "source": r.source,
                "sources": r.sources, "magnet": r.magnet, "url": r.url, "hash": r.hash}
               for r in refine(merged, "seeders", "desc")]
        error = None if any(s["ok"] for s in statuses) else _("Every provider failed. See details below.")
        provs = [{"id": s["id"], "name": s["name"], "ok": s["ok"], "count": s.get("count", 0), "error": s.get("error")}
                 for s in statuses]
        return {"results": out, "providers": provs, "has_more": bool(more), "error": error}

    # -- download
    async def add(self, title: str, platform: str, magnet: str = "", url: str = "") -> str:
        title = " ".join(str(title or "").split())[:300]
        if not title:
            raise ValueError(_("Missing title"))
        if not valid_platform(platform):
            raise ValueError(_("Unknown platform"))
        link = magnet or url
        if not (isinstance(link, str) and (link.startswith("magnet:?") or link.startswith(("http://", "https://")))):
            raise ValueError(_("Give a magnet link or a torrent URL"))
        tx = self._tx()
        folder = self.target_dir(platform)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as err:
            raise ValueError(_("Cannot create {path}: {err}", path=folder, err=str(err)))
        from ..media.transmission import TransmissionError
        try:
            res = await tx.add(link, download_dir=folder)
        except TransmissionError as err:
            raise ValueError(str(err))
        h = (res.get("hash") or "").lower()
        if not h:
            raise ValueError(_("Transmission did not accept the torrent"))
        es = [e for e in self.entries() if e["hash"] != h]
        es.append({"hash": h, "title": title, "platform": platform, "added": time.time(), "state": "downloading",
                   "error": None, "dir": folder, "payload": None})
        self._save(es)
        return h

    def listing(self) -> list[dict]:
        out = []
        for e in self.entries():
            lv = self.live.get(e["hash"]) or {}
            done = e["state"] in ("done", "needs_extraction")
            out.append({"hash": e["hash"], "title": e["title"], "platform": e["platform"], "added": e.get("added"),
                        "progress": 1.0 if done else lv.get("progress", 0.0), "rate": 0 if done else lv.get("rate", 0),
                        "eta": None if done else lv.get("eta"), "state": e["state"], "error": e.get("error")})
        return out

    async def cancel(self, h: str) -> None:
        es = self.entries()
        e = next((x for x in es if x["hash"] == h), None)
        if not e:
            raise KeyError(_("Download not found"))
        if e["state"] == "downloading":
            try:
                await self._tx().remove(h, delete_data=True)
            except Exception as err:      # already gone from Transmission is fine
                log.info("cancel %s: %s", h, err)
        self._save([x for x in es if x["hash"] != h])
        self.live.pop(h, None)

    # -- background
    async def resume(self) -> None:
        """After a restart: finish downloads that were being unpacked."""
        for e in self.entries():
            if e["state"] == "extracting" and e.get("payload"):
                self.p.hub.spawn(self._finish(e["hash"]), name="games-finish")

    async def poll(self) -> None:
        active = [e for e in self.entries() if e["state"] == "downloading"]
        if not active:
            return
        try:
            tx = self._tx()
            found = {t["hash"]: t for t in await tx.torrents([e["hash"] for e in active])}
        except Exception as err:
            log.debug("games poll: %s", err)
            return
        for e in active:
            h, t = e["hash"], found.get(e["hash"])
            if t is None:
                self._update(h, state="error", error=_("The torrent is no longer in Transmission"))
                continue
            done = t.get("percentDone", 0) >= 1 or t.get("leftUntilDone", 1) == 0 and t.get("metadataPercentComplete", 1) >= 1
            self.live[h] = {"progress": 1.0 if done else float(t.get("percentDone") or 0.0),
                            "rate": int(t.get("rateDownload") or 0),
                            "eta": t["eta"] if isinstance(t.get("eta"), int) and t["eta"] >= 0 else None}
            if t.get("error"):
                self._update(h, error=str(t.get("errorString") or "")[:200] or None)
            if done and h not in self._busy:
                payload = os.path.join(t.get("downloadDir") or e["dir"], t.get("name") or "")
                self._update(h, state="extracting", payload=payload, error=None)
                self.p.hub.spawn(self._finish(h), name="games-finish")

    async def _finish(self, h: str) -> None:
        if h in self._busy:
            return
        self._busy.add(h)
        try:
            e = next((x for x in self.entries() if x["hash"] == h), None)
            if not e:
                return
            try:   # the data stays; only the torrent leaves Transmission
                await self._tx().remove(h, delete_data=False)
            except Exception as err:
                log.info("remove %s: %s", h, err)
            root = Path(os.path.expanduser(self.p.root))
            state, msg = "done", None
            try:
                payload = Path(e["payload"])
                if payload.exists():
                    msg = await unpack_payload(payload, root)
                    if msg:
                        state = "needs_extraction"
                else:
                    state, msg = "error", _("The downloaded files were not found")
            except UnpackError as err:
                state, msg = "error", _("Could not unpack: {err}", err=str(err))
            except OSError as err:
                state, msg = "error", str(err)
            self._update(h, state=state, error=msg or None)
            try:
                await self.p.rescan()
            except Exception:
                log.exception("rescan after download")
        finally:
            self._busy.discard(h)
