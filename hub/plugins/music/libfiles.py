"""Find and delete the real files of library songs on the NAS.

Navidrome reports made-up paths ("Artist/Album/01-01 - Title.flac"), so a song is found by its artist
folder, extension and exact byte size (the title breaks ties). Deleting is blocking: run it in an executor.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .find import AUDIO, inside, safe_name

LEFTOVERS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".cue", ".log", ".txt", ".nfo", ".m3u", ".m3u8", ".accurip",
             ".sfv", ".md5", ".pdf", ".ini"}
MAX_WALK = 50000


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _candidates(root: Path, song: dict):
    dirs = []
    for name in (song.get("artist"), song.get("album_artist")):
        if name:
            d = root / safe_name(name, "")
            if d.is_dir() and d not in dirs:
                dirs.append(d)
    return dirs or [root]


def locate(music_dir: Path, song: dict) -> Path | None:
    """The file of one library song, or None when it can't be told apart safely."""
    root = music_dir.resolve()
    size, ext = int(song.get("size") or 0), "." + str(song.get("suffix") or "").lower().lstrip(".")
    if size <= 0 or ext not in AUDIO:
        return None
    hits, seen = [], 0
    for base in _candidates(root, song):
        for dirpath, _dirs, names in os.walk(base):
            for n in names:
                seen += 1
                if seen > MAX_WALK:
                    break
                if not n.lower().endswith(ext):
                    continue
                p = Path(dirpath) / n
                try:
                    if not p.is_symlink() and p.stat().st_size == size:
                        hits.append(p)
                except OSError:
                    pass
        if hits:
            break
    if len(hits) > 1:
        t = _norm(song.get("title"))
        named = [p for p in hits if t and t in _norm(p.stem)]
        hits = named if len(named) == 1 else hits
    if len(hits) != 1 or not inside(root, hits[0].resolve()):
        return None
    return hits[0]


def _only_leftovers(d: Path) -> bool:
    for dirpath, _dirs, names in os.walk(d):
        for n in names:
            if Path(n).suffix.lower() not in LEFTOVERS:
                return False
    return True


def delete(music_dir: Path, files: list[Path]) -> int:
    """Delete these audio files (all checked first), then folders left with only covers/logs. → files removed."""
    root = music_dir.resolve()
    checked = []
    for f in files:
        r = f.resolve()
        if f.is_symlink() or not r.is_file() or r.suffix.lower() not in AUDIO or not inside(root, r) or r.parent == root:
            raise ValueError("refusing to delete %s" % f)
        checked.append(r)
    for r in checked:
        r.unlink()
    for d in sorted({r.parent for r in checked}, key=lambda p: -len(p.parts)):
        cur = d
        while cur != root and inside(root, cur):
            if cur.is_dir() and _only_leftovers(cur):
                shutil.rmtree(cur)
                cur = cur.parent
            else:
                break
    return len(checked)
