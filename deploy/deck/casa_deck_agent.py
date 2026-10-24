#!/usr/bin/env python3
"""Casa Deck agent: runs on the Steam Deck, installs what the Casa hub tells it to.

Standard library only (SteamOS ships Python 3, no pip needed). It

  * listens on a port (default 8765) for the hub, which must send the shared token;
  * downloads a game's files from the hub over signed, expiring links (resumable);
  * puts PC games in ~/Games/<Title> (or in the folder / SD card the hub picks, see /locations), ROMs in
    ~/Emulation/roms/<system>;
  * queues a non-Steam shortcut (and a Steam ROM Manager run for ROMs) and applies it when asked,
    because Steam only reads shortcuts.vdf while it is not running.

Run:  casa_deck_agent.py [--port 8765] [--token-file ~/.config/casa-deck/token]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote

VERSION = "1.3.1"
MEDIA_ROOT = Path("/run/media")      # SD cards are mounted here: /run/media/deck/<label> or /run/media/<label>
SYSTEM_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_BODY = 8 * 1024 * 1024
MAX_ART = 3 * 1024 * 1024                 # per image, after decoding
ART_KINDS = ("grid", "wide", "hero", "logo")
INSTALL_TIMEOUT = 3 * 3600                # FitGirl-style repacks are slow, especially on the Deck's CPU
SCAN_BYTES = 32 << 20                     # how much of a setup.exe is searched for the installer's signature
JOB_ACTIVE = ("queued", "downloading", "finalizing", "installing")


# ------------------------------------------------------------------ binary VDF (shortcuts.vdf)

def _cstr(b: bytes, i: int) -> tuple[str, int]:
    j = b.index(b"\x00", i)
    return b[i:j].decode("utf-8", "replace"), j + 1


def _parse_map(b: bytes, i: int) -> tuple[dict, int]:
    d: dict = {}
    while True:
        t = b[i]
        i += 1
        if t == 8:
            return d, i
        key, i = _cstr(b, i)
        if t == 0:
            d[key], i = _parse_map(b, i)
        elif t == 1:
            d[key], i = _cstr(b, i)
        elif t == 2:
            d[key] = struct.unpack_from("<I", b, i)[0]
            i += 4
        else:
            raise ValueError("unsupported VDF type %d" % t)


def vdf_loads(b: bytes) -> dict:
    try:
        return _parse_map(b, 0)[0]
    except (IndexError, ValueError, struct.error) as err:
        raise ValueError("not a valid shortcuts.vdf: %s" % err)


def vdf_dumps(d: dict) -> bytes:
    out = bytearray()
    for k, v in d.items():
        if isinstance(v, dict):
            out += b"\x00" + k.encode() + b"\x00" + vdf_dumps(v)
        elif isinstance(v, int):
            out += b"\x02" + k.encode() + b"\x00" + struct.pack("<I", v & 0xFFFFFFFF)
        else:
            out += b"\x01" + k.encode() + b"\x00" + str(v).encode() + b"\x00"
    return bytes(out) + b"\x08"


def shortcut_appid(exe: str, name: str) -> int:
    return (zlib.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000


def _get(d: dict, key: str, default=None):
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return default


def add_shortcut(path: Path, name: str, exe: str, startdir: str, options: str = "") -> bool:
    """Add (or update) a non-Steam shortcut. True if the file changed."""
    data = vdf_loads(path.read_bytes()) if path.exists() and path.stat().st_size else {}
    shortcuts = data.get("shortcuts")
    if shortcuts is None:
        shortcuts = data["shortcuts"] = {}
    qexe = '"%s"' % exe
    entry = {
        "appid": shortcut_appid(qexe, name), "AppName": name, "Exe": qexe, "StartDir": '"%s"' % startdir,
        "icon": "", "ShortcutPath": "", "LaunchOptions": options, "IsHidden": 0, "AllowDesktopConfig": 1,
        "AllowOverlay": 1, "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0,
        "LastPlayTime": 0, "FlatpakAppID": "", "tags": {},
    }
    for k, cur in shortcuts.items():
        if isinstance(cur, dict) and _get(cur, "exe") == qexe:
            merged = {**cur, **{kk: vv for kk, vv in entry.items() if kk in ("AppName", "StartDir", "LaunchOptions")}}
            if merged == cur:
                return False
            shortcuts[k] = merged
            break
    else:
        shortcuts[str(max([int(k) for k in shortcuts if str(k).isdigit()] + [-1]) + 1)] = entry
    raw = vdf_dumps(data)
    if path.exists() and not (path.parent / (path.name + ".casa.bak")).exists():
        shutil.copy2(path, path.parent / (path.name + ".casa.bak"))
    tmp = path.with_suffix(".vdf.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    return True


def remove_shortcut(path: Path, exe: str) -> bool:
    if not path.exists():
        return False
    data = vdf_loads(path.read_bytes())
    sc = data.get("shortcuts") or {}
    qexe = '"%s"' % exe
    keep = [v for v in sc.values() if not (isinstance(v, dict) and _get(v, "exe") == qexe)]
    if len(keep) == len(sc):
        return False
    data["shortcuts"] = {str(i): v for i, v in enumerate(keep)}
    tmp = path.with_suffix(".vdf.tmp")
    tmp.write_bytes(vdf_dumps(data))
    os.replace(tmp, path)
    return True


# ------------------------------------------------------------------ Steam process control

def steam_running() -> bool:
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


def steam_shutdown() -> bool:
    subprocess.run(["steam", "-shutdown"], capture_output=True, timeout=30)
    for _ in range(40):
        if not steam_running():
            return True
        time.sleep(1)
    return False


def steam_start() -> None:
    subprocess.Popen(["steam", "-silent"], start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def run_srm(cmd: str) -> bool:
    return subprocess.run([cmd, "add"], capture_output=True, timeout=600).returncode == 0


# ------------------------------------------------------------------ helpers

def image_ext(raw: bytes) -> str | None:
    """'.png' / '.jpg' from the magic bytes; None for anything else."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return None


def decode_art(art) -> dict:
    """{"grid": base64, …} → {"grid": (bytes, ext), …}; raises ValueError on anything odd."""
    if art is None:
        return {}
    if not isinstance(art, dict):
        raise ValueError("bad art")
    out = {}
    for kind, b64 in art.items():
        if kind not in ART_KINDS:
            raise ValueError("bad art kind")
        if not isinstance(b64, str) or len(b64) > MAX_ART * 4 // 3 + 8:
            raise ValueError("art too large")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("art is not valid base64")
        ext = image_ext(raw)
        if not ext or len(raw) > MAX_ART:
            raise ValueError("art must be a PNG or JPEG")
        out[kind] = (raw, ext)
    return out


def art_names(appid: int) -> dict:
    """File names Steam looks for in userdata/<id>/config/grid/ (without extension)."""
    return {"grid": "%dp" % appid, "wide": "%d" % appid, "hero": "%d_hero" % appid, "logo": "%d_logo" % appid}


def safe_rel(p) -> str | None:
    if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or "\x00" in p:
        return None
    return None if any(seg in ("", ".", "..") for seg in p.split("/")) else p


def is_mount(p: Path) -> bool:
    try:
        return os.path.ismount(p)
    except OSError:
        return False


def validate_location(raw, home: Path, media_roots: list[Path], mount_check=is_mount) -> Path:
    """A folder the hub may install PC games into: absolute, no ``..``, and either under the user's home
    (no hidden folders) or on a mounted SD card under /run/media. Symlinks are resolved before the check,
    so a link can't lead out. Raises ValueError("bad location"/…); returns the resolved path."""
    if not isinstance(raw, str) or not raw.startswith("/") or len(raw) > 400 or "\x00" in raw or "\\" in raw:
        raise ValueError("bad location")
    if any(seg in (".", "..") for seg in raw.split("/")):
        raise ValueError("bad location")
    real = Path(os.path.normpath(raw)).resolve()
    home_r = home.resolve()
    if real != home_r and home_r in real.parents:
        if any(part.startswith(".") for part in real.relative_to(home_r).parts):
            raise ValueError("bad location")
        return real
    for root in media_roots:
        root_r = root.resolve()
        if root_r in real.parents:
            card = root_r / real.relative_to(root_r).parts[0]
            if mount_check(card):
                return real
            raise ValueError("the SD card is not mounted")
    raise ValueError("bad location: only %s or an SD card under %s" % (home, MEDIA_ROOT))


def safe_name(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r'[<>:"|?*\x00-\x1f/\\]', "_", s).strip(" .")
    return s if s and s not in (".", "..") else None


# ------------------------------------------------------------------ Proton and Windows installers

def _tool_name(d: Path) -> str:
    """The name Steam stores in CompatToolMapping for a Proton folder."""
    vdf = d / "compatibilitytool.vdf"
    if vdf.is_file():
        m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', vdf.read_text(errors="replace")[:20000])
        if m:
            return m.group(1)
    n = d.name
    if "Experimental" in n:
        return "proton_experimental"
    if "Hotfix" in n:
        return "proton_hotfix"
    m = re.match(r"Proton (\d+)", n)
    return "proton_%s" % m.group(1) if m else ""


def _proton_rank(name: str) -> tuple:
    if name == "Proton - Experimental":
        return (0, 0, 0)
    m = re.match(r"Proton (\d+)\.(\d+)", name)
    if m and not re.search(r"Voice|Hotfix", name):
        return (1, -int(m.group(1)), -int(m.group(2)))
    if re.search(r"Voice|Hotfix", name):
        return (3, 0, 0)
    return (2, 0, 0)


def find_protons(dirs: list[Path]) -> list[dict]:
    """Proton installs found in the given folders, best first: [{name, path, tool}]."""
    out, seen = [], set()
    for root in dirs:
        try:
            kids = sorted(root.iterdir())
        except OSError:
            continue
        for d in kids:
            try:
                real = str(d.resolve())
                if real in seen or not d.is_dir() or not (d / "proton").is_file():
                    continue
            except OSError:
                continue
            if "proton" not in d.name.lower() and "ulwgl" not in d.name.lower() and not (d / "compatibilitytool.vdf").is_file():
                continue
            seen.add(real)
            out.append({"name": d.name, "path": str(d), "tool": _tool_name(d)})
    out.sort(key=lambda x: (_proton_rank(x["name"]), x["name"]))
    return out


def pick_proton(protons: list[dict], wanted: str = "") -> dict | None:
    if wanted:
        for p in protons:
            if p["name"] == wanted:
                return p
    return protons[0] if protons else None


def detect_installer(path: Path) -> str | None:
    """'inno' / 'nsis' from signature strings inside the exe, None if neither."""
    inno = (b"Inno Setup Setup Data", b"Inno Setup")
    nsis = (b"Nullsoft Install System", b"Nullsoft")
    seen_inno = seen_nsis = False
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            pos, tail = 0, b""
            while pos < min(size, SCAN_BYTES):
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                seen_inno |= any(m in buf for m in inno)
                seen_nsis |= any(m in buf for m in nsis)
                if seen_inno:
                    break
                tail = buf[-32:]
                pos += len(chunk)
            if size > SCAN_BYTES and not seen_inno:
                f.seek(max(size - (2 << 20), SCAN_BYTES))
                buf = f.read(2 << 20)
                seen_inno |= any(m in buf for m in inno)
                seen_nsis |= any(m in buf for m in nsis)
    except OSError:
        return None
    return "inno" if seen_inno else "nsis" if seen_nsis else None


def wine_path(p) -> str:
    """/home/deck/Games/X → Z:\\home\\deck\\Games\\X (Wine maps / to Z:)."""
    return "Z:" + str(p).replace("/", "\\")


def installer_args(kind: str, target: Path) -> list[str]:
    """Silent-install arguments (a list, never a shell string). NSIS wants /D= last and unquoted, so its
    target must not contain spaces (Wine would quote the argument)."""
    if kind == "inno":
        # /LANG: GOG installers otherwise stop at a language dialog even in /VERYSILENT mode
        return ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS", "/LANG=english",
                "/DIR=" + wine_path(target)]
    if kind == "nsis":
        return ["/S", "/D=" + wine_path(target)]
    raise ValueError("unknown installer type")


BAD_EXE = re.compile(r"^(unins|setup|install|dxsetup|vcredist|vc_redist|dotnet|oalinst|physx|directx|crashpad|"
                     r"unitycrashhandler|crashreport|dxwebsetup|ue4prereq|quicksfv|redist)|redist|_redist|uninst", re.I)


def _files_lower(dest: Path) -> dict:
    out = {}
    for root, dirs, names in os.walk(dest, followlinks=False):
        dirs.sort()
        for n in names:
            f = Path(root) / n
            if not f.is_symlink():
                out[f.relative_to(dest).as_posix().lower()] = f
    return out


def goggame_exe(dest: Path, files: dict | None = None) -> tuple[Path | None, str]:
    """Primary playTask of a GOG goggame-*.info (depth 0 or 1) → (exe path, working dir relative or '')."""
    files = files if files is not None else _files_lower(dest)
    infos = sorted((k for k in files if k.count("/") <= 1 and re.fullmatch(r"goggame-\d+\.info", k.rsplit("/", 1)[-1])),
                   key=lambda k: (k.count("/"), k))
    best = (None, "")
    for rel in infos[:8]:
        try:
            data = json.loads(files[rel].read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        prefix = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        tasks = [t for t in data.get("playTasks") or [] if isinstance(t, dict) and t.get("type", "FileTask") == "FileTask"
                 and str(t.get("category", "game")) == "game"]
        tasks.sort(key=lambda t: not t.get("isPrimary"))
        for t in tasks:
            raw = str(t.get("path") or "").replace("\\", "/").strip().lstrip("./").lstrip("/")
            if not raw or ".." in raw.split("/"):
                continue
            exe = files.get((prefix + raw).lower())
            if not exe:
                continue
            wd = str(t.get("workingDir") or "").replace("\\", "/").strip().strip("/")
            work = ""
            if wd and ".." not in wd.split("/") and (dest / (prefix + wd)).is_dir():
                work = (prefix + wd).rstrip("/")
            main = str(data.get("rootGameId") or data.get("gameId") or "") == str(data.get("gameId") or "")
            if best[0] is None or main:
                best = (exe, work)
            break
        if best[0] is not None and main:
            break
    return best


def find_game_exe(dest: Path) -> tuple[Path | None, str]:
    """The game's executable inside a freshly installed folder → (exe, working dir relative to dest or '')."""
    files = _files_lower(dest)
    exe, work = goggame_exe(dest, files)
    if exe:
        return exe, work
    cands = []
    for rel, f in files.items():
        if rel.endswith(".exe") and rel.count("/") <= 2 and not BAD_EXE.search(f.name):
            try:
                cands.append((f.stat().st_size, f))
            except OSError:
                pass
    nolaunch = [c for c in cands if "launcher" not in c[1].name.lower()]
    cands = nolaunch or cands
    if not cands:
        return None, ""
    cands.sort(key=lambda c: (-c[0], str(c[1])))
    return cands[0][1], ""


def tail_text(f: Path, lines: int = 40, limit: int = 4000) -> str:
    try:
        with f.open("rb") as h:
            h.seek(0, 2)
            h.seek(max(0, h.tell() - 16384))
            txt = h.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(txt.splitlines()[-lines:])[-limit:]


def kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            continue


def run_logged(argv: list[str], env: dict, log: Path, timeout: float, cancelled, tick=None) -> tuple[int | None, str]:
    """Run ``argv`` in its own process group, output to ``log``. → (exit code, '' | 'timeout' | 'cancelled')."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        t0, last = time.monotonic(), 0.0
        while True:
            try:
                return proc.wait(timeout=0.5), ""
            except subprocess.TimeoutExpired:
                pass
            if cancelled():
                kill_group(proc)
                return None, "cancelled"
            if time.monotonic() - t0 > timeout:
                kill_group(proc)
                return None, "timeout"
            if tick and time.monotonic() - last > 5:
                last = time.monotonic()
                tick()


def set_compat_tool(config_vdf: Path, appid: int, tool: str) -> bool:
    """Add ``appid → tool`` to Steam's CompatToolMapping (text VDF) unless the app already has one.
    Steam must not be running. Both the unsigned and signed spelling of the id are written. True if changed."""
    if not tool or not config_vdf.is_file():
        return False
    txt = config_vdf.read_text(errors="replace")
    m = re.search(r'"CompatToolMapping"\s*\{', txt)
    if not m:
        return False
    depth, i = 1, m.end()
    while i < len(txt) and depth:
        depth += {"{": 1, "}": -1}.get(txt[i], 0)
        i += 1
    if depth:
        return False
    end = i - 1                               # the closing brace of the block
    block = txt[m.end():end]
    ids = [str(appid), str(appid - (1 << 32) if appid >= 1 << 31 else appid)]
    add = ""
    for k in dict.fromkeys(ids):
        if re.search(r'"%s"\s*\{' % re.escape(k), block):
            continue
        add += ('\t\t\t\t\t"%s"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"%s"\n\t\t\t\t\t\t"config"\t\t""\n'
                '\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}\n') % (k, tool)
    if not add:
        return False
    bak = config_vdf.with_name(config_vdf.name + ".casa.bak")
    if not bak.exists():
        shutil.copy2(config_vdf, bak)
    tmp = config_vdf.with_name(config_vdf.name + ".tmp")
    tmp.write_text(txt[:end].rstrip("\t") + add + "\t\t\t\t" + txt[end:])
    os.replace(tmp, config_vdf)
    return True


def clean_title(name: str) -> str:
    """'Gothic 3 [GOG]' → 'Gothic 3'."""
    return re.sub(r"\s*\[[^\]]*\]", "", name).strip() or name


class Agent:
    def __init__(self, token: str, games_dir: Path, roms_dir: Path, steam_dir: Path, data_dir: Path,
                 srm: str = "", home: Path | None = None, media_roots: list[Path] | None = None, mount_check=None,
                 proton_dirs: list[Path] | None = None, proton: str = "", install_timeout: float = INSTALL_TIMEOUT):
        self.token, self.games_dir, self.roms_dir = token, games_dir, roms_dir
        self.steam_dir, self.data_dir, self.srm = steam_dir, data_dir, srm
        self.home = home or Path.home()
        self.media_roots = media_roots if media_roots is not None else [MEDIA_ROOT / self.home.name, MEDIA_ROOT]
        self.mount_check = mount_check or is_mount
        self.proton_dirs = proton_dirs if proton_dirs is not None else [
            steam_dir / "steamapps/common", steam_dir / "compatibilitytools.d", steam_dir.parent / "root/compatibilitytools.d",
            self.home / ".local/share/Steam/steamapps/common", self.home / ".local/share/Steam/compatibilitytools.d"]
        self.proton_default, self.install_timeout = proton, install_timeout
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.reg_file, self.pend_file = data_dir / "installed.json", data_dir / "pending.json"
        threading.Thread(target=self._worker, daemon=True).start()

    # -- persistence
    def _load(self, f: Path, default):
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return default

    def _save(self, f: Path, data) -> None:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, f)

    def registry(self) -> dict:
        return self._load(self.reg_file, {})

    def pending(self) -> dict:
        p = self._load(self.pend_file, {})
        return {"shortcuts": p.get("shortcuts", []), "srm": bool(p.get("srm"))}

    def status(self) -> dict:
        with self.lock:
            reg = {k: {x: v.get(x) for x in ("version", "kind", "note", "installed_at", "dest")} for k, v in self.registry().items()}
            jobs = {k: {x: j.get(x) for x in ("state", "done", "message", "phase", "log_tail") if j.get(x) is not None}
                    for k, j in self.jobs.items()}
            p = self.pending()
        return {"agent": VERSION, "installed": reg, "jobs": jobs, "pending": len(p["shortcuts"]) + (1 if p["srm"] else 0),
                "steam_running": steam_running()}

    def protons(self, wanted: str = "") -> dict:
        lst = find_protons(self.proton_dirs)
        pick = pick_proton(lst, wanted or self.proton_default)
        return {"protons": [{"name": p["name"], "tool": p["tool"]} for p in lst], "default": pick["name"] if pick else None}

    # -- staged installers (copied earlier, waiting to be run)
    def _installer_roots(self) -> list[Path]:
        roots = [self.games_dir / "_installers"]
        for m in self.media_roots:
            try:
                kids = sorted(c for c in m.iterdir() if c.is_dir())
            except OSError:
                continue
            roots += [c / "Games/_installers" for c in kids if self.mount_check(c)]
        return roots

    @staticmethod
    def _setup_exe(folder: Path, hint: str = "") -> Path | None:
        """The setup program inside a staged folder: the hinted one, else setup*.exe / the only exe, top level first."""
        if hint:
            rel = safe_rel(hint)
            f = (folder / rel) if rel else None
            if f and f.is_file() and f.resolve().is_relative_to(folder.resolve()):
                return f
        exes = sorted((f for f in folder.rglob("*.exe") if f.is_file() and not f.is_symlink()
                       and len(f.relative_to(folder).parts) <= 3), key=lambda f: (len(f.relative_to(folder).parts), f.name.lower()))
        pref = [f for f in exes if re.match(r"(setup|install)", f.name, re.I)]
        return (pref or exes or [None])[0]

    def installers(self) -> dict:
        out = []
        for root in self._installer_roots():
            try:
                kids = sorted(c for c in root.iterdir() if c.is_dir() and not c.is_symlink())
            except OSError:
                continue
            for d in kids:
                exe = self._setup_exe(d)
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not f.is_symlink())
                out.append({"folder": d.name, "path": str(d), "title": clean_title(d.name), "size": size,
                            "exe": exe.relative_to(d).as_posix() if exe else "",
                            "type": detect_installer(exe) if exe else None})
        with self.lock:
            busy = {k: {"title": j.get("title"), **{x: j.get(x) for x in ("state", "message", "phase", "log_tail", "done")
                                                    if j.get(x) is not None}}
                    for k, j in self.jobs.items() if j.get("_staged")}
        return {"agent": VERSION, "installers": out, "jobs": busy}

    def run_staged(self, body: dict) -> dict:
        """Run an installer that is already on the Deck: {path} (full folder) or {folder} (in ~/Games/_installers)."""
        raw = body.get("path")
        if raw:
            folder = self.location(raw)
        else:
            nm = safe_name(body.get("folder"))
            if not nm:
                raise ValueError("bad folder")
            folder = (self.games_dir / "_installers" / nm).resolve()
        if folder.parent.name != "_installers" or not folder.is_dir() or folder.is_symlink():
            raise ValueError("not a staged installer folder")
        if not any(folder.resolve() == r.resolve() / folder.name for r in self._installer_roots()):
            raise ValueError("not a staged installer folder")
        title = body.get("title") if isinstance(body.get("title"), str) and body["title"].strip() else clean_title(folder.name)
        gid = "staged-" + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "game")[:80]
        job = {"id": gid, "title": title[:150], "kind": "installer", "folder": folder.name, "run": True,
               "files": [{"p": "x", "s": 0}], "base": "http://staged", "_staged": str(folder)}
        for k in ("exe", "proton", "workdir"):
            if body.get(k):
                job[k] = body[k]
        loc = body.get("install_to")
        base = str(self.location(loc)) if loc else None
        art = decode_art(body.get("art")) if body.get("art") else {}
        with self.lock:
            cur = self.jobs.get(gid)
            if cur and cur["state"] in JOB_ACTIVE:
                raise ValueError("already installing")
            self.jobs[gid] = {**job, "state": "queued", "done": 0.0, "message": "queued", "phase": "copying",
                              "_art": art, "_base": base}
        self.q.put(gid)
        return {"ok": True, "id": gid}

    def cancel(self, gid: str) -> dict:
        with self.lock:
            j = self.jobs.get(gid)
            if not j or j.get("state") not in JOB_ACTIVE:
                raise KeyError("no such running job")
            j["_cancel"] = True
        return {"ok": True}

    # -- where games can go
    def location(self, raw) -> Path:
        return validate_location(raw, self.home, self.media_roots, self.mount_check)

    def _space(self, p: Path) -> dict:
        try:
            u = shutil.disk_usage(p)
            return {"free": u.free, "total": u.total, "writable": os.access(p, os.W_OK)}
        except OSError:
            return {"free": 0, "total": 0, "writable": False}

    def locations(self) -> dict:
        """Where PC games can be installed: internal storage and every mounted SD card, with free space.
        ``path`` is the folder the game folders go into (ROMs always use the Emulation folder)."""
        probe = self.games_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        out = [{"id": "internal", "kind": "internal", "label": "Internal storage", "path": str(self.games_dir),
                **self._space(probe)}]
        seen = {str(probe.resolve())}
        for root in self.media_roots:
            try:
                kids = sorted(c for c in root.iterdir() if c.is_dir())
            except OSError:
                continue
            for card in kids:
                if not self.mount_check(card) or str(card.resolve()) in seen:
                    continue
                seen.add(str(card.resolve()))
                out.append({"id": "sd:" + card.name, "kind": "sd", "label": card.name, "path": str(card / "Games"),
                            **self._space(card)})
        return {"agent": VERSION, "locations": out, "default": str(self.games_dir), "home": str(self.home)}

    def check_location(self, raw) -> dict:
        p = self.location(raw)
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return {"ok": True, "path": str(p), **self._space(probe)}

    # -- install
    def submit(self, job: dict) -> None:
        gid, kind = job.get("id"), job.get("kind")
        if not isinstance(gid, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", gid):
            raise ValueError("bad game id")
        if kind not in ("portable", "installer", "native", "rom"):
            raise ValueError("bad kind")
        files = job.get("files")
        if not isinstance(files, list) or not files or len(files) > 20000:
            raise ValueError("no files")
        for f in files:
            if not (isinstance(f, dict) and safe_rel(f.get("p")) and isinstance(f.get("s"), int) and f["s"] >= 0):
                raise ValueError("bad file entry")
        if kind == "rom":
            if not SYSTEM_RE.match(str(job.get("system", ""))):
                raise ValueError("bad system")
            if job.get("folder") and not safe_name(job["folder"]):
                raise ValueError("bad folder")
        elif not safe_name(job.get("folder")):
            raise ValueError("bad folder")
        if kind in ("portable", "native") and not safe_rel(job.get("exe")):
            raise ValueError("no executable")
        if not str(job.get("base", "")).startswith(("http://", "https://")):
            raise ValueError("bad base url")
        loc = job.pop("location", None)
        base = None
        if loc not in (None, "") and kind != "rom":
            base = str(self.location(loc))
        work = job.get("workdir")
        if work not in (None, "") and not safe_rel(work):
            raise ValueError("bad working directory")
        raw_art = job.pop("art", None)
        art = decode_art(raw_art) if kind in ("portable", "native", "installer") else {}
        if job.get("exe") not in (None, "") and kind == "installer" and not safe_rel(job["exe"]):
            raise ValueError("bad executable")
        job["run"] = bool(job.get("run")) and kind == "installer"
        if job.get("proton") is not None and not isinstance(job["proton"], str):
            raise ValueError("bad proton")
        if job.get("title") is not None and not isinstance(job["title"], str):
            raise ValueError("bad title")
        with self.lock:
            cur = self.jobs.get(gid)
            if cur and cur["state"] in JOB_ACTIVE:
                raise ValueError("already installing")
            job = {**job, "state": "queued", "done": 0.0, "message": "queued", "phase": "copying", "_art": art, "_base": base}
            self.jobs[gid] = job
        self.q.put(gid)

    def _dest(self, job: dict) -> Path:
        kind = job["kind"]
        if job.get("_staged"):
            return Path(job["_staged"])
        if kind == "rom":
            root = self.roms_dir / job["system"]
            return root / safe_name(job["folder"]) if job.get("folder") else root
        root = (Path(job["_base"]) if job.get("_base") else self.games_dir) / ("_installers" if kind == "installer" else "")
        return root / safe_name(job["folder"])

    def _worker(self) -> None:
        while True:
            gid = self.q.get()
            with self.lock:
                job = self.jobs.get(gid)
            if job is None:           # removed while queued
                continue
            try:
                self._run(job)
            except Exception as err:  # never kill the worker
                self._set(job, state="failed", message=str(err) or type(err).__name__)

    def _set(self, job: dict, **kw) -> None:
        with self.lock:
            job.update(kw)

    def _download(self, job: dict, rel: str, size: int, target: Path, counter: list) -> None:
        if target.exists() and target.stat().st_size == size:
            counter[0] += size
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = "%s/api/p/games/dl/%s/%s?e=%s&t=%s" % (job["base"].rstrip("/"), quote(job["id"]), quote(rel),
                                                       job["exp"], job["sig"])
        last = None
        for attempt in range(4):
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            try:
                req = urllib.request.Request(url, headers={"Range": "bytes=%d-" % have} if have else {})
                with urllib.request.urlopen(req, timeout=30) as r:
                    if have and r.status != 206:
                        have = 0
                    with part.open("ab" if have else "wb") as out:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            out.write(chunk)
                            counter[0] += len(chunk)
                            self._set(job, done=min(counter[0] / max(job["_total"], 1), 0.99))
                if part.stat().st_size != size:
                    raise OSError("size mismatch for %s (%d != %d)" % (rel, part.stat().st_size, size))
                os.replace(part, target)
                return
            except urllib.error.HTTPError as err:
                if err.code in (403, 404):
                    raise RuntimeError("the hub refused %s (HTTP %d): link expired or file moved" % (rel, err.code))
                last = err
            except (OSError, urllib.error.URLError) as err:
                last = err
            time.sleep(1 + attempt * 2)
        raise RuntimeError("download failed for %s: %s" % (rel, last))

    def _run(self, job: dict) -> None:
        dest = self._dest(job)
        total = sum(f["s"] for f in job["files"])
        job["_total"] = total
        probe = dest if dest.exists() else dest.parent
        while not probe.exists():
            probe = probe.parent
        if not job.get("_staged") and shutil.disk_usage(probe).free < total * 1.02 + (50 << 20):
            raise RuntimeError("not enough free space on the Deck")
        self._set(job, state="downloading", message="downloading", done=0.0)
        counter = [0]
        if not job.get("_staged"):
            for f in job["files"]:
                self._download(job, f["p"], f["s"], dest / f["p"], counter)
        self._set(job, state="finalizing", message="finishing", done=0.99)
        if job["kind"] == "installer" and job.get("run"):
            return self._install_windows(job, dest)
        files = [str(dest / f["p"]) for f in job["files"]]
        note, state = "", "done"
        entry = {"id": job["id"], "version": job.get("version", ""), "kind": job["kind"], "dest": str(dest),
                 "files": files, "installed_at": int(time.time()), "exe": "",
                 "root": job.get("_base") or ""}
        if job["kind"] in ("portable", "native"):
            exe = dest / job["exe"]
            if job["kind"] == "native":
                exe.chmod(exe.stat().st_mode | 0o111)
            entry["exe"] = str(exe)
            staged = self._stage_art(job)
            with self.lock:
                p = self.pending()
                p["shortcuts"] = [s for s in p["shortcuts"] if s["exe"] != str(exe)] + [
                    {"name": job["title"], "exe": str(exe), "startdir": self._startdir(dest, exe, job.get("workdir")),
                     **({"art": staged} if staged else {})}]
                self._save(self.pend_file, p)
            note = "Apply to Steam to add the shortcut." + (
                " Pick a Proton version for it once (Properties → Compatibility)." if str(exe).lower().endswith(".exe") else "")
        elif job["kind"] == "rom":
            with self.lock:
                p = self.pending()
                p["srm"] = True
                self._save(self.pend_file, p)
            note = "Apply to Steam to run Steam ROM Manager." if self.srm else "Run Steam ROM Manager to show it in Steam."
        else:
            state = "needs_manual"
            note = "Installer saved in %s. Run it with Proton from Desktop Mode." % dest
        if state == "done":
            with self.lock:
                reg = self.registry()
                entry["note"] = note
                reg[job["id"]] = entry
                self._save(self.reg_file, reg)
        self._set(job, state=state, done=1.0, message=note or "installed", phase="manual" if state == "needs_manual" else "done")

    # -- Windows installers through Proton
    def _fail(self, job: dict, msg: str, log: Path | None = None, state: str = "failed") -> None:
        self._set(job, state=state, done=1.0, message=msg, phase="manual" if state == "needs_manual" else "error",
                  log_tail=tail_text(log) if log else None)

    def _inside_roots(self, p: Path) -> bool:
        roots = [self.games_dir.resolve()]
        try:
            roots += [self.location(str(p.parent.parent))] if p.parent.name == "_installers" else []
        except ValueError:
            pass
        return any(p != r and p.is_relative_to(r) for r in roots)

    def _install_windows(self, job: dict, staged: Path) -> None:
        self._set(job, state="installing", phase="installing", message="Running the installer…", done=0.99)
        setup = self._setup_exe(staged, job.get("exe") or "")
        if setup is None or not setup.resolve().is_relative_to(staged.resolve()):
            return self._fail(job, "No setup program found in %s. Run it by hand from Desktop Mode." % staged, state="needs_manual")
        kind = detect_installer(setup)
        if kind is None:
            return self._fail(job, "%s is not an Inno Setup or NSIS installer, so it cannot be run silently. "
                              "Saved in %s: run it with Proton from Desktop Mode." % (setup.name, staged), state="needs_manual")
        pr = pick_proton(find_protons(self.proton_dirs), job.get("proton") or self.proton_default)
        if pr is None:
            return self._fail(job, "No Proton found on the Deck (install one from Steam). Installer saved in %s." % staged,
                              state="needs_manual")
        title = safe_name(clean_title(job.get("title") or job["folder"])) or safe_name(job["folder"])
        base = Path(job["_base"]) if job.get("_base") else self.games_dir
        target = base / (title.replace(" ", "_") if kind == "nsis" else title)
        prefix = self.games_dir / "_prefixes" / title
        log = self.data_dir / "logs" / ("%s.log" % job["id"])
        try:
            target.mkdir(parents=True, exist_ok=True)
            prefix.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            return self._fail(job, "Cannot create the install folder: %s" % err)
        env = {**os.environ, "STEAM_COMPAT_DATA_PATH": str(prefix), "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(self.steam_dir),
               "WINEDEBUG": "-all", "PROTON_USE_XALIA": "0"}   # Xalia (gamepad UI helper) only gets in the way here
        if Path("/tmp/.X11-unix/X0").exists() and not env.get("DISPLAY"):
            env["DISPLAY"] = ":0"
        argv = ["nice", "-n", "10", str(Path(pr["path"]) / "proton"), "run", str(setup)] + installer_args(kind, target)
        t0 = time.monotonic()
        self._set(job, log_tail=None)

        def tick():
            self._set(job, message="Running the installer (%s, %d min)…" % (pr["name"], (time.monotonic() - t0) // 60),
                      log_tail=tail_text(log) or None)
        try:
            rc, why = run_logged(argv, env, log, self.install_timeout, lambda: bool(job.get("_cancel")), tick)
        except OSError as err:
            return self._fail(job, "Cannot start Proton: %s" % err)
        if why == "cancelled":
            return self._fail(job, "Cancelled.", log)
        if why == "timeout":
            return self._fail(job, "The installer did not finish within %d hours and was stopped." % (self.install_timeout // 3600), log)
        now = {p for p in target.rglob("*") if p.is_file()}
        if rc != 0 or not now:
            return self._fail(job, "The installer failed (exit code %s%s). The installer files were kept." % (
                rc, "" if now else ", nothing was installed"), log)
        exe, work = find_game_exe(target)
        if exe is None:
            return self._fail(job, "Installed in %s but no game executable was found: add it to Steam by hand." % target,
                              log, state="needs_manual")
        entry = {"id": job["id"], "version": job.get("version", ""), "kind": "installer", "dest": str(target),
                 "files": [], "tree": True, "installed_at": int(time.time()), "exe": str(exe),
                 "root": job.get("_base") or ""}
        staged_art = self._stage_art(job)
        startdir = str(target / work) if work and (target / work).is_dir() else str(exe.parent)
        note = "Apply to Steam to add the shortcut."
        with self.lock:
            p = self.pending()
            p["shortcuts"] = [s for s in p["shortcuts"] if s["exe"] != str(exe)] + [
                {"name": job["title"], "exe": str(exe), "startdir": startdir, "compat": pr["tool"],
                 **({"art": staged_art} if staged_art else {})}]
            self._save(self.pend_file, p)
            entry["note"] = note
            reg = self.registry()
            reg[job["id"]] = entry
            self._save(self.reg_file, reg)
        removed = False
        try:
            if self._inside_roots(staged.resolve()):
                shutil.rmtree(staged)
                removed = True
        except OSError:
            pass
        self._set(job, state="done", phase="done", done=1.0, log_tail=None,
                  message=note + ("" if removed else " (The installer folder could not be removed.)"))

    @staticmethod
    def _startdir(dest: Path, exe: Path, workdir) -> str:
        """Steam's "Start in" folder: the game's own working directory (GOG says where) or the exe's folder."""
        if workdir and safe_rel(workdir):
            wd = dest / workdir
            if wd.is_dir():
                return str(wd)
        return str(exe.parent)

    def _stage_art(self, job: dict) -> dict:
        """Keep the cover art until Steam is stopped and the shortcut is applied."""
        out = {}
        d = self.data_dir / "art"
        for kind, (raw, ext) in (job.get("_art") or {}).items():
            try:
                d.mkdir(parents=True, exist_ok=True)
                f = d / ("%s-%s%s" % (job["id"], kind, ext))
                f.write_bytes(raw)
                out[kind] = str(f)
            except OSError:
                pass
        return out

    def _write_art(self, s: dict, files: list[Path]) -> int:
        """Copy a shortcut's staged art into every Steam user's grid folder; returns files written."""
        art = s.get("art") or {}
        if not art:
            return 0
        names = art_names(shortcut_appid('"%s"' % s["exe"], s["name"]))
        n = 0
        for vdf in files:
            grid = vdf.parent / "grid"
            for kind, src in art.items():
                if kind not in names:
                    continue
                try:
                    raw = Path(src).read_bytes()
                    ext = image_ext(raw)
                    if not ext:
                        continue
                    grid.mkdir(parents=True, exist_ok=True)
                    for old in grid.glob(names[kind] + ".*"):    # one image per slot, whatever its extension
                        old.unlink()
                    (grid / (names[kind] + ext)).write_bytes(raw)
                    n += 1
                except OSError:
                    continue
        return n

    # -- steam
    def _shortcut_files(self) -> list[Path]:
        out = []
        for u in sorted((self.steam_dir / "userdata").glob("*")):
            if u.name.isdigit() and u.name != "0" and (u / "config").is_dir():
                out.append(u / "config" / "shortcuts.vdf")
        return out

    def apply(self, restart: bool) -> dict:
        files = self._shortcut_files()
        if not files:
            raise ValueError("no Steam user found in %s" % self.steam_dir)
        was_running = steam_running()
        if was_running:
            if not restart:
                raise ValueError("Steam is running. Apply again with restart (this closes and reopens Steam).")
            if not steam_shutdown():
                raise ValueError("Steam did not close in time")
        added, ran_srm = 0, False
        try:
            with self.lock:
                p = self.pending()
                for s in p["shortcuts"]:
                    for f in files:
                        added += add_shortcut(f, s["name"], s["exe"], s["startdir"])
                    self._write_art(s, files)
                    if s.get("compat"):
                        try:
                            set_compat_tool(self.steam_dir / "config/config.vdf",
                                            shortcut_appid('"%s"' % s["exe"], s["name"]), s["compat"])
                        except OSError:
                            pass
                    for src in (s.get("art") or {}).values():
                        try:
                            Path(src).unlink()
                        except OSError:
                            pass
                if p["srm"] and self.srm:
                    ran_srm = run_srm(self.srm)
                self._save(self.pend_file, {"shortcuts": [], "srm": bool(p["srm"] and not (self.srm and ran_srm))})
        finally:
            if was_running:
                steam_start()
        return {"ok": True, "shortcuts": added, "srm": ran_srm, "restarted": was_running}

    def uninstall(self, gid: str) -> dict:
        """Delete the files this agent installed (never anything else, saves next to them stay)."""
        with self.lock:
            cur = self.jobs.get(gid)
            if cur and cur.get("state") in ("queued", "downloading", "finalizing"):
                raise ValueError("still installing: wait for it to finish")
            if cur and cur.get("state") == "installing":
                cur["_cancel"] = True          # the Windows installer is killed, then the files go
        for _ in range(40):
            with self.lock:
                if (self.jobs.get(gid) or {}).get("state") != "installing":
                    break
            time.sleep(0.5)
        with self.lock:
            reg = self.registry()
            e = reg.pop(gid, None)
            if not e:
                raise KeyError("not installed")
            roots = [self.games_dir.resolve(), self.roms_dir.resolve()]
            if e.get("root"):
                try:       # an install made into a custom folder or SD card: only if it is still a valid place
                    roots.append(self.location(e["root"]))
                except ValueError:
                    pass

            def inside(p: Path) -> bool:
                return any(p != r and p.is_relative_to(r) for r in roots)

            for f in e["files"]:
                fp = Path(f).resolve()
                if inside(fp) and fp.is_file():
                    fp.unlink()
            dest = Path(e["dest"]).resolve()
            if e.get("tree") and inside(dest) and dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            shared = e["kind"] == "rom" and dest.parent.resolve() == self.roms_dir.resolve()  # roms/<system>
            if inside(dest) and dest.is_dir() and not shared:
                dirs = [d for d in dest.rglob("*") if d.is_dir()] + [dest]
                for d in sorted(dirs, key=lambda x: -len(x.parts)):
                    try:
                        d.rmdir()          # only empty ones: leftover saves keep their folder
                    except OSError:
                        pass
            p = self.pending()
            for s in p["shortcuts"]:
                if s["exe"] == e.get("exe"):
                    for src in (s.get("art") or {}).values():
                        try:
                            Path(src).unlink()
                        except OSError:
                            pass
            p["shortcuts"] = [s for s in p["shortcuts"] if s["exe"] != e.get("exe")]
            self._save(self.pend_file, p)
            removed = False
            if e.get("exe") and not steam_running():
                for f in self._shortcut_files():
                    removed |= remove_shortcut(f, e["exe"])
            self._save(self.reg_file, reg)
            self.jobs.pop(gid, None)
        return {"ok": True, "shortcut_removed": removed,
                "note": "" if removed or not e.get("exe") else "Remove the Steam shortcut by hand (Steam is running)."}


def make_handler(agent: Agent):
    class H(BaseHTTPRequestHandler):
        server_version = "CasaDeck/" + VERSION

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, data) -> None:
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer ") and hmac.compare_digest(got[7:], agent.token):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def do_GET(self):
            if not self._auth():
                return
            if self.path.split("?")[0] == "/status":
                return self._send(200, agent.status())
            if self.path.split("?")[0] == "/locations":
                return self._send(200, agent.locations())
            if self.path.split("?")[0] == "/protons":
                return self._send(200, agent.protons())
            if self.path.split("?")[0] == "/installers":
                return self._send(200, agent.installers())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            try:
                path = self.path.split("?")[0]
                body = self._body()
                if path == "/install":
                    agent.submit(body)
                    return self._send(200, {"ok": True})
                if path == "/location/check":
                    return self._send(200, agent.check_location(body.get("path")))
                if path == "/installers/run":
                    return self._send(200, agent.run_staged(body))
                if path == "/cancel":
                    return self._send(200, agent.cancel(str(body.get("id", ""))))
                if path == "/uninstall":
                    return self._send(200, agent.uninstall(str(body.get("id", ""))))
                if path == "/apply":
                    return self._send(200, agent.apply(bool(body.get("restart"))))
                self._send(404, {"error": "not found"})
            except KeyError as err:
                self._send(404, {"error": str(err).strip("'\"")})
            except (ValueError, TypeError) as err:
                self._send(400, {"error": str(err)})
            except Exception as err:
                self._send(500, {"error": "%s: %s" % (type(err).__name__, err)})

    return H


def find_srm(explicit: str) -> str:
    home = Path.home()
    for c in ([explicit] if explicit else []) + [
            str(home / "Applications/Steam-ROM-Manager.AppImage"),
            str(home / ".config/EmuDeck/backend/tools/srm/Steam-ROM-Manager.AppImage"),
            str(home / "Emulation/tools/Steam ROM Manager/Steam-ROM-Manager.AppImage")]:
        if c and os.access(c, os.X_OK):
            return c
    return ""


def main(argv=None) -> None:
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-file", default=str(home / ".config/casa-deck/token"))
    ap.add_argument("--games-dir", default=str(home / "Games"))
    ap.add_argument("--roms-dir", default=str(home / "Emulation/roms"))
    ap.add_argument("--steam-dir", default=str(home / ".steam/steam"))
    ap.add_argument("--data-dir", default=str(home / ".local/share/casa-deck"))
    ap.add_argument("--proton", default="", help="name of the Proton folder to run Windows installers with (default: best found)")
    ap.add_argument("--srm", default="", help="path to the Steam ROM Manager AppImage (auto-detected)")
    a = ap.parse_args(argv)
    token = Path(a.token_file).read_text().strip()
    if len(token) < 16:
        sys.exit("token in %s is too short (min 16 characters)" % a.token_file)
    agent = Agent(token, Path(a.games_dir), Path(a.roms_dir), Path(a.steam_dir), Path(a.data_dir), find_srm(a.srm), proton=a.proton)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(agent))
    print("Casa Deck agent %s on %s:%d" % (VERSION, a.host, a.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
