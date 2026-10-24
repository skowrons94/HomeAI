"""Album-level library edits: tags, merge discs, merge albums, delete. Routes call these with the Music plugin.

Everything that touches files runs in an executor; one such job at a time (``plugin.job`` holds its progress).
After the files change Navidrome is scanned and we wait for it, so the app reloads a library that is up to date.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ...i18n import _
from . import libfiles, tags as T
from .subsonic import SubsonicError, valid_id

log = logging.getLogger("hub.music")


async def _run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def load_album(p, aid: str, with_tags: bool = True) -> dict:
    """The album from Navidrome plus the real file of every track (and its tags). Refuses when any is missing."""
    album = await p._call(p.nav.album(aid))
    music = Path(p.music_dir)
    songs = album["tracks"]
    paths = await _run(lambda: [libfiles.locate(music, s) for s in songs])
    missing = [s["title"] for s, f in zip(songs, paths) if f is None]
    if missing:
        raise ValueError(_("Files not found on the NAS (nothing was changed): {list}", list=", ".join(missing[:10])))
    if not songs:
        raise ValueError(_("This album has no tracks"))
    if len(paths) > T.MAX_FILES:
        raise ValueError(_("Too many files for one operation (max {n})", n=T.MAX_FILES))
    album["paths"] = paths
    if with_tags:
        try:
            album["tags"] = await _run(T.read_many, paths)
        except T.TagError as err:
            raise ValueError(str(err))
    return album


def _field(values: list[str]) -> dict:
    vals = sorted({v for v in values if v})
    return {"value": vals[0] if len(vals) == 1 else "", "mixed": len(vals) > 1 or (len(vals) == 1 and "" in values),
            "values": vals[:6]}


async def get_tags(p, aid: str) -> dict:
    a = await load_album(p, aid)
    music = Path(p.music_dir).resolve()
    return {"album": {"id": a["id"], "name": a["name"], "artist": a["artist"], "year": a["year"]},
            "count": len(a["paths"]),
            "fields": {k: _field([t[k] for t in a["tags"]])
                       for k in ("album", "albumartist", "artist", "year", "genre", "discnumber", "disctotal")},
            "files": [{"title": s["title"], "path": str(f.resolve().relative_to(music)), "tags": t}
                      for s, f, t in zip(a["tracks"], a["paths"], a["tags"])]}


async def siblings(p, aid: str) -> dict:
    album = await p._call(p.nav.album(aid))
    if not album["artist_id"]:
        return {"siblings": [], "base": ""}
    others = (await p._call(p.nav.artist(album["artist_id"])))["list"]
    sib = T.find_siblings(album, others)
    return {"siblings": sib["members"] if sib else [], "base": sib["base"] if sib else ""}


async def _apply(p, what: str, aid: str, first_song: str, changes: list[tuple[Path, dict]]) -> dict:
    """Write the tags, scan Navidrome, find where the album went. Logs what happened (INFO) either way."""
    if p.job:
        raise ValueError(_("Another library change is running, try again in a minute"))
    p.job = {"phase": "tags", "done": 0, "total": len(changes)}
    first = str(changes[0][0]) if changes else ""
    log.info("library %s %s: rewriting tags of %d files (first: %s)", what, aid, len(changes), first)

    def progress(done, total):
        p.job.update(done=done, total=total)
    try:
        res = await _run(T.write_many, changes, progress)
        p.job = {"phase": "scan", "done": res["files"], "total": res["files"]}
        scanned = await p.nav.scan_and_wait()
        p._owned = (0.0, [])
        new_id = aid
        try:
            new_id = (await p.nav.song(first_song))["album_id"] or aid
        except Exception as err:
            log.info("library %s %s: could not look up the new album: %s", what, aid, err)
    except T.TagError as err:
        log.warning("library %s %s failed: %s", what, aid, err)
        raise ValueError(str(err))
    except Exception as err:
        log.warning("library %s %s failed: %s", what, aid, err, exc_info=not isinstance(err, ValueError))
        raise
    finally:
        p.job = None
    log.info("library %s %s done: %d files, %d rewritten, scan %s, album now %s", what, aid, res["files"],
             res["changed"], "finished" if scanned else "still running", new_id)
    return {"ok": True, "album_id": new_id, "files": res["files"], "changed": res["changed"], "scanned": scanned}


def _clean(v) -> str:
    return " ".join(str(v).split())


async def edit(p, aid: str, body: dict) -> dict:
    ch = {}
    for k in ("album", "albumartist", "year", "genre"):
        if body.get(k) is not None:
            v = _clean(body[k])
            if k == "year" and v and not (v.isdigit() and len(v) == 4):
                raise ValueError(_("Invalid request"))
            if k in ("album", "albumartist") and not v:
                raise ValueError(_("The name cannot be empty"))
            ch[k] = v
    if not ch:
        raise ValueError(_("Nothing to change"))
    a = await load_album(p, aid, with_tags=False)
    return await _apply(p, "edit", aid, a["tracks"][0]["id"], [(f, dict(ch)) for f in a["paths"]])


def _album_artist(a: dict) -> str:
    return T.consistent([t["albumartist"] for t in a["tags"]]) or a["artist"]


async def merge(p, aid: str, body: dict) -> dict:
    into = str(body.get("into") or "")
    if not into or into == aid:
        raise ValueError(_("Invalid request"))
    if not valid_id(into):
        raise ValueError(_("Invalid request"))
    src, dst = await load_album(p, aid), await load_album(p, into)
    if len(src["paths"]) + len(dst["paths"]) > T.MAX_FILES:
        raise ValueError(_("Too many files for one operation (max {n})", n=T.MAX_FILES))
    dst_discs = [int(t["discnumber"] or 0) or 1 for t in dst["tags"]]
    disc = body.get("as_disc", "auto")
    if disc in ("auto", None, ""):
        disc = max(dst_discs) + 1
    else:
        try:
            disc = int(disc)
        except (TypeError, ValueError):
            raise ValueError(_("Invalid request"))
        if not 1 <= disc <= 99:
            raise ValueError(_("Invalid request"))
    year = str(dst["year"] or T.consistent([t["year"] for t in dst["tags"]]) or src["year"] or "")
    plan = T.plan_discs([{"files": dst["paths"], "tags": dst["tags"], "disc": None},
                         {"files": src["paths"], "tags": src["tags"], "disc": disc}],
                        dst["name"], _album_artist(dst), year)
    return await _apply(p, "merge", aid, src["tracks"][0]["id"], plan)


async def merge_discs(p, aid: str) -> dict:
    sib = await siblings(p, aid)
    if len(sib["siblings"]) < 2:
        raise ValueError(_("No other discs of this album were found"))
    albums = [await load_album(p, m["id"]) for m in sib["siblings"]]
    if sum(len(a["paths"]) for a in albums) > T.MAX_FILES:
        raise ValueError(_("Too many files for one operation (max {n})", n=T.MAX_FILES))
    years = [str(a["year"]) for a in albums if a["year"]] + [y for a in albums for y in (t["year"] for t in a["tags"]) if y]
    artist = T.consistent([_album_artist(a) for a in albums]) or _album_artist(albums[0])
    parts = [{"files": a["paths"], "tags": a["tags"], "disc": m["disc"]} for a, m in zip(albums, sib["siblings"])]
    plan = T.plan_discs(parts, sib["base"], artist, min(years) if years else "")
    mine = next(a for a, m in zip(albums, sib["siblings"]) if m["id"] == aid)
    return await _apply(p, "merge-discs", aid, mine["tracks"][0]["id"], plan)


async def delete(p, kind: str, iid: str, files: list[Path], missing: int) -> dict:
    """Delete the files, scan Navidrome and wait, so the app's next list no longer shows the album."""
    if p.job:
        raise ValueError(_("Another library change is running, try again in a minute"))
    p.job = {"phase": "delete", "done": 0, "total": len(files)}
    log.info("library delete %s %s: %d files (first: %s)", kind, iid, len(files), files[0] if files else "")
    try:
        n = await _run(libfiles.delete, Path(p.music_dir), files)
        p.job = {"phase": "scan", "done": n, "total": n}
        scanned, listed = False, False
        try:
            scanned = await p.nav.scan_and_wait()
            listed = await p.nav.exists(kind, iid)
            if listed:
                log.info("library delete %s %s: still listed after the scan, running a full scan", kind, iid)
                scanned = await p.nav.scan_and_wait(full=True)
                listed = await p.nav.exists(kind, iid)
        except SubsonicError as err:           # the files are gone either way: say so, the scan will catch up
            log.warning("library delete %s %s: Navidrome scan failed: %s", kind, iid, err)
        p._owned = (0.0, [])
    except Exception as err:
        log.warning("library delete %s %s failed: %s", kind, iid, err, exc_info=not isinstance(err, (ValueError, OSError)))
        raise
    finally:
        p.job = None
    log.info("library delete %s %s done: %d files removed, scan %s, still listed: %s", kind, iid, n,
             "finished" if scanned else "still running", listed)
    return {"ok": True, "deleted": n, "missing": missing, "scanned": scanned, "listed": listed}
