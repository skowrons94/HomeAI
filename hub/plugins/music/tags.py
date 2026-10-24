"""Read and write the tags of library files (blocking: run in an executor) and plan album edits/merges.

Writes are safe on SMB: the file is copied to a ``.casa-tag-*.part`` sibling, the copy is tagged and re-opened to
check it, then ``os.replace`` swaps it over the original. Audio data is never touched. mutagen is imported lazily.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

MAX_FILES = 200
FIELDS = ("album", "albumartist", "artist", "year", "genre", "discnumber", "disctotal", "tracknumber")
VORBIS = {".flac", ".ogg", ".oga", ".opus"}
SUPPORTED = VORBIS | {".mp3", ".m4a", ".mp4", ".m4b"}


class TagError(Exception):
    pass


def _open(path: Path, ext: str | None = None):
    """The file with mutagen's 'easy' tag interface, chosen by extension (the temp copy has another one)."""
    ext = ext or path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            return FLAC(path)
        if ext == ".mp3":
            from mutagen.mp3 import EasyMP3
            return EasyMP3(path)
        if ext in (".m4a", ".mp4", ".m4b"):
            from mutagen.easymp4 import EasyMP4
            return EasyMP4(path)
        if ext in (".ogg", ".oga"):
            from mutagen.oggvorbis import OggVorbis
            return OggVorbis(path)
        if ext == ".opus":
            from mutagen.oggopus import OggOpus
            return OggOpus(path)
    except Exception as err:
        raise TagError("%s: %s" % (path.name, err)) from err
    raise TagError("%s: format not supported" % path.name)


def _first(tags, key) -> str:
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v or "").strip()


def _pair(v: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?", v or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def _read(f, ext: str) -> dict:
    tags = f.tags or {}
    disc, total = _pair(_first(tags, "discnumber"))
    if ext in VORBIS:
        total = _pair(_first(tags, "disctotal") or _first(tags, "totaldiscs"))[0] or total
    m = re.match(r"\d{4}", _first(tags, "date"))
    return {"album": _first(tags, "album"), "albumartist": _first(tags, "albumartist"),
            "artist": _first(tags, "artist"), "year": m.group(0) if m else "", "genre": _first(tags, "genre"),
            "discnumber": str(disc or ""), "disctotal": str(total or ""),
            "tracknumber": str(_pair(_first(tags, "tracknumber"))[0] or "")}


def read_tags(path: Path) -> dict:
    return _read(_open(Path(path)), Path(path).suffix.lower())


def read_many(paths: list[Path]) -> list[dict]:
    if len(paths) > MAX_FILES:
        raise TagError("too many files (%d, max %d)" % (len(paths), MAX_FILES))
    return [read_tags(p) for p in paths]


def _apply(f, ext: str, ch: dict) -> None:
    if f.tags is None:
        f.add_tags()
    tags = f.tags

    def put(key, value):
        if value in (None, ""):
            if key in tags:
                del tags[key]
        else:
            tags[key] = [str(value)]
    for k, key in (("album", "album"), ("albumartist", "albumartist"), ("artist", "artist"), ("genre", "genre"),
                   ("year", "date"), ("tracknumber", "tracknumber")):
        if k in ch:
            put(key, ch[k])
    if "discnumber" in ch or "disctotal" in ch:
        cur = _read(f, ext)
        n = str(ch.get("discnumber", cur["discnumber"]) or "")
        tot = str(ch.get("disctotal", cur["disctotal"]) or "")
        if ext in VORBIS:
            if "discnumber" in ch:
                put("discnumber", n)
            if "disctotal" in ch:
                put("disctotal", tot)
                if "totaldiscs" in tags:
                    del tags["totaldiscs"]
        else:
            put("discnumber", (n + "/" + tot if tot else n) if n else "")
    f.save()


def _same(got: dict, ch: dict) -> bool:
    return all(str(got.get(k, "")) == str(v or "") for k, v in ch.items())


def write_tags(path: Path, ch: dict) -> bool:
    """Apply ``ch`` to one file atomically. → False when nothing needed to change."""
    path = Path(path)
    ext = path.suffix.lower()
    cur = _read(_open(path), ext)
    ch = {k: v for k, v in ch.items() if k in FIELDS and str(cur.get(k, "")) != str(v or "")}
    if not ch:
        return False
    fd, tmp = tempfile.mkstemp(prefix=".casa-tag-", suffix=".part", dir=str(path.parent))
    os.close(fd)
    part = Path(tmp)
    try:
        shutil.copyfile(path, part)
        try:
            shutil.copymode(path, part)
        except OSError:
            pass
        f = _open(part, ext)
        _apply(f, ext, ch)
        chk = _open(part, ext)
        if not _same(_read(chk, ext), ch):
            raise TagError("%s: the tags did not stick" % path.name)
        old = _open(path)
        if abs((old.info.length or 0) - (chk.info.length or 0)) > 0.05:
            raise TagError("%s: audio length changed" % path.name)
        os.replace(part, path)
        return True
    finally:
        part.unlink(missing_ok=True)


def write_many(changes: list[tuple[Path, dict]], progress=None) -> dict:
    """Write each (path, tags). Every file is opened first so a broken one stops the job before anything changes."""
    if len(changes) > MAX_FILES:
        raise TagError("too many files (%d, max %d)" % (len(changes), MAX_FILES))
    for p, _c in changes:
        if Path(p).suffix.lower() not in SUPPORTED:
            raise TagError("%s: format not supported" % Path(p).name)
        _open(Path(p))
    done = changed = 0
    for p, c in changes:
        try:
            changed += bool(write_tags(p, c))
        except TagError:
            raise
        except Exception as err:
            raise TagError("%s: %s (after %d files)" % (Path(p).name, err, done)) from err
        done += 1
        if progress:
            progress(done, len(changes))
    return {"files": len(changes), "changed": changed}


# -- planning (pure) ------------------------------------------------------------------------------

_DISC_PATTERNS = (
    re.compile(r"^(.*?)[\s\-–:,]*[\(\[]\s*(?:cd|disc|disk)\s*\.?\s*(\d{1,2})\s*[\)\]]\s*$", re.I),
    re.compile(r"^(.*?)[\s\-–:,]+(?:cd|disc|disk)\s*\.?\s*(\d{1,2})\s*$", re.I),
    re.compile(r"^(.*?)\s*[\(\[]\s*(\d{1,2})\s*[\)\]]\s*$"),
)


def split_disc(name: str) -> tuple[str, int]:
    """'The Bends (2)' / 'Album CD1' / 'Album - Disc 2' → (base, disc); no suffix → (name, 0)."""
    name = " ".join(str(name or "").split())
    for rx in _DISC_PATTERNS:
        m = rx.match(name)
        if m and m.group(1).strip():
            return m.group(1).strip(), int(m.group(2))
    return name, 0


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def find_siblings(album: dict, albums: list[dict]) -> dict | None:
    """The albums of the same artist that are discs of ``album``: names differ only by a (n)/CD n/Disc n suffix.
    → {base, members: [{id, name, disc, songs}]} sorted by disc, or None."""
    base, _d = split_disc(album["name"])
    key, artist = _n(base), _n(album.get("artist"))
    if not key:
        return None
    members, plain = [], None
    for a in albums:
        if _n(a.get("artist")) != artist:
            continue
        b, d = split_disc(a["name"])
        if _n(b) != key:
            continue
        if d:
            members.append({"id": a["id"], "name": a["name"], "disc": d, "songs": a.get("songs", 0)})
        else:
            plain = {"id": a["id"], "name": a["name"], "disc": 0, "songs": a.get("songs", 0)}
    if plain is not None and not any(m["id"] == plain["id"] for m in members):
        members.append(plain)
    if len(members) < 2 or not any(m["id"] == album["id"] for m in members):
        return None
    used = {m["disc"] for m in members if m["disc"]}
    for m in members:
        if not m["disc"]:
            m["disc"] = next(i for i in range(1, 100) if i not in used)
            used.add(m["disc"])
    members.sort(key=lambda m: (m["disc"], m["name"]))
    seen = set()
    for i, m in enumerate(members, 1):          # duplicate numbers (two "(1)" albums): number by order
        if m["disc"] in seen:
            for j, x in enumerate(members, 1):
                x["disc"] = j
            break
        seen.add(m["disc"])
    return {"base": base, "members": members}


def consistent(values: list[str]) -> str:
    vals = {v for v in values if v}
    return vals.pop() if len(vals) == 1 else ""


def plan_discs(parts: list[dict], album: str, albumartist: str, year: str, genre: str | None = None) -> list[tuple[Path, dict]]:
    """parts: [{files: [Path], tags: [dict], disc: n or None}]. Every part gets the album name/artist/year and its
    disc number (None = keep its numbers, only add the total); disctotal goes on all files.
    Files whose tags already match are left out by write_tags."""
    out = []
    discs = []
    for part in parts:
        old = [int(t.get("discnumber") or 0) or 1 for t in part["tags"]]
        base = min(old) if old else 1
        part["new"] = [(part["disc"] + d - base) if part.get("disc") else d for d in old]
        discs += part["new"]
    total = max(discs) if discs else 1
    for part in parts:
        for p, d in zip(part["files"], part["new"]):
            ch = {"album": album, "albumartist": albumartist, "discnumber": str(d), "disctotal": str(total)}
            if year:
                ch["year"] = str(year)
            if genre:
                ch["genre"] = genre
            out.append((p, ch))
    return out
