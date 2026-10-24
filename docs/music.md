# Music

Your library is **Navidrome** (Subsonic API); Casa adds discovery, downloads and playback on the house speakers.

```toml
[plugins.music]
music_dir = "/mnt/media/music"      # the folder Navidrome reads
default_output = ""                 # name of an AirPlay device; empty = this phone
[plugins.music.navidrome]
url = "http://127.0.0.1:4533"
user = "…"
password = "…"
```

Everything is editable in the app (Music → gear). Page: **Library | Discover | Downloads** and a mini-player bar.

## Library
`getAlbumList2`, `getArtists`, `getArtist`, `getAlbum`, `search3`, `getCoverArt`, `stream`, `startScan` with token auth
(`t = md5(password + salt)`). The browser never sees the credentials: covers come from `GET /api/p/music/cover/{id}`,
audio from `GET /api/p/music/stream/{id}?suffix=` (the hub proxies Navidrome, Range supported; formats iOS cannot play
are transcoded to mp3 320).

## Discover (charts, new releases, Deezer, Apple Music)
The default view of the Discover tab: rails "Top in Italia" and "Nuove uscite" (Apple Music RSS, Italy), "Album / Artisti /
Brani del momento" (Deezer charts) and a genre chip row (Deezer `/chart/<genre>`). Album and artist sheets (Deezer, no API
key) show the tracklist with **30 s previews**, label, genres, related artists, and mark what Navidrome already has
(`hub/plugins/music/discover.py`; in-memory cache: charts 3 h, details 24 h, preview-bearing data 10 min because the signed
preview URLs expire; a failing source is reported per rail and stale data is served when possible).
`GET /api/p/music/discover?rails=a,b`, `discover/genres`, `discover/genre/{id}`, `discover/album/{id}`,
`discover/artist/{id}`, `discover/search?q=`, `discover/resolve?artist=&title=`.

## Discover search (MusicBrainz + Cover Art Archive)
Artist/album search, discography (studio albums first), tracklist of a release, covers from coverartarchive.org.
Requests are serialised at one per second with the `Casa/1.0` User-Agent; answers are cached in the store
(`music.mb.*`, 7 days) and covers in `data/music-art` (capped). An album is **owned** when Navidrome has an album by the
same artist with the same normalised title (edition tags like "(Deluxe)" are ignored).

## Download
"Search trackers" queries the enabled trackers of the Torrents plugin with `category="music"` (Torznab `cat=3000`),
`<artist> <album>` or `<artist> discography`. A download goes to Transmission (through the Media plugin) in
`<media download_dir or music_dir>/.incoming`, is tracked in the store key `music.downloads`, and when finished:

1. the torrent is removed from Transmission (data stays);
2. audio (flac, mp3, m4a, ogg, opus, wav, aiff, alac, ape, wv) and `cover.jpg`/`folder.jpg` move to
   `<music_dir>/<Artist>/<Album (year)>/` with their file names (discographies: one folder per album; CD1/CD2 kept);
3. zips are unpacked (zip-slip and zip-bomb safe), everything else is ignored, the leftovers inside `.incoming` are
   deleted, Navidrome rescans.

Nothing is written or deleted outside `music_dir` and the incoming folder. For the Torrents page:
`await plugins["music"].accept_download(result, guess) -> {ok, hash}` and `plugins["music"].downloads_listing()`.

## Play
Outputs: **This phone** (an `<audio>` element) and **AirPlay devices** found with pyatv (RAOP): the KEF LSX, the Apple
TV, HomePods (`airplay.devices` lists IPs to add when discovery misses one). The hub keeps one queue per output, streams
one track at a time (tracks are fetched from Navidrome as mp3 320 into `data/music-tmp`, the next one is prefetched) and
advances when a track ends. Pause stops the stream and resumes from the same point (best effort, mp3).

* **KEF**: when the `kef` plugin runs, playing turns it on, switches it to Wifi and sets its volume through that
  plugin; Music never opens a second connection to the speaker.
* **Pairing**: devices that need it show "Needs pairing" in the output list (PIN flow, credentials in the store key
  `music.outputs`), or a password. For the Apple TV, the AirPlay credentials of the Apple TV plugin are reused.
* The Home page gets an entity `music.player` (media_player) for the output that played last.
