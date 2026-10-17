# Shared feed (sync plugin) — design contract

Several Casa hubs in different houses share **only a social layer** — what people watched,
rated, put on their watchlist, and a common recipe book with everyone's tips — through a
**private git repo**. No hub ever reaches another hub's network or database: there is no SSH
between houses, no shared DB and no open port. Each hub pulls/pushes the repo over SSH with
its own deploy key.

Plugin: `hub/plugins/sync/` (`PLUGIN`, name `sync`, title "Shared feed"). Optional: when the
plugin is disabled nothing else changes.

## Sync repo layout

```
SCHEMA.md                 human description of this layout (written by the first hub if missing)
feeds/<user>.jsonl        append-only, one event per line, written ONLY by <user>'s hub
media/<imdb_id>.json      title card, written once by the first hub that needs it, never rewritten
media/<imdb_id>.jpg       poster (w342-ish, ~30-60 KB), written once, never rewritten
recipes/<slug>.md         one shared recipe per file (front matter + sections)
recipes/<slug>.jpg        optional photo, written once
```

## Events — `feeds/<user>.jsonl`, v1

One JSON object per line, keys in this order, no extra keys:

```json
{"v":1,"id":"01JABC…","ts":"2026-10-17T07:00:00.123Z","user":"mango","kind":"rating",
 "media":"movie","imdb_id":"tt0111161","tmdb_id":278,"rating":4.5,"on":null,
 "watched_at":null,"note":""}
```

| key | type | rules |
|---|---|---|
| `v` | int | `1` |
| `id` | str | ULID (26 chars Crockford base32, time-ordered, monotonic within a process) |
| `ts` | str | UTC ISO-8601 with milliseconds and `Z` |
| `user` | str | `^[a-z0-9_-]{1,32}$`, must equal the file name |
| `kind` | str | `watch` \| `rating` \| `watchlist` |
| `media` | str | `movie` \| `series` (the repo's media types) |
| `imdb_id` | str | `^tt\d{5,10}$`: **the shared key** (both TMDB and Cinemeta resolve it) |
| `tmdb_id` | int \| null | extra info only, never used for matching |
| `rating` | number \| null | `kind=rating`: 0.5–5 in 0.5 steps; `null` = rating removed |
| `on` | bool \| null | `kind=watchlist`: true = added, false = removed |
| `watched_at` | str \| null | `kind=watch`: `YYYY-MM-DD`; `null` = "not watched" (undo) |
| `note` | str | free text ≤ 500 chars, may be `""` |

- **Never match on titles.** Identity is `imdb_id`.
- **Append-only.** To correct something, append a newer event with the same key, never rewrite a line.
- **Merge:** for each `(user, imdb_id, kind)` the event with the greatest `(ts, id)` wins
  (the `id` breaks ties between two events in the same millisecond). Input order doesn't matter.
- A hub never writes to another user's file. Lines in `feeds/<x>.jsonl` whose `user != x`, or
  that fail validation, are ignored when reading (logged once).

### Privacy (non-negotiable)
Only tmdb/imdb ids, ratings, dates, notes, title cards, posters and recipes go into the repo:
never local paths, tracker or indexer names, hostnames, IPs, MACs, keys or library contents.
Enforced in code by `validate_event()` (key whitelist + per-key checks) and `scan_text()`,
which rejects text (notes, tips, recipe bodies) containing: IPv4/IPv6 addresses, MAC addresses,
absolute paths (`/mnt/`, `/Volumes/`, `/home/`, `/Users/`, `~/`, `C:\`, `\\server`), `magnet:`,
`.torrent`, or URLs (only the recipe front matter key `source` may hold a URL). Anything that
fails is refused with a clear error **before** it is written. Tests assert this.

## Title cards — `media/<imdb_id>.json`

```json
{"imdb_id":"tt0111161","tmdb_id":278,"type":"movie","title":"Le ali della libertà",
 "original_title":"The Shawshank Redemption","year":1994,"poster":"tt0111161.jpg"}
```
Built from the media plugin's metadata (`hub.plugins["media"].metadata.details(type, id)`),
which works with or without a TMDB key. The poster is downloaded once from the metadata's
`poster` URL and stored next to it (`poster` is `null` if there was none). Hubs without a TMDB
key, or without the media plugin, render the feed purely from these files. If a file already
exists it is never rewritten (identical content would be fine anyway).

## Recipes — `recipes/<slug>.md`

```markdown
---
title: Carbonara
servings: 2
time_min: 20
tags: [primo, veloce]
author: mango
source: https://www.giallozafferano.it/…
image: carbonara.jpg
---
## Ingredienti
- 200 g spaghetti
- # Per la crema        (heading lines keep the recipes plugin's "# " convention)
- 3 tuorli

## Procedimento
1. Lessate la pasta.
2. …

## Trucchi
- (kuba) Pecorino only, no parmigiano.
- (mango) Turn off the heat before adding the eggs.
```
- Front matter is a strict tiny YAML subset (scalars, `[a, b]` lists): parsed and written by
  our own code, no YAML dependency. `source` and `image` are optional.
- Slug: lower-case ascii title, non-alnum → `-`, plus `-2`, `-3` … if taken by a different recipe.
- The parser also accepts English headings (`Ingredients`, `Steps`/`Method`, `Tips`).
- **Trucchi is append-only**: each line is `- (<user>) <text>`; a hub only adds lines signed by
  its own user and never edits or deletes others' lines.
- Only the `author` re-shares (overwrites) title/ingredients/steps/front matter; Trucchi is
  always kept (the union of the existing lines and the newly added ones, existing order first).

## Hub side

### Local tables (owned by the plugin, prefix `sync_`; created in the plugin, not in `store.py`)
```
sync_events  (id TEXT PK, user, ts, kind, media, imdb_id, data JSON)   all valid events from all feeds
sync_media   (imdb_id TEXT PK, data JSON)                              title cards
sync_recipes (slug TEXT PK, title, author, updated REAL, data JSON)    parsed recipes incl. tips
sync_outbox  (seq INTEGER PK AUTOINCREMENT, op TEXT, data JSON, created REAL)  this hub's unpushed writes
```
The UI reads only these tables, never the files.

### Sync cycle: no rebase, no conflicts
The working copy lives in `data/sync/repo` and is disposable. Every cycle (every `interval`
minutes, right after a local write with a few seconds' debounce, and on `POST /sync`), under
one asyncio lock:

1. Clone on first run; otherwise `git fetch` and `git reset --hard origin/<branch>`
   (an empty remote is fine: create the branch with `SCHEMA.md` and the empty folders).
2. Apply every outbox op on top, idempotently:
   - `event`: append to `feeds/<me>.jsonl` unless that `id` is already in the file
   - `media`: write `media/<id>.json` (+ `.jpg`) if missing
   - `recipe`: write/overwrite `recipes/<slug>.md` (only if absent or `author == me`), merging Trucchi
   - `tip`: append `- (<me>) text` to the recipe's Trucchi unless that exact line is already there
3. Guard: `git status --porcelain` must only touch `feeds/<me>.jsonl`, `media/*`, `recipes/*`
   and `SCHEMA.md`; every changed feed line must pass `validate_event`. Otherwise abort the cycle
   (log an error, keep the outbox).
4. Commit (author `<user> <user@casa.invalid>`, message such as `mango: 2 events, 1 tip`) and push.
   If the push is rejected (someone pushed meanwhile), start again from step 1, at most 3 times.
5. On success delete the applied outbox rows, then rebuild `sync_events`/`sync_media`/`sync_recipes`
   from the files (incrementally by file mtime/size or full; it is small).

Git runs via `asyncio.create_subprocess_exec("git", …, cwd=repo)` with
`GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"`
and `GIT_TERMINAL_PROMPT=0`; a timeout on every command. The `repo_url` may also be a local
path (that's how the tests run).

### Watchlist mirroring
With `share_watchlist` on, every cycle diffs the media plugin's `media_watchlist` (read-only:
`id`, `type`) against the set last shared (kv `sync.watchlist`) and queues `watchlist` on/off
events (plus title cards). The media plugin itself isn't modified.

### Config — `[plugins.sync]` (schema shown in Settings and in the first-run wizard)
| key | type | default | notes |
|---|---|---|---|
| `repo_url` | text, required | `""` | `git@github.com:you/casa-feed.git`, private |
| `user` | text, required | `""` | your name in the feed (`^[a-z0-9_-]{1,32}$`) |
| `branch` | text, advanced | `"main"` | |
| `interval` | number, advanced | `10` | minutes, 2–240 |
| `key_path` | path, advanced | `""` | empty = `data/sync/id_ed25519` (generated on first start) |
| `share_watchlist` | bool | `true` | |

With an empty `repo_url` or `user` the plugin starts idle and `status()` says what's missing.
On first start, if the key doesn't exist, `ssh-keygen -t ed25519 -N "" -C casa-<user>` creates it;
the public key is shown in `status()` and in the plugin's settings panel (`ui = True`,
`web/plugins/sync.js`) so it can be pasted into GitHub → repo → Deploy keys (with write access).

### REST: `/api/p/sync/…`
| method & path | body / query | returns |
|---|---|---|
| `GET /status` | | `{configured, user, users:[…], last_sync, last_error, pending, public_key, repo_url}` |
| `POST /sync` | | runs a cycle now, returns status |
| `GET /feed` | `?user=&kind=&media=&limit=100&before=<ts>` | `[{id, ts, user, kind, media, imdb_id, rating, on, watched_at, note, title, year, poster}]`: **merged** state (one per user/title/kind), newest first; `poster` is a URL to `/api/p/sync/poster/<imdb_id>` or null |
| `GET /title/{imdb_id}` | | `{card, mine:{watch, rating, watchlist}, others:[{user, watch, rating, note}]}` |
| `GET /lookup` | `?type=movie&id=<tmdb:123 or tt…>` | same as `/title` after resolving the imdb id (used by the media page) |
| `POST /events` | `{kind, type, id, rating?, watched_at?, on?, note?}`: `id` = media-plugin id (`tmdb:…`/`tt…`) or `imdb_id` | the stored event; resolves the imdb id + title card + poster via the media plugin and queues them |
| `GET /poster/{imdb_id}` | | jpg from the working copy (cache headers) |
| `GET /recipes` | `?q=&tag=&author=` | `[{slug, title, author, tags, servings, time_min, image, tips_count, updated}]` |
| `GET /recipes/{slug}` | | `{slug, title, author, servings, time_min, tags, source, image, ingredients:[…], steps:[…], tips:[{user, text}]}` |
| `POST /recipes/share` | `{recipe_id}` (local recipes plugin id) | `{slug}`: exports from `hub.plugins["recipes"].book`, author = me |
| `POST /recipes/{slug}/tips` | `{text}` | updated recipe |
| `POST /recipes/{slug}/import` | | `{id}`: copies it into the local recipe book via `book.add(...)` |
| `GET /image/{name}` | | recipe photo from the working copy |

Errors: raise `ValueError` → 400 with the message, like the other plugins.
