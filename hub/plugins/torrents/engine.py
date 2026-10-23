"""Fan-out to providers, then normalise, de-duplicate, filter, sort. Pure logic, no UI."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import pkgutil
import time
from pathlib import Path

from . import sources
from .model import Provider, ProviderError, Result, clean

log = logging.getLogger("hub.torrents")
SORTS = ("relevance", "size", "seeders")


def discover(extra_dir: str = "") -> dict[str, Provider]:
    """Built-in providers plus any ``*.py`` in ``extra_dir`` that exports ``SOURCE``."""
    found: dict[str, Provider] = {}

    def add(mod, where, kind):
        cls = getattr(mod, "SOURCE", None)
        if not (isinstance(cls, type) and issubclass(cls, Provider) and cls.id):
            return
        found[cls.id] = inst = cls()
        inst.kind = kind
        log.debug("provider %s from %s", cls.id, where)

    for m in pkgutil.iter_modules(sources.__path__):
        add(importlib.import_module(f"{sources.__name__}.{m.name}"), m.name, "builtin")
    if extra_dir:
        for f in sorted(Path(extra_dir).expanduser().glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"casa_torrent_src_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                add(mod, str(f), "file")
            except Exception:  # a broken user provider must not take the others down
                log.exception("could not load provider %s", f)
    return found


def dedupe(results: list[Result]) -> list[Result]:
    """Same infohash (or same title+size) = one card; keep the best-seeded, merge the rest."""
    best: dict[str, Result] = {}
    for r in results:
        k = r.key()
        cur = best.get(k)
        if cur is None:
            best[k] = r
            continue
        if (r.seeders or -1) > (cur.seeders or -1):
            r.sources = cur.sources + [s for s in r.sources if s not in cur.sources]
            r.magnet, r.url = r.magnet or cur.magnet, r.url or cur.url
            r.rank = min(r.rank, cur.rank)
            best[k] = r
        else:
            cur.sources += [s for s in r.sources if s not in cur.sources]
            cur.magnet, cur.url = cur.magnet or r.magnet, cur.url or r.url
            cur.rank = min(r.rank, cur.rank)
    return list(best.values())


def refine(results: list[Result], sort: str = "relevance", order: str = "desc",
           min_seeders: int = 0, min_size: int = 0, max_size: int = 0) -> list[Result]:
    out = [r for r in results
           if (not min_seeders or (r.seeders or 0) >= min_seeders)
           and (not min_size or (r.size or 0) >= min_size)
           and (not max_size or (r.size is not None and r.size <= max_size))]
    if sort == "relevance":
        return sorted(out, key=lambda r: r.rank)
    attr = "size" if sort == "size" else "seeders"
    known = [r for r in out if getattr(r, attr) is not None]
    unknown = [r for r in out if getattr(r, attr) is None]   # always last, whatever the order
    known.sort(key=lambda r: getattr(r, attr), reverse=(order != "asc"))
    return known + unknown


class Engine:
    def __init__(self, providers: dict[str, Provider], timeout: float = 10):
        self.providers, self.timeout = providers, timeout
        self.cooldown: dict[str, float] = {}   # provider id → monotonic time it may be asked again

    async def _one(self, session, p: Provider, query, page, page_size):
        status = {"id": p.id, "name": p.name, "ok": False, "count": 0, "error": None, "kind": None}
        wait = self.cooldown.get(p.id, 0) - time.monotonic()
        if wait > 0:
            status.update(kind="rate_limited", error="rate limited, retry in %ds" % (wait + 1))
            return status, []
        t0 = time.monotonic()
        try:
            raw = await asyncio.wait_for(p.search(session, query, page, page_size, self.timeout),
                                         self.timeout + 2)
            if not isinstance(raw, list):
                raise ProviderError("malformed", "provider returned %s, not a list" % type(raw).__name__)
            items = [c for c in (clean(r) for r in raw if isinstance(r, Result)) if c]
            status.update(ok=True, count=len(items), raw=len(raw), ms=int((time.monotonic() - t0) * 1000))
            return status, items
        except ProviderError as err:
            if err.kind == "rate_limited":
                self.cooldown[p.id] = time.monotonic() + (err.retry_after or 30)
            status.update(kind=err.kind, error=str(err))
        except asyncio.TimeoutError:
            status.update(kind="timeout", error="no answer within %ds" % self.timeout)
        except Exception as err:  # a buggy provider is reported, never fatal
            log.exception("provider %s crashed", p.id)
            status.update(kind="error", error="%s: %s" % (type(err).__name__, err))
        return status, []

    async def search(self, session, query: str, page: int, page_size: int, ids=None):
        chosen = [p for i, p in self.providers.items() if not ids or i in ids]
        done = await asyncio.gather(*(self._one(session, p, query, page, page_size) for p in chosen))
        statuses = [s for s, _ in done]
        merged = dedupe([r for _, items in done for r in items])
        has_more = any(s.get("raw", 0) >= page_size for s in statuses)
        for s in statuses:
            s.pop("raw", None)
        return merged, statuses, has_more
