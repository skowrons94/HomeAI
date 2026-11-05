"""Ariston heating log and gas use: sessions from state transitions, consumption sequences, degree-days,
cost, routes. Fake device, no network, temp dirs only."""
import asyncio
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import heating  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.heating import Heating, classify_flame, parse_sequences, to_kwh  # noqa: E402
from hub.plugins.ariston import AristonPlugin  # noqa: E402
from hub.store import Store  # noqa: E402

NOON = datetime(2026, 11, 5, 12, 0).astimezone().timestamp()
T0 = datetime(2026, 11, 5, 0, 0).astimezone().timestamp()
H = 3600


def new(tmp, **kw):
    return Heating(Store(Path(tmp) / "hub.db"), **kw)


def at(h, m=0):
    return T0 + h * H + m * 60


def test_flame_attribution():
    assert classify_flame(True, True) == "heat"
    assert classify_flame(True, None) == "heat"
    assert classify_flame(False, True) == "dhw"      # no heat request: hot water
    assert classify_flame(True, False) == "dhw"      # pump stopped: diverter on hot water


def test_sessions_and_shared_flame():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp)
        # 06:30 zone asks for heat, burner starts; 07:00 shower (pump stops); 07:10 back to heating;
        # 08:10 request ends and burner stops. Polls every 10 minutes.
        steps = [(6, 20, False, False, None, 20.0), (6, 30, True, True, True, 20.0), (6, 40, True, True, True, 20.4),
                 (6, 50, True, True, True, 20.8), (7, 0, True, True, False, 21.0), (7, 10, True, True, True, 21.0),
                 (7, 20, True, True, True, 21.2), (7, 30, True, True, True, 21.3), (7, 40, True, True, True, 21.4),
                 (7, 50, True, True, True, 21.5), (8, 0, True, True, True, 21.5), (8, 10, False, False, False, 21.5)]
        for h_, m, req, flame, pump, temp in steps:
            hh.observe(at(h_, m), heat_req=req, flame=flame, pump=pump, temp=temp, target=21.5, mode="auto")
        s = hh.sessions(1, now=at(9))
        req = [r for r in s["requests"]]
        assert len(req) == 1 and req[0]["minutes"] == 100.0 and req[0]["temp0"] == 20.0 and req[0]["temp1"] == 21.5
        kinds = [(x["kind"], x["minutes"]) for x in sorted(s["sessions"], key=lambda x: x["t0"])]
        assert kinds == [("heat", 30.0), ("dhw", 10.0), ("heat", 60.0)], kinds
        hrs = hh.hours_by_day(date(2026, 11, 5), date(2026, 11, 5), now=at(9))["2026-11-05"]
        assert abs(hrs["heat"] - 1.5) < 1e-9 and abs(hrs["dhw"] - 10 / 60) < 1e-9
        assert abs(hrs["request"] - 100 / 60) < 1e-9
        # an open session counts up to now and is flagged
        hh.observe(at(18), heat_req=True, flame=True, pump=True, temp=19, target=21, mode="auto")
        seg = hh.today(now=at(19))["segments"]
        assert seg[-1]["open"] and seg[-1]["t1"] == at(19)


def test_gap_closes_sessions_and_restart():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp)
        hh.observe(at(6), heat_req=True, flame=True, pump=True, mode="heat", target=20)
        hh.observe(at(6, 2), heat_req=True, flame=True, pump=True, mode="heat", target=20)
        hh.observe(at(9), heat_req=False, flame=False, mode="heat", target=20)   # hub was offline 3 h
        r = hh.sessions(1, now=at(10))["requests"][0]
        assert r["t1"] == at(6, 2), "ends where the boiler was last seen, not after the gap"
        hh.observe(at(10), heat_req=True, flame=False, mode="heat", target=20)
        hh2 = Heating(hh.store)                               # restart with a session open
        assert all(s["t1"] is not None for s in hh2.sessions(1, now=at(11))["requests"])
        assert hh2.sessions(1, now=at(11))["requests"][0]["t1"] == at(10)


def test_log_who_changed():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp)
        hh.observe(at(7), heat_req=False, flame=False, mode="auto", target=20.0)
        hh.note_change("target", 21.0, ts=at(7, 5))                  # from the app
        hh.observe(at(7, 10), heat_req=False, flame=False, mode="auto", target=21.0)   # echo: no 2nd entry
        hh.observe(at(7, 20), heat_req=False, flame=False, mode="heat", target=19.5)   # someone at the boiler
        log = hh.sessions(1, now=at(8))["log"]
        got = sorted((r["kind"], r["old"], r["new"], r["who"]) for r in log)
        assert got == [("mode", "auto", "heat", "external"), ("target", "20.0", "21.0", "hub"),
                       ("target", "21.0", "19.5", "external")], got


# realistic structures: what ariston_api.get_consumptions_sequences returns, see ariston/base_device.py
def seqs(today_val=3.0):
    month = [5.0] * 29 + [today_val]
    return [
        {"k": 1, "p": 1, "v": [0.0] * 23 + [0.4]},
        {"k": 1, "p": 3, "v": [x * 1.0 for x in month]},          # heating energy kWh, 30 days
        {"k": 2, "p": 3, "v": [1.0] * 30},                          # hot water energy kWh
        {"k": 7, "p": 3, "v": [x * 1.2 for x in month]},            # heating gas in account unit
        {"k": 10, "p": 3, "v": [1.5] * 30},                         # hot water gas
        {"k": 1, "p": 4, "v": [100.0] * 11 + [20.0]},               # 12 months
        {"k": 7, "p": 4, "v": [120.0] * 11 + [24.0]},
    ]


def test_parse_sequences_units():
    rows = parse_sequences(seqs(), "KWH", date(2026, 11, 5))
    d = rows["day"]
    assert len(d) == 30 and "2026-11-05" in d and "2026-10-07" in d and "2026-10-06" not in d
    assert d["2026-11-05"]["heat_kwh"] == 3.0 * 1.2           # gas beats total energy when both exist
    assert d["2026-11-05"]["heat_src"] == "gas"
    assert abs(d["2026-11-05"]["heat_m3"] - 3.6 / 10.69) < 1e-9
    assert d["2026-11-04"]["dhw_kwh"] == 1.5
    m = rows["month"]      # the monthly series ends LAST month: the current one comes from the days
    assert "2026-11" not in m and m["2026-10"]["heat_kwh"] == 24.0 and m["2025-11"]["heat_kwh"] == 120.0
    assert "2025-10" not in m


def test_alignment_matches_the_real_boiler():
    """Values read from the user's boiler on 2026-11-05 11:27: the daily heating series (p=3) ends today,
    the monthly one (p=4) ends with October: Oct 2-31 from the daily series + Oct 1 = 190.91 kWh."""
    daily = [0.0] * 15 + [5.37] + [0.0] * 7 + [17.18, 34.36, 30.0, 24.3, 22.7, 22.12, 18.14,
                                                16.74, 17.36, 21.03, 24.75, 37.6]
    monthly = [1733.22, 1807.24, 1425.98, 900.33, 238.81, 7.56, 0, 0, 0, 441.62, 1191.81, 1417.53, 1628.84,
               1071.89, 682.66, 58.73, 70.28, 0, 0, 0, 0, 190.91]
    rows = parse_sequences([{"k": 7, "p": 3, "v": daily}, {"k": 7, "p": 4, "v": monthly}], "KWH", date(2026, 11, 5))
    d, m = rows["day"], rows["month"]
    assert d["2026-11-05"]["heat_kwh"] == 37.6 and d["2026-10-02"]["heat_kwh"] == 0.0 and "2026-10-01" not in d
    october_days = sum(v["heat_kwh"] for k, v in d.items() if k.startswith("2026-10"))
    assert m["2026-10"]["heat_kwh"] == 190.91 and october_days <= 190.91 and "2026-11" not in m
    assert m["2025-01"]["heat_kwh"] == 1733.22                       # 22 months back from October 2026
    # volume unit: the figure is Smc, energy follows from the heating value
    r2 = parse_sequences([{"k": 7, "p": 3, "v": [2.0]}], "SMC", date(2026, 11, 5), 10.0)
    assert r2["day"]["2026-11-05"]["heat_m3"] == 2.0 and r2["day"]["2026-11-05"]["heat_kwh"] == 20.0
    # energy only (no gas sequences): used, labelled "energy"
    r3 = parse_sequences([{"k": 1, "p": 3, "v": [4.0, 5.0]}], "KWH", date(2026, 11, 5))
    assert r3["day"]["2026-11-04"]["heat_src"] == "energy" and r3["day"]["2026-11-05"]["heat_kwh"] == 5.0
    # weekly fallback when no monthly sequence exists; junk is ignored
    r4 = parse_sequences([{"k": 7, "p": 2, "v": [1, 2, None, "x", 5, 6, 7]}, {"k": 99, "p": 3, "v": [1]},
                          {"p": 3}], "KWH", date(2026, 11, 5))
    assert sorted(r4["day"]) == ["2026-10-30", "2026-10-31", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert to_kwh(1.0, "GIGA_JOULE")[0] > 277 and abs(to_kwh(10.0, "THERM")[0] - 293.071) < 1e-6
    assert to_kwh(5.0, None) == (5.0, 5.0 / heating.GAS_KWH_PER_M3)


def test_usage_cost_degree_days_tips():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp, price=1.0)
        hh.ingest_energy(seqs(today_val=3.0), "SMC", now=NOON)      # SMC: figure is m3
        # outdoor 8 C every hour of the last 31 days -> 10 degree-days a day (base 18)
        pts = [(NOON - n * H, 8.0) for n in range(24 * 31)]
        hh.add_outdoor(pts, "om")
        u = hh.usage(30, now=NOON)
        assert u["days"][-1]["day"] == "2026-11-05" and len(u["days"]) == 30
        d = u["days"][-2]
        assert d["out_c"] == 8.0 and d["hdd"] == 10.0
        assert d["heat_m3"] == 6.0 and d["dhw_m3"] == 1.5
        assert d["heat_cost"] == 6.0 and d["cost"] == 7.5                      # price 1.0 per m3
        assert abs(d["heat_kwh"] - 6.0 * heating.GAS_KWH_PER_M3) < 0.01
        assert abs(d["kwh_per_hdd"] - 6.0 * heating.GAS_KWH_PER_M3 / 10) < 0.01
        # yesterday far above the usual kWh per degree-day -> a tip
        hh.store.execute("UPDATE ariston_day SET heat_m3=9, heat_kwh=? WHERE day='2026-11-04'",
                         (9 * heating.GAS_KWH_PER_M3,))
        s = hh.summary(now=NOON)
        assert any("50%" in t for t in s["tips"]), s["tips"]
        # current month = sum of its days; last month = the cloud's figure (its series ends with October)
        nov = round(sum(x["heat_m3"] or 0 for x in u["days"] if x["day"].startswith("2026-11")), 1)
        assert s["month"]["heat_m3"] == round(nov - 6.0 + 9.0, 1) and s["last_month"]["heat_m3"] == 24.0
        # year 2026: Jan-Sep 120 each + October 24 (cloud) + November so far (days)
        assert s["year"]["months"] == 11 and abs(s["year"]["heat_m3"] - (9 * 120 + 24 + s["month"]["heat_m3"])) < 0.2
        assert s["projection"] is not None and s["projection"]["cost"] > 0
        assert s["first_day"] == "2026-10-07" and s["has_outdoor"]


def test_tariffs_apply_by_date_with_fixed_fee():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp, price=1.0)
        hh.ingest_energy(seqs(today_val=3.0), "KWH", now=NOON)
        # before any tariff: the settings price (1.0 €/Smc) and the standard PCS
        d = hh.usage(30, now=NOON)["days"]
        day = next(x for x in d if x["heat_kwh"])
        assert abs(day["heat_cost"] - day["heat_kwh"] / heating.GAS_KWH_PER_M3 * 1.0) < 0.011
        hh.set_tariffs([{"from": "2026-11-01", "price": 1.25, "fixed": 9.5, "pcs": 10.5},
                        {"from": "2026-01-01", "price": 0.9, "fixed": 6}])
        assert [t["from"] for t in hh.tariffs] == ["2026-01-01", "2026-11-01"]       # sorted
        assert hh.tariff_on("2026-10-31")["price"] == 0.9 and hh.tariff_on("2026-11-05")["price"] == 1.25
        assert hh.tariff_on("2025-12-31")["price"] == 1.0                             # settings before the first
        u = {x["day"]: x for x in hh.usage(30, now=NOON)["days"]}
        nov, octo = u["2026-11-04"], u["2026-10-20"]
        assert abs(nov["heat_cost"] - round(nov["heat_kwh"] / 10.5 * 1.25, 2)) < 1e-9
        assert abs(octo["heat_cost"] - round(octo["heat_kwh"] / 10.69 * 0.9, 2)) < 1e-9
        s = hh.summary(now=NOON)
        # November in progress (5th, noon): fixed fee pro rata, gas at the November tariff
        assert 0 < s["month"]["fixed"] < 9.5 and s["month"]["cost"] == round(s["month"]["gas_cost"] + s["month"]["fixed"], 2)
        assert s["last_month"]["fixed"] == 6 and s["tariff"]["price"] == 1.25
        for bad in ([{"from": "x", "price": 1}], [{"from": "2026-01-01", "price": 0}],
                    [{"from": "2026-01-01", "price": 1}, {"from": "2026-01-01", "price": 2}],
                    [{"from": "2026-01-01", "price": 1, "pcs": 30}]):
            try:
                hh.set_tariffs(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        assert len(hh.tariffs) == 2                                                    # a bad save changes nothing


def test_degree_days_need_enough_hours():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp)
        hh.add_outdoor([(NOON - n * H, 5.0) for n in range(2)], "boiler")     # two hours only
        u = hh.usage(1, now=NOON)
        assert u["days"][0]["out_c"] is None and u["days"][0]["hdd"] is None
        hh.add_outdoor([(NOON - n * H, 25.0) for n in range(12)], "om")
        assert hh.usage(1, now=NOON)["days"][0]["hdd"] == 0.0                # warm day: no degree-days


def test_empty_state_is_honest():
    with tempfile.TemporaryDirectory() as tmp:
        hh = new(tmp)
        t = hh.today(now=NOON)
        assert t["segments"] == [] and t["kwh"] is None and t["cost"] is None
        s = hh.summary(now=NOON)
        assert s["month"] is None and s["year"] is None and s["projection"] is None and s["first_day"] is None
        assert hh.usage(7, now=NOON)["days"][0]["heat_kwh"] is None
        assert hh.sessions(3, now=NOON) == {"sessions": [], "requests": [], "log": []}


class FakeDev:
    is_plant_in_heat_mode = True
    zone_numbers = [1]
    is_flame_on_value = True
    is_heating_pump_on_value = True
    outside_temp_value = "7.5"
    gas_energy_unit = "KWH"
    gas_type = "NATURAL_GAS"
    consumptions_sequences = seqs()
    calls = 0

    def get_zone_mode(self, z):
        from ariston.const import ZoneMode
        return ZoneMode.TIME_PROGRAM

    def is_zone_in_time_program_mode(self, z):
        return True

    def get_measured_temp_value(self, z):
        return 20.5

    def get_comfort_temp_value(self, z):
        return 21.0

    def get_zone_heat_request_value(self, z):
        return True

    async def async_update_energy(self):
        FakeDev.calls += 1


class Req:
    def __init__(self, **q):
        self.query = {k: str(v) for k, v in q.items()}


def test_plugin_and_routes():
    with tempfile.TemporaryDirectory() as tmp:
        hub = Hub({"homekit": {"enabled": False}}, Path(tmp))
        p = AristonPlugin(hub, {"gas_price": 1.25})
        p.layout = {"gateway": "gw", "zones": [{"zone": 1, "min": 10, "max": 30, "step": 0.5}], "dhw": None,
                    "galevo": True}
        p.device = FakeDev()
        p._create_entities()
        p._observe()
        assert p.heat.price == 1.25 and "ariston.energy" in hub.entities
        assert hub.entities["ariston.energy"].state["burner_hours_today"] >= 0
        asyncio.run(p.refresh_energy())
        assert FakeDev.calls == 1 and p.heat.energy_ts
        asyncio.run(p.refresh_energy())                     # rate limit: second call within the interval
        assert FakeDev.calls == 1
        for route, args, key in (("r_today", {}, "segments"), ("r_summary", {}, "tips"),
                                 ("r_usage", {"days": 5}, "days"), ("r_sessions", {"days": 2}, "log")):
            r = asyncio.run(getattr(p, route)(Req(**args)))
            body = json.loads(r.text)
            assert r.status == 200 and key in body, (route, body)
        assert len(json.loads(asyncio.run(p.r_usage(Req(days=5))).text)["days"]) == 5
        assert len(json.loads(asyncio.run(p.r_usage(Req(days="junk"))).text)["days"]) == 30
        # a cloud failure keeps the last data and carries a note
        p.heat.error, p.heat.error_ts = "boom", 1.0
        assert json.loads(asyncio.run(p.r_today(Req())).text)["error"] == "boom"
        assert p.routes()[0].path == "/today" and len(p.routes()) == 6
        keys = {f["key"] for f in AristonPlugin.schema}
        assert {"gas_price", "gas_kwh_per_m3", "energy_interval", "latitude", "longitude"} <= keys


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
