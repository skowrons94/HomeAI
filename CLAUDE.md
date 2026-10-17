# HomeAI (Casa) — notes for Claude

A lightweight home hub for a Raspberry Pi (production: Pi 3 B+, 1 GB RAM): one Python process (aiohttp + HAP-python) with
plugins, a HomeKit bridge, a media center and a no-build web app. See README.md for the user
view; this file is how to work on it.

## Working rules

### Delegate to cheaper models
Keep the main conversation for planning, cross-cutting design and review. Hand self-contained
work to subagents and pick the **cheapest model that can do it**:

| Model | Use for | Examples |
|---|---|---|
| `haiku` | Mechanical, well-specified, low-judgement work | add a settings `schema` to a plugin from its config keys, rename/move things, write a test from a given spec, grep/summarise code, update docs/README tables, check a file for secrets |
| `sonnet` | A feature inside one area with a clear contract | a new plugin, a page of the web app, a media indexer, a protocol client, debugging one plugin |
| main model | Design touching `hub/core.py`, `store.py`, `web.py`, `homekit.py`; reviewing agent output; anything with HomeKit pairing or user data at stake | |

- Give agents a precise brief: files they own, contracts to follow (link to the sections below),
  what not to touch, how to test, and to reply with a **short** summary (files changed, what was
  verified) — not file dumps.
- Run independent agents in parallel; never let two agents edit the same file.
- Use `Explore` (or a `haiku` agent) for broad searches instead of reading many files yourself.
- Read only the part of a file you need (`offset`/`limit`, `grep -n`); don't re-read files you
  just edited.

### Safety
- Never commit or print `config.toml`, `data/`, `legacy/` (credentials, HomeKit keys).
- The NAS library is real: tests must never write to `/mnt/nas` or `/Volumes/*`; use a temp dir.
- **Production runs on the user's Raspberry Pi** (deploy details are in Claude's memory, not in
  this repo — the repo is generic for every user). Never start a hub on the Mac with the real
  `config.toml`: test instances use a temp config with `[homekit] enabled = false`, another
  `[web] port`, `[plugins.media.transmission] enabled = false`, and no
  `kef`/`hisense`/`appletv`/`ariston` plugins.
- Only **one** hub may run at a time: HomeKit identity, and the KEF speaker accepts one TCP client.
  Stop the running instance before starting another (`pgrep -f "python -m hub"`).
- `data/homekit.*` must survive every change: losing it means re-pairing every accessory.
  HomeKit accessory uids must stay stable (`homekit.add(uid, …)`).

## Run & test

```sh
.venv/bin/python -m hub [--debug]          # http://localhost:8080 (Mac: run from a terminal;
                                           # launchd lacks Local Network permission)
for t in tests/test_*.py; do .venv/bin/python "$t" || break; done
```

Use `.venv/bin/python -m pip` (the venv was moved; `pip` shebangs are broken). Screenshots: open
the UI with `?static` (disables SSE so headless Chrome finishes loading).

## Architecture

```
hub/core.py      Entity, Plugin base, Hub (entities, event bus, tasks, plugin lifecycle)
hub/settings.py  config.toml load/save + per-plugin schema helpers (everything editable in-app)
hub/store.py     SQLite (WAL): kv, history rollups, notifications
hub/homekit.py   single HAP bridge; persisted AIDs
hub/web.py       REST + SSE + static; plugin routes are dispatched dynamically
hub/plugins/     one module or package per integration, exports PLUGIN
web/             ES modules, no build step; pages in web/pages/*.js
docs/sync.md     shared feed contract
deploy/          Pi installer, VPN script, macOS launchd
```

### Plugin contract
- `hub/plugins/<name>.py` (or package) defines a `Plugin` subclass and `PLUGIN = Cls`.
- Class attributes: `name`, `title`, `description`, `icon` (name from `web/app.js` icons) and
  `schema` — a list of fields the Settings page renders as a form:
  `{"key": "host", "label": "Host", "type": "text", "default": "", "help": "…"}`.
  Types: `text password number bool select list path`; optional `options` (select),
  `placeholder`, `min`/`max`/`step`, `unit`, `advanced: True` (collapsed), `required: True`
  (asked for in the first-run wizard), `link: {"url", "label"}` (where to get a key/account).
  Dotted keys (`"proxy.url"`) map to nested tables.
- `ui = True`: `web/plugins/<name>.js` default-exports `async (container, plugin)` and renders
  an extra panel in the plugin's settings sheet (e.g. Bluetooth nearby list, NAS mounting).
- First run: `/api/state.onboarding` is true until the Welcome wizard (`web/pages/welcome.js`)
  posts `/api/onboarding {done: true}`; it reuses the Settings forms, so a plugin with a good
  schema is automatically part of the guided setup.
- Read config with `self.config.get("key", default)` — the user can change it from the app at
  any time, which stops the plugin and starts a fresh instance with the new config.
- `start()` must return quickly; run loops with `self.hub.every(...)`/`self.hub.spawn(...)` —
  tasks started from `start()` belong to the plugin and are cancelled on reload/disable.
- Entities: `self.hub.add_entity(Entity(...))`, then `self.hub.update(id, **state)`. State keys per
  kind are documented at the top of `hub/core.py`. Any numeric key of a `sensor` entity is
  stored in history automatically; units go in `attrs["units"] = {"key": "unit"}`.
- Routes: `routes()` returns `aiohttp.web` RouteDefs, served under `/api/p/<name>/…`.
- `attention()` returns hints for the Home page's "Needs attention" list (errors and unpaired
  plugins are reported by the hub automatically); each item has an `action` the UI can run.
- Interactive pairing: `POST /api/p/<name>/setup {step, value}` →
  `{message, input, choices, step, done, error}`; set `status()["setup"] = True` to show the wizard.
- Keep dependencies light (Pi 3 B+, 1 GB RAM, ~400 MB memory cap); import heavy libraries inside `start()`.

### Language, people, layout
- The UI is translated, Italian by default (`language` in config, Settings → General). Keys are
  the **English source text**: `t("Ready: {what}", { what })` in JS (from `web/app.js`), `_()`
  from `hub/i18n.py` in Python for user-facing server text (notifications, attention, setup
  messages, UI errors) — never for logs. Dictionaries: `web/i18n/it/<area>.json`, one per area;
  same `{placeholders}` as the source (`tests/test_i18n.py`). Plugin `schema` texts, `title` and
  `description` stay English in Python and are translated client-side (`schemas.json`).
- Dates/numbers: `fmt.date/time/num` and `locale()` in `web/app.js`, never bare `toLocale*()`.
- `people` (config) + "Chi sei?" per phone (localStorage `casa.person`, sent as the
  `X-Casa-Person` header). Layout prefs per person: `GET/PUT /api/prefs?person=` →
  `{tabs: [page ids], home: {order, hidden}}`. The phone tab bar = pinned pages + "More".

### History
Graphite-style rollups in SQLite (`h1m` 1-minute for 2 days, `h5m` 5-minute for 30 days, `h1h`
hourly for 2 years; avg/min/max per bucket). Benchmarked faster and smaller than Whisper for this
load. `GET /api/history/{entity}?key=&hours=` picks the table by range.
