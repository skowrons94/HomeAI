"""Release parsing / ranking tests. Run: python tests/test_media_ranking.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media.ranking import Profile, Want, normalize, parse, rank  # noqa: E402

GB = 1_000_000_000


def test_parse_italian():
    p = parse("Dune (2021) FullHD 1080p.H264 Ita Eng AC3 5.1 Sub")
    assert p.year == 2021 and p.resolution == 1080 and p.codec == "x264"
    assert p.audio == {"it", "en"} and p.head == "dune"

    p = parse("Dogman (2018).1080p.H264.italian.Ac3-5.1.sub.ita-BaMax71-MIRCrew")
    assert p.audio == {"it"} and "it" in p.subs and p.year == 2018

    p = parse("From.S03E01.Shatter.720p.ITA-ENG.WEBRip.x265.AAC-V3SP4EV3R")
    assert (p.season, p.episode) == (3, 1) and p.resolution == 720
    assert p.audio == {"it", "en"} and p.source == "webrip" and p.codec == "x265"
    assert p.head == "from"


def test_parse_subs_only():
    p = parse("The.Bear.S04E01.1080p.WEB.h264-ETHEL.Sub.Ita")
    assert "it" not in p.audio and "it" in p.subs
    p = parse("Anora.2024.1080p.WEBRip.x264.SUB-ITA")
    assert "it" not in p.audio and "it" in p.subs


def test_parse_packs():
    p = parse("The Bear S04 ITA ENG 1080p WEB-DL DDP5.1 H264-MIRCrew")
    assert p.season == 4 and p.episode is None and p.pack and p.source == "web-dl"
    p = parse("From S03e01-10 [Completa] 720p ITA ENG")
    assert (p.season, p.episode, p.episode_end) == (3, 1, 10)
    p = parse("Lost Stagione 1-6 Completa 1080p ITA")
    assert p.season == 1 and p.season_end == 6
    p = parse("Lost - S05E06 - 316 Bluray-1080p.mkv")
    assert (p.season, p.episode, p.episode_end) == (5, 6, None)
    p = parse("Lost - S06E17-E18 - The End Bluray-1080p.mkv")
    assert (p.episode, p.episode_end) == (17, 18)
    p = parse("The.Sopranos.3x05.Another.Toothpick.ITA")
    assert (p.season, p.episode) == (3, 5)


def test_parse_quality():
    assert parse("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC.TrueHD-FGT").source == "remux"
    p = parse("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC.TrueHD-FGT")
    assert p.dv and p.hdr and p.resolution == 2160
    assert parse("Gladiator II 2024 HDCAM x264").source == "cam"
    assert parse("Mickey 17 (2025) TS 720p ITA").source == "cam"
    assert parse("Conclave 2024 1080p WEB-DL DDP5.1 H.264-FLUX").source == "web-dl"


def test_normalize():
    assert normalize("L'amica geniale: Storia!") == "l amica geniale storia"
    assert normalize("Emilia Pérez") == "emilia perez"


def rel(name, seeders=50, gb=2.0, **kw):
    return {"name": name, "seeders": seeders, "size": gb * GB, "hash": name, **kw}


def test_rank_italian_first():
    want = Want(titles=["Conclave"], year=2024)
    out = rank([
        rel("Conclave.2024.1080p.WEB-DL.DDP5.1.H.264-FLUX", seeders=900),
        rel("Conclave (2024) 1080p H264 ITA ENG AC3 5.1 Sub Ita Eng - MIRCrew", seeders=40),
        rel("Conclave.2024.720p.CAM.x264", seeders=2000),
        rel("Conclave.2024.1080p.WEBRip.FRENCH.x264", seeders=300),
        rel("Conclave.1999.DVDRip.XviD", seeders=10),
    ], want, Profile())
    assert "MIRCrew" in out[0]["name"], out[0]
    assert "FLUX" in out[1]["name"]
    rej = {r["name"]: r["rejected"] for r in out}
    assert rej["Conclave.2024.720p.CAM.x264"] == "cam/telesync"
    assert rej["Conclave.2024.1080p.WEBRip.FRENCH.x264"].startswith("language")
    assert rej["Conclave.1999.DVDRip.XviD"] == "different title"


def test_rank_title_guard():
    want = Want(titles=["Dune: Part Two", "Dune - Parte due"], year=2024)
    out = rank([
        rel("Dune (2021) FullHD 1080p.H264 Ita Eng AC3 5.1 Sub"),
        rel("Dune - Parte Due (2024) 1080p H265 ITA ENG AC3 - MIRCrew"),
        rel("Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"),
    ], want, Profile())
    assert out[0]["name"].startswith("Dune - Parte Due")
    assert out[1]["name"].startswith("Dune.Part.Two")
    assert out[2]["rejected"] == "different title"


def test_rank_series():
    want = Want(titles=["The Bear"], type="series", season=4, episode=2)
    out = rank([
        rel("The.Bear.S04E02.1080p.WEB.h264-ETHEL", gb=1.2),
        rel("The Bear S04E02 ITA ENG 1080p WEB-DL DDP5.1 H264-MIRCrew", gb=1.5, seeders=12),
        rel("The Bear S04 ITA ENG 1080p WEB-DL H264-MIRCrew", gb=14, seeders=30),
        rel("The.Bear.S04E03.1080p.WEB.h264-ETHEL", gb=1.2),
        rel("The.Bear.S03E02.1080p.WEB.h264-ETHEL", gb=1.2),
    ], want, Profile())
    assert "S04E02 ITA" in out[0]["name"], [r["name"] for r in out]
    rej = {r["name"]: r["rejected"] for r in out}
    assert rej["The.Bear.S04E03.1080p.WEB.h264-ETHEL"] == "wrong episode 3"
    assert rej["The.Bear.S03E02.1080p.WEB.h264-ETHEL"] == "wrong season 3"


def test_rank_flags_and_trusted():
    want = Want(titles=["Dune"], year=2021)
    out = rank([
        rel("Дюна / Dune: Part One [2021 UHD BDRemux 2160p] Dub + Original (Eng)", gb=18,
            flags="🇬🇧 / 🇷🇺 / 🇺🇦", trusted=True),
        rel("Dune.2021.REPACK.4K.HDR.DV.2160p.BDRemux Ita Eng x265-NAHOM", gb=19,
            flags="🇬🇧 / 🇮🇹", trusted=True),
    ], want, Profile())
    assert "NAHOM" in out[0]["name"] and out[0]["language"] == "it"
    assert out[1]["language"] == "en" and out[1]["rejected"] is None


def test_many_flags_are_subtitles():
    want = Want(titles=["The Bear"], type="series", season=4, episode=2)
    out = rank([rel("The Bear S04E02 2160p DSNP WEB-DL DDP5 1 H 265-FLUX", trusted=True,
                    flags="🇬🇧 / 🇯🇵 / 🇮🇹 / 🇵🇹 / 🇪🇸 / 🇰🇷 / 🇨🇳 / 🇫🇷 / 🇩🇪")], want, Profile())
    assert out[0]["language"] == "en" and "it" in out[0]["subs"]


def test_torrentio_pack_for_whole_season():
    want = Want(titles=["The Bear"], type="series", season=5, episode=None)
    out = rank([rel("The.Bear.S05.ITA.ENG.1080p.WEB-DL-MeM", trusted=True,
                    file="The.Bear.S05E01.ITA.ENG.1080p.mkv", file_idx=0,
                    match_name="The.Bear.S05.ITA.ENG.1080p.WEB-DL-MeM The.Bear.S05E01.ITA.ENG.1080p.mkv")],
               want, Profile())
    assert out[0]["rejected"] is None and out[0]["pack"]


def test_size_limits():
    want = Want(titles=["Dune"], year=2021)
    out = rank([rel("Dune.2021.2160p.BluRay.REMUX.ITA.ENG", gb=80)], want, Profile())
    assert out[0]["rejected"].startswith("too big")


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
    print(f"{n} tests passed")
