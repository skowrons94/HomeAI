"""Release-name parsing and Italian-first ranking.

``parse(name)`` pulls quality/language/episode info out of a torrent name;
``rank(releases, want, profile)`` scores them. Italian audio beats English,
English beats everything else (hidden by default); inside a language the
preferred resolution, a sane size and many seeders win. CAM/TS rips, dead
torrents and releases of a *different* title are rejected with a reason.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------- parsing

LANG_TOKENS = {
    "it": {"ita", "italian", "italiano", "itl", "iTALiAN".lower()},
    "en": {"eng", "english", "en"},
    "fr": {"fre", "french", "vff", "vfq", "truefrench", "francais", "vf", "vf2"},
    "es": {"spa", "spanish", "esp", "castellano", "latino", "espanol"},
    "de": {"ger", "german", "deutsch"},
    "ru": {"rus", "russian"},
    "uk": {"ukr", "ukrainian"},
    "ko": {"kor", "korean"},
    "ja": {"jap", "jpn", "japanese"},
    "hi": {"hin", "hindi"},
    "pt": {"por", "portuguese", "dublado"},
    "pl": {"pol", "polish", "pl"},
}
TOKEN_LANG = {t: lang for lang, toks in LANG_TOKENS.items() for t in toks}
# release groups that only publish Italian audio
ITALIAN_GROUPS = {"mircrew", "bamax71", "v3sp4ev3r", "ccbr", "icv", "nahom", "dlmux",
                  "gaiage", "lepanto", "darksidemux", "nkmux", "pir8", "eurogroup"}
SUB_WORDS = {"sub", "subs", "subbed", "subita", "sottotitoli", "softsub", "sottotitolato"}

FLAG_LANG = {"gb": "en", "us": "en", "it": "it", "fr": "fr", "es": "es", "mx": "es",
             "de": "de", "ru": "ru", "ua": "uk", "kr": "ko", "jp": "ja", "in": "hi",
             "pt": "pt", "br": "pt", "pl": "pl", "cn": "zh", "tw": "zh", "nl": "nl"}

BAD_SOURCES = {"cam", "camrip", "hdcam", "ts", "hdts", "telesync", "tc", "telecine",
               "scr", "screener", "dvdscr", "bdscr", "hdtc", "workprint", "pdvd", "predvd"}

SE_RE = [
    # S03E01, S03E01E02, S03E01-E03, S03E01-03
    re.compile(r"\bs(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e(\d{1,3})|-(\d{1,3}))?\b", re.I),
    # 3x01, 3x01-03
    re.compile(r"\b(\d{1,2})x(\d{2,3})(?:-(\d{2,3}))?\b", re.I),
]
SEASON_RE = [
    re.compile(r"\bs(\d{1,2})(?:[ ._-]?-[ ._-]?s?(\d{1,2}))?\b", re.I),       # S03, S01-S03
    re.compile(r"\b(?:season|stagione|stagioni|seasons)[ ._-]?(\d{1,2})(?:[ ._-]?-[ ._-]?(\d{1,2}))?\b", re.I),
]
YEAR_RE = re.compile(r"(?<![\d])(19[2-9]\d|20[0-4]\d)(?![\d])")
RES_RE = re.compile(r"\b(2160p|4k|uhd|1080p|1080i|fullhd|fhd|720p|576p|480p|360p)\b", re.I)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s: str) -> str:
    """'L'amica geniale: Storia!' -> 'l amica geniale storia'"""
    s = strip_accents(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).replace("_", " ")
    return " ".join(s.split())


def tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", strip_accents(name).lower()) if t]


def flag_langs(text: str) -> set[str]:
    """Regional-indicator flag emojis -> language codes."""
    out = set()
    chars = [c for c in text if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    for a, b in zip(chars[::2], chars[1::2]):
        cc = chr(ord(a) - 0x1F1E6 + 97) + chr(ord(b) - 0x1F1E6 + 97)
        if cc in FLAG_LANG:
            out.add(FLAG_LANG[cc])
    return out


@dataclass
class Parsed:
    name: str
    head: str = ""              # normalized title part (before year / SxxEyy / quality)
    year: int | None = None
    resolution: int | None = None
    source: str = ""            # remux / bluray / web-dl / webrip / hdtv / dvd / cam
    codec: str = ""
    hdr: bool = False
    dv: bool = False
    audio: set = field(default_factory=set)   # language codes of audio tracks
    subs: set = field(default_factory=set)
    multi: bool = False
    season: int | None = None
    season_end: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    complete: bool = False

    @property
    def pack(self) -> bool:
        return self.season is not None and self.episode is None or (
            self.episode is not None and self.episode_end is not None)


def parse(name: str) -> Parsed:
    p = Parsed(name=name)
    toks = tokens(name)
    tset = set(toks)

    # --- episode / season
    cut = len(name)
    for rx in SE_RE:
        m = rx.search(name)
        if m:
            p.season, p.episode = int(m.group(1)), int(m.group(2))
            end = next((g for g in m.groups()[2:] if g), None)
            if end and int(end) > p.episode:
                p.episode_end = int(end)
            cut = min(cut, m.start())
            break
    if p.season is None:
        for rx in SEASON_RE:
            m = rx.search(name)
            if m:
                p.season = int(m.group(1))
                if m.group(2) and int(m.group(2)) > p.season:
                    p.season_end = int(m.group(2))
                cut = min(cut, m.start())
                break
    p.complete = bool(tset & {"complete", "completa", "completo", "integrale"})

    # --- year: the last plausible year before the quality/episode part
    for m in YEAR_RE.finditer(name):
        if m.start() > 0 and (m.start() <= cut or p.year is None):
            p.year = int(m.group(1))
            cut = min(cut, m.start())
            break

    # --- resolution
    m = RES_RE.search(name)
    if m:
        r = m.group(1).lower()
        p.resolution = {"4k": 2160, "uhd": 2160, "fullhd": 1080, "fhd": 1080,
                        "1080i": 1080}.get(r) or int(r.rstrip("p"))
        cut = min(cut, m.start())

    # --- source
    low = name.lower()
    if tset & BAD_SOURCES or re.search(r"\bhd-?(cam|ts|tc)\b|\btele-?sync\b", low):
        p.source = "cam"
    elif "remux" in tset or "bdremux" in tset:
        p.source = "remux"
    elif tset & {"bluray", "blu", "bdrip", "brrip", "bdmux", "bd", "bdrip"} or "blu-ray" in low:
        p.source = "bluray"
    elif re.search(r"web-?dl|webmux|dlmux", low):
        p.source = "web-dl"
    elif tset & {"webrip", "web"} :
        p.source = "webrip"
    elif tset & {"hdtv", "pdtv", "satrip", "dtt", "hdtvrip"}:
        p.source = "hdtv"
    elif tset & {"dvdrip", "dvd", "dvd5", "dvd9", "xvid"}:
        p.source = "dvd"
    if p.resolution is None and p.source == "dvd":
        p.resolution = 480

    if tset & {"x265", "h265", "hevc"}:
        p.codec = "x265"
    elif tset & {"av1"}:
        p.codec = "av1"
    elif tset & {"x264", "h264", "avc"}:
        p.codec = "x264"
    p.hdr = bool(tset & {"hdr", "hdr10", "hdr10plus"}) or "hdr10+" in low
    p.dv = bool(tset & {"dv", "dovi"}) or "dolby vision" in low or "dolby.vision" in low

    # --- languages: a language right after "sub"/"subs" is a subtitle
    prev = ""
    for i, t in enumerate(toks):
        lang = TOKEN_LANG.get(t)
        if t == "subita" or (t == "ita" and prev in SUB_WORDS):
            p.subs.add("it")
        elif t.startswith("sub") and t[3:] in TOKEN_LANG:   # subeng
            p.subs.add(TOKEN_LANG[t[3:]])
        elif lang and prev in SUB_WORDS:
            p.subs.add(lang)
        elif lang:
            # "en"/"pl"/"vf" are too ambiguous unless near other language words
            if t in {"en", "pl", "vf"} and not (TOKEN_LANG.get(prev) or TOKEN_LANG.get(
                    toks[i + 1] if i + 1 < len(toks) else "")):
                pass
            else:
                p.audio.add(lang)
        prev = t if t not in {"and", "e"} else prev
    if tset & ITALIAN_GROUPS:
        p.audio.add("it")
    p.multi = bool(tset & {"multi", "multisub", "dual"}) or "multi audio" in low
    p.head = normalize(name[:cut])
    return p


# ---------------------------------------------------------------- ranking

@dataclass
class Want:
    """What we are looking for."""
    titles: list[str]                 # localized + original + english titles
    year: int | None = None
    type: str = "movie"               # movie | series
    season: int | None = None
    episode: int | None = None        # None + season set = whole season wanted
    original_language: str = "en"


@dataclass
class Profile:
    languages: list[str] = field(default_factory=lambda: ["it", "en"])
    quality: int = 1080
    allow_4k: bool = True
    max_movie_gb: float = 20
    max_episode_gb: float = 4
    show_all: bool = False


ARTICLES = {"the", "a", "an", "il", "lo", "la", "i", "gli", "le", "l"}


def _variants(t: str) -> set[str]:
    n = normalize(t)
    out = {n}
    words = n.split()
    if words and words[0] in ARTICLES:
        out.add(" ".join(words[1:]))
    out.add(n.replace(" and ", " "))
    return {v for v in out if v}


def title_matches(p: Parsed, want: Want) -> bool:
    head = p.head
    if not head:
        return False
    heads = {head, " ".join(w for w in head.split() if w not in ARTICLES)}
    # "Дюна / Dune" style: any slash-separated chunk may be the title
    heads |= {normalize(c) for c in re.split(r"/|\|", p.name[: len(p.name)]) if c.strip()}
    year_ok = want.year is None or p.year is None or abs(p.year - want.year) <= 1
    for t in want.titles:
        for v in _variants(t):
            for hd in heads:
                if hd == v and year_ok:
                    return True
                # extra words are fine only when the year confirms the match
                if p.year and want.year and abs(p.year - want.year) <= 1 and \
                        re.search(rf"(^| ){re.escape(v)}( |$)", hd):
                    return True
            # series: the year is often missing, require an exact head
            if want.type == "series" and head in _variants(t):
                return True
    return False


def episode_matches(p: Parsed, want: Want) -> tuple[bool, str]:
    """Does this release contain the wanted episode / season?"""
    if want.type != "series" or want.season is None:
        return True, ""
    if p.season is None:
        return (p.complete, "complete series") if p.complete else (False, "no season info")
    s_end = p.season_end or p.season
    if not (p.season <= want.season <= s_end):
        return False, f"season {p.season}"
    if want.episode is None:  # whole season wanted
        if p.episode is None:
            return True, "season pack"
        return False, "single episode"
    if p.episode is None:
        return True, "season pack"
    e_end = p.episode_end or p.episode
    if p.episode <= want.episode <= e_end and p.season == s_end:
        return True, "" if e_end == p.episode else "multi-episode"
    return False, f"episode {p.episode}"


def score(r: dict, want: Want, profile: Profile, trusted: bool = False) -> dict:
    """Annotate a release dict (name, size, seeders, flags...) with
    parsed info, ``score`` and ``rejected`` (reason or None)."""
    whole_season = want.type == "series" and want.season is not None and want.episode is None
    # Torrentio tags pack results with one episode's file; for a whole season judge the pack itself
    p = parse(r["name"] if whole_season else (r.get("match_name") or r["name"]))
    flags = flag_langs(r.get("flags", ""))
    subs = set(p.subs)
    if len(flags) > 4:
        # long flag lists come from subtitle tracks (streaming WEB-DLs), not dubs
        subs |= flags
        flags = set()
    audio = set(p.audio) | flags
    if not audio:
        # untagged scene releases are in the original language
        audio = {want.original_language or "en"}
    if p.multi and not (audio - {want.original_language}):
        audio.add("en")
    reasons, rejected = [], None
    gb = (r.get("size") or 0) / 1e9
    seeders = r.get("seeders") or 0

    if not trusted and not title_matches(p, want):
        rejected = "different title"
    ok, why = episode_matches(p, want)
    if not ok:
        rejected = rejected or f"wrong {why}"
    elif why:
        reasons.append(why)
    if p.source == "cam":
        rejected = rejected or "cam/telesync"
    if seeders <= 0:
        rejected = rejected or "no seeders"
    if p.resolution == 2160 and not profile.allow_4k:
        rejected = rejected or "4K not allowed"

    # size limit (packs scale with episode count)
    if want.type == "series":
        n_eps = 1
        if p.episode is None and p.season is not None:
            n_eps = 10 * ((p.season_end or p.season) - p.season + 1)
        elif p.episode_end:
            n_eps = p.episode_end - p.episode + 1
        limit = profile.max_episode_gb * n_eps
    else:
        limit = profile.max_movie_gb
    if gb and gb > limit * 1.15:
        rejected = rejected or f"too big ({gb:.1f} GB)"

    s = 0.0
    lang = next((l for l in profile.languages if l in audio), None)
    if lang is None:
        if not profile.show_all:
            rejected = rejected or "language: " + ",".join(sorted(audio))
        s -= 1000
    else:
        idx = profile.languages.index(lang)
        s += 1000 - idx * 500
        reasons.append({"it": "🇮🇹 Italian audio", "en": "🇬🇧 English audio"}.get(lang, lang))
    if lang != "it" and "it" in subs:
        s += 30
        reasons.append("Italian subs")

    res = p.resolution or 0
    if res == profile.quality:
        s += 200
    elif res == 2160:
        s += 160 if profile.allow_4k else 0
    elif res == 1080:
        s += 170
    elif res == 720:
        s += 110
    elif res:
        s += 30
    s += {"remux": 20, "bluray": 45, "web-dl": 40, "webrip": 30, "hdtv": 10, "dvd": 5}.get(p.source, 0)
    if p.codec == "x265":
        s += 8
    if p.source == "remux" and gb > limit * 0.7:
        s -= 40
    s += min(120, 45 * math.log10(1 + seeders))
    if seeders < 3:
        s -= 40
    if want.type == "series" and want.episode is None and p.episode is None:
        s += 40  # whole season wanted and this is a pack
    if want.type == "series" and want.episode is not None and p.episode is None:
        s -= 60  # one episode wanted: prefer the single file to a big pack

    return {
        **r,
        "audio": sorted(audio, key=lambda l: (l not in profile.languages,
                                              profile.languages.index(l) if l in profile.languages else 0, l)),
        "subs": sorted(subs),
        "language": lang,
        "resolution": p.resolution,
        "quality": " ".join(x for x in [f"{p.resolution}p" if p.resolution else "",
                                         p.source, p.codec, "DV" if p.dv else "",
                                         "HDR" if p.hdr else ""] if x),
        "season": p.season,
        "episode": p.episode,
        "episode_end": p.episode_end,
        "pack": p.episode is None and p.season is not None,
        "score": round(s, 1),
        "rejected": rejected,
        "reasons": reasons,
    }


def rank(releases: list[dict], want: Want, profile: Profile) -> list[dict]:
    """Score releases (each: name, hash, size, seeders, source, flags?, trusted?)
    and sort: accepted first by score, then rejected ones."""
    scored = [score(r, want, profile, trusted=r.get("trusted", False)) for r in releases]
    scored.sort(key=lambda r: (r["rejected"] is not None, -r["score"]))
    return scored
