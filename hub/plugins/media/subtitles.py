"""Automatic subtitles: find, validate and re-sync a subtitle in the user's first language.

When a release is imported and its audio lacks the wanted language (profile.languages[0]) and no
subtitle in that language sits next to the video, we ask OpenSubtitles / SubDL (free API keys),
rank the candidates, and for each of the best three:

    decode -> parse SRT -> language check -> duration check -> timing check against a reference
    (sidecar subtitle in another language > embedded text stream > speech sampled from the audio)

The timing fit is one global line t' = a*t + b (a from the usual fps ratios). A subtitle whose
best fit is still poor belongs to another cut and is rejected. The result is written next to the
video as ``<stem>.<lang>.srt`` (what Infuse picks up). Videos without a result are retried every
6 hours for 14 days.

Per-video state lives in kv ``media.subs.<sha1(path)>``:
    {path, status, provider, release, score, offset, scale, checked_at, message}
status: searching | found | in_sync | adapted | unverified | not_found | error | skipped
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import struct
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from .downloads import LANG3, _is_within
from .library import VIDEO_EXT, _videos
from .ranking import parse
from ...i18n import _

log = logging.getLogger("hub.media.subtitles")

UA = "Casa v1"
TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_ZIP = 5 * 1024 * 1024
MAX_TRIES = 3
RETRY_EVERY = 6 * 3600
RETRY_DAYS = 14
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
OK_STATUS = ("found", "in_sync", "adapted", "unverified")

SCALES = [1.0, 25 / 23.976, 23.976 / 25, 24 / 25, 25 / 24, 24 / 23.976, 23.976 / 24]
MAX_SHIFT = 180.0
BIN = 0.2
THRESH_SUB = 0.40      # normalised overlap score needed against a subtitle reference
THRESH_AUDIO = 0.30    # ... against sampled speech
WIN_LEN = 180
WIN_COUNT = 6
WIN_AGREE = 0.3        # windows agree when their offsets are within this many seconds
EMBEDDED_MAX_BYTES = 4 * 1024 ** 3

# --------------------------------------------------------------------------- text

STOP = {
    "it": "il lo la le gli un una di del della che non per con sono ho hai ha mi ti ci si ma più come "
          "anche questo quello cosa io tu noi voi lei lui è e da in al nel sì no perché quando dove "
          "ora qui essere stato fare molto tutto tutti niente solo".split(),
    "en": "the a an and of to in is you that it he was for on are with as his they be at have this "
          "from or had by not but what all were we when your can said there use each which she do "
          "how if will up about out them then so no i me my just don't it's".split(),
    "es": "el la los las un una de del que y en es por con para se lo me te mi pero su al como más "
          "muy yo tú qué está esto eso ya bien aquí cuando donde nada todo todos son soy eres tiene "
          "hay también porque".split(),
    "fr": "le la les un une des du de et est en que qui pas pour dans ce il elle je tu nous vous "
          "ils sur avec ne se au aux mais ou où si très plus comme tout tous suis es sont être "
          "avoir fait c'est j'ai n'est".split(),
    "de": "der die das ein eine und ist nicht ich du er sie es wir ihr zu den dem des mit auf für "
          "von im in dass ja nein was wie wo auch aber oder wenn noch nur schon sind bin hat haben "
          "wird kann mir dir mich dich".split(),
    "pt": "o a os as um uma de do da dos das que e em é por com para se me te meu minha mas não "
          "mais muito eu tu você ele ela nós está isso isto já bem aqui quando onde nada tudo são "
          "sou tem também porque".split(),
}
STOP = {k: set(v) for k, v in STOP.items()}
WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.U)


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


TIME_RE = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    """Tolerant SRT parser -> [(start, end, text)] sorted by start."""
    lines = text.lstrip("﻿").split("\n")
    cues: list[list] = []
    cur = None
    for i, line in enumerate(lines):
        m = TIME_RE.search(line)
        if m:
            g = m.groups()
            cur = [_secs(*g[:4]), _secs(*g[4:]), []]
            cues.append(cur)
            continue
        s = line.strip()
        if cur is None or not s:
            continue
        if s.isdigit():  # the index line of the next cue
            nxt = next((x for x in lines[i + 1:i + 4] if x.strip()), "")
            if TIME_RE.search(nxt):
                continue
        cur[2].append(s)
    out = [(a, b, "\n".join(t)) for a, b, t in cues if b >= a and t]
    out.sort(key=lambda c: c[0])
    return out


def format_srt(cues) -> str:
    def ts(t):
        t = max(0.0, t)
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1))


def clean_text(t: str) -> str:
    return re.sub(r"\{[^}]*\}|<[^>]+>", " ", t)


def language_scores(text: str) -> dict[str, float]:
    words = [w.lower() for w in WORD_RE.findall(clean_text(text))]
    if not words:
        return {k: 0.0 for k in STOP}
    return {k: sum(1 for w in words if w in v) / len(words) for k, v in STOP.items()}


def language_ok(text: str, lang: str) -> bool:
    """True when the text is clearly `lang` (unknown languages are not checked)."""
    if lang not in STOP:
        return True
    sc = language_scores(text)
    mine = sc[lang]
    others = max((v for k, v in sc.items() if k != lang), default=0)
    return mine >= 0.10 and mine >= 1.3 * others


def osdb_hash(path: str) -> str:
    """OpenSubtitles hash: size + 64-bit sum of the first and last 64 KiB."""
    size = os.path.getsize(path)
    if size < 131072:
        return ""
    h = size
    with open(path, "rb") as f:
        for off in (0, size - 65536):
            f.seek(off)
            for (v,) in struct.iter_unpack("<Q", f.read(65536)):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def extract_srt(data: bytes, season=None, episode=None) -> bytes | None:
    """Raw .srt bytes out of a download that may be a zip (size capped)."""
    if data[:2] != b"PK":
        return data if len(data) <= MAX_ZIP else None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    members = [i for i in zf.infolist() if i.filename.lower().endswith(".srt") and not i.is_dir()
               and 0 < i.file_size <= MAX_ZIP]
    if not members:
        return None
    pick = members[0]
    if len(members) > 1:
        if season is not None and episode is not None:
            rx = re.compile(rf"s0*{season}[ ._-]*e0*{episode}(?!\d)|\b0*{season}x0*{episode}(?!\d)", re.I)
            hit = [i for i in members if rx.search(i.filename)]
            if hit:
                members = hit
        pick = max(members, key=lambda i: i.file_size)
    return zf.read(pick)


# --------------------------------------------------------------------------- alignment

class Ref:
    """Reference activity: merged intervals with cumulative coverage, onsets, and the region it covers."""

    def __init__(self, intervals, lo=None, hi=None):
        iv = sorted((a, b) for a, b in intervals if b > a)
        self.onsets = sorted(a for a, _ in iv)
        merged: list[list[float]] = []
        for a, b in iv:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.starts = [m[0] for m in merged]
        self.ends = [m[1] for m in merged]
        self.cum, tot = [], 0.0
        for a, b in merged:
            self.cum.append(tot)
            tot += b - a
        self.lo = lo if lo is not None else (self.starts[0] if self.starts else 0.0)
        self.hi = hi if hi is not None else (self.ends[-1] if self.ends else 0.0)

    def cov(self, t: float) -> float:
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return 0.0
        return self.cum[i] + min(t, self.ends[i]) - self.starts[i]

    def density(self) -> float:
        span = self.hi - self.lo
        return (self.cov(self.hi) - self.cov(self.lo)) / span if span > 0 else 0.0


def overlap_score(cues, a: float, b: float, ref: Ref, min_dur: float = 20.0) -> tuple[float, int]:
    """Cue-duration fraction on reference activity, normalised against chance (0 = random)."""
    tot = ov = 0.0
    n = 0
    for s, e, *_ in cues:
        s2, e2 = a * s + b, a * e + b
        if s2 < ref.lo or e2 > ref.hi or e2 <= s2:
            continue
        tot += e2 - s2
        ov += ref.cov(e2) - ref.cov(s2)
        n += 1
    d = ref.density()
    if tot < min_dur or d >= 0.95:
        return 0.0, n
    return max(0.0, (ov / tot - d) / (1 - d)), n


def best_offset(cues, a: float, ref: Ref, max_shift: float = MAX_SHIFT, npeaks: int = 3,
                min_dur: float = 20.0) -> tuple[float, float]:
    """For one scale: vote on b = onset - a*cue_start, refine the top peaks. -> (b, score)."""
    if not ref.onsets:
        return 0.0, 0.0
    votes: Counter = Counter()
    onsets = ref.onsets
    for s, *_ in cues:
        x = a * s
        if x < ref.lo - max_shift or x > ref.hi + max_shift:
            continue
        i = bisect.bisect_left(onsets, x - max_shift)
        j = bisect.bisect_right(onsets, x + max_shift)
        for o in onsets[i:j]:
            votes[int((o - x + max_shift) / BIN)] += 1
    if not votes:
        return 0.0, 0.0
    smooth = {k: votes.get(k - 1, 0) + votes[k] + votes.get(k + 1, 0) for k in votes}
    peaks: list[int] = []
    for k in sorted(smooth, key=smooth.get, reverse=True):
        if all(abs(k - p) > 3 for p in peaks):
            peaks.append(k)
        if len(peaks) >= npeaks:
            break
    best = (0.0, -1.0)
    for k in peaks:
        b0 = (k + 0.5) * BIN - max_shift
        for step in range(-8, 9):
            b = b0 + step * 0.05
            sc, _ = overlap_score(cues, a, b, ref, min_dur)
            if sc > best[1]:
                best = (b, sc)
    return best


def fit_single(cues, ref: Ref, thresh: float, scales=SCALES) -> dict:
    """Global fit against one reference region (a sidecar / embedded subtitle)."""
    ident, _ = overlap_score(cues, 1.0, 0.0, ref)
    best = (1.0, 0.0, -1.0)
    for a in scales:
        b, sc = best_offset(cues, a, ref)
        if sc > best[2] + 1e-9:
            best = (a, b, sc)
    return _decide(best, ident, thresh)


def _decide(best, ident, thresh, agree=None) -> dict:
    a, b, sc = best
    res = {"scale": a, "offset": b, "score": round(sc, 3), "identity": round(ident, 3)}
    if agree is not None:
        res["agree"] = agree
    if sc < thresh:
        res["status"] = "rejected"
    elif ident >= thresh and sc - ident < 0.1:
        res.update(status="in_sync", scale=1.0, offset=0.0, score=round(ident, 3))
    elif abs(a - 1) < 1e-6 and abs(b) < 0.15:
        res["status"] = "in_sync"
    else:
        res["status"] = "adapted"
    return res


def combine_windows(per_window: list[dict], thresh: float, tol: float = WIN_AGREE) -> tuple | None:
    """per_window: [{a: (b, score)}] (windows with enough data). Choose the (a, b) that most
    windows agree on. -> (a, b, agree, valid) or None when no windows had data."""
    valid = [w for w in per_window if w]
    if not valid:
        return None
    best = None
    for a in SCALES:
        good = [(w[a][0], w[a][1]) for w in valid if a in w and w[a][1] >= thresh]
        for b0, _ in good:
            grp = [(b, s) for b, s in good if abs(b - b0) <= tol]
            key = (len(grp), sum(s for _, s in grp) / len(grp))
            if best is None or key > best[0]:
                best = (key, a, sum(b for b, _ in grp) / len(grp))
    if best is None:
        return None
    return best[1], best[2], best[0][0], len(valid)


def fit_windows(cues, refs: list[Ref], thresh: float) -> dict:
    """Fit against speech sampled in separate windows; the model must hold in most of them."""
    per_window = []
    for ref in refs:
        # a loud score reads as wall-to-wall "speech" (0.9+ on a real film): no timing information
        if len(ref.onsets) < 8 or ref.density() >= 0.85:
            per_window.append({})
            continue
        w = {}
        for a in SCALES:
            w[a] = best_offset(cues, a, ref, min_dur=8.0)
        per_window.append(w)
    comb = combine_windows(per_window, thresh)
    if comb is None:
        return {"status": "rejected", "score": 0.0, "identity": 0.0, "scale": 1.0, "offset": 0.0}
    a, b, agree, valid = comb
    scores, ident = [], []
    for ref in refs:
        if len(ref.onsets) < 8:
            continue
        scores.append(overlap_score(cues, a, b, ref, 8.0)[0])
        ident.append(overlap_score(cues, 1.0, 0.0, ref, 8.0)[0])
    sc = sum(scores) / len(scores) if scores else 0.0
    idn = sum(ident) / len(ident) if ident else 0.0
    res = _decide((a, b, sc), idn, thresh, agree)
    if agree < 2 or agree < 0.5 * valid:
        res["status"] = "rejected"  # windows disagree: another cut
    return res


def window_starts(duration: float, n: int = WIN_COUNT, length: int = WIN_LEN) -> list[float]:
    lo, hi = duration * 0.05, duration * 0.95
    n = min(n, int((hi - lo) // length))
    if n < 1:
        return []
    if n == 1:
        return [lo]
    return [lo + i * (hi - lo - length) / (n - 1) for i in range(n)]


def speech_from_silence(stderr: str, length: float, offset: float = 0.0) -> list[tuple[float, float]]:
    """Parse ffmpeg silencedetect output -> speech intervals (shifted by `offset`)."""
    sil: list[list[float]] = []
    for line in stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            sil.append([max(0.0, float(m.group(1))), length])
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and sil:
            sil[-1][1] = float(m.group(1))
    out, t = [], 0.0
    for a, b in sil:
        if a - t >= 0.15:
            out.append((t + offset, a + offset))
        t = max(t, b)
    if length - t >= 0.15:
        out.append((t + offset, length + offset))
    return out


# --------------------------------------------------------------------------- ranking

SOURCES = {"webdl": "web", "web": "web", "webrip": "webrip", "bluray": "bluray", "brrip": "bluray",
           "bdrip": "bluray", "hdtv": "hdtv", "dvdrip": "dvd", "remux": "bluray"}


def _release_info(name: str) -> tuple[set, str, str]:
    n = re.sub(r"web[-. ]dl", "webdl", (name or "").lower())
    toks = {t for t in re.split(r"[^a-z0-9]+", n) if t}
    src = next((SOURCES[t] for t in toks if t in SOURCES), "")
    m = re.search(r"-([a-z0-9]+)(?:\.[a-z0-9]{2,4})?$", n.strip())
    return toks, src, m.group(1) if m else ""


def release_sim(a: str, b: str) -> float:
    """0..1: token overlap, with source tag and release group weighing most."""
    ta, sa, ga = _release_info(a)
    tb, sb, gb = _release_info(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    return 0.4 * jac + 0.3 * (1 if sa and sa == sb else 0) + 0.3 * (1 if ga and ga == gb else 0)


@dataclass
class Cand:
    provider: str
    id: str
    release: str = ""
    fps: float = 0.0
    hi: bool = False
    forced: bool = False
    downloads: int = 0
    hash_match: bool = False
    data: dict = field(default_factory=dict)
    score: float = 0.0


def rank_candidates(cands: list[Cand], release: str, fps: float = 0.0) -> list[Cand]:
    for c in cands:
        c.score = (1000 * c.hash_match + 100 * release_sim(c.release, release)
                   + (5 if fps and c.fps and abs(fps - c.fps) < 0.02 else 0)
                   + 3 * min(c.downloads, 10000) / 10000
                   - 400 * c.hi - 600 * c.forced)
    return sorted(cands, key=lambda c: -c.score)


@dataclass
class Video:
    path: str
    title: str = ""
    year: int | None = None
    imdb_id: str = ""
    season: int | None = None
    episode: int | None = None
    release: str = ""
    duration: float = 0.0
    fps: float = 0.0
    size: int = 0
    hash: str = ""

    def meta(self) -> dict:
        return {k: getattr(self, k) for k in ("path", "title", "year", "imdb_id", "season",
                                              "episode", "release")}


def sha(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()


# --------------------------------------------------------------------------- ffmpeg

def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


async def _run(cmd: list[str], timeout: float, want_stdout=False) -> tuple[int, bytes, str]:
    if shutil.which("nice"):
        cmd = ["nice", "-n", "15", *cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return proc.returncode, out, err.decode("utf-8", "replace")


async def probe(path: str) -> dict:
    """{duration, fps, audio: [codes], subs: [(rel_index, codec, lang)]}; {} without ffprobe."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        rc, out, _ = await _run(["ffprobe", "-v", "error", "-show_entries",
                                 "format=duration:stream=index,codec_type,codec_name,r_frame_rate"
                                 ":stream_tags=language", "-of", "json", path], 60)
        info = json.loads(out or b"{}")
    except Exception as err:
        log.debug("ffprobe %s: %s", path, err)
        return {}
    res = {"duration": 0.0, "fps": 0.0, "audio": [], "subs": []}
    with contextlib.suppress(KeyError, TypeError, ValueError):
        res["duration"] = float(info["format"]["duration"])
    n_sub = 0
    for s in info.get("streams", []):
        lang = (s.get("tags") or {}).get("language", "").lower()
        lang = LANG3.get(lang, lang)
        kind = s.get("codec_type")
        if kind == "video" and not res["fps"]:
            with contextlib.suppress(ValueError, ZeroDivisionError):
                a, _, b = s.get("r_frame_rate", "0/1").partition("/")
                res["fps"] = float(a) / float(b or 1)
        elif kind == "audio":
            res["audio"].append(lang)
        elif kind == "subtitle":
            if s.get("codec_name") in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"):
                res["subs"].append((n_sub, s["codec_name"], lang))
            n_sub += 1
    return res


async def extract_embedded(path: str, idx: int) -> str:
    rc, out, err = await _run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:s:{idx}",
                               "-f", "srt", "-"], 300, True)
    if rc != 0:
        raise RuntimeError(err.strip()[-200:])
    return decode_text(out)


async def sample_speech(path: str, duration: float, lock: asyncio.Lock) -> list[Ref]:
    """Speech activity in ~6 windows of 3 min (input seeking: only those parts are read)."""
    refs = []
    async with lock:
        for w in window_starts(duration):
            try:
                _, _, err = await _run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{w:.2f}", "-t", str(WIN_LEN), "-i",
                     path, "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-af",
                     "silencedetect=noise=-30dB:d=0.3", "-f", "null", "-"], 90)
            except asyncio.TimeoutError:
                log.debug("speech window at %.0fs timed out", w)
                continue
            refs.append(Ref(speech_from_silence(err, WIN_LEN, w), w, w + WIN_LEN))
    return refs


# --------------------------------------------------------------------------- providers

def _imdb_num(imdb: str) -> str:
    return re.sub(r"\D", "", imdb or "")


class Providers:
    """The three online sources; every call may raise, the caller moves on to the next provider."""

    def __init__(self, cfg: dict, store, lang: str):
        self.store = store
        self.lang = lang
        self.os_key = (cfg.get("opensubtitles_api_key") or "").strip()
        self.os_user = (cfg.get("opensubtitles_username") or "").strip()
        self.os_pass = cfg.get("opensubtitles_password") or ""
        self.subdl_key = (cfg.get("subdl_api_key") or "").strip()
        self.session: aiohttp.ClientSession | None = None
        self._token: str | None = None

    def configured(self) -> list[str]:
        return (["opensubtitles"] if self.os_key else []) + (["subdl"] if self.subdl_key else [])

    async def open(self):
        self.session = aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA})

    async def close(self):
        if self.session:
            await self.session.close()

    async def search(self, v: Video) -> list[Cand]:
        out: list[Cand] = []
        for name, fn in (("opensubtitles", self._os_search), ("subdl", self._subdl_search)):
            if name not in self.configured():
                continue
            try:
                got = await fn(v)
                log.debug("%s: %d candidates for %s", name, len(got), Path(v.path).name)
                out += got
            except Exception as err:
                log.warning("subtitles: %s search failed: %s", name, err)
        return out

    async def download(self, c: Cand) -> bytes:
        fn = {"opensubtitles": self._os_download, "subdl": self._subdl_download}[c.provider]
        return await fn(c)

    async def _get(self, url: str, **kw) -> bytes:
        async with self.session.get(url, **kw) as r:
            r.raise_for_status()
            return await r.read()

    # -- OpenSubtitles.com ---------------------------------------------------------

    OS = "https://api.opensubtitles.com/api/v1"

    def os_quota_left(self) -> bool:
        q = self.store.get("media.subs.quota.opensubtitles") or {}
        return time.time() >= q.get("until", 0)

    def _os_headers(self) -> dict:
        h = {"Api-Key": self.os_key, "Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _os_login(self):
        if not (self.os_user and self.os_pass):
            return
        async with self.session.post(f"{self.OS}/login", headers=self._os_headers(),
                                     json={"username": self.os_user, "password": self.os_pass}) as r:
            r.raise_for_status()
            self._token = (await r.json(content_type=None)).get("token")

    async def _os_json(self, method: str, path: str, **kw) -> dict:
        for attempt in (0, 1):
            if not self._token and self.os_user and self.os_pass:
                await self._os_login()
            async with self.session.request(method, f"{self.OS}{path}", headers=self._os_headers(),
                                            **kw) as r:
                if r.status == 401 and attempt == 0:
                    self._token = None
                    if self.os_user:
                        continue
                if r.status in (406, 429) and path == "/download":
                    self._os_exhausted(None)
                r.raise_for_status()
                return await r.json(content_type=None)
        return {}

    def _os_exhausted(self, reset: str | None):
        until = None
        if reset:
            with contextlib.suppress(ValueError):
                until = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        if not until or until < time.time():
            now = datetime.now(timezone.utc)
            until = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0).timestamp()
        self.store.set("media.subs.quota.opensubtitles", {"until": until})
        log.info("OpenSubtitles download quota used up; pausing until %s",
                 datetime.fromtimestamp(until).isoformat(timespec="minutes"))

    async def _os_search(self, v: Video) -> list[Cand]:
        params = {"languages": self.lang}
        if v.hash:
            params["moviehash"] = v.hash
        imdb = _imdb_num(v.imdb_id)
        if v.season is not None and v.episode is not None:
            params["season_number"], params["episode_number"] = v.season, v.episode
            if imdb:
                params["parent_imdb_id"] = imdb
        elif imdb:
            params["imdb_id"] = imdb
        if not imdb and v.title:
            params["query"] = v.title
            if v.year:
                params["year"] = v.year
        data = await self._os_json("GET", "/subtitles", params=dict(sorted(params.items())))
        out = []
        for it in data.get("data", []):
            a = it.get("attributes") or {}
            files = a.get("files") or []
            if not files:
                continue
            out.append(Cand("opensubtitles", str(files[0].get("file_id")),
                            release=a.get("release") or "", fps=float(a.get("fps") or 0),
                            hi=bool(a.get("hearing_impaired")), forced=bool(a.get("foreign_parts_only")),
                            downloads=int(a.get("download_count") or 0),
                            hash_match=bool(a.get("moviehash_match"))))
        return out

    async def _os_download(self, c: Cand) -> bytes:
        if not self.os_quota_left():
            raise RuntimeError(_("OpenSubtitles daily quota used up"))
        d = await self._os_json("POST", "/download", json={"file_id": int(c.id), "sub_format": "srt"})
        if d.get("remaining") == 0:
            self._os_exhausted(d.get("reset_time_utc"))
        if not d.get("link"):
            raise RuntimeError(d.get("message") or _("no download link"))
        return await self._get(d["link"])

    # -- SubDL ---------------------------------------------------------------------

    async def _subdl_search(self, v: Video) -> list[Cand]:
        imdb = _imdb_num(v.imdb_id)
        params = {"api_key": self.subdl_key, "languages": self.lang.upper(),
                  "type": "tv" if v.season is not None else "movie"}
        if imdb:
            params["imdb_id"] = f"tt{imdb}"
        elif v.title:
            params["film_name"] = v.title
            if v.year:
                params["year"] = v.year
        else:
            return []
        if v.season is not None:
            params["season_number"] = v.season
            if v.episode is not None:
                params["episode_number"] = v.episode
        data = json.loads(await self._get("https://api.subdl.com/api/v1/subtitles", params=params))
        if data.get("status") is False and data.get("error") != "no subtitles found":
            raise RuntimeError(data.get("message") or data.get("error") or "SubDL error")
        out = []
        for it in data.get("subtitles", []):
            if it.get("url"):
                out.append(Cand("subdl", it["url"], release=it.get("release_name") or it.get("name") or "",
                                hi=bool(it.get("hi"))))
        return out

    async def _subdl_download(self, c: Cand) -> bytes:
        return await self._get("https://dl.subdl.com" + c.id)


# --------------------------------------------------------------------------- manager

class Subtitles:
    def __init__(self, plugin, cfg: dict):
        self.plugin = plugin
        self.hub = plugin.hub
        self.store = plugin.hub.store
        c = cfg.get("subtitles") or {}
        self.enabled = bool(c.get("enabled", True))
        langs = cfg.get("languages") or ["it", "en"]
        self.lang = (langs[0] if langs else "it").lower()
        self.movies_dir = cfg.get("movies_dir", "")
        self.series_dir = cfg.get("series_dir", "")
        self.providers = Providers(c, self.store, self.lang)
        self._job_lock = asyncio.Lock()
        self._ffmpeg_lock = asyncio.Lock()
        self._running: set[str] = set()

    async def open(self):
        if self.enabled:
            await self.providers.open()

    async def close(self):
        await self.providers.close()

    def status(self) -> dict:
        return {"enabled": self.enabled, "language": self.lang,
                "providers": self.providers.configured() if self.enabled else [],
                "ffmpeg": have_ffmpeg()}

    # -- state ----------------------------------------------------------------------

    def state(self, path: str) -> dict | None:
        return self.store.get(f"media.subs.{sha(path)}")

    def _set(self, v: Video, **kw) -> dict:
        st = {**(self.state(v.path) or {}), "path": v.path, **kw}
        self.store.set(f"media.subs.{sha(v.path)}", st)
        return st

    def allowed(self, path: str) -> bool:
        return any(_is_within(path, r) for r in (self.movies_dir, self.series_dir))

    def videos_in(self, path: str) -> list[str]:
        if os.path.isdir(path):
            return sorted(p for p, _, _ in _videos(path, 3))
        return [path] if Path(path).suffix.lower() in VIDEO_EXT else []

    # -- entry points -----------------------------------------------------------------

    def wanted_audio(self, audio) -> bool:
        """True when the release audio is known and already has the wanted language."""
        return bool(audio) and self.lang in audio

    def on_imported(self, row: dict, targets: list[str]) -> None:
        if not self.enabled:
            return
        data = row.get("data") or {}
        if self.wanted_audio(data.get("audio")):
            return
        for t in targets:
            if Path(t).suffix.lower() not in VIDEO_EXT:
                continue
            p = parse(Path(t).name)
            v = Video(t, title=data.get("title") or row.get("title") or "",
                      year=row.get("year"), imdb_id=data.get("imdb_id") or "",
                      season=p.season if p.season is not None else row.get("season"),
                      episode=p.episode if p.episode is not None else row.get("episode"),
                      release=row.get("name") or "")
            self.spawn(v, check_audio=not data.get("audio"))

    def spawn(self, v: Video, check_audio=False, manual=False):
        if v.path in self._running:
            return
        self._running.add(v.path)
        self._set(v, status="searching", message="", checked_at=time.time())
        self.hub.spawn(self._guard(v, check_audio, manual), "subtitles")

    async def _guard(self, v: Video, check_audio: bool, manual: bool):
        try:
            async with self._job_lock:
                await self.fetch(v, check_audio=check_audio, manual=manual)
        except Exception as err:
            log.exception("subtitle job for %s failed", v.path)
            self._set(v, status="error", message=str(err), checked_at=time.time())
        finally:
            self._running.discard(v.path)

    # -- retries ----------------------------------------------------------------------

    def _pending(self) -> dict:
        return self.store.get("media.subs.pending") or {}

    def _add_pending(self, v: Video):
        p = self._pending()
        p.setdefault(sha(v.path), {**v.meta(), "since": time.time()})
        self.store.set("media.subs.pending", p)

    def _drop_pending(self, v: Video):
        p = self._pending()
        if p.pop(sha(v.path), None) is not None:
            self.store.set("media.subs.pending", p)

    async def retry(self):
        if not self.enabled:
            return
        for key, m in list(self._pending().items()):
            if time.time() - m.get("since", 0) > RETRY_DAYS * 86400 or not os.path.exists(m["path"]):
                p = self._pending()
                p.pop(key, None)
                self.store.set("media.subs.pending", p)
                continue
            v = Video(**{k: m.get(k) for k in ("path", "title", "year", "imdb_id", "season",
                                               "episode", "release")})
            v.title, v.imdb_id, v.release = v.title or "", v.imdb_id or "", v.release or ""
            if v.path in self._running:
                continue
            self._running.add(v.path)
            await self._guard(v, False, False)

    # -- the job ------------------------------------------------------------------------

    def target_path(self, v: Video) -> str:
        return str(Path(v.path).with_suffix("")) + f".{self.lang}.srt"

    def existing_sub(self, v: Video) -> str | None:
        p = Path(v.path)
        try:
            names = os.listdir(p.parent)
        except OSError:
            return None
        for n in sorted(names):
            if not n.startswith(p.stem + ".") or Path(n).suffix.lower() not in SUB_EXTS:
                continue
            tags = n[len(p.stem) + 1:].lower().rsplit(".", 1)[0].split(".")
            if self.lang in tags and "forced" not in tags:
                return str(p.parent / n)
        return None

    def other_sidecars(self, v: Video) -> list[str]:
        p = Path(v.path)
        out = []
        with contextlib.suppress(OSError):
            for n in sorted(os.listdir(p.parent)):
                if n.startswith(p.stem + ".") and n.lower().endswith(".srt"):
                    tags = n[len(p.stem) + 1:-4].lower().split(".")
                    if self.lang not in tags and "forced" not in tags:
                        out.append(str(p.parent / n))
        return sorted(out, key=lambda n: ".en." not in n.lower())

    async def fetch(self, v: Video, check_audio=False, manual=False) -> dict:
        now = time.time
        target = self.target_path(v)
        old = self.existing_sub(v)
        prev = self.state(v.path) or {}
        ours = bool(prev.get("written")) and old == target
        if old and not ours:
            return self._set(v, status="found", provider="", message=_("a subtitle already exists"),
                             checked_at=now(), written=False)
        info = await probe(v.path)
        v.duration, v.fps = info.get("duration", 0.0), info.get("fps", 0.0)
        if check_audio and not manual and self.lang in info.get("audio", []):
            self._drop_pending(v)
            return self._set(v, status="skipped", message=_("audio already has the language"),
                             checked_at=now())
        with contextlib.suppress(OSError):
            v.size = os.path.getsize(v.path)
            v.hash = await asyncio.to_thread(osdb_hash, v.path)
        self._set(v, status="searching", message="", checked_at=now())

        if not self.providers.configured():
            return self._miss(v, _("add an OpenSubtitles or SubDL API key in Settings → Media"))
        cands = rank_candidates(await self.providers.search(v), v.release or Path(v.path).stem, v.fps)
        if not cands:
            return self._miss(v, _("no subtitles found online"))
        ref_cache: dict = {}
        tried, last = 0, _("no candidate passed the checks")
        rejected = set(prev.get("rejected") or [])
        for c in cands:
            if tried >= MAX_TRIES:
                break
            if f"{c.provider}:{c.id}" in rejected:
                continue
            if c.provider == "opensubtitles" and not self.providers.os_quota_left():
                continue
            tried += 1
            try:
                raw = await self.providers.download(c)
                srt = extract_srt(raw, v.season, v.episode)
                if not srt:
                    last = _("download had no usable .srt")
                    continue
                res = await self._check(v, c, decode_text(srt), ref_cache)
            except Exception as err:
                log.warning("subtitles: %s %s failed: %s", c.provider, c.id, err)
                last = f"{c.provider}: {err}"
                continue
            if res.get("reject"):
                last = res["reject"]
                rejected.add(f"{c.provider}:{c.id}")
                self._set(v, rejected=sorted(rejected)[-50:])
                log.info("subtitles: rejected %s %s for %s: %s", c.provider, c.release,
                         Path(v.path).name, last)
                continue
            await asyncio.to_thread(self._write, target, res["text"])
            status = res["status"]
            st = self._set(v, status=status, provider=c.provider, release=c.release,
                           score=res.get("score"), offset=round(res.get("offset", 0.0), 2),
                           scale=round(res.get("scale", 1.0), 5), checked_at=now(), written=True,
                           message=res.get("note", ""))
            self._drop_pending(v)
            await self._notify(v, st)
            return st
        return self._miss(v, last)

    def _miss(self, v: Video, msg: str) -> dict:
        self._add_pending(v)
        return self._set(v, status="not_found", message=msg, checked_at=time.time())

    async def _notify(self, v: Video, st: dict):
        what = v.title or Path(v.path).stem
        if v.season is not None and v.episode is not None:
            what += f" S{v.season:02d}E{v.episode:02d}"
        names = {"it": _("Italian"), "en": _("English"), "es": _("Spanish"), "fr": _("French"),
                 "de": _("German"), "pt": _("Portuguese")}
        lang = names.get(self.lang, self.lang.upper())
        note = {"adapted": _("timing adjusted to the video"),
                "unverified": _("timing not verified")}.get(st["status"], "")
        with contextlib.suppress(Exception):
            await self.hub.notify.send(_("{lang} subtitles ready: {what}", lang=lang, what=what), note, tag="tv")

    @staticmethod
    def _write(target: str, text: str):
        tmp = f"{target}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    # -- validation -----------------------------------------------------------------------

    async def _check(self, v: Video, c: Cand, text: str, ref_cache: dict) -> dict:
        cues = await asyncio.to_thread(parse_srt, text)
        need = 20 if v.season is not None else 50
        if len(cues) < need:
            return {"reject": _("only {n} cues", n=len(cues))}
        if not language_ok(" ".join(t for _, _, t in cues), self.lang):
            return {"reject": _("wrong language")}
        if v.duration and cues[-1][1] > v.duration * 1.06 + 60:
            return {"reject": _("longer than the video")}

        res = {"status": "unverified", "scale": 1.0, "offset": 0.0, "score": None}
        ref = await self._reference(v, c, ref_cache)
        if ref is None:
            if c.hash_match:
                res["status"] = "in_sync"
        else:
            kind, refs = ref
            if kind == "audio":
                fit = await asyncio.to_thread(fit_windows, cues, refs, THRESH_AUDIO)
            else:
                fit = await asyncio.to_thread(fit_single, cues, refs, THRESH_SUB)
            log.debug("subtitles: %s fit vs %s: %s", Path(v.path).name, kind, fit)
            if fit["status"] == "rejected":
                return {"reject": _("timing doesn't match the video ({kind} score {score})",
                                    kind=_("audio") if kind == "audio" else _("subtitle"),
                                    score=fit["score"])}
            res.update(status=fit["status"], scale=fit["scale"], offset=fit["offset"],
                       score=fit["score"], note=_("checked against {kind}", kind=_("audio") if kind == "audio" else _("subtitle")))
        a, b = res["scale"], res["offset"]
        if res["status"] == "adapted":
            cues = [(max(0.0, a * s + b), a * e + b, t) for s, e, t in cues if a * e + b > 0]
        if v.duration and cues:
            if cues[-1][1] > v.duration + 60:
                return {"reject": _("longer than the video after alignment")}
            if (cues[-1][1] - cues[0][0]) < 0.6 * v.duration:
                return {"reject": _("covers too little of the runtime")}
        res["text"] = format_srt(cues)
        return res

    async def _reference(self, v: Video, c: Cand, cache: dict):
        """('sub', Ref) | ('audio', [Ref]) | None, cheapest first; each step is computed once per job.
        Sampling the audio is skipped for a moviehash match (accepted as in sync)."""
        if "free" not in cache:
            cache["free"] = await self._free_reference(v)
        if cache["free"] or c.hash_match or not v.duration or not have_ffmpeg():
            return cache["free"]
        if "audio" not in cache:
            cache["audio"] = None
            try:
                refs = await sample_speech(v.path, v.duration, self._ffmpeg_lock)
                if refs:
                    cache["audio"] = ("audio", refs)
            except Exception as err:
                log.debug("speech sampling failed: %s", err)
        return cache["audio"]

    async def _free_reference(self, v: Video):
        """Sidecar subtitle in another language, else an embedded text stream (small files only)."""
        for side in self.other_sidecars(v):
            try:
                cues = parse_srt(decode_text(await asyncio.to_thread(Path(side).read_bytes)))
            except OSError:
                continue
            if len(cues) >= 30:
                return ("sub", Ref([(s, e) for s, e, _ in cues]))
        if have_ffmpeg() and 0 < v.size <= EMBEDDED_MAX_BYTES:
            subs = sorted((await probe(v.path)).get("subs", []), key=lambda s: s[2] != "en")
            if subs:
                try:
                    cues = parse_srt(await extract_embedded(v.path, subs[0][0]))
                    if len(cues) >= 30:
                        return ("sub", Ref([(s, e) for s, e, _ in cues]))
                except Exception as err:
                    log.debug("embedded subtitle extraction failed: %s", err)
        return None
