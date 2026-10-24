# Games: NAS library → Steam Deck

Casa lists the games on your NAS and installs them on the Steam Deck from the phone. The hub only
*coordinates*: files stay on the NAS, the Deck downloads them straight from the hub over your
LAN (Wi-Fi is fine) and puts them in place.

```
phone ── Casa (Pi) ── NAS            Steam Deck
          │  scan games folder        casa-deck agent (systemd user service)
          │── "install X" ──────────► downloads files from Casa (signed, expiring links)
          │◄── status / progress ──── ~/Games/<Title>   ~/Emulation/roms/<system>
                                      queues a Steam shortcut / Steam ROM Manager run
```

## 1. Library layout on the NAS

Default folder: `games/` inside the NAS mount (e.g. `/mnt/nas/games`; the Pi installer creates it).
Change it in Settings → Games → *Games folder*. Casa only **reads** it.

```
games/
  pc/
    Celeste v1.4/Celeste.exe …        one folder = one game (version from "v1.4" in the name)
    Hades/v1.2/Hades.exe …            or one sub-folder per version: the newest is used
    Some Game/setup_some_game.exe     an installer (GOG offline installers look like this)
    Penguin/Penguin.AppImage          Linux builds: .AppImage, .x86_64, .sh
    Odd Game/game.json                optional manifest, see below
  emu/
    snes/Super Game.sfc               <system> = the EmuDeck folder name: snes, gba, psx, n64, …
    psx/Disc Game/Disc.cue + .bin     a folder is one game (cue/bin, multi-disc)
```

Optional `game.json` next to the files: `{"title": "…", "version": "1.2", "kind": "portable",
"exe": "bin/run.exe"}` (use it when the .exe can't be guessed). `cover.jpg|png|webp` in the game
folder (or `<rom name>.jpg` next to a ROM) is shown in the app. A folder Casa can't classify is listed
under "items skipped" with the reason; nothing is guessed.

### The Games page
Search by title, filter by platform chip (PC, Linux and each emulator system, with counts) and by status
(installed / not installed / update available / installing), sort A-Z, recently added (folder date), size or
system; the choice is remembered on each device. Tap a game for its details (version, size, files, NAS
folder, installed version, progress) and the actions. The gear opens the settings sheet: games folder, Deck
address/port/token (empty token = keep), hub address, *Test connection*, *Rescan library*, *Apply to Steam*
and the list of skipped items. Browsing works even when the Deck is offline. Extra routes:
`GET /library` (adds `systems`, `exists`, per-game `platform`/`added`) and `POST /test`.

## Metadata, covers and finding games

**Metadata and art.** Optional keys in Settings → Games: IGDB (`igdb.client_id`, `igdb.client_secret`, a free Twitch
developer app) gives description, genres, release date, rating, developer and screenshots; SteamGridDB
(`steamgriddb.key`) gives covers, banners and logos. After a scan Casa looks up new games one at a time in the
background (titles are cleaned: version, `[Repack]`, `(USA)` and `[!]` tags removed; the platform is used to pick the
right edition). Wrong match? Search IGDB by hand and pick the right one (`GET /meta/search`, `POST /meta/match`);
`POST /meta/refresh` refetches. Records live in the store (`games.meta.<id>`), images in `data/games-art` (capped at
200 MB, oldest removed first). A `cover.jpg` in the game folder always wins. Without keys nothing is fetched.

**Cover art on the Deck.** Installing a portable/native game sends the grid, wide, hero and logo images we have
(PNG/JPEG, each up to 1.5 MB, 4 MB in total) to the agent, which writes them to `userdata/<id>/config/grid/` when you
*Apply to Steam*. Update the agent on the Deck (version 1.1) to get this; an old agent ignores the art and an old hub
sends none.

**Find and download.** `GET /find?q=&platform=pc|<system>` searches the trackers enabled in Torrent search (needs that
plugin running; Torznab trackers are asked for the games categories 4000 and 1000). `POST /find/download` adds the
magnet/URL to Transmission (the Media plugin's instance) in `<games>/pc` or `<games>/emu/<system>`;
`GET /find/downloads` shows progress, `DELETE /find/downloads/<hash>` cancels (and deletes the partial data) or forgets
a finished entry. When a download finishes the torrent leaves Transmission (files stay), `.zip` files are unpacked in
place (paths outside the folder and zip bombs are refused) and removed, `.rar`/`.7z` need `7z`, `unar` or `unrar`
on the hub (otherwise the entry says `needs_extraction`), ISOs and ROMs are left as they are, then the library is rescanned.
Nothing outside the games folder is touched.

## 2. Install the agent on the Deck

In Desktop Mode, in a terminal:

```sh
git clone https://github.com/skowrons94/HomeAI.git ~/casa
~/casa/deploy/deck/install-deck.sh          # PORT=8765 by default; re-run with the same PORT after updates
```

It installs `casa_deck_agent.py` (Python standard library only) in `~/.local/bin`, creates a token,
and starts a systemd *user* service. It prints the address and token.

## 3. Configure Casa

Settings → Games (or `config.toml`, see `config.example.toml`):

```toml
[plugins.games]
# library = "/mnt/nas/games"
[plugins.games.deck]
host = "steamdeck.local"      # or the Deck's IP
token = "…"                   # printed by install-deck.sh
```

The Deck downloads from the address you use in the app (`http://casa.local:8080`). If the Deck can't
resolve that name, set *Address of this hub* (`base_url`) to an IP URL. Updating Casa:
`~/casa/deploy/update.sh` as usual (no new dependencies).

## What happens on Install

| Kind | On the Deck | Then |
|---|---|---|
| `portable` (.exe) | `~/Games/<Title>/` | queues a non-Steam shortcut |
| `native` (AppImage/.sh) | `~/Games/<Title>/`, made executable | queues a shortcut |
| `rom` | `~/Emulation/roms/<system>/` | queues a Steam ROM Manager run |
| `installer` (setup.exe) | copied to `~/Games/_installers/<Title>/`; agent 1.3+ runs it silently through Proton (Inno Setup `/VERYSILENT`, NSIS `/S`) into `~/Games/<Title>`, prefix in `~/Games/_prefixes/<Title>`, then adds a Steam shortcut and removes the copy | unknown type or no Proton: "needs a manual step"; agent < 1.3: copy only |

Downloads resume after a Wi-Fi drop, are size-checked and refuse to start without free space.
**Update** shows when the version in the library differs from the installed one. **Remove** deletes
the installed files (never anything else; save files left in the folder keep it alive).

### Why "Apply to Steam" is a separate button
Steam only reads `shortcuts.vdf` while it is **not running**, so the agent can't add shortcuts behind
Steam's back. Installs queue them; *Apply to Steam* closes Steam, writes all queued shortcuts, runs
Steam ROM Manager if the agent finds its AppImage (`--srm PATH` to point at it) and reopens Steam.
Do this in Desktop Mode. Without a Steam ROM Manager the ROM is still in the right folder.

## GOG, genres, editing, deleting, where to install

- **GOG offline installers** (`setup_*.exe` + `-N.bin`): after a download Casa runs `innoextract --gog -e` (needs
  `innoextract` on the hub), keeps the game files, drops `tmp`, `commonappdata`, `__support` and the installers (kept
  only if extraction fails). The library reads `goggame-*.info` for the title, primary exe and working directory.
- **Auto-install** (Settings → Steam Deck): a finished download is sent to the Deck, retried every minute until it is online.
- **Install location** (agent 1.2+): internal storage, an SD card or a custom folder under /home/deck or /run/media,
  default in Settings, override per game; the agent validates the path (`GET /locations`, `POST /location/check`).
- **Genres** in Discover: Steam store tags (no key), IGDB genres/themes per console with keys. The Library filters by genre.
- **Edit details**: title, year, genres, description, cover/banner (SteamGridDB or an https URL, cached on the hub); overrides
  live in the store (`games.override.<id>`) on top of fetched metadata.
- **Delete**: downloads can be removed from the list or with their files; library games can be deleted. Only paths under
  `pc/<x>` or `emu/<system>/<x>` are touched, never symlinks.

## Known limits

- Proton: a new Windows shortcut still needs a compatibility tool picked once
  (Steam → game → Properties → Compatibility), or set it with ProtonUp-Qt / Steam ROM Manager.
- Installers are run silently only when they are Inno Setup or NSIS (agent 1.3+, setting `deck.run_installers`); anything else is only copied. Installers already staged in `_installers` are listed in the app ("On the Deck: waiting installers") with an Install now button.
- The agent is plain HTTP with a bearer token: LAN only, don't expose the port.
- Not verified on a real Deck: the agent, `shortcuts.vdf` writer and the UI are tested here against
  fake Steam/Deck folders. First run: install something small and check the shortcut shows up.

## Security

The agent accepts requests only with the token (constant-time compare), validates every path
(no absolute paths, `..`, odd characters) and only writes under `~/Games` and `~/Emulation/roms`. The
hub serves game files only through links signed per game with a random key and an expiry
(`/api/p/games/dl/`, the one plugin route reachable without the app login); only files that are
part of a scanned game can be fetched, symlinks are refused.

## Tests

```sh
python tests/test_games.py     # scan, VDF, agent safety, full Casa → agent → files flow, auth
```
