"""Merging feed events: the newest (ts, id) per (user, imdb_id, kind) wins. Pure."""

from __future__ import annotations


def sort_key(ev: dict) -> tuple[str, str]:
    return ev["ts"], ev["id"]


def merge(events) -> dict[tuple[str, str, str], dict]:
    """Input order doesn't matter; ``id`` breaks ties between events of the same millisecond."""
    out: dict[tuple[str, str, str], dict] = {}
    for ev in events:
        k = (ev["user"], ev["imdb_id"], ev["kind"])
        if k not in out or sort_key(ev) > sort_key(out[k]):
            out[k] = ev
    return out


def is_active(ev: dict) -> bool:
    """False for undo events (rating removed, watchlist off, 'not watched')."""
    return {"rating": ev["rating"] is not None, "watchlist": bool(ev["on"]),
            "watch": ev["watched_at"] is not None}[ev["kind"]]


def newest_first(events) -> list[dict]:
    return sorted(events, key=sort_key, reverse=True)
