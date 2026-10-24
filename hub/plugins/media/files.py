"""Safe access to library files: path checks, download responses, deletion.

Nothing here trusts a path from a client: callers pass ids that Library resolves to paths it
indexed, and every path is checked again against the configured library roots after resolving
symlinks (a link pointing out of the roots is refused, a root itself is never touched).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ...i18n import _
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".mpg", ".mpeg", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
DL_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt"}  # single-file subtitles worth sending to a phone


def hid(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:20]


def inside(path: str | os.PathLike, roots: list[str]) -> Path | None:
    """The resolved path if it lies strictly inside one of the roots, else None."""
    try:
        real = Path(path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    for root in roots:
        if not root:
            continue
        try:
            r = Path(root).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if real != r and real.is_relative_to(r) and r != Path(r.anchor):
            return real
    return None


def check_file(path: str, roots: list[str]) -> Path:
    """Resolve ``path`` to an existing regular file inside the roots, or raise."""
    real = inside(path, roots)
    if real is None:
        raise PermissionError(_("That file is outside the media library"))
    if not real.is_file():
        raise FileNotFoundError(_("The file is no longer there"))
    return real


def content_disposition(name: str) -> str:
    """attachment with an ASCII fallback and the real name as RFC 5987 filename*."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", name).strip() or "file"
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode() or "file" + Path(name).suffix
    ascii_name = re.sub(r'["%;\\]', "_", plain)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def file_response(path: Path, name: str | None = None) -> web.FileResponse:
    """Range-capable response served with sendfile; the file is never read into memory."""
    ctype = mimetypes.guess_type(path.name)[0]
    if path.suffix.lower() in VIDEO_EXT or not ctype:
        ctype = "application/octet-stream"  # make iOS save to Files rather than play inline
    return web.FileResponse(path, headers={
        "Content-Disposition": content_disposition(name or path.name),
        "Content-Type": ctype, "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff"})


def siblings_of(video: str) -> list[str]:
    """Subtitle files that belong to a video (same stem prefix, or the only video of the folder)."""
    folder = Path(video).parent
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    stem = Path(video).stem.lower()
    subs = [n for n in names if Path(n).suffix.lower() in SUB_EXT and n.lower().startswith(stem)]
    if not subs and sum(Path(n).suffix.lower() in VIDEO_EXT for n in names) == 1:
        subs = [n for n in names if Path(n).suffix.lower() in SUB_EXT]
    return [str(folder / n) for n in subs]


def tree_size(path: str, limit: int = 50_000) -> int:
    """Total bytes of a file or folder, without following symlinks."""
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            return os.lstat(path).st_size
    except OSError:
        return 0
    total, seen = 0, 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            seen += 1
            if seen > limit:
                return total
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def _link_below_root(path: str, roots: list[str]) -> bool:
    """True if a folder or file between the root (as configured) and the path is a symlink."""
    ap = os.path.abspath(path)
    for root in roots:
        if not root:
            continue
        ar = os.path.abspath(root)
        if ap.startswith(ar.rstrip(os.sep) + os.sep):
            cur = ar
            for part in Path(ap[len(ar):].lstrip(os.sep)).parts:
                cur = os.path.join(cur, part)
                if os.path.islink(cur):
                    return True
            return False
    return False


def check_deletable(paths: list[str], roots: list[str], allow_dirs=False) -> list[Path]:
    """All-or-nothing validation for deleting: strictly inside the roots, no symlinks involved."""
    out = []
    for p in paths:
        if os.path.islink(p):
            raise PermissionError(_("Refusing to delete a link: {path}", path=p))
        real = inside(p, roots)
        if real is None:
            raise PermissionError(_("Refusing to delete outside the media folders: {path}", path=p))
        if _link_below_root(p, roots):
            raise PermissionError(_("Refusing to delete through a link: {path}", path=p))
        if not real.exists():
            continue
        if real.is_dir() and not allow_dirs:
            raise PermissionError(_("Refusing to delete a folder: {path}", path=p))
        out.append(real)
    return out


def delete_paths(paths: list[Path], roots: list[str], prune: bool = True) -> dict:
    """Delete validated files / folders, then empty parent folders up to (never including) a root."""
    import shutil

    freed, parents = 0, set()
    for p in paths:
        freed += tree_size(str(p))
        parents.add(p.parent)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    if prune:
        for d in sorted(parents, key=lambda x: -len(x.parts)):
            while inside(d, roots) is not None:
                try:
                    d.rmdir()  # only succeeds when empty
                except OSError:
                    break
                d = d.parent
    return {"deleted": len(paths), "bytes": freed}


def size_of(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def delete_video_with_subs(video: str, roots: list[str]) -> dict:
    """Delete one library video and the subtitles that belong to it (all checked first)."""
    targets = check_deletable([video, *siblings_of(video)], roots)
    if not targets:
        raise FileNotFoundError(_("The file is no longer there"))
    return delete_paths(targets, roots)
