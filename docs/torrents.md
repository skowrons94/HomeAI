# Torrent search

A search box over the providers **you** enable. The hub fans the query out, normalises the answers,
removes duplicates and lets you sort and filter. It never downloads anything by itself: each
card gives you a magnet / `.torrent` link to copy or open in your client.

## Enable

```toml
[plugins.torrents]
providers = ["internetarchive"]   # ids to search; empty = every provider found
# extra_dir = "/home/pi/casa-sources"   # your own providers (below)
# timeout = 10                          # seconds per provider
# page_size = 20                        # results per provider and page
```

Or use Settings → Torrent search. No API key and no new dependency (aiohttp is already required).
The page appears at `#/torrents` while the plugin runs.

## Built in

| id | Source | Notes |
|---|---|---|
| `internetarchive` | archive.org `advancedsearch` (documented, keyless) | Public-domain / freely licensed items with an official torrent. Gives size and a `.torrent` URL; no seeder counts (shown as unknown). |

Sites of the Media plugin that take a free-text query are offered too (off until you enable them,
same mirrors and proxy as `[plugins.media]`): `knaben`, `apibay` (The Pirate Bay), `yts`,
`ilcorsaronero`, `1337x`. `torrentio` and `eztv` need an IMDb id and are not exposed. These sites
answer a single page and may be blocked in your country; use the Test button in the Trackers menu.

## Trackers menu

The **Trackers** button in the page header lists every source: name, kind (Built-in, Torznab, File),
an on/off switch, last health (OK + latency, or the error) and **Test** (a tiny search for "ubuntu").
Toggling needs no restart. The enabled set and the custom trackers are kept in the hub store
(`torrents.enabled`, `torrents.trackers`), not in `config.toml`; the `providers` config list is only
the initial default (empty = every built-in and file provider, no media site or Torznab entry).

**Add tracker** takes a name, a Torznab URL and an API key: Jackett
(`http://host:9117/api/v2.0/indexers/all/results/torznab/`), Prowlarr (`http://host:9696/<id>/api`)
or any Torznab API (`?t=search&q=…&apikey=…&offset=&limit=`). An `apikey=` typed inside the URL is
moved to the key field. The key is stored in the store and never sent back to the client
(`has_key: true`); leave the field empty when editing to keep it. Results use `torznab:attr`
`seeders`/`peers`/`size`/`infohash`/`magneturl` and the `enclosure` link. A download link that
contains your key is hidden whenever a magnet is available.

## Add your own provider

Drop a `.py` file in `extra_dir` (outside the repo, so `update.sh` never touches it) and restart
the plugin from Settings. A broken file is logged and skipped; it can't take other providers down.

```python
from hub.plugins.torrents.model import Provider, ProviderError, Result, fetch_json

class MySource(Provider):
    id = "mysource"
    name = "My source"

    async def search(self, session, query, page, page_size, timeout):
        data = await fetch_json(session, "https://example.org/api", {"q": query, "page": page}, timeout)
        try:
            return [Result(title=i["name"], size=i["bytes"], seeders=i["seeds"], leechers=i["peers"],
                           magnet=i["magnet"], source=self.name, rank=n)
                    for n, i in enumerate(data["items"])]
        except (KeyError, TypeError) as err:
            raise ProviderError("malformed", "unexpected answer shape") from err

SOURCE = MySource
```

Contract: return `Result`s best match first (`rank` = position), raise `ProviderError(kind, msg)`
(`timeout | rate_limited | unavailable | malformed | error`) and use `fetch_json`, which already maps
timeouts, HTTP 429/503 (`Retry-After`), other HTTP errors and invalid JSON. Missing numbers are `None`.

## What the hub does for you

- **Schema**: `title, size (bytes), seeders, leechers, source, magnet, url, hash`; results without a
  magnet or http(s) link are dropped, numbers are coerced, the infohash is read from the magnet.
- **Dedupe**: same infohash, else same normalised title + size; the best-seeded copy wins and the
  `sources` of all copies are kept.
- **Sort / filter**: `sort=relevance|seeders|size`, `order=asc|desc`, `min_seeders`, `min_size`,
  `max_size` (bytes). Unknown values sort last.
- **Pagination**: `page` is passed to every provider; `has_more` is true if any returned a full page.
- **Errors**: each provider has its own status in `providers[]`; a timeout, crash or bad answer in
  one never hides the others. A rate-limited provider is skipped until its `Retry-After` passes.

## Download into the right library

Every result has a **Download** button. The hub guesses what it is from the name
(`hub/plugins/torrents/identify.py`: film, series, music or game, plus title/year, season/episode,
artist/album, game platform), the sheet lets you correct it — for films and series you pick the
TMDB match — and shows where it will go. The release is then handed to the plugin that owns that
library, which downloads it with Transmission and files it exactly as from its own page:

| Kind | Plugin | Folder |
|---|---|---|
| film / series | `media` (`accept_download`) | `movies/Title (year)/`, `series/Show/Season N/` |
| game | `games` (`finder.add`) | `games/pc`, `games/emu/<system>` |
| music | `music` (`accept_download`) | `music/Artist/Album (year)/` |

`POST /api/p/torrents/identify {title, kind?, query?}` → `{kind, confidence, guess, candidates[], available}`;
`POST /api/p/torrents/download {result, kind, guess}` → `{ok, kind, hash}`;
`GET /api/p/torrents/downloads` → what is in progress, whichever page started it.

The Media plugin also searches your own Torznab trackers (Jackett, Prowlarr…) on every film/series
page, next to its built-in sites.

## API

`GET /api/p/torrents/search?q=…&sort=&order=&page=&min_seeders=&min_size=&max_size=&providers=a,b`
→ `{query, page, results[], providers[{id,name,ok,count,ms,error,kind}], has_more, error}`.
`GET /api/p/torrents/providers` → enabled `[{id, name}]`.

Trackers: `GET /trackers` → `{trackers: [{id, name, kind, enabled, editable, has_key, url, health}]}`;
`POST /trackers {name, url, key}` (add, 201); `PUT /trackers/{id} {enabled?, name?, url?, key?}`
(only `enabled` for non-Torznab); `DELETE /trackers/{id}` (Torznab only); `POST /trackers/{id}/test`
→ `{ok, count, ms, error, kind}`; `POST /trackers/test {url, key, id?}` tries an unsaved endpoint.

## Test

```sh
python tests/test_torrents.py      # no network: fake sessions and providers
```
