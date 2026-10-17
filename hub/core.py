"""Hub core: entities, plugins, event bus and background tasks.

Everything the hub knows about is an ``Entity``. Plugins create entities, keep
their ``state`` up to date with ``hub.update()`` and attach ``actions`` (async
callables) that the web UI and HomeKit invoke.

State conventions per ``kind`` (so the UI and HomeKit can render any plugin):

  sensor        temperature (°C), humidity (%), battery (%)
  light         on, brightness (0-100), color_temp (K)
                actions: turn_on(brightness?, color_temp?), turn_off
  switch        on                       actions: turn_on, turn_off
  climate       current_temperature, target_temperature, mode, heating
                attrs: min, max, step, modes   actions: set_temperature(value), set_mode(mode)
  water_heater  same as climate
  speaker       on, volume (0-100), muted, source, playing
                attrs: sources   actions: turn_on, turn_off, set_volume(value),
                mute(value), set_source(source), play_pause, next, previous
  media_player  on, state (playing/paused/idle/off), title, artist, app,
                artwork (url), position, duration
                actions: turn_on, turn_off, play_pause, next, previous,
                remote(key), launch_app(app)
  tv            on, volume, muted, source
                actions: turn_on, turn_off, remote(key), set_volume(value), set_source(source)
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("hub")

Action = Callable[..., Awaitable[Any]]

# numeric state keys worth keeping a history of (for charts)
HISTORY_KEYS = ("temperature", "humidity", "current_temperature", "target_temperature")
HISTORY_EVERY = 300  # seconds between stored samples of an unchanged value


@dataclass
class Entity:
    id: str
    name: str
    kind: str
    plugin: str
    room: str = ""
    state: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    available: bool = True
    actions: dict[str, Action] = field(default_factory=dict, repr=False)
    updated: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "plugin": self.plugin,
            "room": self.room,
            "state": self.state,
            "attrs": self.attrs,
            "available": self.available,
            "actions": sorted(self.actions),
            "updated": self.updated,
        }


class Plugin:
    """Base class. Subclasses live in ``hub/plugins/<name>.py`` and are enabled
    by a ``[plugins.<name>]`` table in config.toml."""

    name = ""
    title = ""

    def __init__(self, hub: "Hub", config: dict):
        self.hub = hub
        self.config = config
        self.log = logging.getLogger(f"hub.{self.name}")
        self.error: str | None = None

    async def start(self) -> None:
        """Connect, create entities, register HomeKit accessories.

        Must return quickly: run long loops with ``hub.every``/``hub.spawn``.
        HomeKit accessories must be registered here (before HomeKit starts) so
        the Home app keeps them across restarts.
        """

    async def stop(self) -> None:
        pass

    def routes(self) -> list:
        """aiohttp ``web.RouteDef`` list, mounted under ``/api/p/<name>``."""
        return []

    def status(self) -> dict:
        """Extra info for the settings page."""
        return {}


class Hub:
    def __init__(self, config: dict, root: Path):
        from .homekit import HomeKit
        from .notify import Notifier
        from .store import Store

        self.config = config
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.store = Store(self.data_dir / "hub.db")
        self.notify = Notifier(self, config.get("notify", {}))
        self.homekit = HomeKit(self, config.get("homekit", {}), self.data_dir)
        self.entities: dict[str, Entity] = {}
        self.plugins: dict[str, Plugin] = {}
        self.listeners: list[Callable[[Entity], None]] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        self._history_last: dict[tuple[str, str], tuple[float, Any]] = {}
        self.zeroconf = None  # shared AsyncZeroconf, created in start()
        self.http = None  # shared aiohttp.ClientSession, created in start()
        self.started = time.time()

    # -- entities -----------------------------------------------------------

    def add_entity(self, entity: Entity) -> Entity:
        ov = self.store.get(f"entity:{entity.id}", {})
        entity.name = ov.get("name") or entity.name
        entity.room = ov.get("room", entity.room)
        if ov.get("hidden"):
            entity.attrs["hidden"] = True
        self.entities[entity.id] = entity
        self._changed(entity)
        return entity

    def remove_entity(self, entity_id: str) -> None:
        if self.entities.pop(entity_id, None):
            self.publish("entity_removed", {"id": entity_id})

    def update(self, entity_id: str, available: bool | None = None, **state) -> None:
        e = self.entities.get(entity_id)
        if e is None:
            return
        changed = False
        if available is not None and available != e.available:
            e.available, changed = available, True
        for k, v in state.items():
            if e.state.get(k) != v:
                e.state[k] = v
                changed = True
        if changed:
            e.updated = time.time()
            self._changed(e)

    def set_override(self, entity_id: str, **ov) -> None:
        """Rename / move / hide an entity from the UI; persisted."""
        e = self.entities[entity_id]
        cur = self.store.get(f"entity:{entity_id}", {})
        cur.update({k: v for k, v in ov.items() if v is not None})
        self.store.set(f"entity:{entity_id}", cur)
        if "name" in ov and ov["name"]:
            e.name = ov["name"]
        if "room" in ov and ov["room"] is not None:
            e.room = ov["room"]
        if "hidden" in ov:
            if ov["hidden"]:
                e.attrs["hidden"] = True
            else:
                e.attrs.pop("hidden", None)
        self._changed(e)

    async def call(self, entity_id: str, action: str, **params) -> Any:
        e = self.entities.get(entity_id)
        if e is None:
            raise KeyError(f"unknown entity {entity_id}")
        fn = e.actions.get(action)
        if fn is None:
            raise KeyError(f"{entity_id} has no action {action}")
        log.info("%s.%s(%s)", entity_id, action, params or "")
        return await fn(**params)

    def _changed(self, e: Entity) -> None:
        for cb in self.listeners:
            try:
                cb(e)
            except Exception:
                log.exception("entity listener failed")
        self.publish("entity", e.to_json())
        self._record_history(e)

    def _record_history(self, e: Entity) -> None:
        now = time.time()
        for key in HISTORY_KEYS:
            v = e.state.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            last = self._history_last.get((e.id, key))
            if last and last[1] == v and now - last[0] < HISTORY_EVERY:
                continue
            if last and now - last[0] < 60:  # at most one sample a minute
                continue
            self._history_last[(e.id, key)] = (now, v)
            self.store.add_history(e.id, key, v, now)

    # -- event bus (server-sent events) ---------------------------------------

    def publish(self, type_: str, data: Any) -> None:
        msg = {"type": type_, "data": data}
        for q in list(self._subscribers):
            if q.qsize() < 500:
                q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # -- tasks ----------------------------------------------------------------

    def spawn(self, coro: Awaitable, name: str = "") -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("task %s crashed", name or t, exc_info=t.exception())

        task.add_done_callback(done)
        return task

    def every(self, seconds: float, fn: Callable[[], Awaitable], name: str = "",
              initial_delay: float | None = None) -> asyncio.Task:
        """Run ``fn`` forever every ``seconds``; errors are logged, not fatal."""

        async def loop():
            await asyncio.sleep(seconds if initial_delay is None else initial_delay)
            while True:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    log.warning("%s failed: %s", name or fn.__qualname__, err,
                                exc_info=log.isEnabledFor(logging.DEBUG))
                await asyncio.sleep(seconds)

        return self.spawn(loop(), name)

    # -- lifecycle ------------------------------------------------------------

    def _load_plugins(self) -> None:
        for name, cfg in (self.config.get("plugins") or {}).items():
            cfg = cfg if isinstance(cfg, dict) else {}
            if cfg.get("enabled", True) is False:
                continue
            try:
                mod = importlib.import_module(f"hub.plugins.{name}")
            except ImportError as err:
                log.error("plugin %s can't be loaded: %s", name, err)
                continue
            cls = getattr(mod, "PLUGIN", None)
            if cls is None:
                log.error("plugin %s has no PLUGIN class", name)
                continue
            self.plugins[name] = cls(self, cfg)

    async def start(self) -> None:
        import aiohttp
        from zeroconf.asyncio import AsyncZeroconf

        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Casa/1.0"},
        )
        self.zeroconf = AsyncZeroconf()
        self._load_plugins()

        async def start_one(p: Plugin):
            try:
                await asyncio.wait_for(p.start(), 60)
                log.info("plugin %s started", p.name)
            except Exception as err:
                p.error = str(err) or type(err).__name__
                log.error("plugin %s failed to start: %s", p.name, p.error,
                          exc_info=not isinstance(err, (asyncio.TimeoutError, OSError)))

        await asyncio.gather(*(start_one(p) for p in self.plugins.values()))
        await self.homekit.start()

    async def stop(self) -> None:
        await self.homekit.stop()
        for p in self.plugins.values():
            try:
                await asyncio.wait_for(p.stop(), 10)
            except Exception:
                log.exception("plugin %s stop failed", p.name)
        for t in list(self._tasks):
            t.cancel()
        if self.zeroconf:
            await self.zeroconf.async_close()
        if self.http:
            await self.http.close()
        self.store.close()
