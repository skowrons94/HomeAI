"""Notes plugin: CRUD, ordering, validation, author header, notify + publish. No network, temp dir."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.plugins.notes import NotesPlugin, PLUGIN  # noqa: E402


class Req:
    def __init__(self, body=None, headers=None, nid=None):
        self._b, self.headers = body, headers or {}
        self.match_info = {"id": str(nid)} if nid is not None else {}

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


class Notify:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", url="", image="", tag="", push=True):
        self.sent.append((title, body, url, tag))


def make(tmp):
    hub = Hub({"homekit": {"enabled": False}}, Path(tmp))
    hub.notify = Notify()
    hub.published = []
    hub.publish = lambda t, d: hub.published.append((t, d))
    p = NotesPlugin(hub, {})
    asyncio.run(p.start())
    return hub, p


def call(fn, req):
    r = asyncio.run(fn(req))
    return r.status, json.loads(r.text)


def fails(fn, req, exc=ValueError):
    try:
        asyncio.run(fn(req))
    except exc:
        return
    raise AssertionError("expected %s" % exc.__name__)


def test_contract():
    assert PLUGIN is NotesPlugin and NotesPlugin.name == "notes" and NotesPlugin.schema == []
    assert NotesPlugin.icon == "edit" and NotesPlugin.title == "Notes"


def test_create_author_notify_publish():
    with tempfile.TemporaryDirectory() as d:
        hub, p = make(d)
        s, n = call(p.r_create, Req({"text": "comprare il pane"}, {"X-Casa-Person": "Kuba%20%C3%88"}))
        assert s == 200 and n["author"] == "Kuba È" and n["color"] == "yellow" and n["items"] is None
        assert n["pinned"] is False and n["created"] == n["updated"]
        assert hub.notify.sent[-1][1:] == ("comprare il pane", "#/notes", "note") and "Kuba È" in hub.notify.sent[-1][0]
        assert hub.published[-1][0] == "notes" and hub.published[-1][1]["items"][0]["id"] == n["id"]
        s, n2 = call(p.r_create, Req({"items": [{"text": "latte"}, {"text": "uova", "done": True}], "color": "blue"}))
        assert n2["author"] == "" and n2["items"] == [{"text": "latte", "done": False}, {"text": "uova", "done": True}]
        assert hub.notify.sent[-1][1] == "latte"


def test_order_patch_delete_persist():
    with tempfile.TemporaryDirectory() as d:
        hub, p = make(d)
        a = call(p.r_create, Req({"text": "a"}))[1]
        b = call(p.r_create, Req({"text": "b"}))[1]
        c = call(p.r_create, Req({"text": "c", "pinned": True}))[1]
        assert [x["text"] for x in call(p.r_list, Req())[1]] == ["c", "b", "a"]
        n_sent = len(hub.notify.sent)
        s, up = call(p.r_patch, Req({"text": "a2", "color": "pink"}, nid=a["id"]))
        assert up["text"] == "a2" and up["color"] == "pink" and up["updated"] >= a["updated"]
        assert len(hub.notify.sent) == n_sent  # edits don't notify
        assert [x["text"] for x in call(p.r_list, Req())[1]] == ["c", "a2", "b"]
        call(p.r_patch, Req({"pinned": False}, nid=c["id"]))
        assert [x["text"] for x in call(p.r_list, Req())[1]][0] == "c"  # most recently updated
        assert call(p.r_delete, Req(nid=b["id"])) == (200, {"ok": True})
        assert hub.published[-1][1]["items"] and len(hub.published[-1][1]["items"]) == 2
        # a fresh plugin instance on the same store sees the data (reload / restart)
        p2 = NotesPlugin(hub, {})
        asyncio.run(p2.start())
        assert len(call(p2.r_list, Req())[1]) == 2
        fails(p.r_delete, Req(nid=b["id"]), KeyError)
        fails(p.r_patch, Req({"text": "x"}, nid=999), KeyError)


def test_validation():
    with tempfile.TemporaryDirectory() as d:
        hub, p = make(d)
        for bad in ({}, {"text": "  "}, {"text": "x" * 2001}, {"text": "x", "color": "red"},
                    {"items": [{"text": "x" * 201}]}, {"items": [{"text": str(i)} for i in range(51)]},
                    {"text": 5}, {"items": "no"}, {"items": [{"text": ""}]}):
            fails(p.r_create, Req(bad))
        fails(p.r_create, Req(None))
        n = call(p.r_create, Req({"text": "ok"}))[1]
        fails(p.r_patch, Req({"text": ""}, nid=n["id"]))
        assert call(p.r_create, Req({"text": "x" * 2000, "items": [{"text": str(i)} for i in range(50)]}))[0] == 200
        assert not [r for r in hub.published if len(r[1]["items"]) > 2]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
