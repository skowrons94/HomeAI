"""Hub-side player: one queue per output, one stream at a time, automatic advance when a track ends."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from typing import Awaitable, Callable

log = logging.getLogger("hub.music")

RESTART_AFTER = 3.0      # "previous" within this many seconds goes to the previous track, later restarts it


class Output:
    """Something that can play a local audio file. Subclasses: AirPlayOutput (outputs.py), fakes in tests."""

    id = ""
    name = ""
    kind = "airplay"

    async def prepare(self) -> None:
        """Called before the first track of a play session (turn a speaker on, switch its source…)."""

    async def stream(self, path: str, offset: float = 0.0, track: dict | None = None) -> None:
        """Play ``path`` from ``offset`` seconds; returns when the track has ended."""
        raise NotImplementedError

    async def set_volume(self, value: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class Player:
    def __init__(self, output: Output, fetch: Callable[[dict], Awaitable[str]], spawn: Callable,
                 on_change: Callable[["Player"], None] | None = None):
        self.output, self.fetch, self.spawn, self.on_change = output, fetch, spawn, on_change
        self.queue: list[dict] = []
        self.index = -1
        self.state = "idle"           # idle | playing | paused
        self.error: str | None = None
        self.volume: int | None = None
        self.shuffle = False
        self._task: asyncio.Task | None = None
        self._gen = 0
        self._started = 0.0           # monotonic time the current track (re)started
        self._offset = 0.0            # seconds into the track at that moment
        self._frozen = 0.0            # position while paused
        self._prepared = False

    # -- state ---------------------------------------------------------------------------------
    @property
    def track(self) -> dict | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def position(self) -> float:
        if self.state == "playing":
            return self._offset + time.monotonic() - self._started
        return self._frozen if self.state == "paused" else 0.0

    def to_json(self) -> dict:
        tr = self.track
        return {"output": self.output.id, "output_name": self.output.name, "state": self.state, "index": self.index,
                "track": tr, "queue": self.queue, "position": round(self.position(), 1),
                "duration": (tr or {}).get("duration") or 0, "volume": self.volume, "error": self.error,
                "shuffle": self.shuffle}

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                log.exception("player listener failed")

    # -- the one stream ------------------------------------------------------------------------
    async def _halt(self) -> None:
        self._gen += 1
        t, self._task = self._task, None
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _begin(self, offset: float = 0.0) -> None:
        await self._halt()
        self.error = None
        self._offset, self._started, self.state = offset, time.monotonic(), "playing"
        self._task = self.spawn(self._run(self._gen), "music player")
        self._changed()

    async def _run(self, gen: int) -> None:
        try:
            while gen == self._gen and self.track is not None:
                track = self.track
                path = await self.fetch(track)
                if gen != self._gen:
                    return
                if not self._prepared:
                    await self.output.prepare()
                    self._prepared = True
                    if gen != self._gen:
                        return
                self._started = time.monotonic()
                self._changed()
                nxt = self.queue[self.index + 1] if self.index + 1 < len(self.queue) else None
                if nxt:
                    self.spawn(self._warm(nxt), "music prefetch")
                await self.output.stream(path, self._offset, track)
                if gen != self._gen:
                    return
                self._offset = 0.0
                self.index += 1
                self._started = time.monotonic()
                self._changed()
            if gen == self._gen:
                self.state, self.index = "idle", -1 if not self.queue else len(self.queue) - 1
                self._prepared = False
                await self.output.close()
                self._changed()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("music playback on %s failed: %s", self.output.name, err)
            if gen == self._gen:
                self.state, self.error, self._prepared = "idle", str(err) or type(err).__name__, False
                with contextlib.suppress(Exception):
                    await self.output.close()
                self._changed()

    async def _warm(self, track: dict) -> None:
        with contextlib.suppress(Exception):
            await self.fetch(track)

    # -- controls ------------------------------------------------------------------------------
    async def play(self, tracks: list[dict], start: int = 0, shuffle: bool = False) -> None:
        tracks = [t for t in tracks if isinstance(t, dict) and t.get("id")]
        if not tracks:
            raise ValueError("nothing to play")
        start = min(max(int(start), 0), len(tracks) - 1)
        self.shuffle = bool(shuffle)
        if shuffle:
            first = tracks[start] if start else None
            rest = tracks[:]
            if first:
                rest.remove(first)
            random.shuffle(rest)
            tracks = ([first] if first else []) + rest
            start = 0
        self.queue, self.index = list(tracks), start
        await self._begin()

    async def enqueue(self, tracks: list[dict], next_: bool = False) -> None:
        tracks = [t for t in tracks if isinstance(t, dict) and t.get("id")]
        if not self.queue or self.state == "idle":
            if tracks:
                await self.play(tracks)
            return
        at = self.index + 1 if next_ else len(self.queue)
        self.queue[at:at] = tracks
        self._changed()

    async def jump(self, index: int) -> None:
        if not 0 <= index < len(self.queue):
            raise ValueError("no such track")
        self.index = index
        await self._begin()

    async def next(self) -> None:
        if self.index + 1 < len(self.queue):
            self.index += 1
            await self._begin()
        else:
            await self.stop()

    async def previous(self) -> None:
        if self.position() > RESTART_AFTER or self.index <= 0:
            await self._begin()
        else:
            self.index -= 1
            await self._begin()

    async def pause(self) -> None:
        if self.state != "playing":
            return
        self._frozen = self.position()
        await self._halt()
        self.state = "paused"
        self._changed()

    async def resume(self) -> None:
        if self.state == "paused":
            await self._begin(self._frozen)
        elif self.state == "idle" and self.queue:
            self.index = max(self.index, 0)
            await self._begin()

    async def toggle(self) -> None:
        await (self.pause() if self.state == "playing" else self.resume())

    async def stop(self) -> None:
        await self._halt()
        self.state, self._prepared = "idle", False
        self.queue, self.index = [], -1
        with contextlib.suppress(Exception):
            await self.output.close()
        self._changed()

    async def set_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        await self.output.set_volume(value)
        self.volume = value
        self._changed()
