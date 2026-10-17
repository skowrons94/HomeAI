"""Notifications: always stored and shown in the web UI, pushed to the phones that turned
them on in Casa (web push, hub/push.py), optionally also through ntfy (https://ntfy.sh, free app, no account) or Telegram.

config.toml:
    [notify]
    ntfy_url = "https://ntfy.sh/casa-<something-random>"
    telegram_token = "123:ABC"      # optional
    telegram_chat_id = "123456"
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("hub.notify")
TIMEOUT = aiohttp.ClientTimeout(total=15)


class Notifier:
    def __init__(self, hub, cfg: dict):
        self.hub = hub
        self.ntfy_url = cfg.get("ntfy_url", "")
        self.ntfy_token = cfg.get("ntfy_token", "")
        self.tg_token = cfg.get("telegram_token", "")
        self.tg_chat = str(cfg.get("telegram_chat_id", ""))

    @property
    def channels(self) -> list[str]:
        out = ["app"]
        if self.hub.push.subscriptions():
            out.append("phone")
        if self.ntfy_url:
            out.append("ntfy")
        if self.tg_token and self.tg_chat:
            out.append("telegram")
        return out

    async def send(self, title: str, body: str = "", url: str = "", image: str = "",
                   tag: str = "", push: bool = True) -> None:
        note = self.hub.store.add_notification(title, body, url, image, tag)
        self.hub.publish("notification", note)
        log.info("notify: %s - %s", title, body)
        if not push:
            return
        try:
            await self.hub.push.send(title, body, url, tag)
        except Exception as err:
            log.warning("web push failed: %s", err)
        if self.ntfy_url:
            await self._ntfy(title, body, url, image, tag)
        if self.tg_token and self.tg_chat:
            await self._telegram(title, body, url)

    async def _ntfy(self, title, body, url, image, tag):
        # JSON publishing keeps accents/emoji intact (headers would be latin-1)
        server, _, topic = self.ntfy_url.rstrip("/").rpartition("/")
        payload = {"topic": topic, "title": title, "message": body or title,
                   "tags": [tag or "house"]}
        if url:
            payload["click"] = url
        if image:
            payload["attach"] = image
        headers = {"Authorization": f"Bearer {self.ntfy_token}"} if self.ntfy_token else {}
        try:
            async with self.hub.http.post(server, json=payload, headers=headers,
                                          timeout=TIMEOUT) as r:
                if r.status >= 300:
                    log.warning("ntfy answered %s", r.status)
        except Exception as err:
            log.warning("ntfy failed: %s", err)

    async def _telegram(self, title, body, url):
        text = f"*{title}*\n{body}" + (f"\n{url}" if url else "")
        try:
            async with self.hub.http.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": text, "parse_mode": "Markdown"},
                timeout=TIMEOUT,
            ) as r:
                if r.status >= 300:
                    log.warning("telegram answered %s", r.status)
        except Exception as err:
            log.warning("telegram failed: %s", err)
