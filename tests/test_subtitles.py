"""Subtitle fetcher: parsing, language check, OSDb hash, alignment, ranking, a mocked job.
Run: python tests/test_subtitles.py (no network, temp dirs only)"""

import asyncio
import json
import os
import random
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media import subs_translate as T  # noqa: E402
from hub.plugins.media import subtitles as S  # noqa: E402
from hub.store import Store  # noqa: E402

IT = ["Non lo so, ma credo che sia una buona idea per tutti noi.",
      "Che cosa hai detto? Non ho capito niente di quello che è successo.",
      "Sono qui da due ore e ti sto aspettando con la macchina.",
      "Ma perché non me lo hai detto prima, io non sono d'accordo.",
      "Il problema è che tu non ascolti mai quello che dico.",
      "Andiamo via di qui, la polizia sta arrivando per noi."]
EN = ["I don't know what you are talking about, but it is the end of it.",
      "She said that they were going to have the car for the night.",
      "You can not do this to me and then walk away from here."]


def fake_cues(n=1200, seed=1, texts=IT, gap=(1.0, 7.0), dur=(1.2, 4.0)):
    rnd, t, out = random.Random(seed), 20.0, []
    for i in range(n):
        d = rnd.uniform(*dur)
        out.append((t, t + d, texts[i % len(texts)]))
        t += d + rnd.uniform(*gap)
    return out


def shifted(cues, a, b):
    return [(a * s + b, a * e + b, t) for s, e, t in cues]


def worst_error(fixed_a, fixed_b, sub_cues, true_cues):
    return max(abs(fixed_a * s + fixed_b - ts) for (s, _, _), (ts, _, _) in zip(sub_cues, true_cues))


def test_decode_and_parse():
    raw = "1\r\n00:00:01,000 --> 00:00:02,500\r\nÈ già così, perché sì\r\n\r\n".encode("cp1252")
    text = S.decode_text(raw)
    assert "È già così, perché sì" in text and "\r" not in text
    assert S.decode_text("﻿cià".encode("utf-8")) == "ciao".replace("ciao", "cià")
    messy = ("﻿\n\n7\n00:00:01.5 --> 00:00:03.000\nHello\nthere\n\n\n\n"
             "00:01:00,000 --> 00:01:02,000 X1:1\n<i>No index</i>\n\n12\n\n"
             "1:02:03,004 --> 1:02:04,500\nLast\n")
    cues = S.parse_srt(messy)
    assert len(cues) == 3, cues
    assert cues[0][:2] == (1.5, 3.0) and cues[0][2] == "Hello\nthere"
    assert cues[1][2] == "<i>No index</i>"
    assert abs(cues[2][0] - 3723.004) < 1e-6 and cues[2][2] == "Last", cues[2]
    back = S.parse_srt(S.format_srt(cues))
    assert [c[:2] for c in back] == [c[:2] for c in cues]


def test_language():
    it = " ".join(IT * 5)
    en = " ".join(EN * 5)
    assert S.language_ok(it, "it") and not S.language_ok(en, "it")
    assert S.language_ok(en, "en") and not S.language_ok(it, "en")
    assert S.language_ok("whatever", "ja")  # unknown language: not checked
    assert not S.language_ok("", "it")


def test_osdb_hash():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "v.mkv")
        data = random.Random(5).randbytes(300_000)
        Path(p).write_bytes(data)
        want = len(data)
        for chunk in (data[:65536], data[-65536:]):
            for i in range(0, 65536, 8):
                want = (want + int.from_bytes(chunk[i:i + 8], "little")) % 2 ** 64
        assert S.osdb_hash(p) == f"{want:016x}"
        Path(p).write_bytes(b"x" * 1000)
        assert S.osdb_hash(p) == ""


def test_zip_extract():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Show.S01E01.srt", "one")
        z.writestr("Show.S01E02.srt", "two")
        z.writestr("readme.txt", "x")
    assert S.extract_srt(buf.getvalue(), 1, 2) == b"two"
    assert S.extract_srt(b"plain", None, None) == b"plain"
    assert S.extract_srt(b"PK\x03\x04junk") is None


def test_align_against_subtitle():
    true = fake_cues()
    ref = S.Ref([(s, e) for s, e, _ in fake_cues(seed=1, texts=EN)])
    for a, b in [(1.0, -7.3), (23.976 / 25, 0.0), (23.976 / 25, -7.3), (25 / 24, 41.0)]:
        sub = shifted(true, a, b)  # what a provider would hand us
        fit = S.fit_single(sub, ref, S.THRESH_SUB)
        assert fit["status"] == "adapted", (a, b, fit)
        err = worst_error(fit["scale"], fit["offset"], sub, true)
        assert err < 0.1, (a, b, fit, err)
    fit = S.fit_single(true, ref, S.THRESH_SUB)
    assert fit["status"] == "in_sync", fit


def test_reject_unrelated():
    ref = S.Ref([(s, e) for s, e, _ in fake_cues()])
    other = fake_cues(seed=99, gap=(0.5, 9.0))
    fit = S.fit_single(other, ref, S.THRESH_SUB)
    assert fit["status"] == "rejected", fit
    refs = windows_for(fake_cues(seed=1), 7200)
    fit = S.fit_windows(other, refs, S.THRESH_AUDIO)
    assert fit["status"] == "rejected", fit


def speech_intervals(cues, seed=3):
    """What silencedetect would roughly see: cues jittered, plus some non-speech noise."""
    rnd = random.Random(seed)
    iv = [(s + rnd.uniform(-0.3, 0.3), e + rnd.uniform(-0.3, 0.3)) for s, e, _ in cues]
    t = 0.0
    while t < 7200:
        iv.append((t, t + rnd.uniform(0.3, 1.0)))
        t += rnd.uniform(15, 40)
    return iv


def windows_for(cues, duration):
    iv = speech_intervals(cues)
    out = []
    for w in S.window_starts(duration):
        part = [(max(a, w), min(b, w + S.WIN_LEN)) for a, b in iv if b > w and a < w + S.WIN_LEN]
        out.append(S.Ref(part, w, w + S.WIN_LEN))
    return out


def test_align_against_speech():
    true = fake_cues(seed=1)
    refs = windows_for(true, 7200)
    assert len(refs) == 6
    for a, b in [(1.0, -7.3), (23.976 / 25, -7.3), (25 / 23.976, 12.4)]:
        sub = shifted(true, a, b)
        fit = S.fit_windows(sub, refs, S.THRESH_AUDIO)
        assert fit["status"] == "adapted", (a, b, fit)
        err = worst_error(fit["scale"], fit["offset"], sub, true)
        assert err < 0.1, (a, b, fit, err)
    fit = S.fit_windows(true, refs, S.THRESH_AUDIO)
    assert fit["status"] == "in_sync", fit


def test_combine_windows():
    A = S.SCALES
    # five windows agree on (1.0, 7.3); the other scales have drifting offsets
    wins = []
    for i in range(5):
        w = {a: (7.3 + (a - 1) * 3000 * (i + 1) + 40 * (a != 1), 0.1) for a in A}
        w[1.0] = (7.3 + 0.05 * i, 0.7)
        wins.append(w)
    a, b, agree, valid = S.combine_windows(wins, 0.3)
    assert a == 1.0 and abs(b - 7.3) < 0.3 and agree == 5 and valid == 5
    # an empty window (no speech) is ignored; one outlier does not matter
    wins[2][1.0] = (90.0, 0.8)
    wins.append({})
    a, b, agree, valid = S.combine_windows(wins, 0.3)
    assert a == 1.0 and agree == 4 and valid == 5
    # 25/24 consistent across windows beats a scale-1 fit that drifts
    wins = [{1.0: (3.0 * i, 0.5), 25 / 24: (-2.0, 0.6)} for i in range(1, 5)]
    a, b, agree, _ = S.combine_windows(wins, 0.3)
    assert abs(a - 25 / 24) < 1e-9 and agree == 4
    # windows that disagree: only 1 agrees -> fit_windows would reject (agree < 2)
    wins = [{1.0: (10.0 * i, 0.6)} for i in range(1, 5)]
    assert S.combine_windows(wins, 0.3)[2] == 1
    assert S.combine_windows([{}, {}], 0.3) is None


def test_speech_parsing_and_windows():
    err = ("[silencedetect @ 0x1] silence_start: 2.5\n[silencedetect @ 0x1] silence_end: 4.0 | silence_duration: 1.5\n"
           "[silencedetect @ 0x1] silence_start: 170.0\n")
    assert S.speech_from_silence(err, 180, 1000) == [(1000.0, 1002.5), (1004.0, 1170.0)]
    ws = S.window_starts(7200)
    assert len(ws) == 6 and ws[0] >= 360 and ws[-1] + S.WIN_LEN <= 6840 + 1
    assert S.window_starts(150) == []


def test_ranking():
    rel = "Movie.2020.1080p.WEB-DL.DDP5.1.H264-GRP"
    cands = [S.Cand("a", "1", "Movie.2020.720p.BluRay-XYZ", downloads=9000),
             S.Cand("b", "2", "Movie.2020.1080p.WEB-DL.H264-GRP", fps=23.976, downloads=10),
             S.Cand("c", "3", "Movie.2020.1080p.WEB-DL.H264-GRP", hi=True, downloads=5000),
             S.Cand("d", "4", "Movie.2020.1080p.WEB-DL.H264-GRP", forced=True),
             S.Cand("e", "5", "whatever", hash_match=True)]
    ranked = S.rank_candidates(cands, rel, 23.976)
    assert [c.id for c in ranked] == ["5", "2", "1", "3", "4"], [c.id for c in ranked]
    assert S.release_sim("A.WEB-DL-GRP", "A.WEBRip-GRP") < S.release_sim("A.WEB-DL-GRP", "A.WEB-DL-GRP")


class FakeProviders:
    quota = True

    def __init__(self, files):
        self.files = files
        self.downloaded = []

    def configured(self):
        return ["subdl"]

    def os_quota_left(self):
        return True

    async def open(self):
        pass

    async def close(self):
        pass

    async def search(self, v, lang=None):
        return [S.Cand("subdl", k, release=k, lang=lang or "it") for k in self.files]

    async def download(self, c):
        self.downloaded.append(c.id)
        return self.files[c.id]


def make(tmp, files, sub_cfg=None):
    sent = []

    async def send(title, body="", **kw):
        sent.append(title)
    hub = SimpleNamespace(store=Store(Path(tmp) / "t.db"), notify=SimpleNamespace(send=send),
                          spawn=lambda coro, name="": asyncio.ensure_future(coro),
                          data_dir=Path(tmp) / "data")
    cfg = {"languages": ["it", "en"], "movies_dir": f"{tmp}/movies", "series_dir": f"{tmp}/series",
           "subtitles": dict(sub_cfg or {})}
    subs = S.Subtitles(SimpleNamespace(hub=hub), cfg)
    subs.providers = FakeProviders(files)
    return subs, sent


def test_job_end_to_end():
    true = fake_cues(n=400, seed=7)
    ref = fake_cues(n=400, seed=7, texts=EN)
    wrong = fake_cues(n=400, seed=8, gap=(0.5, 9.0))
    good = shifted(true, 1.0, -7.3)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "movies" / "Film (2020)"
        d.mkdir(parents=True)
        video = d / "Film (2020) 1080p.mkv"
        video.write_bytes(random.Random(1).randbytes(200_000))
        (d / "Film (2020) 1080p.en.srt").write_text(S.format_srt(ref), encoding="utf-8")
        files = {"wrong": S.format_srt(wrong).encode(), "english": S.format_srt(ref).encode(),
                 "good": S.format_srt(good).encode("utf-8")}
        subs, sent = make(tmp, files)
        assert subs.allowed(str(video)) and not subs.allowed("/etc/passwd")
        v = S.Video(str(video), title="Film", year=2020, release="Film.2020.1080p")

        async def run():
            subs._set(v, status="searching")
            return await subs.fetch(v)
        st = asyncio.run(run())
        assert st["status"] == "adapted", st
        assert abs(st["offset"] - 7.3) < 0.15 and st["provider"] == "subdl"
        out = S.parse_srt((d / "Film (2020) 1080p.it.srt").read_text(encoding="utf-8"))
        assert max(abs(a - t[0]) for (a, _, _), t in zip(out, true)) < 0.1
        assert sent and "Italian subtitles ready" in sent[0]
        assert subs.state(str(video))["written"] and not subs._pending()
        assert len(subs.providers.downloaded) <= S.MAX_TRIES

        # a second run with our own file replaces it; a user's file is never touched
        mine = d / "Film (2020) 1080p.it.srt"
        mine.write_text("hand made")
        subs.store.set(f"media.subs.{S.sha(str(video))}", {"path": str(video), "status": "found"})
        st = asyncio.run(run())
        assert st["status"] == "found" and mine.read_text() == "hand made", st

        # nothing acceptable -> not_found and queued for retry
        mine.unlink()
        subs.providers.files = {"wrong": files["wrong"], "english": files["english"]}
        subs.store.delete(f"media.subs.{S.sha(str(video))}")
        st = asyncio.run(run())
        assert st["status"] == "not_found" and not mine.exists(), st
        assert S.sha(str(video)) in subs._pending()

        # a retry doesn't download the already-rejected candidates again (download quota)
        subs.providers.downloaded.clear()
        st = asyncio.run(subs.fetch(v))
        assert st["status"] == "not_found" and not subs.providers.downloaded, subs.providers.downloaded


def test_on_imported_skips_when_audio_has_language():
    with tempfile.TemporaryDirectory() as tmp:
        subs, _ = make(tmp, {})
        spawned = []
        subs.spawn = lambda v, **kw: spawned.append(v)
        row = {"title": "X", "year": 2020, "name": "X.2020", "season": None, "episode": None,
               "data": {"audio": ["it", "en"]}}
        subs.on_imported(row, ["/a/b.mkv"])
        assert not spawned
        row["data"]["audio"] = ["en"]
        subs.on_imported(row, ["/a/b.mkv", "/a/b.it.srt"])
        assert len(spawned) == 1 and spawned[0].release == "X.2020"
        subs.enabled = False
        subs.on_imported(row, ["/a/c.mkv"])
        assert len(spawned) == 1


# --------------------------------------------------------------------------- providers (canned JSON)

class Resp:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self, content_type=None):
        return self.data

    async def text(self):
        return self.data if isinstance(self.data, str) else json.dumps(self.data)

    async def read(self):
        return self.data if isinstance(self.data, bytes) else json.dumps(self.data).encode()


class FakeSession:
    """Answers from a list of (url-substring, response-or-callable); records the calls."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, url, kw):
        self.calls.append((url, kw))
        for key, r in self.routes:
            if key in url:
                if isinstance(r, list):
                    return r.pop(0) if len(r) > 1 else r[0]
                return r(kw) if callable(r) else r
        raise AssertionError(f"unexpected {url}")

    def get(self, url, **kw):
        return self._answer(url, kw)

    def post(self, url, **kw):
        return self._answer(url, kw)

    def request(self, method, url, **kw):
        return self._answer(url, kw)


def test_provider_parsing():
    osj = {"data": [
        {"attributes": {"release": "Film.2020.1080p.WEB-DL-GRP", "fps": 23.976, "download_count": 900,
                        "hearing_impaired": False, "moviehash_match": True,
                        "files": [{"file_id": 77, "file_name": "film.it.srt"}]}},
        {"attributes": {"release": "Film.2020.HDTV", "download_count": 5, "hearing_impaired": True,
                        "foreign_parts_only": False, "files": [{"file_id": 78}]}},
        {"attributes": {"files": []}}]}
    sd = {"status": True, "subtitles": [
        {"release_name": "Film.2020.BluRay-X", "name": "SUBDL::x", "url": "/subtitle/1-2.zip", "hi": False},
        {"release_name": "no url"}]}
    p = S.Providers({"opensubtitles_api_key": "k", "subdl_api_key": "k2"}, SimpleNamespace(get=lambda *a: None), "it")
    p.session = FakeSession([("opensubtitles.com", Resp(osj)), ("subdl.com", Resp(json.dumps(sd).encode()))])
    got = asyncio.run(p.search(S.Video("/x/f.mkv", title="Film", year=2020, imdb_id="tt123"), "en"))
    assert [c.provider for c in got] == ["opensubtitles", "opensubtitles", "subdl"], got
    a, b, c = got
    assert a.id == "77" and a.hash_match and a.lang == "en" and a.name == "film.it.srt" and a.downloads == 900
    assert b.hi and not b.hash_match and c.release == "Film.2020.BluRay-X" and c.id == "/subtitle/1-2.zip"
    assert p.session.calls[0][1]["params"]["languages"] == "en"
    assert p.session.calls[1][1]["params"]["languages"] == "EN"
    ranked = S.rank_candidates(got, "Film.2020.1080p.WEB-DL-GRP")
    assert ranked[0] is a and ranked[-1] is b  # hash match first, hearing-impaired last


# --------------------------------------------------------------------------- manual offset / fps

def test_shift_and_presets():
    cues = [(1.0, 2.0, "a"), (10.0, 12.0, "b"), (100.0, 103.0, "c")]
    out = T.shift_cues(cues, -1.5)
    assert [round(x[0], 3) for x in out] == [0.0, 8.5, 98.5] and len(out) == 3  # first clamps at 0
    assert len(T.shift_cues(cues, -50.0)) == 1  # cues ending before 0 go
    sc = T.FPS_PRESETS["23.976->25"]
    out = T.shift_cues(cues, 0.0, sc)
    assert abs(out[2][0] - 100 * 23.976 / 25) < 1e-9
    rows = T.preview_rows(cues, 2.0, 1.0, n=3)
    assert [r["n"] for r in rows] == [1, 2, 3] and rows[0]["new"] == 3.0 and rows[2]["old"] == 100.0


def test_adjust_saves_with_backup_outside_library():
    cues = fake_cues(n=120, seed=4)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "movies" / "Film (2020)"
        d.mkdir(parents=True)
        video = d / "Film (2020).mkv"
        video.write_bytes(b"x" * 1000)
        srt = d / "Film (2020).it.srt"
        srt.write_text(S.format_srt(cues), encoding="utf-8")
        subs, _ = make(tmp, {})
        v = S.Video(str(video), title="Film")
        prev = asyncio.run(subs.adjust(v, srt.name, 2.5, 1.0))
        assert not prev["saved"] and prev["rows"][0]["new"] - prev["rows"][0]["old"] == 2.5
        assert S.parse_srt(srt.read_text())[0][0] == cues[0][0]  # a preview never writes
        res = asyncio.run(subs.adjust(v, srt.name, 2.5, 1.0, save=True))
        assert res["saved"]
        new = S.parse_srt(srt.read_text())
        assert abs(new[10][0] - (cues[10][0] + 2.5)) < 0.002
        orig = Path(tmp) / "data" / "subs-orig" / S.sha(str(video)) / srt.name
        assert orig.exists() and not list(d.glob("*.orig")) and not list(d.glob("*.bak"))
        asyncio.run(subs.adjust(v, srt.name, -1.0, 1.0, save=True))  # a second save keeps the FIRST original
        assert abs(S.parse_srt(orig.read_text())[10][0] - cues[10][0]) < 0.002
        asyncio.run(subs.restore(v, srt.name))
        assert abs(S.parse_srt(srt.read_text())[10][0] - cues[10][0]) < 0.002
        for bad in ("../x.srt", "other.srt", "Film (2020).it.exe"):
            try:
                asyncio.run(subs.adjust(v, bad, 1.0))
                raise AssertionError(bad)
            except (ValueError, FileNotFoundError):
                pass


# --------------------------------------------------------------------------- translation

def test_fit_lines_and_xml():
    assert T.fit_lines("Ciao.") == "Ciao."
    long = "Non so davvero cosa tu stia cercando di dirmi in questo momento, ma va bene."
    out = T.fit_lines(long)
    l = out.split("\n")
    assert len(l) == 2 and all(len(x) <= 46 for x in l), out
    assert T.fit_lines("- Ciao.\n- Come stai?") == "- Ciao.\n- Come stai?"
    two = T.fit_lines("<i>Non so davvero cosa tu stia cercando di dirmi in questo momento, ma va bene.</i>")
    a, b = two.split("\n")
    assert a.startswith("<i>") and a.endswith("</i>") and b.startswith("<i>") and b.endswith("</i>"), two
    x = T._to_xml("<i>Tom & Jerry</i>\nsecond <b>line")  # unbalanced <b> is dropped by _clean_cue
    assert x == "<x><i>Tom &amp; Jerry</i><br/>second <b>line</x>"
    lead, body = T._clean_cue("{\\an8}<i>Hi</i> <b>x")
    assert lead == "{\\an8}" and body == "<i>Hi</i> x"
    assert T._from_xml("<x>Ciao &amp; <i>a</i><br/>due</x>") == "Ciao & <i>a</i>\ndue"


def en_cues(n=250):
    return fake_cues(n=n, seed=11, texts=EN)


def deepl_session(log):
    def reply(kw):
        body = kw["json"]
        log.append(body)
        texts = body["text"]
        assert body["tag_handling"] == "xml" and body["formality"] == "less" and body["target_lang"] == "IT"
        assert all(t.startswith("<x>") and t.endswith("</x>") for t in texts)
        return Resp({"translations": [{"text": "<x>" + IT[i % len(IT)] + "</x>"} for i, _t in enumerate(texts)]})
    return FakeSession([("/v2/translate", reply),
                        ("/v2/usage", Resp({"character_count": 1000, "character_limit": 500000}))])


def test_translate_with_fake_deepl():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        log = []
        tr = T.DeepL("abc:fx", store, deepl_session(log))
        assert tr.base == "https://api-free.deepl.com"
        cues = en_cues(250)
        prog = []
        out = asyncio.run(T.translate_cues(cues, tr, title="Film", src="en", progress=lambda a, b: prog.append((a, b))))
        assert len(out) == 250 and [x[:2] for x in out] == [x[:2] for x in cues]  # timing untouched
        assert all(len(x[2].split("\n")) <= 2 for x in out)
        assert [len(b["text"]) for b in log] == [50] * 5  # DeepL takes 50 texts per request
        assert prog[-1] == (250, 250) and len(prog) == 3
        assert "context" in log[1]  # earlier cues travel as (free) context
        assert tr.tracked() == T.source_chars(cues)
        u = asyncio.run(tr.usage())
        assert u["remaining"] == 499000


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records each messages.create call, answers from a script."""

    def __init__(self, answers, seen, key="sk-test"):
        self.answers, self.seen, self.key = answers, seen, key
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kw):
        import anthropic
        import httpx2
        self.seen.append(kw)
        if self.key != "sk-test":
            req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=req), body=None)
        assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "low"
        a = self.answers.pop(0) if self.answers else "auto"
        cues = json.loads(kw["messages"][0]["content"].split("\n\n")[0])["cues"]
        if a == "auto":
            a = json.dumps({"cues": [{"id": c["id"], "text": IT[c["id"] % len(IT)]} for c in cues]})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=a)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500))


def claude(key, model, store, answers, seen):
    return T.Claude(key, model, store, None, client=FakeClaude(answers, seen, key))


def test_translate_with_fake_claude_retry_and_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        cues = en_cues(230)
        # chunks of 100: 100 + 100 + 30; the 2nd chunk is answered short once, then right
        seen = []
        short = json.dumps({"cues": [{"id": 1, "text": "uno"}]})
        tr = claude("sk-test", "claude-haiku-5-5", store, ["auto", short, "auto", "auto"], seen)
        out = asyncio.run(T.translate_cues(cues, tr, title="Film", src="en"))
        assert len(out) == 230 and out[0][2].replace("\n", " ") == IT[1 % len(IT)] and out[229][2].replace("\n", " ") == IT[30 % len(IT)]
        assert len(seen) == 4  # 3 chunks + 1 retry
        first = json.loads(seen[0]["messages"][0]["content"])
        assert set(first) == {"title", "already_translated_context", "cues"} and len(first["cues"]) == 100
        second = json.loads(seen[2]["messages"][0]["content"].split("\n\n")[0])
        assert len(second["already_translated_context"]) == T.CTX_CUES  # context flows across chunks
        assert tr.tokens_in == 4000 and tr.cost() > 0
        # two bad answers in a row -> error, nothing half-written
        tr2 = claude("sk-test", "claude-haiku-5-5", store, ["not json", short], [])
        try:
            asyncio.run(T.translate_cues(cues, tr2, title="Film", src="en"))
            raise AssertionError("should fail")
        except T.TranslateError as err:
            assert "sk-test" not in str(err)
        # a wrong id set is as bad as a wrong count
        bad_ids = json.dumps({"cues": [{"id": i + 2, "text": "x"} for i in range(100)]})
        tr3 = claude("sk-test", "m", store, [bad_ids, bad_ids], [])
        try:
            asyncio.run(T.translate_cues(cues[:100], tr3, title="F", src="en"))
            raise AssertionError("should fail")
        except T.TranslateError:
            pass
        # the key never shows up in the error of a rejected request
        tr4 = claude("wrong-key", "m", store, [], [])
        try:
            asyncio.run(T.translate_cues(cues[:10], tr4, title="F", src="en"))
            raise AssertionError("should fail")
        except T.TranslateError as err:
            assert "Anthropic" in str(err) and "wrong-key" not in str(err)
        # prices: Haiku 5.5 is $0.10 / $0.50 per million tokens
        assert abs(tr.cost(1_000_000, 1_000_000) - 0.60) < 1e-9
        est = tr.estimate(55000, 800)
        assert est["usd"] > 0 and est["tokens_in"] > 15000


def test_backoff_on_429():
    waited = []

    async def fake_sleep(s):
        waited.append(s)
    old = T._sleep
    T._sleep = fake_sleep
    try:
        sess = FakeSession([("x.test", [Resp("slow down", 429, {"Retry-After": "3"}), Resp("", 503),
                                        Resp({"ok": 1})])])
        d = asyncio.run(T.post_json(sess, "https://x.test/a", {}, {}))
        assert d == {"ok": 1} and waited == [3.0, 4.0], waited
        sess = FakeSession([("x.test", Resp("no", 429))])
        try:
            asyncio.run(T.post_json(sess, "https://x.test/a", {}, {}, tries=3))
            raise AssertionError
        except T.TranslateError as err:
            assert "429" in str(err)
    finally:
        T._sleep = old


class FakeTranslator:
    name, label, model = "deepl", "DeepL", ""
    tokens_in = tokens_out = 0

    def __init__(self):
        self.calls = 0

    async def translate(self, texts, **kw):
        self.calls += 1
        return [IT[i % len(IT)] for i, _t in enumerate(texts)]

    async def usage(self):
        return {"used": 0, "limit": 500000, "remaining": 500000}

    def cost(self, *a):
        return 0.0


def video_with_english(tmp, n=300):
    d = Path(tmp) / "movies" / "Film (2020)"
    d.mkdir(parents=True)
    video = d / "Film (2020) 1080p.mkv"
    video.write_bytes(random.Random(1).randbytes(100_000))
    en = en_cues(n)
    (d / "Film (2020) 1080p.en.srt").write_text(S.format_srt(en), encoding="utf-8")
    return d, video, en


def test_translated_sub_is_replaced_by_a_real_one():
    true = fake_cues(n=400, seed=7)
    ref = fake_cues(n=400, seed=7, texts=EN)
    with tempfile.TemporaryDirectory() as tmp:
        d, video, _en = video_with_english(tmp)
        (d / "Film (2020) 1080p.en.srt").write_text(S.format_srt(ref), encoding="utf-8")
        subs, sent = make(tmp, {}, {"deepl_api_key": "k:fx"})
        subs._translators["deepl"] = FakeTranslator()
        v = S.Video(str(video), title="Film", year=2020, release="Film.2020.1080p")
        target = d / "Film (2020) 1080p.it.srt"

        # translate from the sidecar: same timing, state marks it as a machine translation
        res = asyncio.run(subs.do_translate(v))
        assert res["outcome"] == "translated" and target.exists()
        out = S.parse_srt(target.read_text())
        assert len(out) == len(ref), (len(out), len(ref))
        assert max(abs(a[0] - b[0]) for a, b in zip(out, ref)) < 0.002
        st = subs.state(str(video))
        assert st["status"] == "translated" and st["translated"]["provider"] == "deepl" and st["translated"]["from"] == "en"

        # nothing real online: the translation stays and stays marked
        subs.providers.files = {}
        st = asyncio.run(subs.fetch(v))
        assert st["status"] == "translated" and st["translated"] and target.exists(), st

        # a real Italian subtitle appears: it replaces the translation (original kept in the data dir)
        subs.providers.files = {"good": S.format_srt(shifted(true, 1.0, -4.0)).encode()}
        st = asyncio.run(subs.fetch(v))
        assert st["status"] in ("adapted", "in_sync") and not st.get("translated"), st
        assert "Non lo so" in target.read_text()
        assert (Path(tmp) / "data" / "subs-orig" / S.sha(str(video)) / target.name).exists()

        # translating again is refused now that a real one is there, unless asked to replace
        try:
            asyncio.run(subs.do_translate(v))
            raise AssertionError
        except T.TranslateError as err:
            assert "already exists" in str(err)

        # a hand-edited translation is no longer ours: never replaced
        target.unlink()
        asyncio.run(subs.do_translate(v))
        target.write_text(target.read_text() + "\n\n")
        subs.providers.files = {"good": S.format_srt(shifted(true, 1.0, -4.0)).encode()}
        st = asyncio.run(subs.fetch(v))
        assert st["status"] == "found" and not st.get("translated"), st


async def _noloop(fn, *a):
    return fn(*a)


def test_translation_off_without_keys_and_budget():
    with tempfile.TemporaryDirectory() as tmp:
        d, video, _ = video_with_english(tmp)
        subs, _s = make(tmp, {})
        assert subs.translator_names() == [] and asyncio.run(_noloop(subs.translator)) is None
        subs2, _s = make(tmp, {}, {"deepl_api_key": "a:fx", "anthropic_api_key": "b", "translator": "claude"})
        assert subs2.translator_names() == ["claude", "deepl"]
        assert asyncio.run(_noloop(subs2.translator)).name == "claude"
        assert asyncio.run(_noloop(subs2.translator, "deepl")).name == "deepl"
        assert asyncio.run(_noloop(subs2.translator, "nope")) is None
        # DeepL budget: not enough characters left -> refuse before sending anything
        subs3, _s = make(tmp, {}, {"deepl_api_key": "a:fx"})
        fake = FakeTranslator()

        async def low():
            return {"used": 499990, "limit": 500000, "remaining": 10}
        fake.usage = low
        subs3._translators["deepl"] = fake
        try:
            asyncio.run(subs3.do_translate(S.Video(str(video), title="Film")))
            raise AssertionError
        except T.TranslateError as err:
            assert "10" in str(err) and fake.calls == 0


# --------------------------------------------------------------------------- manual pick

def test_manual_pick_outcomes():
    true = fake_cues(n=400, seed=7)
    ref = fake_cues(n=400, seed=7, texts=EN)
    wrong = fake_cues(n=400, seed=8, gap=(0.5, 9.0))
    with tempfile.TemporaryDirectory() as tmp:
        d, video, _ = video_with_english(tmp)
        (d / "Film (2020) 1080p.en.srt").write_text(S.format_srt(ref), encoding="utf-8")
        files = {"good": S.format_srt(shifted(true, 1.0, -7.3)).encode(), "wrong": S.format_srt(wrong).encode()}
        subs, _s = make(tmp, files)
        v = S.Video(str(video), title="Film", year=2020, release="Film.2020")
        target = d / "Film (2020) 1080p.it.srt"
        res = asyncio.run(subs.do_pick(v, S.Cand("subdl", "wrong", lang="it")))
        assert res["outcome"] == "rejected" and res["can_force"] and not target.exists(), res
        res = asyncio.run(subs.do_pick(v, S.Cand("subdl", "good", lang="it")))
        assert res["outcome"] == "adapted" and abs(res["offset"] - 7.3) < 0.15 and target.exists(), res
        assert subs.state(str(video))["status"] == "adapted"
        # adapt again: already right -> in sync, file unchanged
        before = target.read_text()
        res = asyncio.run(subs.do_adapt_auto(v, target.name))
        assert res["outcome"] == "in_sync" and target.read_text() == before, res
        # an off-by-3s file is brought back by the automatic adapt
        target.write_text(S.format_srt(shifted(true, 1.0, 3.0)))
        res = asyncio.run(subs.do_adapt_auto(v, target.name))
        assert res["outcome"] == "adapted" and abs(res["offset"] + 3.0) < 0.15, res
        out = S.parse_srt(target.read_text())
        assert max(abs(a - t[0]) for (a, _, _), t in zip(out, true)) < 0.1
        # forcing a rejected one keeps its text
        res = asyncio.run(subs.do_pick(v, S.Cand("subdl", "wrong", lang="it"), force=True))
        assert res["outcome"] == "unverified" and "kept as is" in res["message"], res
        # an English pick lands next to the Italian one, state of the Italian file untouched
        before_state = dict(subs.state(str(video)))
        asyncio.run(subs.do_pick(v, S.Cand("subdl", "good", lang="en"), lang="en", force=True))
        assert (d / "Film (2020) 1080p.en.srt").exists()
        assert subs.state(str(video))["status"] == before_state["status"]


def test_job_runner_records_outcome():
    with tempfile.TemporaryDirectory() as tmp:
        subs, _s = make(tmp, {})
        v = S.Video(str(Path(tmp) / "a.mkv"))

        async def run():
            async def ok(progress):
                progress(1, 2)
                return {"outcome": "in_sync"}

            async def boom(progress):
                raise T.TranslateError("nope")
            subs.start_job(v, "pick", ok)
            try:
                subs.start_job(v, "pick", ok)  # same video again while busy
                raise AssertionError
            except RuntimeError:
                pass
            while v.path in subs._running:
                await asyncio.sleep(0.01)
            first = dict(subs.job_state(v))
            subs.start_job(v, "translate", boom)
            while v.path in subs._running:
                await asyncio.sleep(0.01)
            return first, subs.job_state(v)
        first, second = asyncio.run(run())
        assert first["phase"] == "done" and first["ok"] and first["outcome"] == "in_sync"
        assert second["phase"] == "done" and not second["ok"] and second["message"] == "nope"


# --------------------------------------------------------------------------- library scan

def scan_setup(tmp, cfg=None):
    from hub.plugins.media.library import Library
    from hub.plugins.media.subs_scan import Scanner
    subs, _s = make(tmp, {}, {"scan_per_day": 2, **(cfg or {})})
    subs.providers.configured = lambda: ["subdl"]
    plugin = SimpleNamespace(hub=subs.hub)
    plugin.library = Library(plugin, {"movies_dir": f"{tmp}/movies", "series_dir": f"{tmp}/series"})
    names = {}
    for i, name in enumerate(["Italian", "Todo B", "Todo C", "Todo D"]):
        d = Path(tmp) / "movies" / f"{name} (2020)"
        d.mkdir(parents=True)
        (d / f"{name} (2020).mkv").write_bytes(b"x" * 100)
        if name == "Italian":
            (d / f"{name} (2020).it.srt").write_text("x")
        names[name] = str(d / f"{name} (2020).mkv")
        plugin.library.store.execute(
            "INSERT INTO media_lib_movies (path,title,norm,year,size,mtime) VALUES (?,?,?,?,?,?)",
            (str(d), name, name.lower(), 2020, 100, 0.0))
    sd = Path(tmp) / "series" / "Show (2019)" / "Season 1"
    sd.mkdir(parents=True)
    plugin.library.store.execute(
        "INSERT INTO media_lib_shows (path,title,norm,year,episodes,size,mtime) VALUES (?,?,?,?,?,?,?)",
        (str(sd.parent), "Show", "show", 2019, 1, 1, 0.0))
    for ep in (1, 2):
        (sd / f"Show.S01E0{ep}.mkv").write_bytes(b"x")
        plugin.library.store.execute("INSERT INTO media_lib_episodes VALUES (?,?,?,?)",
                                     (str(sd.parent), 1, ep, str(sd / f"Show.S01E0{ep}.mkv")))
    sc = Scanner(subs, plugin, {"subtitles": {"scan_per_day": 2, **(cfg or {})}})
    done = []

    async def fake_one(v, translate=True):
        done.append(Path(v.path).name)
        subs._set(v, status="not_found", checked_at=time.time())
        return "not_found"
    subs.scan_one = fake_one
    return subs, sc, done


def test_scan_queue_respects_daily_limit_and_skips_italian():
    with tempfile.TemporaryDirectory() as tmp:
        subs, sc, done = scan_setup(tmp)
        os.getloadavg = lambda: (0.1, 0.1, 0.1)
        asyncio.run(sc.refresh(force=True))
        assert sc.counts["total"] == 6 and sc.counts["todo"] == 5 and sc.counts["present"] == 1, sc.counts
        for _i in range(5):
            asyncio.run(sc.tick())
        assert done == ["Todo B (2020).mkv", "Todo C (2020).mkv"], done  # 2 per day, Italian one never
        assert sc.status()["reason"] == "daily limit reached" and sc.status()["done_today"] == 2
        assert sc.counts["todo"] == 3 and sc.counts["not_found"] == 2
        # a new day lifts the limit; pause stops everything
        sc.store.set("media.subs.scan", {"day": "2000-01-01", "done_today": 99})
        sc.set_paused(True)
        asyncio.run(sc.tick())
        assert len(done) == 2 and sc.status()["reason"] == "paused" and sc.status()["paused"]
        sc.set_paused(False)
        asyncio.run(sc.tick())
        assert len(done) == 3
        # movies come before series
        assert done[2] == "Todo D (2020).mkv"


def test_scan_waits_for_quota_load_and_other_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        subs, sc, done = scan_setup(tmp)
        os.getloadavg = lambda: (0.1, 0.1, 0.1)
        subs.providers.configured = lambda: ["opensubtitles"]
        subs.store.set("media.subs.quota.opensubtitles", {"until": time.time() + 3600})
        subs.providers.os_quota_left = lambda: False
        asyncio.run(sc.tick())
        assert not done and "quota" in sc.status()["reason"]
        subs.providers.os_quota_left = lambda: True
        os.getloadavg = lambda: (5.0, 1.0, 1.0)
        asyncio.run(sc.tick())
        assert not done and sc.status()["reason"] == "CPU busy"
        os.getloadavg = lambda: (0.1, 0.1, 0.1)
        subs._running.add("/some/video.mkv")
        asyncio.run(sc.tick())
        assert not done and "another" in sc.status()["reason"]
        subs._running.clear()
        asyncio.run(sc.tick())
        assert len(done) == 1
        # disabled: nothing at all
        sc.enabled = False
        asyncio.run(sc.tick())
        assert len(done) == 1


def test_scan_skips_italian_audio_and_embedded_and_revisits_translations():
    with tempfile.TemporaryDirectory() as tmp:
        subs, sc, _done = scan_setup(tmp)
        d = Path(tmp) / "movies" / "Todo B (2020)"
        v = S.Video(str(d / "Todo B (2020).mkv"), title="Todo B")
        calls = []

        async def fake_probe(path):
            calls.append(path)
            return {"duration": 100.0, "fps": 24.0, "audio": ["it", "en"], "subs": []}
        old = S.probe
        S.probe = fake_probe
        try:
            st = asyncio.run(subs.fetch(v, check_audio=True, scan=True))
            assert st["status"] == "skipped" and "audio" in st["message"]

            async def probe_emb(path):
                return {"duration": 100.0, "fps": 24.0, "audio": ["en"], "subs": [(0, "subrip", "it")]}
            S.probe = probe_emb
            v2 = S.Video(str(d.parent / "Todo C (2020)" / "Todo C (2020).mkv"), title="Todo C")
            st = asyncio.run(subs.fetch(v2, check_audio=True, scan=True))
            assert st["status"] == "skipped" and "embedded" in st["message"]
        finally:
            S.probe = old
        # a translated file is due again after 14 days, a fresh one is not
        asyncio.run(sc.refresh(force=True))
        item = next(i for i in sc.items if "Todo D" in i["path"])
        subs._set(S.Video(item["path"]), status="translated", translated={"provider": "deepl"},
                  checked_at=time.time())
        item["has_sub"] = True
        assert not sc._due(item, time.time())
        assert sc._due(item, time.time() + 15 * 86400)
        # a scan miss doesn't enter the 6-hourly retry queue
        subs.providers.search = lambda v, lang=None: asyncio.sleep(0, result=[])
        v3 = S.Video(item["path"], title="Todo D")
        item["has_sub"] = False
        subs.store.delete(f"media.subs.{S.sha(item['path'])}")
        st = asyncio.run(subs.fetch(v3, scan=True))
        assert st["status"] == "not_found" and not subs._pending()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
