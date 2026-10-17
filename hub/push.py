"""Web push: notifications straight to the phones, no extra app.

iPhone (iOS 16.4+) delivers them to Casa added to the Home Screen and opened over HTTPS
(Tailscale ``https://casa.<tailnet>.ts.net``). The browser subscribes with our VAPID public key
(``web/sw.js`` shows the notification); subscriptions live in the store, one per phone, and are
dropped when the push service says they're gone (404/410).

    GET  /api/push/key          -> {"key": "<VAPID public key, base64url>"}
    POST /api/push/subscribe    {subscription, person}
    POST /api/push/unsubscribe  {endpoint}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

log = logging.getLogger("hub.push")

SUBS = "push.subscriptions"
VAPID = "push.vapid"
CONTACT = "https://github.com/skowrons94/HomeAI"  # push services want a contact (Apple rejects localhost)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Push:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store

    # -- keys -------------------------------------------------------------------------

    def _vapid(self) -> dict:
        """Generated once and kept in the store: changing it would break every subscription."""
        v = self.store.get(VAPID)
        if v:
            return v
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
        v = {"private_pem": pem, "public": _b64(pub)}
        self.store.set(VAPID, v)
        return v

    def public_key(self) -> str:
        return self._vapid()["public"]

    # -- subscriptions ------------------------------------------------------------------

    def subscriptions(self) -> list[dict]:
        return self.store.get(SUBS) or []

    def subscribe(self, sub: dict, person: str = "", agent: str = "") -> None:
        if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://")
                and isinstance(sub.get("keys"), dict)):
            raise ValueError("invalid push subscription")
        subs = [s for s in self.subscriptions() if s["sub"]["endpoint"] != sub["endpoint"]]
        subs.append({"sub": {"endpoint": sub["endpoint"], "keys": sub["keys"]},
                     "person": person, "agent": agent[:200], "added": time.time()})
        self.store.set(SUBS, subs)

    def unsubscribe(self, endpoint: str) -> None:
        self.store.set(SUBS, [s for s in self.subscriptions() if s["sub"]["endpoint"] != endpoint])

    # -- sending ------------------------------------------------------------------------

    async def send(self, title: str, body: str = "", url: str = "", tag: str = "") -> int:
        subs = self.subscriptions()
        if not subs:
            return 0
        payload = json.dumps({"title": title, "body": body, "url": url or "/", "tag": tag})
        results = await asyncio.gather(*(asyncio.to_thread(self._send_one, s["sub"], payload)
                                         for s in subs))
        gone = {s["sub"]["endpoint"] for s, ok in zip(subs, results) if ok is None}
        if gone:
            self.store.set(SUBS, [s for s in self.subscriptions()
                                  if s["sub"]["endpoint"] not in gone])
            log.info("push: dropped %d expired subscription(s)", len(gone))
        return sum(1 for r in results if r)

    def _send_one(self, sub: dict, payload: str) -> bool | None:
        """True sent, False failed (keep), None gone (drop)."""
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush

        try:
            webpush(sub, payload, vapid_private_key=Vapid.from_pem(self._vapid()["private_pem"].encode()),
                    vapid_claims={"sub": CONTACT}, ttl=24 * 3600, timeout=15)
            return True
        except WebPushException as err:
            status = getattr(err.response, "status_code", None)
            if status in (404, 410):
                return None
            log.warning("push to %s failed: %s", sub["endpoint"][:40], err)
            return False
        except Exception as err:
            log.warning("push to %s failed: %s", sub["endpoint"][:40], err)
            return False
