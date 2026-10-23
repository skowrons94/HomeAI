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
| `installer` (setup.exe) | `~/Games/_installers/<Title>/` | status "needs a manual step": run it with Proton |

Downloads resume after a Wi-Fi drop, are size-checked and refuse to start without free space.
**Update** shows when the version in the library differs from the installed one. **Remove** deletes
the installed files (never anything else; save files left in the folder keep it alive).

### Why "Apply to Steam" is a separate button
Steam only reads `shortcuts.vdf` while it is **not running**, so the agent can't add shortcuts behind
Steam's back. Installs queue them; *Apply to Steam* closes Steam, writes all queued shortcuts, runs
Steam ROM Manager if the agent finds its AppImage (`--srm PATH` to point at it) and reopens Steam.
Do this in Desktop Mode. Without a Steam ROM Manager the ROM is still in the right folder.

## Known limits

- Proton: a new Windows shortcut still needs a compatibility tool picked once
  (Steam → game → Properties → Compatibility), or set it with ProtonUp-Qt / Steam ROM Manager.
- Installers are downloaded, not run (silent switches differ per installer; some need clicks).
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
