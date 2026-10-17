"""Hub core: entities, plugins, event bus and background tasks.

Everything the hub knows about is an ``Entity``. Plugins create entities, keep
their ``state`` up to date with ``hub.update()`` and attach ``actions`` (async
callables) that the web UI and HomeKit invoke.

State conventions per ``kind`` (so the UI and HomeKit can render any plugin):

  sensor        temperature (°C), humidity (%), battery (%)
  light         on, brightness (0-100), color_temp (K), hue (0-360), saturation (0-100)
                attrs: color_temp_range (warm K, cool K), color (True if hue works)
                actions: turn_on(brightness?, color_temp?, hue?, saturation?), turn_off
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

History: every numeric state key of a ``sensor`` (and the climate keys below
for other kinds) is stored in SQLite rollups; ``attrs["units"]`` maps a key to
its unit for charts, ``attrs["history"]`` overrides which keys are recorded.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import logging
import pkgutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("hub")

Action = Callable[..., Awaitable[Any]]

# numeric state keys worth keeping a history of for non-sensor kinds
HISTORY_KEYS = ("temperature", "humidity", "current_temperature", "target_temperature")
HISTORY_EVERY = 300  # seconds between stored samples of an unchanged value
HISTORY_MIN = 30  # never more than one sample per series in this many seconds

# name of the plugin whose code is running: tasks and HomeKit accessories
# created while it is set belong to that plugin (asyncio tasks inherit it)
current_plugin: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_plugin", default=None)


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
    description = ""
    icon = "plug"
    schema: list[dict] = []  # settings form, see CLAUDE.md
    ui = False  # True: web/plugins/<name>.js adds a custom panel to its settings sheet

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

    def attention(self) -> list[dict]:
        """Things the user should fix or set up, shown in the attention list.

        ``{"id", "level": "error"|"warning"|"info", "title", "detail",
        "action": {"type": "settings"|"setup"|"panel"|"page"|"url", ...}}``.
        Errors and "needs pairing" are reported by the hub itself; return only
        plugin-specific hints (a missing optional key, an unreachable service).
        """
        return []


class Hub:
    def __init__(self, cfg, root: Path):
        from .homekit import HomeKit
        from .notify import Notifier
        from .settings import Config
        from .store import Store

        if not isinstance(cfg, Config):  # tests may pass a plain dict
            c = Config.__new__(Config)
            c.path, c.data = root / "config.test.toml", cfg
            c.save = lambda: None
            cfg = c
        self.cfg = cfg
        self.config = cfg.data
        config = self.config
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.store = Store(self.data_dir / "hub.db")
        self._apply_history_config()
        self.notify = Notifier(self, config.get("notify", {}))
        self.homekit = HomeKit(self, config.get("homekit", {}), self.data_dir)
        self.entities: dict[str, Entity] = {}
        self.plugins: dict[str, Plugin] = {}
        self.listeners: list[Callable[[Entity], None]] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        self._plugin_tasks: dict[str, set[asyncio.Task]] = {}
        self.restart_requested = False
        self.shutdown = asyncio.Event()  # set to stop (or restart) the process
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
        keys = e.attrs.get("history") or (e.state if e.kind == "sensor" else HISTORY_KEYS)
        for key in keys:
            v = e.state.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            last = self._history_last.get((e.id, key))
            if last and last[1] == v and now - last[0] < HISTORY_EVERY:
                continue
            if last and now - last[0] < HISTORY_MIN:
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
        owner = current_plugin.get()
        if owner:
            self._plugin_tasks.setdefault(owner, set()).add(task)

        def done(t: asyncio.Task):
            self._tasks.discard(t)
            if owner:
                self._plugin_tasks.get(owner, set()).discard(t)
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

    # -- plugins ------------------------------------------------------------------

    @staticmethod
    def plugin_class(name: str) -> type[Plugin]:
        mod = importlib.import_module(f"hub.plugins.{name}")
        cls = getattr(mod, "PLUGIN", None)
        if cls is None:
            raise ImportError(f"plugin {name} has no PLUGIN class")
        return cls

    def catalog(self) -> list[dict]:
        """Every plugin that ships with the hub, enabled or not."""
        from . import plugins

        names = {m.name for m in pkgutil.iter_modules(plugins.__path__)}
        names |= set(self.config.get("plugins") or {}) | set(self.plugins)
        out = []
        for name in names:
            info = {"name": name, "title": name.title(), "description": "", "icon": "plug",
                    "schema": [], "enabled": self.cfg.plugin_enabled(name)}
            try:
                cls = self.plugin_class(name)
                info.update(title=cls.title or name.title(), description=cls.description,
                            icon=cls.icon, schema=cls.schema, ui=cls.ui)
            except Exception as err:  # missing optional dependency etc.
                info["error"] = f"can't be loaded: {err}"
            out.append(info)
        return sorted(out, key=lambda i: i["title"].lower())

    async def _start_plugin(self, p: Plugin) -> None:
        current_plugin.set(p.name)  # runs in its own task: doesn't leak to the caller
        try:
            await asyncio.wait_for(p.start(), 60)
            log.info("plugin %s started", p.name)
        except Exception as err:
            p.error = str(err) or type(err).__name__
            log.error("plugin %s failed to start: %s", p.name, p.error,
                      exc_info=not isinstance(err, (asyncio.TimeoutError, OSError)))

    async def start_plugin(self, name: str) -> Plugin | None:
        """(Re)start one plugin with its current config."""
        await self.stop_plugin(name)
        if not self.cfg.plugin_enabled(name):
            return None
        try:
            p = self.plugin_class(name)(self, self.cfg.plugin(name))
        except Exception as err:
            log.error("plugin %s can't be loaded: %s", name, err)
            return None
        self.plugins[name] = p
        await asyncio.ensure_future(self._start_plugin(p))
        self.publish("plugins", None)
        return p

    async def stop_plugin(self, name: str, forget_homekit: bool = False) -> None:
        """Stop a plugin, cancel its tasks and drop its entities. HomeKit
        accessories stay (they follow entities by id) unless ``forget_homekit``."""
        p = self.plugins.pop(name, None)
        if p is None:
            return
        try:
            await asyncio.wait_for(p.stop(), 10)
        except Exception:
            log.exception("plugin %s stop failed", name)
        for t in self._plugin_tasks.pop(name, set()):
            t.cancel()
        for eid in [e.id for e in self.entities.values() if e.plugin == name]:
            self.remove_entity(eid)
        if forget_homekit:
            self.homekit.remove_owned(name)
        log.info("plugin %s stopped", name)
        self.publish("plugins", None)

    def _apply_history_config(self) -> None:
        from .store import ROLLUPS

        h = self.config.get("history", {})
        for (table, _, default), key in zip(ROLLUPS, ("detail_days", "medium_days", "long_days")):
            self.store.retention[table] = float(h.get(key, default))

    async def apply_core(self, section: str) -> None:
        """Apply a core settings section that doesn't need a restart."""
        from .notify import Notifier

        if section == "notify":
            self.notify = Notifier(self, self.config.get("notify", {}))
        elif section == "history":
            self._apply_history_config()

    def attention(self, include_dismissed: bool = False) -> list[dict]:
        """Everything that needs the user's attention, most important first."""
        items: list[dict] = []
        for name, p in self.plugins.items():
            title = p.title or name.title()
            settings = {"type": "settings", "plugin": name}
            if p.error:
                items.append({"id": f"{name}.error", "level": "error",
                              "title": f"{title} isn't working", "detail": p.error,
                              "action": settings})
                continue
            try:
                st = p.status() or {}
            except Exception:
                st = {}
            if st.get("setup") and not st.get("configured"):
                items.append({"id": f"{name}.setup", "level": "warning",
                              "title": f"Pair {title}", "detail": st.get("setup_hint", ""),
                              "action": {"type": "setup", "plugin": name}})
            try:
                for it in p.attention() or []:
                    it = dict(it)
                    it["id"] = f"{name}.{it.get('id', 'hint')}"
                    it.setdefault("level", "info")
                    it.setdefault("action", settings)
                    items.append(it)
            except Exception:
                log.exception("plugin %s attention() failed", name)
        hk = self.homekit.info()
        if hk.get("running") and not hk.get("paired"):
            items.append({"id": "homekit.pair", "level": "info", "title": "Add Casa to Apple Home",
                          "detail": "Scan the setup code with the Home app.",
                          "action": {"type": "homekit"}})
        if self.notify.channels == ["app"]:
            items.append({"id": "notify.phone", "level": "info",
                          "title": "Get notifications on your phone",
                          "detail": "Free with the ntfy app: downloads, favourites found, alerts.",
                          "action": {"type": "core", "section": "notify"}})
        dismissed = set(self.store.get("attention.dismissed", []))
        for it in items:
            it["dismissed"] = it["id"] in dismissed
        if not include_dismissed:
            # errors can't be dismissed: they come back until fixed
            items = [i for i in items if not i["dismissed"] or i["level"] == "error"]
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(items, key=lambda i: order.get(i["level"], 3))

    def dismiss_attention(self, item_id: str, dismissed: bool = True) -> None:
        cur = set(self.store.get("attention.dismissed", []))
        (cur.add if dismissed else cur.discard)(item_id)
        self.store.set("attention.dismissed", sorted(cur))
        self.publish("attention", None)

    @property
    def onboarded(self) -> bool:
        """False until the first-run guided setup is finished. Installs that
        already had plugins configured before it existed count as done."""
        done = self.store.get("onboarding.done")
        if done is None:
            done = bool(self.config.get("plugins"))
            self.store.set("onboarding.done", done)
        return done

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        import aiohttp
        from zeroconf import InterfaceChoice
        from zeroconf.asyncio import AsyncZeroconf

        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Casa/1.0"},
        )
        # default-route interfaces only: VPN / docker interfaces just produce errors
        self.zeroconf = AsyncZeroconf(interfaces=InterfaceChoice.Default)
        for name in (self.config.get("plugins") or {}):
            if not self.cfg.plugin_enabled(name):
                continue
            try:
                self.plugins[name] = self.plugin_class(name)(self, self.cfg.plugin(name))
            except Exception as err:
                log.error("plugin %s can't be loaded: %s", name, err)
        await asyncio.gather(*(self._start_plugin(p) for p in self.plugins.values()))
        await self.homekit.start()
        self._watch_loop()

    def _watch_loop(self, limit: float = 1.0) -> None:
        """Log where the event loop is stuck when it stops answering for ``limit``
        seconds (blocking calls make the whole hub feel frozen)."""
        import sys
        import threading
        import traceback

        loop, tid = asyncio.get_running_loop(), threading.get_ident()

        def run():
            while not self.shutdown.is_set():
                answered = threading.Event()
                loop.call_soon_threadsafe(answered.set)
                if not answered.wait(limit):
                    frame = sys._current_frames().get(tid)
                    stack = "".join(traceback.format_stack(frame)[-8:]) if frame else "?"
                    log.warning("event loop blocked for more than %.0f s at:\n%s", limit, stack)
                    answered.wait(60)
                time.sleep(2)

        threading.Thread(target=run, name="loop-watch", daemon=True).start()

    async def stop(self) -> None:
        await self.homekit.stop()
        for name in list(self.plugins):
            await self.stop_plugin(name)
        for t in list(self._tasks):
            t.cancel()
        if self.zeroconf:
            await self.zeroconf.async_close()
        if self.http:
            await self.http.close()
        self.store.close()
