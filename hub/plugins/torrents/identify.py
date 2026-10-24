"""What is this torrent? Guess film / series / music / game from its name, so one Download button
can hand it to the plugin that files it in the right library folder (see docs/torrents.md).

Only the name is used: it is all a search result carries. The guess is a starting point; the
page lets the user change the kind and the details before anything is downloaded.
"""

from __future__ import annotations

import re

from ..media.ranking import normalize, parse

KINDS = ("movie", "series", "music", "game")

# platform words in game release names -> games plugin platform (EmuDeck folder names, "pc")
GAME_PLATFORMS = [
    (r"\b(nsw|switch|nsp|xci)\b", "switch"), (r"\bps2\b", "ps2"), (r"\b(ps1|psx|psone)\b", "psx"),
    (r"\bpsp\b", "psp"), (r"\b(ps ?vita|psvita)\b", "psvita"), (r"\b3ds\b", "n3ds"), (r"\bnds\b", "nds"),
    (r"\bgba\b", "gba"), (r"\b(gamecube|ngc)\b", "gc"), (r"\bwii ?u\b", "wiiu"), (r"\bwii\b", "wii"),
    (r"\bn64\b", "n64"), (r"\bsnes\b", "snes"), (r"\bnes\b", "nes"), (r"\b(genesis|mega ?drive)\b", "genesis"),
    (r"\bdreamcast\b", "dreamcast"),
]
GAME_RE = re.compile(
    r"\b(repack|fitgirl|dodi|elamigos|kaoskrew|gog|codex|skidrow|plaza|rune|tenoke|empress|razor1911|flt|"
    r"steamrip|p2p game|pc game|game of the year|goty|update v?\d|build \d{4,}|multi\d{1,2}|"
    r"nsw|nsp|xci|rom|roms|ps2|psp|psx|gba|nds|3ds|snes|n64|gamecube|wii)\b", re.I)
MUSIC_RE = re.compile(
    r"\b(flac|mp3|aac|alac|ogg|opus|320 ?kbps|256 ?kbps|v0|24 ?bit|16 ?bit|44[.,]1 ?khz|96 ?khz|"
    r"discography|discografia|album|vinyl|lp|ep|single|ost|soundtrack|lossless|hi-?res)\b", re.I)
VIDEO_RE = re.compile(r"\b(2160p|1080p|720p|480p|x264|x265|h\.?264|h\.?265|hevc|web-?dl|webrip|bluray|"
                      r"bdrip|hdtv|dvdrip|remux|hdr|dv)\b", re.I)


def identify(title: str) -> dict:
    """-> {kind, confidence (0-1), guess: {...}} for a release name."""
    name = " ".join((title or "").replace("_", " ").split())
    name = re.sub(r"^(\[[^\]]*\]\s*)+", "", name) or name   # "[Group] Show ..." -> "Show ..."
    p = parse(name)
    video = bool(VIDEO_RE.search(name))
    music = len(MUSIC_RE.findall(name))
    game = bool(GAME_RE.search(name))

    if p.season is not None:
        return {"kind": "series", "confidence": 0.9,
                "guess": {"query": p.head, "season": p.season, "episode": p.episode, "year": p.year}}
    ep_range = re.search(r"(?<!\d)\d{2,4}\s*[-~]\s*\d{2,4}(?!\d)", name)
    if (p.complete or ep_range) and not p.year and not music:   # "Show Complete (001-500)"
        head = re.split(r"\b(complete|completa|integrale)\b|[\[(]", name, flags=re.I)[0]
        return {"kind": "series", "confidence": 0.6,
                "guess": {"query": normalize(head), "season": None, "episode": None, "year": None}}
    if game and not video and music < 2:
        platform = next((plat for rx, plat in GAME_PLATFORMS if re.search(rx, name, re.I)), "pc")
        return {"kind": "game", "confidence": 0.8, "guess": {"title": _game_title(name), "platform": platform}}
    if music and not video:
        return {"kind": "music", "confidence": 0.8 if music > 1 else 0.6, "guess": _music_guess(name)}
    if video or p.year:
        return {"kind": "movie", "confidence": 0.8 if video and p.year else 0.6,
                "guess": {"query": p.head, "year": p.year}}
    return {"kind": "movie", "confidence": 0.2, "guess": {"query": normalize(name), "year": None}}


def _game_title(name: str) -> str:
    t = re.split(r"[\[(]|\b(v\d[\d.]*|build \d+|repack|multi\d+|update|goty|nsw|nsp|xci)\b|[-.]\s*(fitgirl|dodi|"
                 r"elamigos|gog|codex|skidrow|plaza|rune|tenoke|empress|flt)", name, flags=re.I)[0]
    return " ".join(t.replace(".", " ").split()).strip(" -") or name


def _music_guess(name: str) -> dict:
    """'Pink Floyd - The Wall (1979) [FLAC]' -> artist, album, year."""
    year = None
    m = re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", name)
    if m:
        year = int(m.group(1))
    core = re.split(r"[\[(]|\b(flac|mp3|aac|alac|320|256|v0|24 ?bit|16 ?bit|web|cd|vinyl|lossless)\b", name,
                    flags=re.I)[0]
    core = re.sub(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", " ", core)
    parts = [x.strip(" -.") for x in re.split(r"\s+[-–]\s+", core) if x.strip(" -.")]
    discog = bool(re.search(r"\b(discography|discografia)\b", name, re.I))
    if discog:
        artist = re.split(r"\b(discography|discografia)\b", core, flags=re.I)[0].strip(" -.")
        return {"artist": artist or (parts[0] if parts else ""), "album": "", "year": None, "discography": True}
    return {"artist": parts[0] if parts else "", "album": parts[1] if len(parts) > 1 else "",
            "year": year, "discography": False}
