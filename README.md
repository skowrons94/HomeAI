# Casa — a small home hub for the Raspberry Pi

One lightweight Python process (aiohttp + HAP-python, ~60 MB RAM) that:

- shows and controls the house from a web app (phone-friendly, add it to the Home Screen)
- bridges devices into **Apple Home** (one HomeKit bridge for everything)
- finds and downloads **films and series** (Italian first, then English) into the NAS library, with a
  watchlist that notifies you when something you want shows up
- lists **everything it sees on the network**

Everything is a plugin; enable one by adding its `[plugins.<name>]` section to `config.toml`.

| Plugin | Talks to | What you get |
|---|---|---|
| `ariston` | Ariston NET cloud | Heating zones + hot water (web + HomeKit thermostats) |
| `govee` | Bluetooth LE (Pi's built-in radio) | Temperature/humidity/battery of Govee H5075-type sensors, history charts, HomeKit sensors |
| `kef` | KEF LSX, TCP 50001 | Power, volume, mute, source, play/pause/skip |
| `appletv` | Apple TV (pyatv) | Now playing with artwork, remote, power, launch Infuse |
| `hisense` | Hisense VIDAA TV | Power on (Wake-on-LAN), on/off state; remote keys experimental |
| `dirigera` | IKEA DIRIGERA hub (local API) | Lamps (on/off, dim, colour temperature), outlets, sensors — live |
| `network` | mDNS + ARP | Every device on the LAN, what it is, which plugin fits |
| `media` | TMDB / Cinemeta, Torrentio, Knaben, TPB, EZTV (+ YTS, ilCorSaRoNeRo, 1337x via proxy/VPN), Transmission, NAS | Discover, search, one-click download, watchlist + notifications, library on the NAS |

## Layout

```
hub/            core: entities, plugins, event bus, SQLite store, HomeKit bridge, web API
hub/plugins/    one file (or package) per integration
web/            the web app (plain ES modules, no build step)
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

Bluetooth (Govee) doesn't work from a terminal on macOS without granting it Bluetooth access; on
the Pi it works out of the box.

## Install on the Raspberry Pi 4

Raspberry Pi OS Lite 64-bit (Bookworm). Copy this folder to the Pi (e.g. `~/casa`), then:

```sh
cd ~/casa && ./deploy/install-pi.sh
```

This installs Python deps, BlueZ and Transmission, mounts the NAS `Multimedia` share at
`/mnt/nas` (asks for the NAS user), points Transmission's downloads at the NAS and installs the
`casa` systemd service. Open `http://<pi-ip>:8080`.

**Moving the HomeKit bridge from the Mac to the Pi** without re-pairing: stop Casa on the Mac
(`launchctl unload ~/Library/LaunchAgents/com.casa.hub.plist`) and copy the whole `data/` folder
to the Pi *before* starting it there. Only one copy may run at a time.

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
- **Watchlist (♥)**: favourite a film or series. Casa checks the trackers every few hours and
  notifies you when a release appears (or a new episode airs). With *auto-download* on it also
  downloads it. If only an English release exists it waits `italian_grace_days` for an Italian one.

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

## Device setup notes

- **Apple TV**: on the TV, Settings → AirPlay and HomeKit → *Allow Access* → *Everyone on the
  Same Network*. Then in Casa, Settings → Apple TV → Set up, and type the PINs shown on the TV.
- **IKEA DIRIGERA**: power it on, Settings → IKEA DIRIGERA → Set up, press the action button on the
  bottom of the hub when asked. Lamps stay out of HomeKit by default (`homekit = false`), because
  DIRIGERA can join Apple Home itself via Matter.
- **Govee**: sensors appear by themselves within a minute of the Pi hearing them. Rename them and
  assign rooms with the ⋯ button on a tile.
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

The UI renders any entity from its `kind` (see the docstring in `hub/core.py` for the state keys
of each kind), so most plugins need no UI code.
