"""Subtitle fetcher: parsing, language check, OSDb hash, alignment, ranking, a mocked job.
Run: python tests/test_subtitles.py (no network, temp dirs only)"""

import asyncio
import os
import random
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

    async def search(self, v):
        return [S.Cand("subdl", k, release=k) for k in self.files]

    async def download(self, c):
        self.downloaded.append(c.id)
        return self.files[c.id]


def make(tmp, files):
    sent = []

    async def send(title, body="", **kw):
        sent.append(title)
    hub = SimpleNamespace(store=Store(Path(tmp) / "t.db"), notify=SimpleNamespace(send=send),
                          spawn=lambda coro, name="": asyncio.ensure_future(coro))
    cfg = {"languages": ["it", "en"], "movies_dir": f"{tmp}/movies", "series_dir": f"{tmp}/series"}
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
