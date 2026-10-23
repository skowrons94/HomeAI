#!/usr/bin/env python3
"""Casa Deck agent: runs on the Steam Deck, installs what the Casa hub tells it to.

Standard library only (SteamOS ships Python 3, no pip needed). It

  * listens on a port (default 8765) for the hub, which must send the shared token;
  * downloads a game's files from the hub over signed, expiring links (resumable);
  * puts PC games in ~/Games/<Title>, ROMs in ~/Emulation/roms/<system>;
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

VERSION = "1.1"
SYSTEM_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_BODY = 8 * 1024 * 1024
MAX_ART = 3 * 1024 * 1024                 # per image, after decoding
ART_KINDS = ("grid", "wide", "hero", "logo")


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


def safe_name(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r'[<>:"|?*\x00-\x1f/\\]', "_", s).strip(" .")
    return s if s and s not in (".", "..") else None


class Agent:
    def __init__(self, token: str, games_dir: Path, roms_dir: Path, steam_dir: Path, data_dir: Path,
                 srm: str = ""):
        self.token, self.games_dir, self.roms_dir = token, games_dir, roms_dir
        self.steam_dir, self.data_dir, self.srm = steam_dir, data_dir, srm
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
            reg = {k: {x: v.get(x) for x in ("version", "kind", "note", "installed_at")} for k, v in self.registry().items()}
            jobs = {k: {x: j.get(x) for x in ("state", "done", "message")} for k, j in self.jobs.items()}
            p = self.pending()
        return {"agent": VERSION, "installed": reg, "jobs": jobs, "pending": len(p["shortcuts"]) + (1 if p["srm"] else 0),
                "steam_running": steam_running()}

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
        raw_art = job.pop("art", None)
        art = decode_art(raw_art) if kind in ("portable", "native") else {}
        with self.lock:
            cur = self.jobs.get(gid)
            if cur and cur["state"] in ("queued", "downloading", "finalizing"):
                raise ValueError("already installing")
            job = {**job, "state": "queued", "done": 0.0, "message": "queued", "_art": art}
            self.jobs[gid] = job
        self.q.put(gid)

    def _dest(self, job: dict) -> Path:
        kind = job["kind"]
        if kind == "rom":
            root = self.roms_dir / job["system"]
            return root / safe_name(job["folder"]) if job.get("folder") else root
        root = self.games_dir / ("_installers" if kind == "installer" else "")
        return root / safe_name(job["folder"])

    def _worker(self) -> None:
        while True:
            gid = self.q.get()
            try:
                self._run(self.jobs[gid])
            except Exception as err:  # never kill the worker
                self._set(self.jobs[gid], state="failed", message=str(err) or type(err).__name__)

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
        if shutil.disk_usage(probe).free < total * 1.02 + (50 << 20):
            raise RuntimeError("not enough free space on the Deck")
        self._set(job, state="downloading", message="downloading", done=0.0)
        counter = [0]
        for f in job["files"]:
            self._download(job, f["p"], f["s"], dest / f["p"], counter)
        self._set(job, state="finalizing", message="finishing", done=0.99)
        files = [str(dest / f["p"]) for f in job["files"]]
        note, state = "", "done"
        entry = {"id": job["id"], "version": job.get("version", ""), "kind": job["kind"], "dest": str(dest),
                 "files": files, "installed_at": int(time.time()), "exe": ""}
        if job["kind"] in ("portable", "native"):
            exe = dest / job["exe"]
            if job["kind"] == "native":
                exe.chmod(exe.stat().st_mode | 0o111)
            entry["exe"] = str(exe)
            staged = self._stage_art(job)
            with self.lock:
                p = self.pending()
                p["shortcuts"] = [s for s in p["shortcuts"] if s["exe"] != str(exe)] + [
                    {"name": job["title"], "exe": str(exe), "startdir": str(exe.parent),
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
        self._set(job, state=state, done=1.0, message=note or "installed")

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
            reg = self.registry()
            e = reg.pop(gid, None)
            if not e:
                raise KeyError("not installed")
            roots = [self.games_dir.resolve(), self.roms_dir.resolve()]

            def inside(p: Path) -> bool:
                return any(p != r and p.is_relative_to(r) for r in roots)

            for f in e["files"]:
                fp = Path(f).resolve()
                if inside(fp) and fp.is_file():
                    fp.unlink()
            dest = Path(e["dest"]).resolve()
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
    ap.add_argument("--srm", default="", help="path to the Steam ROM Manager AppImage (auto-detected)")
    a = ap.parse_args(argv)
    token = Path(a.token_file).read_text().strip()
    if len(token) < 16:
        sys.exit("token in %s is too short (min 16 characters)" % a.token_file)
    agent = Agent(token, Path(a.games_dir), Path(a.roms_dir), Path(a.steam_dir), Path(a.data_dir), find_srm(a.srm))
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(agent))
    print("Casa Deck agent %s on %s:%d" % (VERSION, a.host, a.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
