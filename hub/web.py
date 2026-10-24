"""HTTP API + static web UI (aiohttp).

  GET   /api/state                         everything the UI needs at load
  POST  /api/entities/{id}/{action}        run an action, JSON body = params
  PATCH /api/entities/{id}                 {name?, room?, hidden?}
  GET   /api/history                       recorded series
  GET   /api/history/{id}?key=&hours=      [[ts, avg, min, max], ...]
  GET   /api/settings                      core sections + plugin catalog with schemas & values
  PUT   /api/settings/core/{section}       {values}
  PUT   /api/settings/plugins/{name}       {enabled?, values?}  -> plugin restarts live
  POST  /api/restart                       restart the whole hub
  GET   /api/fs?path=                      folder browser for "path" settings
  POST  /api/onboarding                    {done} - first-run guided setup finished / rerun
  GET   /api/attention[?all=1]             things to fix / set up, most important first
  POST  /api/attention/{id}                {dismissed} - hide (or restore) a hint
  GET   /api/events                        server-sent events (entity, notification, ...)
  GET   /api/notifications   POST /api/notifications/read   POST /api/notifications/test
  POST  /api/notifications/clear   DELETE /api/notifications/{id}
  GET   /api/homekit         GET /api/homekit/qr.svg
  *     /api/p/<plugin>/...                plugin routes
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

from . import i18n, settings

log = logging.getLogger("hub.web")
COOKIE = "casa_session"


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


# Plugin routes that carry their own credential (the Steam Deck agent has no login cookie: the
# games plugin checks a signed, expiring link on every request to /dl/).
PUBLIC_PREFIXES = ("/api/p/games/dl/", "/api/p/music/speaker/")   # signed links for the Deck / DLNA speakers


def remote_error(err: Exception) -> str:
    """A readable sentence for a failed request to an outside service."""
    _ = i18n._
    host = getattr(getattr(err, "request_info", None), "url", None)
    host = getattr(host, "host", None) or getattr(err, "host", None) or ""
    if isinstance(err, aiohttp.ClientResponseError):
        if err.status == 404:
            return _("Not found on {host}", host=host) if host else _("Not found")
        if err.status == 429:
            return _("{host} is busy: try again in a minute", host=host or "?")
        return _("{host} answered with an error ({code})", host=host or "?", code=err.status)
    if isinstance(err, asyncio.TimeoutError):
        return _("The service did not answer in time")
    return _("{host} is not reachable", host=host) if host else _("The service is not reachable")


class WebServer:
    def __init__(self, hub, cfg: dict, static_dir: Path):
        self.hub = hub
        self.host = cfg.get("host", "0.0.0.0")
        self.port = int(cfg.get("port", 8080))
        self.password = cfg.get("password", "")
        secret = hub.store.get("web_secret")
        if not secret:
            secret = secrets.token_hex(16)
            hub.store.set("web_secret", secret)
        self.token = hmac.new(secret.encode(), self.password.encode(), hashlib.sha256).hexdigest()
        self.static_dir = static_dir
        self.runner: web.AppRunner | None = None
        self._routers: dict[int, web.UrlDispatcher] = {}  # id(plugin) -> its routes

    # -- app ---------------------------------------------------------------------

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._errors, self._auth])
        r = app.router
        r.add_get("/", self.index)
        r.add_post("/api/login", self.login)
        r.add_get("/api/state", self.state)
        r.add_get("/api/i18n", self.i18n)
        r.add_get("/sw.js", self.service_worker)
        r.add_get("/api/push/key", self.push_key)
        r.add_post("/api/push/subscribe", self.push_subscribe)
        r.add_post("/api/push/unsubscribe", self.push_unsubscribe)
        r.add_get("/api/prefs", self.prefs)
        r.add_put("/api/prefs", self.prefs_save)
        r.add_post("/api/entities/{id}/{action}", self.action)
        r.add_patch("/api/entities/{id}", self.patch_entity)
        r.add_get("/api/history", self.history_series)
        r.add_get("/api/history/{id}", self.history)
        r.add_get("/api/settings", self.settings)
        r.add_put("/api/settings/core/{section}", self.settings_core)
        r.add_put("/api/settings/plugins/{name}", self.settings_plugin)
        r.add_post("/api/restart", self.restart)
        r.add_get("/api/fs", self.fs)
        r.add_post("/api/onboarding", self.onboarding)
        r.add_get("/api/attention", self.attention)
        r.add_post("/api/attention/{id}", self.attention_dismiss)
        r.add_get("/api/events", self.events)
        r.add_get("/api/notifications", self.notifications)
        r.add_post("/api/notifications/read", self.notifications_read)
        r.add_post("/api/notifications/test", self.notifications_test)
        r.add_post("/api/notifications/clear", self.notifications_clear)
        r.add_delete("/api/notifications/{id}", self.notification_delete)
        r.add_get("/api/homekit", self.homekit)
        r.add_get("/api/homekit/qr.svg", self.homekit_qr)
        # plugins come and go at runtime, so their routes are resolved per request
        r.add_route("*", "/api/p/{plugin}/{tail:.*}", self.plugin_route)
        r.add_get("/static/{path:.+}", self.static)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.build(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        log.info("web UI on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # -- plugin routes -------------------------------------------------------------

    def _router(self, plugin) -> web.UrlDispatcher:
        router = self._routers.get(id(plugin))
        if router is None:
            router = web.UrlDispatcher()
            for rd in plugin.routes():
                kw = dict(rd.kwargs)
                if rd.method == "GET":  # web.get() adds allow_head, only add_get takes it
                    router.add_get(rd.path, rd.handler, **kw)
                else:
                    kw.pop("allow_head", None)
                    router.add_route(rd.method, rd.path, rd.handler, **kw)
            self._routers = {k: v for k, v in self._routers.items()
                             if k in {id(p) for p in self.hub.plugins.values()}}
            self._routers[id(plugin)] = router
        return router

    async def plugin_route(self, request):
        plugin = self.hub.plugins.get(request.match_info["plugin"])
        if plugin is None:
            raise KeyError(i18n._("The {name} integration is off: turn it on in Settings",
                                  name=request.match_info["plugin"]))
        tail = "/" + request.match_info["tail"]
        sub = request.clone(rel_url=URL.build(path=tail, query_string=request.query_string,
                                               encoded=False))
        match = await self._router(plugin).resolve(sub)
        if match.http_exception is not None:
            raise match.http_exception
        match.add_app(request.app)
        match.freeze()
        sub._match_info = match  # what aiohttp itself does after resolving
        return await match.handler(sub)

    # -- middlewares -------------------------------------------------------------

    @web.middleware
    async def _errors(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except KeyError as err:
            return _json({"error": str(err).strip("'\"")}, 404)
        except (ValueError, TypeError) as err:
            return _json({"error": str(err)}, 400)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            # an outside service failed: say so in words, not "404, message='Not Found', url=..."
            log.warning("%s %s: %s", request.method, request.path, err)
            return _json({"error": remote_error(err)}, 502)
        except Exception as err:
            log.exception("%s %s failed", request.method, request.path)
            return _json({"error": str(err) or type(err).__name__}, 500)

    @web.middleware
    async def _auth(self, request, handler):
        if (self.password and request.path.startswith("/api/")
                and request.path not in ("/api/login", "/api/i18n")
                and not request.path.startswith(PUBLIC_PREFIXES)
                and not hmac.compare_digest(request.cookies.get(COOKIE, ""), self.token)):
            return _json({"error": "login required"}, 401)
        return await handler(request)

    # -- handlers ----------------------------------------------------------------

    async def index(self, request):
        html = (self.static_dir / "index.html").read_text()
        # cache-bust the static files on every start
        html = html.replace("{{v}}", str(int(self.hub.started)))
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def static(self, request):
        path = (self.static_dir / request.match_info["path"]).resolve()
        if not path.is_relative_to(self.static_dir.resolve()) or not path.is_file():
            raise web.HTTPNotFound()
        # small files, revalidated each load (ETag/Last-Modified -> 304)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def login(self, request):
        body = await request.json()
        if not self.password or hmac.compare_digest(str(body.get("password", "")), self.password):
            resp = _json({"ok": True})
            self._remember(request, resp)
            return resp
        await asyncio.sleep(1)
        return _json({"error": "wrong password"}, 403)

    def _remember(self, request, resp) -> None:
        """Stay logged in: a year, renewed on every app start (GET /api/state)."""
        if self.password:
            secure = request.secure or request.headers.get("X-Forwarded-Proto") == "https"
            resp.set_cookie(COOKIE, self.token, max_age=365 * 86400, httponly=True,
                            samesite="Lax", secure=secure)

    async def service_worker(self, request):
        # served from / so it may control the whole app (scope "/")
        return web.FileResponse(self.static_dir / "sw.js", headers={
            "Cache-Control": "no-cache", "Content-Type": "text/javascript"})

    async def push_key(self, request):
        return _json({"key": self.hub.push.public_key()})

    async def push_subscribe(self, request):
        body = await request.json()
        self.hub.push.subscribe(body.get("subscription"), str(body.get("person", ""))[:60],
                                request.headers.get("User-Agent", ""))
        return _json({"ok": True})

    async def push_unsubscribe(self, request):
        body = await request.json()
        self.hub.push.unsubscribe(str(body.get("endpoint", "")))
        return _json({"ok": True})

    async def state(self, request):
        resp = await self._state()
        self._remember(request, resp)
        return resp

    async def _state(self):
        hub = self.hub
        return _json({
            "entities": [e.to_json() for e in hub.entities.values()],
            "plugins": [
                {"name": p.name, "title": p.title or p.name.title(), "error": p.error,
                 "icon": p.icon, "status": _safe(p.status)}
                for p in hub.plugins.values()
            ],
            "homekit": hub.homekit.info(),
            "notify_channels": hub.notify.channels,
            "unread": hub.store.query(
                "SELECT COUNT(*) AS n FROM notifications WHERE read=0")[0]["n"],
            "rooms": hub.config.get("rooms", []),
            "language": i18n.language(),
            "people": [str(p) for p in hub.config.get("people", []) if str(p).strip()],
            "name": hub.config.get("name", "Casa"),
            "onboarding": not hub.onboarded,
            "attention": sum(1 for i in hub.attention() if i["level"] != "info"),
        })

    async def i18n(self, request):
        """Translations for the web app (no login needed: the login screen uses them too)."""
        return _json({"language": i18n.language(), "texts": i18n.texts()})

    def _prefs_key(self, request) -> str:
        person = request.query.get("person", "")
        people = [str(p) for p in self.hub.config.get("people", [])]
        return f"ui.prefs.{person if person in people else '_'}"

    async def prefs(self, request):
        """Per-person layout: tab bar order, Home sections order/hidden ({} = defaults)."""
        return _json(self.hub.store.get(self._prefs_key(request)) or {})

    async def prefs_save(self, request):
        body = await request.json()
        if not isinstance(body, dict) or len(json.dumps(body)) > 20_000:
            raise ValueError("invalid preferences")
        self.hub.store.set(self._prefs_key(request), body)
        return _json(body)

    async def action(self, request):
        params = await request.json() if request.can_read_body else {}
        result = await self.hub.call(request.match_info["id"], request.match_info["action"],
                                     **params)
        return _json({"ok": True, "result": result})

    async def patch_entity(self, request):
        body = await request.json()
        self.hub.set_override(request.match_info["id"], name=body.get("name"),
                              room=body.get("room"), hidden=body.get("hidden"))
        return _json({"ok": True})

    async def history_series(self, request):
        ents = self.hub.entities
        return _json([
            {**row, "name": ents[row["entity"]].name if row["entity"] in ents else row["entity"],
             "room": ents[row["entity"]].room if row["entity"] in ents else "",
             "unit": (ents[row["entity"]].attrs.get("units") or {}).get(row["key"], "")
             if row["entity"] in ents else ""}
            for row in self.hub.store.series()
        ])

    async def history(self, request):
        hours = float(request.query.get("hours", 24))
        rows = self.hub.store.history(request.match_info["id"],
                                      request.query.get("key", "temperature"),
                                      time.time() - hours * 3600)
        return _json(rows)

    async def events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = self.hub.subscribe()
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 25)
                    data = json.dumps(msg, default=str)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(q)
        return resp

    async def notifications(self, request):
        return _json(self.hub.store.notifications(int(request.query.get("limit", 50))))

    async def notifications_read(self, request):
        self.hub.store.mark_notifications_read()
        return _json({"ok": True})

    async def notifications_clear(self, request):
        self.hub.store.clear_notifications()
        return _json({"ok": True})

    async def notification_delete(self, request):
        self.hub.store.delete_notification(int(request.match_info["id"]))
        return _json({"ok": True})

    async def notifications_test(self, request):
        await self.hub.notify.send("Casa", i18n._("Test notification - it works 🎉"), tag="bell")
        return _json({"ok": True, "channels": self.hub.notify.channels})

    # -- settings ---------------------------------------------------------------------

    async def settings(self, request):
        cfg = self.hub.cfg
        plugins = []
        for info in self.hub.catalog():
            running = self.hub.plugins.get(info["name"])
            plugins.append({
                **info,
                "configured": cfg.plugin(info["name"]) is not None,
                "running": running is not None,
                "error": info.get("error") or (running.error if running else None),
                "status": _safe(running.status) if running else {},
                "values": settings.values_for(info["schema"], cfg.plugin(info["name"]) or {}),
            })
        return _json({
            "core": [{"name": k, "title": v["title"], "restart": v["restart"],
                      "schema": v["fields"], "values": cfg.core_values(k)}
                     for k, v in settings.CORE.items()],
            "plugins": plugins,
            "config_file": str(cfg.path),
        })

    async def settings_core(self, request):
        section = request.match_info["section"]
        if section not in settings.CORE:
            raise KeyError(f"unknown section {section}")
        body = await request.json()
        self.hub.cfg.set_core(section, body.get("values") or {})
        await self.hub.apply_core(section)
        return _json({"ok": True, "restart": settings.CORE[section]["restart"]})

    async def settings_plugin(self, request):
        name = request.match_info["name"]
        cls = self.hub.plugin_class(name)  # validates the name
        body = await request.json()
        enabled = body.get("enabled")
        was_running = name in self.hub.plugins
        self.hub.cfg.set_plugin(name, cls.schema, body.get("values"), enabled)
        if enabled is False:
            await self.hub.stop_plugin(name, forget_homekit=True)
        elif enabled or body.get("values") or was_running:
            await self.hub.start_plugin(name)
        p = self.hub.plugins.get(name)
        return _json({"ok": True, "running": p is not None, "error": p.error if p else None})

    async def fs(self, request):
        """Folder browser for "path" settings: ?path= -> {path, parent, dirs, roots}."""
        import os
        import shutil

        path = Path(request.query.get("path") or Path.home()).expanduser().resolve()
        if not path.is_dir():
            raise KeyError(f"{path} is not a folder")
        try:
            dirs = sorted((e.name for e in os.scandir(path)
                           if e.is_dir() and not e.name.startswith(".")), key=str.lower)
        except PermissionError:
            dirs = []
        usage = shutil.disk_usage(path)
        roots = [str(p) for p in (Path.home(), Path("/mnt"), Path("/media"), Path("/Volumes"),
                                  self.hub.root / "data") if p.is_dir()]
        return _json({"path": str(path), "parent": str(path.parent) if path.parent != path else None,
                      "dirs": dirs, "roots": roots, "writable": os.access(path, os.W_OK),
                      "free_gb": round(usage.free / 1e9, 1)})

    async def attention(self, request):
        return _json(self.hub.attention(include_dismissed=bool(request.query.get("all"))))

    async def attention_dismiss(self, request):
        body = await request.json() if request.can_read_body else {}
        self.hub.dismiss_attention(request.match_info["id"], bool(body.get("dismissed", True)))
        return _json({"ok": True})

    async def onboarding(self, request):
        body = await request.json()
        self.hub.store.set("onboarding.done", bool(body.get("done", True)))
        return _json({"ok": True})

    async def restart(self, request):
        self.hub.restart_requested = True
        asyncio.get_running_loop().call_later(0.3, self.hub.shutdown.set)
        return _json({"ok": True})

    async def homekit(self, request):
        return _json(self.hub.homekit.info())

    async def homekit_qr(self, request):
        return web.Response(text=self.hub.homekit.qr_svg(), content_type="image/svg+xml")


def _safe(fn):
    try:
        return fn()
    except Exception as err:
        return {"error": str(err)}
