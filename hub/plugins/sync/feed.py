"""Feed events: ULIDs, building/validating events, the privacy scan and JSONL helpers.

Everything that ends up in the shared repo passes through ``validate_event`` / ``scan_text``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from ...i18n import _

log = logging.getLogger("hub.sync.feed")

KEYS = ("v", "id", "ts", "user", "kind", "media", "imdb_id", "tmdb_id", "rating", "on", "watched_at", "note")
KINDS = ("watch", "rating", "watchlist")
MEDIA = ("movie", "series")
USER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
IMDB_RE = re.compile(r"^tt\d{5,10}$")
ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOTE_MAX = 500

# -- privacy -------------------------------------------------------------------------------------

_HEX = "[0-9a-fA-F]"
_FORBIDDEN = [
    ("an IP address", re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")),
    ("an IPv6 address", re.compile(
        rf"(?<![\w:])(?:(?:{_HEX}{{1,4}}:){{7}}{_HEX}{{1,4}}|(?:{_HEX}{{1,4}}:)+:(?:{_HEX}{{1,4}}(?::{_HEX}{{1,4}})*)?"
        rf"|::{_HEX}{{1,4}}(?::{_HEX}{{1,4}})*)(?![\w:])")),
    ("a MAC address", re.compile(rf"(?<![\w:-]){_HEX}{{2}}([:-]){_HEX}{{2}}(?:\1{_HEX}{{2}}){{4}}(?![\w:-])")),
    ("a local path", re.compile(r"/mnt/|/Volumes/|/home/|/Users/|~/|(?<![\w])[A-Za-z]:\\|\\\\\w", re.I)),
    ("a magnet link", re.compile(r"magnet:", re.I)),
    ("a torrent file", re.compile(r"\.torrent\b", re.I)),
    ("a URL", re.compile(r"[a-z][a-z0-9+.-]*://|(?<![\w])www\.", re.I)),
]


def scan_text(text: str, what: str = "text") -> str:
    """Return ``text`` or raise ValueError if it holds anything private (IPs, paths, URLs, magnets…)."""
    if not isinstance(text, str):
        raise ValueError(f"{what} must be text")
    for label, rx in _FORBIDDEN:
        if rx.search(text):
            raise ValueError(_("{what} can't contain {label}: it would be shared with the other houses", what=_(what), label=_(label)))
    return text


# -- ids / time ----------------------------------------------------------------------------------

_ALPHA = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_last = [0, 0]


def ulid(ms: int | None = None) -> str:
    """26-char time-ordered id; strictly increasing within the process."""
    t = int(time.time() * 1000) if ms is None else ms
    if t <= _last[0]:
        t, r = _last[0], _last[1] + 1
    else:
        r = int.from_bytes(os.urandom(10), "big")
    if r >= 1 << 80:
        t, r = t + 1, 0
    _last[0], _last[1] = t, r
    n = (t << 80) | r
    return "".join(_ALPHA[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def now_ts() -> str:
    t = time.time()
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(t * 1000) % 1000:03d}Z"


# -- events --------------------------------------------------------------------------------------


def validate_event(ev, user: str | None = None) -> dict:
    """Check an event against the v1 contract; return it with keys in canonical order."""
    if not isinstance(ev, dict):
        raise ValueError("an event must be an object")
    for k in ev:
        if k not in KEYS:
            raise ValueError(f"field '{k}' is not allowed in a feed event")
    for k in KEYS:
        if k not in ev:
            raise ValueError(f"missing field '{k}'")
    if ev["v"] != 1 or isinstance(ev["v"], bool):
        raise ValueError("unsupported event version")
    if not isinstance(ev["id"], str) or not ID_RE.match(ev["id"]):
        raise ValueError("bad event id")
    if not isinstance(ev["ts"], str) or not TS_RE.match(ev["ts"]):
        raise ValueError("bad timestamp")
    if not isinstance(ev["user"], str) or not USER_RE.match(ev["user"]):
        raise ValueError("bad user name (a-z, 0-9, _ and -, up to 32)")
    if user is not None and ev["user"] != user:
        raise ValueError("event belongs to another user")
    if ev["kind"] not in KINDS:
        raise ValueError("kind must be watch, rating or watchlist")
    if ev["media"] not in MEDIA:
        raise ValueError("media must be movie or series")
    if not isinstance(ev["imdb_id"], str) or not IMDB_RE.match(ev["imdb_id"]):
        raise ValueError("bad IMDb id")
    t = ev["tmdb_id"]
    if t is not None and (not isinstance(t, int) or isinstance(t, bool) or t <= 0):
        raise ValueError("bad TMDB id")
    r, on, w = ev["rating"], ev["on"], ev["watched_at"]
    if r is not None:
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not 0.5 <= r <= 5 or (r * 2) % 1:
            raise ValueError("rating must be 0.5 to 5 in steps of 0.5")
    if on is not None and not isinstance(on, bool):
        raise ValueError("'on' must be true or false")
    if w is not None:
        if not isinstance(w, str) or not DAY_RE.match(w):
            raise ValueError("watched_at must be YYYY-MM-DD")
        date.fromisoformat(w)
    kind = ev["kind"]
    if kind == "watchlist" and on is None:
        raise ValueError("a watchlist event needs 'on'")
    if (kind != "rating" and r is not None) or (kind != "watchlist" and on is not None) \
            or (kind != "watch" and w is not None):
        raise ValueError(f"field not valid for kind '{kind}'")
    note = ev["note"]
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        raise ValueError(f"note must be text up to {NOTE_MAX} characters")
    scan_text(note, "a note")
    return {k: ev[k] for k in KEYS}


def build_event(user: str, kind: str, media: str, imdb_id: str, *, tmdb_id: int | None = None,
                rating=None, on=None, watched_at=None, note: str = "", ts: str | None = None,
                id: str | None = None) -> dict:
    return validate_event({"v": 1, "id": id or ulid(), "ts": ts or now_ts(), "user": user, "kind": kind,
                           "media": media, "imdb_id": imdb_id, "tmdb_id": tmdb_id, "rating": rating,
                           "on": on, "watched_at": watched_at, "note": (note or "").strip()})


def dumps(ev: dict) -> str:
    return json.dumps(ev, separators=(",", ":"), ensure_ascii=False)


_warned: set[tuple[str, int]] = set()


def read_feed(path: Path, user: str | None = None) -> list[dict]:
    """Valid events of one feed file; bad lines are skipped (logged once)."""
    user = user if user is not None else path.stem
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            out.append(validate_event(json.loads(line), user))
        except (ValueError, TypeError) as err:
            if (str(path), hash(line)) not in _warned:
                _warned.add((str(path), hash(line)))
                log.warning("ignoring %s line %d: %s", path.name, n, err)
    return out


def append_events(path: Path, events: list[dict]) -> int:
    """Append events whose id isn't in the file yet (validated first); returns how many were added."""
    path.parent.mkdir(parents=True, exist_ok=True)
    have = {e["id"] for e in read_feed(path)}
    new = []
    for e in events:
        if e["id"] not in have:
            have.add(e["id"])
            new.append(validate_event(e, path.stem))
    if not new:
        return 0
    nl = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if nl else "") + "".join(dumps(e) + "\n" for e in new))
    return len(new)
