"""HTTP API + static web UI (aiohttp).

  GET   /api/state                         everything the UI needs at load
  POST  /api/entities/{id}/{action}        run an action, JSON body = params
  PATCH /api/entities/{id}                 {name?, room?, hidden?}
  GET   /api/history/{id}?key=&hours=      sensor history
  GET   /api/events                        server-sent events (entity, notification, ...)
  GET   /api/notifications   POST /api/notifications/read   POST /api/notifications/test
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

from aiohttp import web

log = logging.getLogger("hub.web")
COOKIE = "casa_session"


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


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

    # -- app ---------------------------------------------------------------------

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._errors, self._auth])
        r = app.router
        r.add_get("/", self.index)
        r.add_post("/api/login", self.login)
        r.add_get("/api/state", self.state)
        r.add_post("/api/entities/{id}/{action}", self.action)
        r.add_patch("/api/entities/{id}", self.patch_entity)
        r.add_get("/api/history/{id}", self.history)
        r.add_get("/api/events", self.events)
        r.add_get("/api/notifications", self.notifications)
        r.add_post("/api/notifications/read", self.notifications_read)
        r.add_post("/api/notifications/test", self.notifications_test)
        r.add_get("/api/homekit", self.homekit)
        r.add_get("/api/homekit/qr.svg", self.homekit_qr)
        for name, plugin in self.hub.plugins.items():
            for rd in plugin.routes():
                path, kw = f"/api/p/{name}{rd.path}", dict(rd.kwargs)
                if rd.method == "GET":  # web.get() adds allow_head, only add_get takes it
                    r.add_get(path, rd.handler, **kw)
                else:
                    kw.pop("allow_head", None)
                    r.add_route(rd.method, path, rd.handler, **kw)
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
        except Exception as err:
            log.exception("%s %s failed", request.method, request.path)
            return _json({"error": str(err) or type(err).__name__}, 500)

    @web.middleware
    async def _auth(self, request, handler):
        if (self.password and request.path.startswith("/api/")
                and request.path != "/api/login"
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
            resp.set_cookie(COOKIE, self.token, max_age=365 * 86400, httponly=True,
                            samesite="Lax")
            return resp
        await asyncio.sleep(1)
        return _json({"error": "wrong password"}, 403)

    async def state(self, request):
        hub = self.hub
        return _json({
            "entities": [e.to_json() for e in hub.entities.values()],
            "plugins": [
                {"name": p.name, "title": p.title or p.name.title(), "error": p.error,
                 "status": _safe(p.status)}
                for p in hub.plugins.values()
            ],
            "homekit": hub.homekit.info(),
            "notify_channels": hub.notify.channels,
            "unread": hub.store.query(
                "SELECT COUNT(*) AS n FROM notifications WHERE read=0")[0]["n"],
            "rooms": hub.config.get("rooms", []),
            "name": hub.config.get("name", "Casa"),
        })

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

    async def notifications_test(self, request):
        await self.hub.notify.send("Casa", "Test notification - it works 🎉", tag="bell")
        return _json({"ok": True, "channels": self.hub.notify.channels})

    async def homekit(self, request):
        return _json(self.hub.homekit.info())

    async def homekit_qr(self, request):
        return web.Response(text=self.hub.homekit.qr_svg(), content_type="image/svg+xml")


def _safe(fn):
    try:
        return fn()
    except Exception as err:
        return {"error": str(err)}
