"""Web push: VAPID key kept once, subscriptions stored per phone, expired ones dropped."""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub import push as P  # noqa: E402


class Store(dict):
    def set(self, k, v):
        self[k] = v


def make():
    return P.Push(types.SimpleNamespace(store=Store()))


SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "x", "auth": "y"}}


def test_key_is_stable_and_valid():
    p = make()
    k = p.public_key()
    assert k == p.public_key() and len(P.base64.urlsafe_b64decode(k + "==")) == 65


def test_subscribe_validates_and_dedupes():
    p = make()
    p.subscribe(SUB, "Kuba", "Safari")
    p.subscribe(dict(SUB), "Kuba", "Safari")
    assert len(p.subscriptions()) == 1 and p.subscriptions()[0]["person"] == "Kuba"
    for bad in (None, {"endpoint": "http://x", "keys": {}}, {"endpoint": "https://x"}):
        try:
            p.subscribe(bad)
            raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass
    p.unsubscribe(SUB["endpoint"])
    assert not p.subscriptions()


def test_send_drops_gone_subscriptions():
    p = make()
    p.subscribe(SUB)
    p.subscribe({**SUB, "endpoint": "https://fcm.googleapis.com/ok"})
    p._send_one = lambda sub, payload: None if "apple" in sub["endpoint"] else True
    assert asyncio.run(p.send("Ciao", "corpo", "#/media")) == 1
    assert [s["sub"]["endpoint"] for s in p.subscriptions()] == ["https://fcm.googleapis.com/ok"]
    assert asyncio.run(make().send("nobody")) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
