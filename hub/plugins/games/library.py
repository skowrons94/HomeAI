"""Scan the games folder on the NAS into a list of installable games. Read-only.

    <library>/pc/<Title>[ vX.Y]/            a Windows or Linux game (one folder = one game)
    <library>/pc/<Title>/<version>/         …or one sub-folder per version (newest is used)
    <library>/emu/<system>/<rom or folder>  a ROM (folders for cue/bin and multi-disc)
    optional  game.json  {"title", "version", "kind", "exe", "system"}  next to the files
    optional  cover.jpg|png|webp            shown in the app

kind: ``portable`` (an .exe that runs as is), ``installer`` (setup.exe), ``native`` (Linux
AppImage / .sh), ``rom``. Anything else is reported in ``problems`` instead of guessed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

KINDS = ("portable", "installer", "native", "rom")
COVERS = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "poster.jpg")
ROM_SKIP = {".txt", ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".xml", ".json", ".md", ".sav", ".srm",
            ".state", ".db", ".ini", ".url", ".pdf"}
EXE_SKIP = re.compile(r"unins|crash|redist|vcredist|dxsetup|dotnet|vc_?redist|helper|updater|launcherhelper", re.I)
INSTALLER = re.compile(r"^(setup|install|installer)(?![a-z0-9])|[_\- .](setup|installer)(?![a-z0-9])", re.I)
VERSION_DIR = re.compile(r"^v?\d+(\.\d+)*[\w.+-]*$", re.I)
NAME_VERSION = re.compile(r"^(?P<t>.*?)[\s_]*[\(\[]?\bv(?P<v>\d[\w.+-]*)[\)\]]?$", re.I)
SYSTEM = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_FILES = 20000


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "game"


def vkey(v: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v) if p]


def split_name(name: str) -> tuple[str, str]:
    """'Celeste v1.4.0' → ('Celeste', '1.4.0'); no version marker → (name, '')."""
    m = NAME_VERSION.match(name.strip())
    if m and m["t"].strip():
        return m["t"].strip(), m["v"]
    return name.strip(), ""


def _manifest(d: Path) -> dict:
    try:
        data = json.loads((d / "game.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _walk(d: Path) -> tuple[list[dict], int]:
    """Files under d as [{'p': relative posix path, 's': bytes}], skipping manifest/cover."""
    out, total = [], 0
    for root, dirs, names in os.walk(d, followlinks=False):
        dirs.sort()
        for n in sorted(names):
            full = Path(root) / n
            rel = full.relative_to(d).as_posix()
            if rel == "game.json" or rel.lower() in COVERS or full.is_symlink():
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            out.append({"p": rel, "s": size})
            total += size
            if len(out) >= MAX_FILES:
                return out, total
    return out, total


def _cover(d: Path) -> str | None:
    for c in COVERS:
        if (d / c).is_file():
            return str(d / c)
    return None


def _pick_exe(files: list[dict], hint: str = "") -> tuple[str, str]:
    """→ (kind, exe path) for a game folder, or ('', '')."""
    paths = [f["p"] for f in files]
    if hint and hint in paths:
        kind = "native" if not hint.lower().endswith(".exe") else (
            "installer" if INSTALLER.search(Path(hint).name) else "portable")
        return kind, hint
    size = {f["p"]: f["s"] for f in files}
    exes = [p for p in paths if p.lower().endswith(".exe") and p.count("/") <= 2]
    games = [p for p in exes if not EXE_SKIP.search(Path(p).name) and not INSTALLER.search(Path(p).name)]
    if games:
        return "portable", max(games, key=lambda p: size[p])
    setups = [p for p in exes if INSTALLER.search(Path(p).name)]
    if setups:
        return "installer", sorted(setups, key=lambda p: (p.count("/"), p))[0]
    nat = [p for p in paths if p.count("/") <= 1 and (p.lower().endswith((".appimage", ".x86_64"))
                                                      or (p.lower().endswith(".sh") and not INSTALLER.search(Path(p).name)))]
    if nat:
        return "native", sorted(nat, key=lambda p: (not p.lower().endswith(".appimage"), p))[0]
    return "", ""


def _game_dir(entry: Path) -> tuple[Path, str]:
    """Versioned layout: <Title>/<v1.0>/ → newest version folder; else the folder itself."""
    subs = [p for p in entry.iterdir() if p.is_dir()]
    loose = [p for p in entry.iterdir() if p.is_file() and p.name != "game.json" and p.name.lower() not in COVERS]
    if subs and not loose and all(VERSION_DIR.match(p.name) for p in subs):
        best = max(subs, key=lambda p: vkey(p.name.lstrip("vV")))
        return best, best.name.lstrip("vV")
    return entry, ""


def scan(root: str) -> tuple[list[dict], list[dict]]:
    """→ (games, problems). Public fields only; internal ones start with ``_``."""
    games: list[dict] = []
    problems: list[dict] = []
    base = Path(root).expanduser()
    if not base.is_dir():
        return games, [{"path": str(base), "reason": "library folder not found"}]
    seen: set[str] = set()

    def add(g: dict):
        gid, n = g["id"], 2
        while gid in seen:
            gid, n = f"{g['id']}-{n}", n + 1
        g["id"] = gid
        seen.add(gid)
        games.append(g)

    def top(name: str) -> Path | None:
        for p in base.iterdir():
            if p.is_dir() and p.name.lower() == name:
                return p
        return None

    pc = top("pc")
    for entry in sorted(pc.iterdir()) if pc else []:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            gdir, dir_version = _game_dir(entry)
            man = {**_manifest(entry), **_manifest(gdir)}
            title, name_version = split_name(entry.name)
            files, size = _walk(gdir)
            kind, exe = _pick_exe(files, str(man.get("exe") or ""))
            if man.get("kind") in ("portable", "installer", "native"):
                kind = man["kind"]
            if not kind or (kind != "installer" and not exe):
                problems.append({"path": entry.name, "reason": "no .exe, AppImage or .sh found: add a game.json with \"exe\""})
                continue
            add({"id": "pc-" + slug(str(man.get("title") or title)), "title": str(man.get("title") or title),
                 "version": str(man.get("version") or dir_version or name_version), "kind": kind, "system": "",
                 "size": size, "files": len(files), "exe": exe, "folder": "",
                 "cover": bool(_cover(gdir) or _cover(entry)),
                 "_dir": str(gdir), "_cover": _cover(gdir) or _cover(entry), "_list": files,
                 "path": str(gdir.relative_to(base))})
        except OSError as err:
            problems.append({"path": entry.name, "reason": str(err)})

    emu = top("emu")
    for sysdir in sorted(emu.iterdir()) if emu else []:
        if not sysdir.is_dir() or not SYSTEM.match(sysdir.name.lower()):
            continue
        system = sysdir.name.lower()
        for entry in sorted(sysdir.iterdir()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file():
                    if entry.suffix.lower() in ROM_SKIP:
                        continue
                    files, size, gdir, folder = [{"p": entry.name, "s": entry.stat().st_size}], entry.stat().st_size, sysdir, ""
                    title, version = split_name(entry.stem)
                elif entry.is_dir():
                    files, size = _walk(entry)
                    if not files:
                        continue
                    gdir, folder = entry, entry.name
                    title, version = split_name(entry.name)
                else:
                    continue
                cov = None
                for ext in (".jpg", ".png", ".webp"):
                    c = sysdir / (entry.stem + ext) if entry.is_file() else entry / ("cover" + ext)
                    if c.is_file():
                        cov = str(c)
                        break
                add({"id": f"{system}-" + slug(title), "title": title, "version": version, "kind": "rom",
                     "system": system, "size": size, "files": len(files), "exe": "", "folder": folder,
                     "cover": bool(cov), "_dir": str(gdir), "_cover": cov, "_list": files,
                     "path": str(entry.relative_to(base))})
            except OSError as err:
                problems.append({"path": f"{sysdir.name}/{entry.name}", "reason": str(err)})
    return games, problems


def public(g: dict) -> dict:
    return {k: v for k, v in g.items() if not k.startswith("_")}


def safe_file(g: dict, rel: str) -> Path | None:
    """The file a download asks for, only if it is one of the game's own files."""
    if any(f["p"] == rel for f in g["_list"]):
        p = Path(g["_dir"]) / rel
        if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(Path(g["_dir"]).resolve()):
            return p
    return None
