"""Client for the Casa agent running on the Steam Deck (deploy/deck/casa_deck_agent.py)."""

from __future__ import annotations

import asyncio

import aiohttp

from ...i18n import _


class DeckError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Deck:
    def __init__(self, host: str, port: int, token: str, session: aiohttp.ClientSession):
        self.host, self.port, self.token, self.session = host, port, token, session

    @property
    def configured(self) -> bool:
        return bool(self.host and self.token)

    async def call(self, method: str, path: str, body: dict | None = None, timeout: float = 5):
        if not self.configured:
            raise DeckError(_("Set the Steam Deck address and token in Settings → Games."))
        url = f"http://{self.host}:{self.port}{path}"
        try:
            async with self.session.request(method, url, json=body, headers={"Authorization": "Bearer " + self.token},
                                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    raise DeckError(_("The Deck agent sent an invalid answer"))
                if r.status == 401:
                    raise DeckError(_("Wrong token: check it matches the one on the Deck"), 401)
                if r.status >= 400:
                    raise DeckError(str(data.get("error") if isinstance(data, dict) else r.status), r.status)
                return data
        except asyncio.TimeoutError:
            raise DeckError(_("The Steam Deck did not answer (asleep or off Wi-Fi?)"))
        except aiohttp.ClientError as err:
            raise DeckError(_("Cannot reach the Steam Deck: {err}", err=str(err) or type(err).__name__))
