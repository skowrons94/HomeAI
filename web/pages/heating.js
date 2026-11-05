// Heating: when the heating and the boiler ran, how much gas it used and what it cost (plugin: ariston).
// Data: /api/p/ariston/{today,usage,sessions,summary}. Gas figures come from the boiler's own daily
// counters (about an hour late), on/off times from the hub's polling (about 2 minutes).
import { api, h, icon, store, bus, ensureCss, fmt, t, locale, errorCard, sheet, act, toast } from "../app.js";

const C_HEAT = "#ff7a45", C_DHW = "#33a4d8", C_OUT = "#30b85a", C_OTHER = "#9a9aa5";
const num = (v, d = 1) => (v === null || v === undefined ? "–" : fmt.num(v, d));
const eur = (v) => (v === null || v === undefined ? "–" : (+v).toLocaleString(locale(), { style: "currency", currency: "EUR" }));
const mins = (m) => {
  m = Math.round(m);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
};
const hrs = (x) => mins(x * 60);
const deg = (v) => (v === null || v === undefined ? "–" : `${fmt.temp(v)} °C`);
const dayLabel = (d) => fmt.date(new Date(d + "T12:00:00"), { weekday: false });

export default {
  async render(root) {
    ensureCss(`/static/pages/heating.css?v=${window.CASA_V}`);
    let range = 30, metric = null, usage = null, sessions = null, today = null, summary = null;
    const now = h("div.card.heat-now");
    const todayBox = h("div.card.heat-today");
    const chartBox = h("div.card.heat-chart");
    const totals = h("div.heat-totals");
    const tips = h("div");
    const logBox = h("div");
    const note = h("div");

    root.append(
      h("header.page", h("h1", t("Heating")), h("div.spacer"),
        h("button.icon", { title: t("Refresh"), onclick: load }, icon("refresh"))),
      note, now, tips,
      h("div.section", h("h2", t("Today"))), todayBox,
      h("div.section", h("h2", t("Gas use")), h("div.spacer"), rangeSeg()), chartBox,
      h("div.section", h("h2", t("Totals"))), totals,
      h("div.section", h("h2", t("Log"))), logBox);

    function rangeSeg() {
      const seg = h("div.seg");
      const draw = () => seg.replaceChildren(...[[7, t("7 days")], [30, t("30 days")], [90, t("90 days")]].map(([n, l]) =>
        h(`button${range === n ? ".active" : ""}`, { onclick: () => { range = n; draw(); loadUsage(); } }, l)));
      draw();
      return seg;
    }

    async function loadUsage() {
      try { usage = await api(`/api/p/ariston/usage?days=${range}`); } catch (e) { usage = { error: e.message }; }
      drawChart();
    }

    async function load() {
      const [a, b, c, d] = await Promise.allSettled([
        api("/api/p/ariston/today"), api(`/api/p/ariston/usage?days=${range}`),
        api("/api/p/ariston/sessions?days=4"), api("/api/p/ariston/summary")]);
      if (a.status === "rejected" && d.status === "rejected") {
        root.replaceChildren(errorCard(a.reason?.message, { title: t("Heating data unavailable"), retry: () => location.reload() }));
        return;
      }
      today = a.value || today; usage = b.value || usage; sessions = c.value || sessions; summary = d.value || summary;
      drawAll();
    }

    function drawAll() { drawNote(); drawNow(); drawTips(); drawToday(); drawChart(); drawTotals(); drawLog(); }

    // -- status line ------------------------------------------------------------------------------
    function drawNote() {
      const err = summary?.error || today?.error;
      const ts = summary?.error_ts || today?.error_ts;
      note.replaceChildren(err ? h("div.heat-note.warn", icon("alert"),
        h("div", t("The Ariston cloud did not answer ({err}). Showing the last data received.", { err }),
          ts ? h("span.muted", ` · ${fmt.ago(ts)}`) : null)) : "");
    }

    function zone() { return [...store.entities.values()].find((e) => e.plugin === "ariston" && e.kind === "climate"); }

    function drawNow() {
      const z = zone(), live = today?.live || {};
      const burner = live.burner;
      const state = !z ? t("Not connected") : z.state.mode === "off" ? t("Heating off")
        : burner === "heat" ? t("Heating now") : burner === "dhw" ? t("Burner on for hot water")
        : z.state.heating ? t("Heat requested") : t("Idle");
      const lamp = burner === "heat" ? C_HEAT : burner === "dhw" ? C_DHW : z?.state.heating ? "var(--on)" : "var(--muted)";
      now.replaceChildren(...[
        h("div.heat-lamp", { style: { "--c": lamp } }, icon("flame")),
        h("div.heat-nowtxt", h("div.heat-state", state),
          h("div.muted", z?.state.current_temperature != null
            ? t("{cur} °C now, set to {tgt} °C", { cur: fmt.temp(z.state.current_temperature), tgt: fmt.temp(z.state.target_temperature) }) : "")),
        today?.out_c != null || live.outdoor != null
          ? h("div.heat-out", h("b", deg(live.outdoor ?? today.out_c)), h("span.muted", t("outside"))) : null].filter(Boolean));
    }

    function drawTips() {
      tips.replaceChildren(...(summary?.tips || []).map((s) => h("div.heat-tip", icon("chart"), h("span", s))));
    }

    // -- today ---------------------------------------------------------------------------------------
    function drawToday() {
      if (!today) return;
      const span = today.end - today.start;
      const pos = (ts) => `${Math.max(0, Math.min(100, ((ts - today.start) / span) * 100))}%`;
      const seg = (s, lane) => {
        const l = (s.t0 - today.start) / span * 100, w = Math.max(0.35, (s.t1 - s.t0) / span * 100);
        const label = { request: t("Heat requested"), heat: t("Heating"), dhw: t("Hot water"), other: t("Burner on") }[s.kind];
        return h(`i.heat-seg.${s.kind}`, { style: { left: `${l}%`, width: `${w}%` },
          title: `${label} ${fmt.time(s.t0)} → ${s.open ? t("now") : fmt.time(s.t1)} · ${mins((s.t1 - s.t0) / 60)}` });
      };
      const ticks = [0, 6, 12, 18, 24].map((x) => h("span", { style: { left: `${x / 24 * 100}%` } }, x === 24 ? "24" : String(x).padStart(2, "0")));
      const bar = h("div.heat-bar",
        h("div.heat-lane.req", today.segments.filter((s) => s.kind === "request").map((s) => seg(s))),
        h("div.heat-lane.burn", today.segments.filter((s) => s.kind !== "request").map((s) => seg(s))),
        today.now >= today.start && today.now <= today.end ? h("b.heat-nowmark", { style: { left: pos(today.now) } }) : null);
      const hr = today.hours;
      const stat = (label, value, sub) => h("div.heat-stat", h("div.muted", label), h("b", value), sub ? h("small.muted", sub) : null);
      const nodata = !today.segments.length;
      todayBox.replaceChildren(...[
        h("div.heat-legend",
          h("span", h("i", { style: { background: "color-mix(in srgb, var(--on) 45%, transparent)" } }), t("Heat requested")),
          h("span", h("i", { style: { background: C_HEAT } }), t("Heating")),
          h("span", h("i", { style: { background: C_DHW } }), t("Hot water"))),
        bar, h("div.heat-ticks", ticks),
        nodata ? h("div.heat-hint.muted", t("Nothing recorded today yet. The log fills in as the boiler is polled, about every 2 minutes.")) : null,
        h("div.heat-stats",
          stat(t("Heat requested"), hrs(hr.request)),
          stat(t("Burner for heating"), hrs(hr.heat)),
          stat(t("Burner for hot water"), hrs(hr.dhw + hr.other)),
          stat(t("Gas today"), today.kwh == null ? "–" : `${num(today.kwh, 1)} kWh`,
            today.kwh == null ? t("available from the boiler after the first hour") : eur(today.cost)))].filter(Boolean));
    }

    // -- bar chart ---------------------------------------------------------------------------------
    function drawChart() {
      if (!usage) return;
      if (usage.error) { chartBox.replaceChildren(h("div.heat-hint.muted", usage.error)); return; }
      const days = usage.days;
      const haveKwh = days.some((d) => d.heat_kwh != null || d.dhw_kwh != null);
      const haveHours = days.some((d) => d.heat_h || d.dhw_h);
      const m = metric || (haveKwh ? "kwh" : "hours");
      const metricSeg = h("div.seg", ["kwh", "hours"].map((k) => h(`button${m === k ? ".active" : ""}`,
        { onclick: () => { metric = k; drawChart(); } }, k === "kwh" ? "kWh" : t("Hours"))));
      if (!haveKwh && !haveHours) {
        chartBox.replaceChildren(h("div.heat-hint.muted", t("No data yet. The boiler's gas figures appear after its first hour of counting; on/off times as soon as the log has filled.")));
        return;
      }
      const canvas = h("canvas.heat-canvas");
      const sum = days.reduce((a, d) => a + (m === "kwh" ? (d.heat_kwh || 0) + (d.dhw_kwh || 0) : d.heat_h + d.dhw_h), 0);
      chartBox.replaceChildren(
        h("div.row.wrap.heat-chart-head", metricSeg, h("div.spacer"),
          h("span.muted", m === "kwh" ? t("{n} kWh in {d} days", { n: num(sum, 0), d: days.length })
            : t("{n} in {d} days", { n: hrs(sum), d: days.length }))),
        canvas,
        h("div.heat-legend",
          h("span", h("i", { style: { background: C_HEAT } }), t("Heating")),
          h("span", h("i", { style: { background: C_DHW } }), t("Hot water")),
          days.some((d) => d.out_c != null) ? h("span", h("i.line", { style: { background: C_OUT } }), t("Outdoor temperature")) : null),
        h("div.heat-hint.muted", days.some((d) => d.out_c != null) ? "" : t("Set the home location in the Ariston settings to compare with the outdoor temperature.")));
      requestAnimationFrame(() => new Bars(canvas, days, m).draw());
    }

    // -- totals ---------------------------------------------------------------------------------------
    function drawTotals() {
      if (!summary) return;
      const { month: mo, last_month: lm, year: yr, projection: pr } = summary;
      const tot = (o) => (o ? (o.heat_kwh || 0) + (o.dhw_kwh || 0) : null);
      const card = (title, o, extra) => h("div.card.heat-total", h("div.muted", title),
        o ? [h("b.big", `${num(tot(o), 0)} kWh`), h("div", eur(o.cost), h("span.muted", ` · ${num(o.heat_m3 + o.dhw_m3, 0)} m³`)),
             h("div.heat-split", h("span", h("i", { style: { background: C_HEAT } }), `${num(o.heat_kwh, 0)}`),
               h("span", h("i", { style: { background: C_DHW } }), `${num(o.dhw_kwh, 0)}`)), extra]
          : h("div.muted", t("No data yet")));
      const delta = mo && lm && tot(lm) ? (() => {
        return h("div.muted", t("Last month in total: {n} kWh", { n: num(tot(lm), 0) }));
      })() : null;
      totals.replaceChildren(
        card(new Date().toLocaleDateString(locale(), { month: "long" }), mo, [delta,
          pr ? h("div.muted", t("Estimate for the whole month: {n} kWh, {c}", { n: num(pr.kwh, 0), c: eur(pr.cost) })) : null]),
        card(t("Last month"), lm),
        card(new Date().getFullYear(), yr, yr && yr.months < new Date().getMonth() + 1
          ? h("div.muted", t("Only {n} months recorded", { n: yr.months })) : null),
        tariffCard());
    }

    // -- the user's gas tariffs ----------------------------------------------------------------------
    function tariffCard() {
      const tf = summary.tariff || { price: summary.price, fixed: 0, pcs: summary.kwh_per_m3 };
      const own = (summary.tariffs || []).length > 0;
      return h("div.card.heat-tariff",
        h("div.heat-tariff-main",
          h("div.muted", t("Gas tariff")),
          h("b", t("{price} per Smc", { price: eur(tf.price) })),
          h("div.muted", [tf.fixed ? t("+ {fee} a month fixed", { fee: eur(tf.fixed) }) : null,
            t("PCS {pcs} kWh/Smc", { pcs: fmt.num(tf.pcs, 3) }),
            tf.from ? t("since {d}", { d: fmt.date(new Date(tf.from + "T12:00")) }) : null].filter(Boolean).join(" · ")),
          own ? null : h("div.heat-warn-sm", icon("alert"), t("Example price: enter the one from your bill"))),
        h("button", { onclick: editTariffs }, icon("edit"), t("Edit")));
    }

    async function editTariffs() {
      let rows = [];
      try { rows = (await api("/api/p/ariston/tariffs")).tariffs; } catch (e) { toast(e.message, true); return; }
      if (!rows.length) rows = [{ from: `${new Date().getFullYear()}-01-01`, price: "", fixed: "", pcs: summary.kwh_per_m3 }];
      const list = h("div.heat-tlist");
      const field = (r, k, label, attrs) => h("label.heat-tf", h("span", label),
        h("input", { value: r[k] ?? "", ...attrs, oninput: (e) => { r[k] = e.target.value; } }));
      const paint = () => list.replaceChildren(...rows.map((r, i) => h("div.heat-trow",
        field(r, "from", t("Starts on"), { type: "date" }),
        field(r, "price", t("€ per Smc"), { type: "number", step: "0.001", min: "0", inputmode: "decimal", placeholder: "1,150" }),
        field(r, "fixed", t("€ a month fixed"), { type: "number", step: "0.01", min: "0", inputmode: "decimal", placeholder: "0" }),
        field(r, "pcs", t("PCS kWh/Smc"), { type: "number", step: "0.001", min: "8", max: "13", inputmode: "decimal" }),
        h("button.icon.ghost", { "aria-label": t("Remove"), onclick: () => { rows.splice(i, 1); paint(); } }, icon("trash")))));
      const s = sheet(t("Gas tariffs"), [
        h("p.muted", t("Take them from your gas bill: the price per Smc is everything you pay for the gas used (materia gas, transport, system charges, excise and VAT) divided by the Smc billed; the fixed part is the monthly fee. When the price changes, add a row from that date: past days keep their own price.")),
        list,
        h("button", { onclick: () => { rows.push({ from: new Date().toISOString().slice(0, 10), price: "", fixed: rows.at(-1)?.fixed ?? "", pcs: rows.at(-1)?.pcs ?? summary.kwh_per_m3 }); paint(); } },
          icon("plus"), t("Add a price change")),
        h("button.primary", { onclick: (e) => act(async () => {
          const body = rows.filter((r) => r.from && r.price !== "").map((r) => ({
            from: r.from, price: +String(r.price).replace(",", "."), fixed: +String(r.fixed || 0).replace(",", "."),
            pcs: +String(r.pcs || summary.kwh_per_m3).replace(",", ".") }));
          await api("/api/p/ariston/tariffs", { method: "PUT", body: { tariffs: body } });
          s.close(); toast(t("Saved")); load();
        }, e.currentTarget) }, t("Save"))]);
      paint();
    }

    // -- log ---------------------------------------------------------------------------------------------
    function drawLog() {
      if (!sessions) return;
      const items = [
        ...sessions.requests.map((r) => ({ ts: r.t0, kind: "on", r })),
        ...sessions.log.map((l) => ({ ts: l.ts, kind: "chg", l }))].sort((a, b) => b.ts - a.ts);
      if (!items.length) {
        logBox.replaceChildren(h("div.card.heat-hint.muted", t("No heating cycles recorded yet.")));
        return;
      }
      const burners = sessions.sessions;
      const out = []; let last = "", box = null;
      for (const it of items) {
        const d = new Date(it.ts * 1000).toDateString();
        if (d !== last) { last = d; out.push(h("div.heat-day", fmt.date(it.ts)), box = h("div.list.heat-log")); }
        box.append(it.kind === "on" ? onRow(it.r, burners) : chgRow(it.l));
      }
      logBox.replaceChildren(...out);
    }

    function onRow(r, burners) {
      const end = r.open ? null : r.t1;
      const own = burners.filter((b) => b.t0 < (end ?? Infinity) && (b.t1 ?? Infinity) > r.t0);
      const heatMin = own.filter((b) => b.kind === "heat").reduce((a, b) => a + b.minutes, 0);
      const dhwMin = own.filter((b) => b.kind !== "heat").reduce((a, b) => a + b.minutes, 0);
      return h("div.item.heat-row",
        h("div.heat-dot", { style: { background: C_HEAT } }, icon("flame")),
        h("div.stack", { style: { gap: "2px", minWidth: 0, flex: 1 } },
          h("div", h("b", t("On")), ` ${fmt.time(r.t0)} → ${r.open ? t("now") : fmt.time(r.t1)}`,
            h("span.muted", ` · ${mins(r.minutes)}`)),
          h("div.muted.small", [
            r.temp0 != null ? `${fmt.temp(r.temp0)} → ${r.open ? "…" : r.temp1 != null ? fmt.temp(r.temp1) : "…"} °C` : null,
            r.target0 != null ? t("set {n} °C", { n: fmt.temp(r.target0) }) : null,
            heatMin ? t("burner {m} for heating", { m: mins(heatMin) }) : t("burner off"),
            dhwMin >= 1 ? t("{m} for hot water", { m: mins(dhwMin) }) : null].filter(Boolean).join(" · "))));
    }

    function chgRow(l) {
      const who = l.who === "hub" ? t("from this app") : t("from the boiler or Ariston app");
      const modes = { off: t("Off"), heat: t("Heat"), auto: t("Auto") };
      const text = l.kind === "mode" ? t("Mode {a} → {b}", { a: modes[l.old] || l.old || "?", b: modes[l.new] || l.new })
        : t("Set-point {a} → {b} °C", { a: l.old ? fmt.temp(+l.old) : "?", b: fmt.temp(+l.new) });
      return h("div.item.heat-row.chg", h("div.heat-dot.small", icon("settings")),
        h("div.stack", { style: { gap: "2px", flex: 1, minWidth: 0 } },
          h("div", text), h("div.muted.small", `${fmt.time(l.ts)} · ${who}`)));
    }

    const off = bus.addEventListener ? (() => {
      const on = (e) => { if (e.detail?.id?.startsWith?.("ariston.")) { drawNow(); } };
      bus.addEventListener("entity", on);
      return () => bus.removeEventListener("entity", on);
    })() : () => {};
    const timer = setInterval(() => { if (!document.hidden) load(); }, 120000);
    const onResize = () => drawChart();
    window.addEventListener("resize", onResize);

    await load();
    return () => { clearInterval(timer); off(); window.removeEventListener("resize", onResize); };
  },
};

class Bars {
  constructor(canvas, days, metric) {
    Object.assign(this, { canvas, days, metric, hover: null });
    canvas.onpointermove = canvas.onpointerdown = (e) => { this.hover = e.clientX - canvas.getBoundingClientRect().left; this.draw(); };
    canvas.onpointerleave = () => { this.hover = null; this.draw(); };
  }

  css(n) { return getComputedStyle(document.body).getPropertyValue(n).trim(); }

  val(d) {
    return this.metric === "kwh" ? [d.heat_kwh, d.dhw_kwh] : [d.heat_h, d.dhw_h];
  }

  draw() {
    const cv = this.canvas, dpr = window.devicePixelRatio || 1, w = cv.clientWidth, H = cv.clientHeight;
    if (!w || !H) return;
    cv.width = Math.round(w * dpr); cv.height = Math.round(H * dpr);
    const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
    const muted = this.css("--muted") || "#888", line = this.css("--line") || "#ddd";
    const text = this.css("--text") || "#000", surface = this.css("--surface") || "#fff";
    const font = "11px -apple-system, system-ui, sans-serif";
    ctx.font = font;
    const days = this.days, n = days.length;
    const outs = days.map((d) => d.out_c).filter((v) => v != null);
    const hasOut = outs.length > 0;
    const totals = days.map((d) => this.val(d).reduce((a, v) => a + (v || 0), 0));
    let top = Math.max(...totals, 0.001);
    const step = niceStep(top, 4);
    top = Math.ceil(top / step) * step;
    const L = 34, R = hasOut ? 34 : 8, T = 14, B = 22;
    const pw = w - L - R, ph = H - T - B, slot = pw / n, bw = Math.max(2, slot * 0.7);
    const Y = (v) => T + (1 - v / top) * ph;
    let o0 = 0, o1 = 1;
    if (hasOut) { o0 = Math.floor(Math.min(...outs, 0) / 5) * 5; o1 = Math.ceil(Math.max(...outs, o0 + 5) / 5) * 5; }
    const YO = (v) => T + (1 - (v - o0) / (o1 - o0)) * ph;
    ctx.textBaseline = "middle"; ctx.textAlign = "right"; ctx.lineWidth = 1;
    for (let v = 0; v <= top + step / 2; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = line; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.fillText(fmt.num(v, step < 1 ? 1 : 0), L - 5, y);
    }
    if (hasOut) {
      ctx.textAlign = "left";
      for (let v = o0; v <= o1; v += 5) { ctx.fillStyle = muted; ctx.fillText(`${v}°`, w - R + 5, YO(v)); }
    }
    ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    const every = Math.ceil(n / Math.max(2, Math.floor(pw / 56)));
    days.forEach((d, i) => {
      if ((n - 1 - i) % every) return;
      ctx.fillStyle = muted; ctx.fillText(dayLabel(d.day), L + slot * (i + 0.5), H - 6);
    });
    days.forEach((d, i) => {
      const [a, b] = this.val(d), x = L + slot * (i + 0.5) - bw / 2;
      const ya = Y(a || 0), yb = Y((a || 0) + (b || 0));
      if (a) { ctx.fillStyle = C_HEAT; ctx.fillRect(x, ya, bw, Y(0) - ya); }
      if (b) { ctx.fillStyle = C_DHW; ctx.fillRect(x, yb, bw, ya - yb); }
    });
    if (hasOut) {
      ctx.beginPath(); let pen = false;
      days.forEach((d, i) => {
        if (d.out_c == null) { pen = false; return; }
        const x = L + slot * (i + 0.5), y = YO(d.out_c);
        pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y); pen = true;
      });
      ctx.strokeStyle = C_OUT; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
    }
    if (this.hover !== null && this.hover >= L && this.hover <= w - R) {
      const i = Math.min(n - 1, Math.max(0, Math.floor((this.hover - L) / slot))), d = days[i];
      const x = L + slot * (i + 0.5);
      ctx.strokeStyle = muted; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, H - B); ctx.stroke(); ctx.setLineDash([]);
      const k = this.metric === "kwh";
      const lines = [fmt.date(new Date(d.day + "T12:00:00")),
        `${t("Heating")}: ${k ? num(d.heat_kwh, 1) + " kWh" : hrs(d.heat_h)}`,
        `${t("Hot water")}: ${k ? num(d.dhw_kwh, 1) + " kWh" : hrs(d.dhw_h)}`];
      if (k && d.cost != null) lines.push(eur(d.cost));
      if (d.out_c != null) lines.push(`${t("Outdoor")}: ${deg(d.out_c)}`);
      if (k && d.kwh_per_hdd != null) lines.push(`${num(d.kwh_per_hdd, 1)} kWh/${t("degree-day")}`);
      ctx.font = `600 ${font}`;
      const bwid = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16, bh = lines.length * 16 + 10;
      let bx = x + 12; if (bx + bwid > w - 2) bx = x - 12 - bwid; bx = Math.max(2, bx);
      ctx.fillStyle = surface; ctx.strokeStyle = line; ctx.shadowColor = "rgb(0 0 0 / .18)"; ctx.shadowBlur = 8;
      ctx.beginPath(); ctx.roundRect(bx, T + 2, bwid, bh, 8); ctx.fill(); ctx.shadowBlur = 0; ctx.stroke();
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      lines.forEach((l, j) => { ctx.fillStyle = j ? text : muted; ctx.fillText(l, bx + 8, T + 2 + 13 + j * 16); });
    }
  }
}

function niceStep(range, n) {
  const raw = range / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
