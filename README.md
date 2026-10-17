# Casa — a small home hub for the Raspberry Pi

One lightweight Python process (aiohttp + HAP-python, ~60 MB RAM) that:

- shows and controls the house from a web app (phone-friendly, add it to the Home Screen)
- bridges devices into **Apple Home** (one HomeKit bridge for everything)
- finds and downloads **films and series** (Italian first, then English) into the NAS library, with a
  watchlist that notifies you when something you want shows up
- lists **everything it sees on the network**
- records the history of every sensor and charts it

Everything is configured from the web app. On first launch a **Welcome wizard** guides you through
your home name and rooms, network discovery (it suggests integrations for the devices it finds),
each integration's keys and accounts (with links to get them), notifications and Apple Home pairing.
Any integration can be changed later in Settings; changes apply live, with no restart. *Run setup
again* in Settings re-opens the wizard.

`config.toml` is still a plain file. The app writes it and keeps a `config.toml.bak` backup.
`config.example.toml` documents every option.

| Plugin | Talks to | What you get |
|---|---|---|
| `ariston` | Ariston NET cloud | Heating zones + hot water (web + HomeKit thermostats) |
| `bluetooth` | Bluetooth LE (Pi's built-in radio) | Sensors from Govee, Xiaomi (pvvx/ATC firmware), BTHome, SwitchBot, Qingping and Inkbird. Nearby list with Add/Ignore in Settings |
| `kef` | KEF LSX, TCP 50001 | Power, volume, mute, source, play/pause/skip |
| `appletv` | Apple TV (pyatv) | Now playing with artwork, remote, power, launch Infuse |
| `hisense` | Hisense VIDAA TV | Power on (Wake-on-LAN), on/off state; remote keys experimental |
| `dirigera` | IKEA DIRIGERA hub (local API) | Lamps (on/off, dim, colour temperature), outlets, sensors — live |
| `network` | mDNS + ARP | Every device on the LAN, what it is, which plugin fits |
| `system` | This hub | CPU, temperature, memory, disk, network download/upload, Wi-Fi signal, internet latency — recorded in History |
| `storage` | NAS via mDNS | Find the NAS, list its shares, mount it from the app, suggest library folders, free-space sensors |
| `recipes` | GialloZafferano, TheMealDB, any recipe URL | Search or import recipes (stored locally, with images); favourites, notes, your own edits (with reset); shopping list with substitutions; calories per serving and a daily log against a goal |
| `notes` | This hub (SQLite) | Shared notes and small checklists for the household: colours, pinned notes, tick items on the card, phone notification for new notes |
| `media` | TMDB / Cinemeta, Torrentio, Knaben, TPB, EZTV (+ YTS, ilCorSaRoNeRo, 1337x via proxy/VPN), Transmission, NAS | Discover, search, one-click download, watchlist + notifications, library on the NAS |

## Layout

```
hub/            core: entities, plugins, event bus, SQLite store, HomeKit bridge, web API
hub/plugins/    one file (or package) per integration
hub/settings.py config.toml read/write and the per-plugin settings schema
web/            the web app (plain ES modules, no build step)
web/plugins/    custom settings panels (Bluetooth nearby list, NAS mounting, …)
deploy/         Raspberry Pi installer, VPN script, macOS launchd installer
tests/          python tests/test_*.py
data/           runtime state: hub.db, HomeKit pairing (keep it to keep your Home setup)
```

## Run it on the Mac (testing)

```sh
cd ~/Desktop/home
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m hub            # http://localhost:8080
./deploy/install-mac.sh            # or: run in the background at login
```

Bluetooth doesn't work from a terminal on macOS until the terminal is granted Bluetooth access; on
the Pi it works out of the box.

## Install on a Raspberry Pi

Any 64-bit Debian 12 based OS (Raspberry Pi OS Lite, DietPi). A Pi 3 B+ with 1 GB is enough
(Casa uses ~70 MB); a Pi 4 is comfortable.

```sh
git clone https://github.com/skowrons94/HomeAI.git ~/casa
cd ~/casa && ./deploy/install-pi.sh
```

This installs Python deps, BlueZ and Transmission, and the `casa-mount` helper, which lets the app
mount the NAS. The helper is root-owned, and a sudo rule allows only that helper. The installer also
points Transmission's downloads at the NAS and installs the `casa` systemd service. By default it adds
a boot-time mount of the NAS `Multimedia` share at `/mnt/nas` (it asks for the NAS user). To manage
the NAS only from the app, skip that mount: `NAS_FSTAB=0 ./deploy/install-pi.sh`. If your NAS
is already mounted (your own fstab lines), point the installer at it instead:
`NAS_FSTAB=0 MOUNT=/mnt/media DOWNLOAD_DIR=/mnt/download/incomplete ./deploy/install-pi.sh`.
A machine that must never run Transmission: `TRANSMISSION=0 ./deploy/install-pi.sh` skips it (and
stops/disables an existing daemon); it also follows `[plugins.media.transmission] enabled = false`
in `config.toml`, which keeps the hub itself away from Transmission too.

**Updating**: `~/casa/deploy/update.sh` (or from your computer `ssh <pi> '~/casa/deploy/update.sh'`)
pulls the latest version and restarts Casa; settings and the HomeKit pairing are kept.

Open `http://<pi-ip>:8080`.

**Moving the HomeKit bridge from the Mac to the Pi** without re-pairing: stop Casa on the Mac
(`launchctl unload ~/Library/LaunchAgents/com.casa.hub.plist`) and copy the whole `data/` folder
to the Pi *before* starting it there. Only one copy may run at a time.

## History

Every sensor value is recorded in SQLite, using Graphite-style rollups: 1-minute detail for 2 days,
5-minute for 30 days and hourly for 2 years, each with min/max. Retention is editable in
Settings → History.

We benchmarked it against Graphite's Whisper format. Reads are in the same speed class (under 5 ms for
any range), there is no extra daemon, and min/max are included.

The History page lets you pick up to 4 values and view them over 24h, 7d, 30d, 1y or a custom range.

## Media: films and series

- **Discover**: trending, in cinema, upcoming, popular series, on the air. Add a free TMDB key
  (themoviedb.org → Settings → API) as `tmdb_api_key` for Italian titles/plots and better lists.
  Without a key it falls back to Cinemeta.
- **Search → Download**: releases from several public trackers, ranked by
  *Italian audio → English → others*, then quality (1080p preferred, 4K allowed), size limits and
  seeders. CAM/TS and fakes are filtered out. One click sends the best match to Transmission.
- **When it finishes** the file is moved into the library with the same naming you already use:
  `movies/Title (Year)/…` and `series/Show/Season N/Show - S01E02 - Title.mkv`. Infuse on the
  Apple TV sees it straight away.
- **Folders**: the download, movie and series folders are set in Settings → Media with a folder
  browser. Storage & NAS can mount the NAS and fill them in for you. Transmission's download folder
  is updated automatically.
- **Watchlist (♥)**: favourite a film or series. Casa checks the trackers every few hours and
  notifies you when a release appears (or a new episode airs). With *auto-download* on it also
  downloads it. If only an English release exists it waits `italian_grace_days` for an Italian one.

- **Subtitles**: when an import has no audio in your first language (normally Italian) and no
  subtitle of that language next to it, Casa searches OpenSubtitles and SubDL, checks the
  language and timing (against a sidecar/embedded subtitle or speech sampled from the audio with
  `ffmpeg`), re-syncs it if needed and saves `<video>.it.srt` for Infuse. It needs a free OpenSubtitles
  API key (plus your account, for a higher daily download limit) or a SubDL key in Settings → Media. The detail page has an "Italian subtitles" button to run it by hand.

### More trackers: proxy or VPN

Some trackers (YTS, ilCorSaRoNeRo, 1337x) are blocked by the ISP's DNS. Two ways to reach them:

- **Proxy, searches only** (simplest): in `config.toml`
  ```toml
  [plugins.media.proxy]
  url = "socks5://user:pass@proxy-host:1080"   # or http://… ; DNS resolves through the proxy
  ```
  Many VPN providers include a SOCKS5 proxy. Settings → Media → *Test sources* shows which
  trackers are reachable.
- **VPN for the whole Pi** (searches *and* downloads): get a WireGuard config file from your
  provider and run `./deploy/vpn-wireguard.sh provider.conf`. The home network (HomeKit, NAS, TV)
  keeps working, and Transmission is bound to the tunnel with a kill-switch, so torrent traffic stops
  if the VPN drops. `./deploy/vpn-wireguard.sh --off` undoes it.

## Shared feed

Houses can share a small social layer: what everyone watched, rated and put on their watchlist, plus
a common recipe book where each person adds their own tips (*Trucchi*). It goes through a **private
git repo**; hubs never talk to each other.

1. Create an empty private repo (e.g. `casa-feed` on GitHub).
2. In Settings → Shared feed enter the repo URL (`git@github.com:you/casa-feed.git`) and your name in the feed.
3. Copy the public key shown there into the repo's *Settings → Deploy keys* and tick *Allow write access*.
   Casa generates the key on first start (`data/sync/id_ed25519`); each house has its own.

The hub syncs every few minutes and right after you rate or share something. The repo only ever gets
IMDb/TMDB ids, ratings, dates, short notes, title cards, posters and recipes. Paths, IPs, hostnames,
tracker names, magnet links, URLs in notes and library contents are refused before anything is written.
Details of the format are in [docs/sync.md](docs/sync.md).

## Device setup notes

- **Apple TV**: on the TV, Settings → AirPlay and HomeKit → *Allow Access* → *Everyone on the
  Same Network*. Then in Casa, Settings → Apple TV → Set up, and type the PINs shown on the TV.
- **IKEA DIRIGERA**: power it on, Settings → IKEA DIRIGERA → Set up, press the action button on the
  bottom of the hub when asked. Lamps stay out of HomeKit by default (`homekit = false`), because
  DIRIGERA can join Apple Home itself via Matter.
- **Bluetooth sensors**: nearby sensors appear in Settings → Bluetooth sensors, where you add or
  ignore them. Rename them and assign rooms with the ⋯ button on a tile. On macOS the terminal needs
  Bluetooth access; the Pi works out of the box.
- **Hisense**: power on uses Wake-on-LAN; enable *Wake on LAN/Wi-Fi* in the TV's network settings.
  The TV is already in Apple Home natively (HomeKit + AirPlay).
- **Notifications on the phone**: install the free *ntfy* app, subscribe to a topic with a
  hard-to-guess name, and set `ntfy_url = "https://ntfy.sh/<topic>"` under `[notify]`.

## Writing a plugin

```python
# hub/plugins/myplug.py
from ..core import Entity, Plugin

class MyPlugin(Plugin):
    name = "myplug"
    title = "My plugin"
    description = "Controls the lamp"
    icon = "plug"                     # a name from the icons in web/app.js
    schema = [                        # the Settings form (and the Welcome wizard) is built from this
        {"key": "host", "label": "Host", "type": "text", "default": ""},
    ]

    async def start(self):
        self.hub.add_entity(Entity(id="myplug.lamp", name="Lamp", kind="light",
                                   plugin=self.name, state={"on": False},
                                   actions={"turn_on": self.on, "turn_off": self.off}))
        self.hub.homekit.expose(self.hub.entities["myplug.lamp"])   # optional
        self.hub.every(30, self.poll)

    async def on(self, **_):  ...
    async def off(self, **_): ...
    async def poll(self):     self.hub.update("myplug.lamp", on=True)

PLUGIN = MyPlugin
```

Once it has `title`, `description`, `icon` and `schema`, the plugin shows up in Settings and in the
Welcome wizard automatically. The UI renders any entity from its `kind` (see the docstring in
`hub/core.py` for the state keys of each kind), so most plugins need no UI code.

For the full plugin contract (field types, `required` and `link` fields, custom settings panels,
pairing steps) see `CLAUDE.md`.
