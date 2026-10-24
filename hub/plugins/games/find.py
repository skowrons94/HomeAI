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
GOG_TIMEOUT = 6 * 3600               # a 40 GB GOG installer on a Pi 3 and a NAS is slow
GOG_SETUP = re.compile(r"^(setup|patch)_.+\.exe$", re.I)
GOG_LEFTOVERS = {"tmp", "commonappdata", "__support"}     # installer scaffolding, not game files
GOG_TEMP = ".casa-extract"

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
    if ext == "rar":   # Debian's 7-Zip is built without RAR: unrar or unar first
        if shutil.which("unrar"):
            return [shutil.which("unrar"), "x", "-y"]
        if shutil.which("unar"):
            return [shutil.which("unar"), "-q", "-f"]
    for tool in ("7z", "7zz", "7za"):
        if shutil.which(tool):
            return [shutil.which(tool), "x", "-y", "-mmt=2"]     # 2 of the Pi's 4 cores: the web app keeps the rest
    if ext == "rar" and shutil.which("unrar"):
        return [shutil.which("unrar"), "x", "-y"]
    if shutil.which("unar"):
        return [shutil.which("unar"), "-q", "-f"]
    return None


def background(argv: list[str]) -> list[str]:
    """Lowest CPU and disk priority (nice 19, idle I/O class): unpacking a game must never slow the web app.
    nice and ionice exec the tool, so killing the process still kills the tool."""
    pre = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    return pre + argv


async def run_argv(argv: list[str], timeout: float, log_tail: bool = False) -> tuple[int, str]:
    """Run an external tool off the event loop: killed on timeout or cancel. → (exit code, last output)."""
    import tempfile
    out = tempfile.TemporaryFile() if log_tail else None
    proc = await asyncio.create_subprocess_exec(
        *background(argv), stdout=out or asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.STDOUT if out else asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise UnpackError(_("unpacking took too long"))
    except asyncio.CancelledError:   # plugin reload or Cancel: don't leave a second extractor running
        proc.kill()
        raise
    finally:
        tail = ""
        if out:
            try:
                size = out.seek(0, 2)
                out.seek(max(0, size - 400))
                lines = [x.strip() for x in out.read().decode("utf-8", "replace").replace("\r", "\n").splitlines() if x.strip()]
                tail = lines[-1][:200] if lines else ""
            except Exception:  # noqa: BLE001 - the log is a courtesy
                tail = ""
            out.close()
    return code, tail


async def run_tool(cmd: list[str], archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = Path(cmd[0]).name
    if base.startswith("7z"):
        argv = [*cmd, "-o" + str(dest), str(archive)]
    elif base == "unrar":
        argv = [*cmd, str(archive), str(dest) + os.sep]
    else:
        argv = [*cmd, "-o", str(dest), str(archive)]
    code, _tail = await run_argv(argv, TOOL_TIMEOUT)
    if code != 0:
        raise UnpackError(_("{tool} failed (exit {code})", tool=base, code=code))


_UNPACKING: set[str] = set()   # archives being unpacked (process-wide: survives a plugin reload)


def _unzip_marked(arc: Path, dest: Path, key: str) -> None:
    try:
        unzip(arc, dest)
    finally:            # in the worker thread: cleared when the unzip really ends, even after a cancel
        _UNPACKING.discard(key)


def zip_top_name(archive: Path) -> str | None:
    """The one top-level folder of a zip, or None."""
    try:
        with zipfile.ZipFile(archive) as zf:
            tops = {n.replace("\\", "/").lstrip("/").split("/")[0] for n in zf.namelist() if n.strip("/")}
            return next(iter(tops)) if len(tops) == 1 else None
    except (zipfile.BadZipFile, OSError):
        return None


# -- GOG offline installers (Inno Setup + .bin parts) --------------------------------------------

def gog_parts(installer: Path) -> list[Path]:
    """The installer's own data files: ``<stem>-1.bin``, ``<stem>-2.bin``…"""
    stem = installer.stem
    try:
        return sorted(p for p in installer.parent.iterdir()
                      if p.is_file() and re.fullmatch(re.escape(stem) + r"-\d+\.bin", p.name, re.I))
    except OSError:
        return []


def is_gog_installer(p: Path) -> bool:
    """``setup_<game>_<ver>.exe`` that is an Inno Setup installer: it has ``-N.bin`` parts or the Inno marker."""
    if not GOG_SETUP.match(p.name) or not p.is_file() or p.is_symlink():
        return False
    if gog_parts(p):
        return True
    try:
        with p.open("rb") as f:
            return b"Inno Setup" in f.read(2 << 20)
    except OSError:
        return False


def find_gog(payload: Path) -> list[Path]:
    """GOG installers in a payload (a file or a folder, two levels deep), main game first."""
    if payload.is_file():
        cands = [payload]
    else:
        cands = []
        base = len(payload.parts)
        for dirpath, dirs, names in os.walk(payload, followlinks=False):
            dirs[:] = [d for d in dirs if len(Path(dirpath, d).parts) - base <= 2 and not d.startswith(".")]
            cands += [Path(dirpath, n) for n in sorted(names)]
    out = [p for p in cands if is_gog_installer(p)]
    # setup_* before patch_*, bigger first (the main game, then DLC)
    return sorted(out, key=lambda p: (p.name.lower().startswith("patch_"), -p.stat().st_size, p.name))


def gog_title(installer: Path) -> str:
    """``setup_the_witcher_3_1.2_(12345).exe`` → ``The Witcher 3``."""
    n = re.sub(r"^(setup|patch)_", "", installer.stem, flags=re.I)
    n = re.sub(r"\(\d+\)", " ", n)
    n = re.sub(r"[_ ]v?\d+(\.\d+)+\S*", " ", n)
    n = re.sub(r"[_ ](64|32)bit\b", " ", n, flags=re.I)
    words = [w for w in re.split(r"[_\s]+", n) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or installer.stem


def plan_gog(payload: Path, root: Path) -> dict[Path, list[Path]]:
    """{folder to extract into: [installers]}. Installers inside a folder extract in place; a bare file
    straight in ``pc/`` gets a folder named after the game."""
    plan: dict[Path, list[Path]] = {}
    pc = root / "pc"
    for inst in find_gog(payload):
        if inst.parent.resolve() == pc.resolve():
            dest = pc / (gog_title(inst).replace("/", "-"))
        else:
            dest = inst.parent
        plan.setdefault(dest, []).append(inst)
    return plan


def _merge_move(src: Path, dst: Path) -> None:
    """Move ``src`` onto ``dst`` (same filesystem: renames). Folders merge, files are replaced."""
    if src.is_dir() and not src.is_symlink():
        if dst.exists() and not dst.is_dir():
            dst.unlink()
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _merge_move(child, dst / child.name)
        src.rmdir()
    else:
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        os.replace(src, dst)


def _gog_cleanup(tmp: Path, dest: Path, installers: list[Path]) -> int:
    """After a good extraction: keep the game, drop installer scaffolding, then the installer files."""
    top = {p.name.lower(): p for p in tmp.iterdir()}
    game = top.get("app") if top.get("app") is not None and top["app"].is_dir() else tmp
    for leftover in list(game.iterdir()):
        if leftover.is_dir() and leftover.name.lower() in GOG_LEFTOVERS:
            shutil.rmtree(leftover, ignore_errors=True)
    n = sum(1 for p in game.rglob("*") if p.is_file())
    for inst in installers:        # only now that the files are in place
        for part in [*gog_parts(inst), inst]:
            part.unlink(missing_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    for child in sorted(game.iterdir()):
        _merge_move(child, dest / child.name)
    shutil.rmtree(tmp, ignore_errors=True)
    return n


def _gog_size(installers: list[Path]) -> int:
    total = 0
    for i in installers:
        for p in [i, *gog_parts(i)]:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


async def extract_gog(installers: list[Path], dest: Path, tool: str) -> int:
    """Extract GOG offline installers into ``dest`` with innoextract (subprocess: killed on cancel, off the
    event loop). Files are unpacked into a hidden folder first, so a failure leaves the installers (and
    nothing else) behind. Returns the number of game files."""
    loop = asyncio.get_running_loop()
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / GOG_TEMP
    await loop.run_in_executor(None, lambda: shutil.rmtree(tmp, ignore_errors=True))   # stale from a crash
    need = await loop.run_in_executor(None, _gog_size, installers)
    if shutil.disk_usage(dest).free < need * 1.05 + (512 << 20):
        raise UnpackError(_("not enough free space to unpack"))
    try:
        for inst in installers:
            code, tail = await run_argv([tool, "--gog", "--exclude-temp", "--silent", "-e", "-d", str(tmp), str(inst)],
                                        GOG_TIMEOUT, log_tail=True)
            if code != 0:
                raise UnpackError(_("innoextract failed on {name} (exit {code}) {tail}", name=inst.name, code=code,
                                    tail=tail).strip())
        if not tmp.is_dir() or not any(tmp.iterdir()):
            raise UnpackError(_("innoextract produced no files"))
        return await loop.run_in_executor(None, _gog_cleanup, tmp, dest, installers)
    except BaseException:           # failure or cancel: drop the half-extracted copy, keep the installers
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _unique(paths: list[Path]) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


async def unpack_payload(payload: Path, root: Path, created: list | None = None) -> str:
    """Unpack every archive of a finished download in place, then extract GOG installers.
    → "" when done, else a reason it needs a hand. ``created`` (a list) receives the paths that now hold
    this download (what "remove and delete the files" is allowed to touch)."""
    root = root.resolve()
    if not _inside(root, payload.resolve()) or payload.resolve() == root:
        raise UnpackError(_("outside the games folder"))
    if created is None:
        created = []
    if _inside(root / "emu", payload.resolve()):
        created.append(payload)
        return ""   # ROMs: emulators read .zip/.7z directly, and arcade/Neo Geo sets must stay zipped
    loop = asyncio.get_running_loop()
    made: list[Path] = []
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
                top = await loop.run_in_executor(None, zip_top_name, arc) if single else None
                made.append(arc.parent / top if top else dest)
            else:
                dest = arc.parent
            _UNPACKING.add(key)
            await loop.run_in_executor(None, _unzip_marked, arc, dest, key)
        else:
            cmd = tool_for(name)
            if not cmd:
                return _("Archive {name} needs 7z, unar or unrar on the hub to unpack", name=arc.name)
            dest = arc.parent / arc.stem if in_pc else arc.parent
            if in_pc:
                made.append(dest)
            _UNPACKING.add(key)
            try:
                await run_tool(cmd, arc, dest)
            finally:
                _UNPACKING.discard(key)
        await loop.run_in_executor(None, arc.unlink)
    scan = payload if await loop.run_in_executor(None, payload.exists) else None
    plan: dict[Path, list[Path]] = {}
    for base in ([scan] if scan else []) + [m for m in made if m.exists()]:
        for dest, insts in (await loop.run_in_executor(None, plan_gog, base, root)).items():
            plan.setdefault(dest, []).extend(i for i in insts if i not in plan.get(dest, []))
    if plan:
        tool = shutil.which("innoextract")
        if not tool:
            return _("GOG installers need innoextract on the hub to unpack")
        for dest, insts in plan.items():
            await extract_gog(insts, dest, tool)
            if dest.parent.resolve() == (root / "pc").resolve():
                made.append(dest)
    created += [payload] if await loop.run_in_executor(None, payload.exists) else _unique(made)
    return ""


# -- deleting what a download put on the NAS -------------------------------------------------------

def deletable(root: Path, path: Path) -> Path:
    """The path to delete, only if it is a game's own folder/file under ``<root>/pc`` or ``<root>/emu/<system>``.
    Never the root, ``pc``, ``emu`` or a system folder; never a symlink (the link, not its target, is what
    we would hit) or anything that resolves outside the root. Raises ValueError otherwise."""
    root_r = root.resolve()
    refuse = ValueError(_("Refusing to delete outside the games folder"))
    if not path.is_absolute() or path.is_symlink():
        raise refuse
    target = path.parent.resolve() / path.name
    if not _inside(root_r, target):
        raise refuse
    parts = target.relative_to(root_r).parts
    if not ((parts and parts[0] == "pc" and len(parts) >= 2) or (parts and parts[0] == "emu" and len(parts) >= 3)):
        raise refuse
    if target.name.startswith(".") or target.name in ("", ".", ".."):
        raise refuse
    return target


def tree_size(path: Path, limit: int = 200000) -> tuple[int, int]:
    """(bytes, files) under a path, not following symlinks."""
    if path.is_file() or path.is_symlink():
        try:
            return path.lstat().st_size, 1
        except OSError:
            return 0, 0
    size = n = 0
    for dirpath, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                size += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
            n += 1
            if n >= limit:
                return size, n
    return size, n


def delete_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# -- the service --------------------------------------------------------------------------------

class Finder:
    """Search + download bookkeeping for the Games plugin (``plugin`` gives hub, root and rescan)."""

    def __init__(self, plugin):
        self.p = plugin
        self.live: dict[str, dict] = {}       # hash → progress numbers from the last poll
        self._busy: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}    # hash → the running _finish (so Cancel can stop it)

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

    def _spawn_finish(self, h: str) -> None:
        t = self.p.hub.spawn(self._finish(h), name="games-finish")
        self._tasks[h] = t
        t.add_done_callback(lambda _t, h=h: self._tasks.pop(h, None) if self._tasks.get(h) is _t else None)

    def root_path(self) -> Path:
        return Path(os.path.expanduser(self.p.root))

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
                   "error": None, "dir": folder, "payload": None, "owned": []})
        self._save(es)
        return h

    def listing(self) -> list[dict]:
        out = []
        for e in self.entries():
            lv = self.live.get(e["hash"]) or {}
            done = e["state"] in ("done", "needs_extraction")
            out.append({"hash": e["hash"], "title": e["title"], "platform": e["platform"], "added": e.get("added"),
                        "progress": 1.0 if done else lv.get("progress", 0.0), "rate": 0 if done else lv.get("rate", 0),
                        "eta": None if done else lv.get("eta"), "state": e["state"], "error": e.get("error"),
                        "path": self.rel_path(e), "deck": self.p.auto_status(e)})
        return out

    def rel_path(self, e: dict) -> str:
        """Where the download lives, relative to the games folder ("" when it is gone or unknown)."""
        paths = self.owned(e)
        try:
            return str(paths[0].relative_to(self.root_path())) if paths else ""
        except ValueError:
            return ""

    def owned(self, e: dict) -> list[Path]:
        """Paths this download created (absolute, as stored); falls back to the torrent's own payload."""
        raw = [x for x in (e.get("owned") or []) if isinstance(x, str)] or ([e["payload"]] if e.get("payload") else [])
        return [Path(x) for x in raw]

    async def files_info(self, h: str) -> dict:
        """What "remove and delete the files" would delete: paths and sizes (computed off the event loop)."""
        e = next((x for x in self.entries() if x["hash"] == h), None)
        if not e:
            raise KeyError(_("Download not found"))
        root = self.root_path()
        loop = asyncio.get_running_loop()
        out, total, count = [], 0, 0
        for p in self.owned(e):
            try:
                target = deletable(root, p)
            except ValueError:
                continue
            if not await loop.run_in_executor(None, lambda t=target: t.exists() or t.is_symlink()):
                continue
            size, n = await loop.run_in_executor(None, tree_size, target)
            out.append({"path": str(target.relative_to(root.resolve())), "size": size, "files": n})
            total, count = total + size, count + n
        return {"paths": out, "size": total, "files": count, "root": str(root), "state": e["state"]}

    async def cancel(self, h: str, delete_files: bool = False) -> int:
        """Forget a download (and stop it). ``delete_files`` also deletes what it put under the games folder:
        only its own payload, checked by :func:`deletable`. Returns how many paths were deleted."""
        es = self.entries()
        e = next((x for x in es if x["hash"] == h), None)
        if not e:
            raise KeyError(_("Download not found"))
        targets: list[Path] = []
        if delete_files:       # validate first: a refused path must not leave a half-removed entry
            root = self.root_path()
            for p in self.owned(e):
                try:
                    targets.append(deletable(root, p))
                except ValueError:
                    log.warning("games: not deleting %s (outside the library)", p)
        if e["state"] == "downloading":
            try:
                await self._tx().remove(h, delete_data=True)
            except Exception as err:      # already gone from Transmission is fine
                log.info("cancel %s: %s", h, err)
        task = self._tasks.get(h)
        if task and not task.done():      # unpacking: stop the extractor before touching its files
            task.cancel()
            await asyncio.wait({task}, timeout=30)
        n = 0
        if targets:
            loop = asyncio.get_running_loop()
            for t in targets:
                if await loop.run_in_executor(None, lambda t=t: t.exists() or t.is_symlink()):
                    await loop.run_in_executor(None, delete_tree, t)
                    n += 1
        self._save([x for x in self.entries() if x["hash"] != h])
        self.live.pop(h, None)
        self.p.auto_forget(h)
        if n:
            try:
                await self.p.rescan()
            except Exception:
                log.exception("rescan after delete")
        return n

    # -- background
    async def retry(self, h: str) -> None:
        e = next((x for x in self.entries() if x["hash"] == h), None)
        if not e:
            raise KeyError(_("Download not found"))
        if e["state"] not in ("error", "needs_extraction") or not e.get("payload"):
            raise ValueError(_("Only a failed import can be retried"))
        self._update(h, state="extracting", error=None)
        self._spawn_finish(h)

    async def resume(self) -> None:
        """After a restart: finish downloads that were being unpacked."""
        for e in self.entries():
            if e["state"] == "extracting" and e.get("payload"):
                self._spawn_finish(e["hash"])

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
                self._spawn_finish(h)

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
            owned: list[Path] = []
            try:
                payload = Path(e["payload"])
                if payload.exists():
                    msg = await unpack_payload(payload, root, owned)
                    if msg:
                        state = "needs_extraction"
                else:
                    state, msg = "error", _("The downloaded files were not found")
            except UnpackError as err:
                state, msg = "error", _("Could not unpack: {err}", err=str(err))
            except OSError as err:
                state, msg = "error", str(err)
            self._update(h, state=state, error=msg or None, owned=[str(x) for x in owned] or e.get("owned") or [])
            try:
                await self.p.rescan()
            except Exception:
                log.exception("rescan after download")
            if state == "done":
                try:
                    self.p.auto_queue(h)
                except Exception:
                    log.exception("auto-install after download")
        finally:
            self._busy.discard(h)
