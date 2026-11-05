"""Heating log and gas-use statistics for the Ariston plugin (pure Python + the hub store, no cloud).

What is recorded (tables prefixed ``ariston_``, all in the hub SQLite file, kept two years):

* ``ariston_session``  intervals in which something was on: ``request`` (the zone asks for heat),
  ``heat`` (burner firing for the radiators), ``dhw`` (burner firing for hot water), ``other`` (burner
  firing, cause unknown). Time resolution is the poll interval (about 2 minutes).
* ``ariston_log``      mode and set-point changes, with who made them (``hub`` = this app or HomeKit,
  ``external`` = seen on the boiler or in the Ariston app).
* ``ariston_day`` / ``ariston_month``  gas use per day / month as reported by the Ariston cloud
  (consumption sequences): exact, the boiler's own meter, but daily at best.
* ``ariston_outdoor``  hourly outdoor temperature (Open-Meteo, or the boiler's probe) for degree-days.

The burner of a combi boiler also fires for hot water. A flame sample is attributed to heating when the
zone requests heat and the heating pump runs (or is unknown), to hot water when the zone requests no heat
or the pump is stopped, and is left unattributed only if the inputs say nothing at all.
"""

from __future__ import annotations

import statistics
import time
from datetime import date, datetime, timedelta

from .i18n import _

RETENTION_DAYS = 730
HDD_BASE = 18.0
GAS_KWH_PER_M3 = 10.69   # standard natural gas, upper heating value, per standard cubic metre (Smc)
GAP = 600                # seconds without observations after which open sessions are closed

# kWh per unit of the account's "gas energy unit" (ariston.const.GasEnergyUnit names); None = volume
UNIT_KWH = {"KWH": 1.0, "GIGA_JOULE": 277.7778, "THERM": 29.3071, "MEGA_BTU": 293.071,
            "SMC": None, "CUBE_METER": None}

# ariston.const.ConsumptionType values and ConsumptionTimeInterval ("p") values
K_CH_ENERGY, K_DHW_ENERGY, K_CH_GAS, K_DHW_GAS = 1, 2, 7, 10
P_DAY, P_WEEK, P_MONTH, P_YEAR = 1, 2, 3, 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS ariston_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, t0 REAL NOT NULL, t1 REAL,
    temp0 REAL, temp1 REAL, target0 REAL, out0 REAL);
CREATE INDEX IF NOT EXISTS ariston_session_t ON ariston_session(t0);
CREATE TABLE IF NOT EXISTS ariston_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    old TEXT, new TEXT, who TEXT);
CREATE TABLE IF NOT EXISTS ariston_day (
    day TEXT PRIMARY KEY, heat_kwh REAL, dhw_kwh REAL, heat_m3 REAL, dhw_m3 REAL,
    src TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS ariston_month (
    month TEXT PRIMARY KEY, heat_kwh REAL, dhw_kwh REAL, heat_m3 REAL, dhw_m3 REAL,
    src TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS ariston_outdoor (
    ts INTEGER NOT NULL, src TEXT NOT NULL, temp REAL, PRIMARY KEY (ts, src)) WITHOUT ROWID;
"""


def local_ts(d: date) -> float:
    """Start of a local calendar day as a timestamp."""
    return datetime(d.year, d.month, d.day).astimezone().timestamp()


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def classify_flame(heat_req: bool, pump) -> str:
    """Who is the burner firing for? ``pump`` is True/False/None (unknown)."""
    if not heat_req:
        return "dhw"
    if pump is False:
        return "dhw"
    return "heat"


def to_kwh(value: float, unit: str | None, kwh_per_m3: float = GAS_KWH_PER_M3) -> tuple[float, float]:
    """Gas figure in the account's unit -> (fuel kWh, standard m3)."""
    u = (unit or "").upper()
    if u in UNIT_KWH and UNIT_KWH[u] is None:      # volume units (Smc, m3)
        return value * kwh_per_m3, value
    kwh = value * UNIT_KWH.get(u, 1.0)             # unknown unit: assume kWh (the Ariston default)
    return kwh, kwh / kwh_per_m3


def _align(values: list, last: date, step: str) -> list[tuple[str, float]]:
    """Dates for a sequence. Checked on the real boiler (2026-11-05): daily series end TODAY (this morning's
    hourly values add up to the last daily value), monthly series end LAST MONTH (Oct 2–31 from the daily
    series + Oct 1 = the last monthly value; the current month is not in it)."""
    out = []
    n = len(values)
    for i, v in enumerate(values):
        back = n - 1 - i
        if step == "day":
            key = (last - timedelta(days=back)).strftime("%Y-%m-%d")
        else:
            y, m = last.year, last.month - 1 - back
            while m <= 0:
                y, m = y - 1, m + 12
            key = f"{y:04d}-{m:02d}"
        out.append((key, v))
    return out


def parse_sequences(seqs: list, unit: str | None, today: date,
                    kwh_per_m3: float = GAS_KWH_PER_M3) -> dict:
    """Ariston consumption sequences -> {"day": {date: row}, "month": {month: row}}.

    Each sequence is ``{"k": ConsumptionType, "p": ConsumptionTimeInterval, "v": [numbers...]}``;
    p=3 holds about 30 daily values, p=2 seven daily values, p=4 twelve monthly values; the last value
    is the current period. Energy types (1, 2) are kWh, gas types (7, 10) are in ``unit``.
    Gas wins over total energy when both exist for the same use."""
    rows: dict = {"day": {}, "month": {}}
    for want, (pset, step) in (("day", ((P_MONTH, P_WEEK), "day")), ("month", ((P_YEAR,), "month"))):
        for p in pset:
            found = False
            for s in seqs or []:
                if s.get("p") != p or not isinstance(s.get("v"), list):
                    continue
                k = s.get("k")
                if k not in (K_CH_ENERGY, K_DHW_ENERGY, K_CH_GAS, K_DHW_GAS):
                    continue
                found = True
                use = "heat" if k in (K_CH_ENERGY, K_CH_GAS) else "dhw"
                gas = k in (K_CH_GAS, K_DHW_GAS)
                for key, v in _align(s["v"], today, step):
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    row = rows[want].setdefault(key, {})
                    if gas:
                        kwh, m3 = to_kwh(float(v), unit, kwh_per_m3)
                        row[f"{use}_kwh"], row[f"{use}_m3"], row[f"{use}_src"] = kwh, m3, "gas"
                    elif row.get(f"{use}_src") != "gas":
                        row[f"{use}_kwh"], row[f"{use}_m3"] = float(v), float(v) / kwh_per_m3
                        row[f"{use}_src"] = "energy"
            if found:
                break  # a finer/longer sequence found; skip the shorter fallback
    return rows


class Heating:
    def __init__(self, store, price: float = 1.10, kwh_per_m3: float = GAS_KWH_PER_M3):
        self.store = store
        self.price = price
        self.kwh_per_m3 = kwh_per_m3
        self.gas_unit: str | None = store.get("ariston.heat.unit")
        self.gas_type: str | None = None
        self.account_cost: float | None = None
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                store.execute(stmt)
        self._ob = (0, 0)
        self.open: dict[str, int] = {}          # kind -> session id
        self.last_ts: float | None = store.get("ariston.heat.last_ts")
        self.last: dict = store.get("ariston.heat.last", {})
        self._last_prune = 0.0
        self._close_stale()
        self.now: dict = {}                     # latest observation, for /today
        self.error: str | None = None
        self.error_ts: float | None = None
        self.energy_ts: float | None = store.get("ariston.heat.energy_ts")

    # -- sessions and log ---------------------------------------------------------------

    def _close_stale(self) -> None:
        """Sessions left open by a restart end where we last saw the boiler."""
        end = self.last_ts or time.time()
        self.store.execute("UPDATE ariston_session SET t1=MAX(t0, ?) WHERE t1 IS NULL", (end,))

    def observe(self, ts: float, *, heat_req: bool, flame: bool | None, pump=None,
                temp=None, target=None, mode=None, outdoor=None) -> None:
        """One poll of the boiler: open/close sessions, log mode and set-point changes."""
        if self.last_ts and ts - self.last_ts > GAP:
            self._close_all(self.last_ts)
        active = set()
        if heat_req:
            active.add("request")
        if flame:
            active.add(classify_flame(heat_req, pump))
        for kind in ("request", "heat", "dhw", "other"):
            sid = self.open.get(kind)
            if kind in active and sid is None:
                cur = self.store.execute(
                    "INSERT INTO ariston_session(kind, t0, temp0, target0, out0) VALUES(?,?,?,?,?)",
                    (kind, ts, temp, target, outdoor))
                self.open[kind] = cur.lastrowid
            elif kind not in active and sid is not None:
                self.store.execute("UPDATE ariston_session SET t1=?, temp1=? WHERE id=?", (ts, temp, sid))
                del self.open[kind]
        for field, value in (("mode", mode), ("target", target)):
            if value is None:
                continue
            old = self.last.get(field)
            if old is not None and old != value:
                self._log(ts, field, old, value, "external")
            self.last[field] = value
        self.last_ts = ts
        self.store.set("ariston.heat.last_ts", ts)
        self.store.set("ariston.heat.last", self.last)
        self.now = {"ts": ts, "heat_request": bool(heat_req), "flame": flame, "pump": pump,
                    "temp": temp, "target": target, "mode": mode, "outdoor": outdoor,
                    "burner": classify_flame(heat_req, pump) if flame else None}
        if ts - self._last_prune > 86400:
            self._last_prune = ts
            self.prune(ts)

    def _close_all(self, ts: float) -> None:
        for kind, sid in list(self.open.items()):
            self.store.execute("UPDATE ariston_session SET t1=MAX(t0, ?) WHERE id=?", (ts, sid))
            del self.open[kind]

    def note_change(self, field: str, new, ts: float | None = None) -> None:
        """A change made through the hub (app, HomeKit): logged as ``hub`` and remembered, so the
        next poll does not log it again as external."""
        ts = ts or time.time()
        old = self.last.get(field)
        if old != new:
            self._log(ts, field, old, new, "hub")
        self.last[field] = new
        self.store.set("ariston.heat.last", self.last)

    def _log(self, ts, kind, old, new, who) -> None:
        self.store.execute("INSERT INTO ariston_log(ts, kind, old, new, who) VALUES(?,?,?,?,?)",
                           (ts, kind, None if old is None else str(old), str(new), who))

    def prune(self, now: float) -> None:
        cut = now - RETENTION_DAYS * 86400
        for sql, arg in (("DELETE FROM ariston_session WHERE t0 < ?", cut),
                         ("DELETE FROM ariston_log WHERE ts < ?", cut),
                         ("DELETE FROM ariston_outdoor WHERE ts < ?", cut),
                         ("DELETE FROM ariston_day WHERE day < ?", day_of(cut))):
            self.store.execute(sql, (arg,))
        self.store.execute("DELETE FROM ariston_month WHERE month < ?",
                           (datetime.fromtimestamp(cut).strftime("%Y-%m"),))

    # -- energy (cloud consumption sequences) ------------------------------------------------

    def ingest_energy(self, seqs: list, unit: str | None, now: float | None = None) -> int:
        now = now or time.time()
        if unit and unit != self.gas_unit:
            self.store.set("ariston.heat.unit", unit)
        self.gas_unit = unit or self.gas_unit
        unit = self.gas_unit
        rows = parse_sequences(seqs, unit, datetime.fromtimestamp(now).date(), self.kwh_per_m3)
        n = 0
        for table, col, data in (("ariston_day", "day", rows["day"]), ("ariston_month", "month", rows["month"])):
            for key, r in data.items():
                src = "gas" if "gas" in (r.get("heat_src"), r.get("dhw_src")) else "energy"
                self.store.execute(
                    f"INSERT INTO {table}({col}, heat_kwh, dhw_kwh, heat_m3, dhw_m3, src, updated) "
                    f"VALUES(?,?,?,?,?,?,?) ON CONFLICT({col}) DO UPDATE SET heat_kwh=excluded.heat_kwh, "
                    "dhw_kwh=excluded.dhw_kwh, heat_m3=excluded.heat_m3, dhw_m3=excluded.dhw_m3, "
                    "src=excluded.src, updated=excluded.updated",
                    (key, r.get("heat_kwh"), r.get("dhw_kwh"), r.get("heat_m3"), r.get("dhw_m3"), src, now))
                n += 1
        self.energy_ts = now
        self.store.set("ariston.heat.energy_ts", now)
        return n

    # -- outdoor temperature -------------------------------------------------------------------

    def add_outdoor(self, points: list[tuple[float, float]], src: str) -> None:
        for ts, temp in points:
            if temp is None:
                continue
            self.store.execute("INSERT OR REPLACE INTO ariston_outdoor(ts, src, temp) VALUES(?,?,?)",
                               (int(ts) // 3600 * 3600, src, float(temp)))

    def add_boiler_outdoor(self, ts: float, temp: float) -> None:
        """Running hourly mean of the boiler's own outdoor probe."""
        hour = int(ts) // 3600 * 3600
        row = self.store.query("SELECT temp FROM ariston_outdoor WHERE ts=? AND src='boiler'", (hour,))
        cnt = self._ob[1] if self._ob[0] == hour else 0
        new = temp if not row or not cnt else (row[0]["temp"] * cnt + temp) / (cnt + 1)
        self._ob = (hour, cnt + 1)
        self.add_outdoor([(hour, new)], "boiler")

    def outdoor_by_day(self, first: str, last: str) -> dict[str, float]:
        t0 = local_ts(date.fromisoformat(first))
        t1 = local_ts(date.fromisoformat(last) + timedelta(days=1))
        per: dict[str, dict[str, list]] = {}
        for r in self.store.query("SELECT ts, src, temp FROM ariston_outdoor WHERE ts>=? AND ts<?", (t0, t1)):
            per.setdefault(day_of(r["ts"]), {}).setdefault(r["src"], []).append(r["temp"])
        out = {}
        for d, by in per.items():
            vals = by.get("om") or by.get("boiler")
            if vals and len(vals) >= 4:   # a few hours at least, else the mean misleads
                out[d] = sum(vals) / len(vals)
        return out

    # -- queries ----------------------------------------------------------------------------------

    def _sessions(self, t0: float, t1: float, kinds=None) -> list[dict]:
        rows = self.store.query(
            "SELECT id, kind, t0, t1, temp0, temp1, target0, out0 FROM ariston_session "
            "WHERE t0 < ? AND COALESCE(t1, ?) > ? ORDER BY t0", (t1, t1, t0))
        return [r for r in rows if kinds is None or r["kind"] in kinds]

    def hours_by_day(self, first: date, last: date, now: float | None = None) -> dict[str, dict[str, float]]:
        now = now or time.time()
        res: dict[str, dict[str, float]] = {}
        d = first
        while d <= last:
            res[d.strftime("%Y-%m-%d")] = {"request": 0.0, "heat": 0.0, "dhw": 0.0, "other": 0.0}
            d += timedelta(days=1)
        for s in self._sessions(local_ts(first), local_ts(last + timedelta(days=1))):
            a, b = s["t0"], s["t1"] if s["t1"] is not None else now
            d = datetime.fromtimestamp(a).date()
            while True:
                lo, hi = local_ts(d), local_ts(d + timedelta(days=1))
                seg = min(b, hi) - max(a, lo)
                key = d.strftime("%Y-%m-%d")
                if seg > 0 and key in res:
                    res[key][s["kind"]] += seg / 3600
                if hi >= b:
                    break
                d += timedelta(days=1)
        return res

    def usage(self, days: int = 30, now: float | None = None) -> dict:
        now = now or time.time()
        days = max(1, min(int(days), 800))
        today = datetime.fromtimestamp(now).date()
        first = today - timedelta(days=days - 1)
        rows = {r["day"]: r for r in self.store.query(
            "SELECT * FROM ariston_day WHERE day >= ?", (first.strftime("%Y-%m-%d"),))}
        hrs = self.hours_by_day(first, today, now)
        out = self.outdoor_by_day(first.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
        series = []
        for i in range(days):
            key = (first + timedelta(days=i)).strftime("%Y-%m-%d")
            r = rows.get(key)
            o = out.get(key)
            hdd = max(0.0, HDD_BASE - o) if o is not None else None
            item = {"day": key, "out_c": None if o is None else round(o, 1),
                    "hdd": None if hdd is None else round(hdd, 1),
                    "heat_h": round(hrs[key]["heat"], 2), "dhw_h": round(hrs[key]["dhw"], 2),
                    "request_h": round(hrs[key]["request"], 2), "other_h": round(hrs[key]["other"], 2),
                    "heat_kwh": None, "dhw_kwh": None, "heat_m3": None, "dhw_m3": None,
                    "cost": None, "heat_cost": None, "dhw_cost": None, "kwh_per_hdd": None}
            if r:
                for k in ("heat_kwh", "dhw_kwh", "heat_m3", "dhw_m3"):
                    item[k] = None if r[k] is None else round(r[k], 3)
                item["heat_cost"] = self._cost(r["heat_m3"])
                item["dhw_cost"] = self._cost(r["dhw_m3"])
                item["cost"] = self._sum(item["heat_cost"], item["dhw_cost"])
                if hdd and hdd >= 2 and r["heat_kwh"]:
                    item["kwh_per_hdd"] = round(r["heat_kwh"] / hdd, 2)
            series.append(item)
        return {"days": series, "price": self.price, "kwh_per_m3": self.kwh_per_m3,
                "gas_unit": self.gas_unit, "hdd_base": HDD_BASE}

    def _cost(self, m3) -> float | None:
        return None if m3 is None else round(m3 * self.price, 2)

    @staticmethod
    def _sum(*vals):
        v = [x for x in vals if x is not None]
        return round(sum(v), 2) if v else None

    def month_totals(self, month: str, now: float | None = None) -> dict | None:
        """Totals of a month: the cloud's monthly figure, or the sum of the days we hold. The current month
        is always the sum of its days (the cloud's monthly series stops at last month)."""
        cur = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m")
        r = self.store.query("SELECT * FROM ariston_month WHERE month=?", (month,)) if month != cur else []
        if r:
            r = r[0]
            tot = {k: r[k] for k in ("heat_kwh", "dhw_kwh", "heat_m3", "dhw_m3")}
        else:
            days = self.store.query("SELECT * FROM ariston_day WHERE day LIKE ?", (month + "-%",))
            if not days:
                return None
            tot = {k: sum(d[k] or 0 for d in days) for k in ("heat_kwh", "dhw_kwh", "heat_m3", "dhw_m3")}
        tot = {k: None if v is None else round(v, 1) for k, v in tot.items()}
        tot["cost"] = self._sum(self._cost(tot["heat_m3"]), self._cost(tot["dhw_m3"]))
        return tot

    def summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        today = datetime.fromtimestamp(now).date()
        cur = today.strftime("%Y-%m")
        prev_d = today.replace(day=1) - timedelta(days=1)
        prev = prev_d.strftime("%Y-%m")
        month, last_month = self.month_totals(cur, now), self.month_totals(prev, now)
        year_rows = self.store.query("SELECT * FROM ariston_month WHERE month >= ? AND month < ?",
                                     (f"{today.year}-01", cur))
        if month:      # the current month, from its days
            year_rows = [*year_rows, month]
        year = None
        if year_rows:
            year = {k: round(sum(r[k] or 0 for r in year_rows), 1)
                    for k in ("heat_kwh", "dhw_kwh", "heat_m3", "dhw_m3")}
            year["cost"] = self._sum(self._cost(year["heat_m3"]), self._cost(year["dhw_m3"]))
            year["months"] = len(year_rows)
        projection = None
        if month and today.day >= 3 and month["cost"] is not None:
            dim = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            per_day = month["cost"] / (today.day - 1 + (datetime.fromtimestamp(now).hour / 24))
            projection = {"cost": round(per_day * dim.day, 2),
                          "kwh": round(((month["heat_kwh"] or 0) + (month["dhw_kwh"] or 0))
                                       / (today.day - 1 + datetime.fromtimestamp(now).hour / 24) * dim.day)}
        days = self.usage(45, now)["days"]
        first = self.store.query("SELECT MIN(day) AS d FROM ariston_day")[0]["d"]
        return {"month": month, "last_month": last_month, "year": year, "projection": projection,
                "tips": self.tips(days, today, now), "price": self.price, "kwh_per_m3": self.kwh_per_m3,
                "gas_unit": self.gas_unit, "energy_updated": self.energy_ts, "first_day": first,
                "has_outdoor": bool(self.store.query("SELECT 1 FROM ariston_outdoor LIMIT 1")),
                "error": self.error, "error_ts": self.error_ts}

    def tips(self, days: list[dict], today: date, now: float) -> list[str]:
        """A few honest observations, only when the data support them."""
        out: list[str] = []
        yk = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        y = next((d for d in days if d["day"] == yk), None)
        base = [d["kwh_per_hdd"] for d in days if d["kwh_per_hdd"] and d["day"] < yk][-14:]
        if y and y["kwh_per_hdd"] and len(base) >= 5:
            ref = statistics.median(base)
            pct = round((y["kwh_per_hdd"] / ref - 1) * 100)
            if pct >= 20:
                out.append(_("Yesterday {pct}% more gas than usual for the same cold", pct=pct))
            elif pct <= -20:
                out.append(_("Yesterday {pct}% less gas than usual for the same cold", pct=-pct))
        t0 = local_ts(today)
        short = [s for s in self._sessions(t0, now, ("heat",))
                 if s["t1"] is not None and s["t1"] - s["t0"] <= 600]
        if len(short) >= 6:
            out.append(_("The burner started {n} short times today (under 10 minutes each)", n=len(short)))
        week = [d for d in days if d["day"] >= (today - timedelta(days=7)).strftime("%Y-%m-%d")
                and d["dhw_kwh"] is not None and d["heat_kwh"] is not None]
        tot = sum(d["heat_kwh"] + d["dhw_kwh"] for d in week)
        if len(week) >= 5 and tot > 0 and sum(d["dhw_kwh"] for d in week) / tot >= 0.5:
            out.append(_("Hot water is {pct}% of the gas this week", pct=round(
                100 * sum(d["dhw_kwh"] for d in week) / tot)))
        return out

    def today(self, now: float | None = None) -> dict:
        now = now or time.time()
        d = datetime.fromtimestamp(now).date()
        t0, t1 = local_ts(d), local_ts(d + timedelta(days=1))
        segs = [{"kind": s["kind"], "t0": max(s["t0"], t0),
                 "t1": min(s["t1"] if s["t1"] is not None else now, t1), "open": s["t1"] is None}
                for s in self._sessions(t0, t1)]
        hrs = self.hours_by_day(d, d, now)[d.strftime("%Y-%m-%d")]
        row = self.usage(1, now)["days"][0]
        return {"day": d.strftime("%Y-%m-%d"), "start": t0, "end": t1, "now": now, "segments": segs,
                "hours": {k: round(v, 2) for k, v in hrs.items()}, "live": self.now,
                "kwh": None if row["heat_kwh"] is None and row["dhw_kwh"] is None
                else round((row["heat_kwh"] or 0) + (row["dhw_kwh"] or 0), 1),
                "heat_kwh": row["heat_kwh"], "dhw_kwh": row["dhw_kwh"], "cost": row["cost"],
                "out_c": row["out_c"], "energy_updated": self.energy_ts, "price": self.price,
                "error": self.error, "error_ts": self.error_ts}

    def sessions(self, days: int = 3, now: float | None = None) -> dict:
        now = now or time.time()
        days = max(1, min(int(days), 800))
        t0 = local_ts(datetime.fromtimestamp(now).date() - timedelta(days=days - 1))
        rows = self._sessions(t0, now + 1, ("heat", "dhw", "other"))
        sessions = [{**r, "t1": r["t1"], "open": r["t1"] is None,
                     "minutes": round(((r["t1"] if r["t1"] is not None else now) - r["t0"]) / 60, 1)}
                    for r in rows]
        # a "request" interval is the heating demand; show it too, it is what the user calls "on"
        req = self._sessions(t0, now + 1, ("request",))
        requests = [{**r, "open": r["t1"] is None,
                     "minutes": round(((r["t1"] if r["t1"] is not None else now) - r["t0"]) / 60, 1)}
                    for r in req]
        log = self.store.query("SELECT ts, kind, old, new, who FROM ariston_log WHERE ts >= ? "
                               "ORDER BY ts DESC", (t0,))
        return {"sessions": sessions[::-1], "requests": requests[::-1], "log": log}
