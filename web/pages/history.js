// History: browse the recorded values of any sensor (GET /api/history, /api/history/{entity}).
// Route: #/history/<entity>:<key>,<entity>:<key>/<range>   range = 24h | 7d | 30d | 1y
import { api, h, icon, store, ensureCss, debounce, fmt, t, locale } from "../app.js";

const COLORS = ["#ff7a45", "#33a4d8", "#30b85a", "#b36bff"];
const MAX_SEL = 4;
const RANGES = { "24h": 24, "7d": 168, "30d": 720, "1y": 8760 };
const keyLabel = (k) => ({ temperature: t("Temperature"), humidity: t("Humidity"), battery: t("Battery"), usage: t("Usage"),
                           load: t("Load"), used: t("Used"), free: t("Free"), download: t("Download"), upload: t("Upload"),
                           signal: t("Signal"), quality: t("Link quality"), latency: t("Latency") }[k]);
const pretty = (k) => keyLabel(k) || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const sid = (s) => `${s.entity}:${s.key}`;

export default {
  async render(root, args = []) {
    ensureCss(`/static/pages/history.css?v=${window.CASA_V}`);
    let series = [], filter = "";
    let selected = (args[0] || "").split(",").filter(Boolean).slice(0, MAX_SEL);
    let range = RANGES[args[1]] ? args[1] : "24h";
    let custom = { from: "", to: "" };
    const data = new Map();   // id -> points
    const charts = [];
    const chartsBox = h("div.hist-charts");
    const list = h("div.hist-list");
    const rangeBox = h("div.row.wrap.hist-range");
    const q = h("input", { type: "search", placeholder: t("Search sensors…"),
                           oninput: debounce((e) => { filter = e.target.value.toLowerCase(); drawList(); }, 120) });

    root.append(
      h("header.page", h("h1", t("History"))),
      h("div.hist-layout",
        h("div.hist-main", rangeBox, chartsBox),
        h("div.hist-side", h("div.hist-search", q), list)));

    const saveHash = () => {
      const hash = `#/history${selected.length ? `/${selected.map(encodeURIComponent).join(",")}` : ""}` +
        (selected.length && range !== "24h" && range !== "custom" ? `/${range}` : "");
      history.replaceState(null, "", hash);
    };
    const find = (id) => series.find((s) => sid(s) === id);

    function drawRange() {
      const btn = (id, label) => h(`button${range === id ? ".active" : ""}`,
        { onclick: () => { range = id; drawRange(); saveHash(); loadAll(); } }, label);
      rangeBox.replaceChildren(...[
        h("div.seg", btn("24h", t("24 h")), btn("7d", t("7 days")), btn("30d", t("30 days")), btn("1y", t("1 year")),
          btn("custom", t("Custom"))),
        range === "custom" ? h("div.row.hist-custom",
          h("input", { type: "datetime-local", value: custom.from, "aria-label": t("From"),
                       onchange: (e) => { custom.from = e.target.value; loadAll(); } }),
          h("span.muted", "→"),
          h("input", { type: "datetime-local", value: custom.to, "aria-label": t("To"),
                       onchange: (e) => { custom.to = e.target.value; loadAll(); } })) : null].filter(Boolean));
    }

    function span() {
      const now = Date.now() / 1000;
      if (range !== "custom") return { from: now - RANGES[range] * 3600, to: now };
      const f = custom.from ? new Date(custom.from).getTime() / 1000 : now - 86400;
      const t = custom.to ? new Date(custom.to).getTime() / 1000 : now;
      return { from: Math.min(f, t - 60), to: t };
    }

    async function loadAll() {
      const { from, to } = span();
      const hours = Math.max(1, Math.ceil((Date.now() / 1000 - from) / 3600));
      await Promise.all(selected.map(async (id) => {
        const s = find(id);
        if (!s) return;
        try {
          const pts = await api(`history/${encodeURIComponent(s.entity)}?key=${encodeURIComponent(s.key)}&hours=${hours}`);
          data.set(id, pts.filter((p) => p[0] >= from && p[0] <= to));
        } catch { data.set(id, []); }
      }));
      drawCharts();
    }

    function drawCharts() {
      charts.length = 0;
      if (!selected.length) {
        chartsBox.replaceChildren(h("div.empty.hist-hint", icon("chart"),
          h("div", t("Pick up to {n} values from the list to chart them.", { n: MAX_SEL }))));
        return;
      }
      let { from, to } = span();
      if (range !== "custom") {  // young data: zoom to what exists instead of an empty 24 h axis
        const first = Math.min(...selected.flatMap((id) => (data.get(id) || []).slice(0, 1).map((p) => p[0])));
        if (isFinite(first) && first > from + (to - from) * 0.25) from = Math.max(from, first - (to - first) * 0.04);
      }
      const groups = new Map();
      selected.forEach((id, i) => {
        const s = find(id); if (!s) return;
        const u = s.unit || "";
        if (!groups.has(u)) groups.set(u, []);
        groups.get(u).push({ s, id, color: COLORS[i % COLORS.length], pts: data.get(id) || [] });
      });
      const cards = [...groups].map(([unit, items]) => {
        const canvas = h("canvas.hist-canvas");
        const card = h("div.card.hist-card",
          h("div.row.wrap.hist-legend", items.map((it) => h("span.hist-key", { style: { "--c": it.color } },
            h("i"), h("b", it.s.name), h("span.muted", pretty(it.s.key)),
            h("a.hist-x", { href: "javascript:void 0", title: t("Remove"), onclick: () => toggle(it.id) }, "×")))),
          canvas,
          items.every((it) => it.pts.length < 2) ? h("div.hist-nodata", t("Not enough data in this range yet.")) : null);
        charts.push({ canvas, items, unit, from, to });
        return card;
      });
      chartsBox.replaceChildren(...cards);
      requestAnimationFrame(() => charts.forEach(paint));
    }

    function paint(c) { new Chart(c.canvas, c.items, c.unit, c.from, c.to).draw(); }

    function toggle(id) {
      if (selected.includes(id)) selected = selected.filter((x) => x !== id);
      else if (selected.length < MAX_SEL) selected = [...selected, id];
      else selected = [...selected.slice(1), id];
      saveHash(); drawList(); loadAll();
    }

    function drawList() {
      const rows = series.filter((s) => !filter ||
        [s.name, s.room, s.entity, s.key, pretty(s.key)].join(" ").toLowerCase().includes(filter));
      if (!rows.length) {
        list.replaceChildren(h("div.empty", series.length ? t("No match.") : t("No history recorded yet.")));
        return;
      }
      const byRoom = new Map();
      for (const s of rows) {
        const r = s.room || "Other";
        if (!byRoom.has(r)) byRoom.set(r, new Map());
        const m = byRoom.get(r);
        if (!m.has(s.entity)) m.set(s.entity, []);
        m.get(s.entity).push(s);
      }
      const rooms = [...byRoom].sort((a, b) => (a[0] === "Other") - (b[0] === "Other") || a[0].localeCompare(b[0]));
      list.replaceChildren(...rooms.map(([room, ents]) => h("div.hist-group",
        h("div.hist-room", t(room)),
        h("div.list", [...ents].sort((a, b) => a[1][0].name.localeCompare(b[1][0].name)).map(([, ss]) =>
          h("div.hist-ent", h("div.hist-entname", ss[0].name),
            h("div.row.wrap.hist-chips", ss.map((s) => {
              const on = selected.includes(sid(s));
              const idx = selected.indexOf(sid(s));
              return h(`button.hist-chip${on ? ".active" : ""}`,
                { onclick: () => toggle(sid(s)), style: on ? { "--c": COLORS[idx % COLORS.length] } : null },
                h("span", pretty(s.key)),
                h("b", fmtVal(s.value, s.unit)));
            }))))))));
    }

    drawRange(); drawCharts();
    try {
      series = await api("history");
      const ents = store.entities;
      for (const s of series) {
        const e = ents.get(s.entity);
        if (e) { s.name = e.name || s.name; s.room = e.room ?? s.room; s.value = e.state?.[s.key]; }
      }
    } catch (e) { list.replaceChildren(h("div.empty", e.message)); return; }
    selected = selected.filter((id) => find(id));
    drawList();
    await loadAll();

    const onResize = debounce(() => charts.forEach(paint), 120);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  },
};

function fmtVal(v, unit) {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v);
  const s = a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2).replace(/\.?0+$/, "") || "0";
  return unit ? `${s}${/^[°%]/.test(unit) ? "" : " "}${unit}` : s;
}

function niceStep(range, n) {
  const raw = range / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

const TSTEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 2 * 86400, 7 * 86400, 14 * 86400,
                30 * 86400, 91 * 86400];

function timeTicks(from, to, max) {
  const step = TSTEPS.find((s) => (to - from) / s <= max) || TSTEPS.at(-1);
  const out = [];
  const off = (t) => -new Date(t * 1000).getTimezoneOffset() * 60;
  if (step >= 30 * 86400) {  // month-ish: first of each month
    const d = new Date(from * 1000); d.setDate(1); d.setHours(0, 0, 0, 0);
    const every = step > 40 * 86400 ? 3 : 1;
    d.setMonth(d.getMonth() - (d.getMonth() % every));
    for (; d.getTime() / 1000 <= to; d.setMonth(d.getMonth() + every)) if (d.getTime() / 1000 >= from) out.push(d.getTime() / 1000);
    return { ticks: out, step };
  }
  let t = Math.ceil((from + off(from)) / step) * step - off(from);
  for (; t <= to; t += step) out.push(t);
  return { ticks: out, step };
}

function timeLabel(t, step, span) {
  const d = new Date(t * 1000);
  if (step >= 86400) return d.toLocaleDateString(locale(), span > 400 * 86400 || step >= 30 * 86400 ?
    { month: "short", year: "2-digit" } : { day: "numeric", month: "short" });
  if (d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString(locale(), { day: "numeric", month: "short" });
  return fmt.time(d);
}

function fullTime(t, span) {
  const d = new Date(t * 1000);
  return span > 3 * 86400 ? d.toLocaleString(locale(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
                          : d.toLocaleString(locale(), { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

class Chart {
  constructor(canvas, items, unit, from, to) {
    Object.assign(this, { canvas, items, unit, from, to, hover: null });
    canvas.onpointermove = (e) => this.move(e);
    canvas.onpointerdown = (e) => this.move(e);
    canvas.onpointerleave = () => { this.hover = null; this.draw(); };
  }

  css(name) { return getComputedStyle(document.body).getPropertyValue(name).trim(); }

  move(e) {
    const r = this.canvas.getBoundingClientRect();
    this.hover = e.clientX - r.left;
    this.draw();
  }

  draw() {
    const cv = this.canvas, dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, H = cv.clientHeight;
    if (!w || !H) return;
    cv.width = Math.round(w * dpr); cv.height = Math.round(H * dpr);
    const ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);
    const muted = this.css("--muted") || "#888", line = this.css("--line") || "#ddd";
    const text = this.css("--text") || "#000", surface = this.css("--surface") || "#fff";
    const font = "11px -apple-system, system-ui, sans-serif";
    ctx.font = font;

    // data extent
    let lo = Infinity, hi = -Infinity;
    for (const it of this.items) for (const p of it.pts) {
      lo = Math.min(lo, p[2] ?? p[1]); hi = Math.max(hi, p[3] ?? p[1]);
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.06;
    const nonNeg = lo >= 0;
    lo -= pad; hi += pad;
    if (nonNeg) lo = Math.max(0, lo);
    const step = niceStep(hi - lo, Math.max(2, Math.floor(H / 48)));
    const y0 = Math.floor(lo / step) * step, y1 = Math.ceil(hi / step) * step;
    const dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)));
    const yl = (v) => v.toFixed(dec);
    const labW = Math.max(...[y0, y1].map((v) => ctx.measureText(yl(v)).width)) + 8;
    const L = Math.max(30, labW), R = 8, T = 16, B = 22;
    const X = (t) => L + (t - this.from) / (this.to - this.from || 1) * (w - L - R);
    const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);

    // grid + y axis
    ctx.textBaseline = "middle"; ctx.textAlign = "right";
    ctx.lineWidth = 1;
    for (let v = y0; v <= y1 + step / 2; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = line; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.fillText(yl(v), L - 6, y);
    }
    if (this.unit) { ctx.textAlign = "left"; ctx.textBaseline = "alphabetic"; ctx.fillText(this.unit, L, 10); }
    // x axis
    const { ticks, step: tstep } = timeTicks(this.from, this.to, Math.max(2, Math.floor((w - L) / 78)));
    ctx.textBaseline = "alphabetic"; ctx.textAlign = "center";
    const span = this.to - this.from;
    for (const t of ticks) {
      const x = X(t);
      ctx.strokeStyle = line; ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, T); ctx.lineTo(Math.round(x) + 0.5, H - B + 4); ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(timeLabel(t, tstep, span), Math.min(Math.max(x, L + 18), w - R - 18), H - 6);
    }

    // series
    ctx.save();
    ctx.beginPath(); ctx.rect(L, T - 2, w - L - R, H - T - B + 4); ctx.clip();
    for (const it of this.items) {
      const pts = it.pts;
      if (!pts.length) continue;
      const dts = pts.slice(1).map((p, i) => p[0] - pts[i][0]).sort((a, b) => a - b);
      const gap = Math.max(60, (dts[Math.floor(dts.length / 2)] || 60) * 4);
      const runs = []; let cur = [];
      pts.forEach((p, i) => {
        if (i && p[0] - pts[i - 1][0] > gap) { runs.push(cur); cur = []; }
        cur.push(p);
      });
      runs.push(cur);
      for (const run of runs) {
        if (run.length < 2) {  // lone point
          ctx.fillStyle = it.color; ctx.beginPath(); ctx.arc(X(run[0][0]), Y(run[0][1]), 2.5, 0, 7); ctx.fill();
          continue;
        }
        if (run.some((p) => p[3] !== undefined && (p[3] - p[2]) > 1e-9)) {
          ctx.beginPath();
          run.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[3])) : ctx.moveTo(X(p[0]), Y(p[3]))));
          for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(X(run[i][0]), Y(run[i][2]));
          ctx.closePath(); ctx.globalAlpha = 0.18; ctx.fillStyle = it.color; ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.beginPath();
        run.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
        ctx.strokeStyle = it.color; ctx.lineWidth = 1.8; ctx.lineJoin = "round"; ctx.stroke();
      }
    }
    ctx.restore();

    // crosshair + tooltip
    if (this.hover !== null && this.hover >= L && this.hover <= w - R) {
      const t = this.from + (this.hover - L) / (w - L - R) * (this.to - this.from);
      const hits = [];
      for (const it of this.items) {
        let best = null;
        for (const p of it.pts) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
        if (best && Math.abs(X(best[0]) - this.hover) < 40) hits.push({ it, p: best });
      }
      if (hits.length) {
        const x = X(hits[0].p[0]);
        ctx.strokeStyle = muted; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, H - B); ctx.stroke(); ctx.setLineDash([]);
        for (const { it, p } of hits) {
          ctx.fillStyle = surface; ctx.strokeStyle = it.color; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(x, Y(p[1]), 4, 0, 7); ctx.fill(); ctx.stroke();
        }
        const lines = [{ t: fullTime(hits[0].p[0], span), c: muted }];
        for (const { it, p } of hits) {
          const mm = p[2] !== undefined && Math.abs(p[3] - p[2]) > 1e-9 ? `  (${yv(p[2])}–${yv(p[3])})` : "";
          lines.push({ t: `${hits.length > 1 ? it.s.name + " " + pretty(it.s.key) + ": " : ""}${yv(p[1])} ${this.unit}${mm}`, c: it.color, b: true });
        }
        function yv(v) { return fmtVal(v, "").trim(); }
        ctx.font = `600 ${font}`;
        const bw = Math.max(...lines.map((l) => ctx.measureText(l.t).width)) + 16, bh = lines.length * 16 + 10;
        let bx = x + 12; if (bx + bw > w - 2) bx = x - 12 - bw;
        bx = Math.max(2, bx);
        const by = T + 2;
        ctx.fillStyle = surface; ctx.strokeStyle = line; ctx.lineWidth = 1;
        ctx.shadowColor = "rgb(0 0 0 / .18)"; ctx.shadowBlur = 8;
        ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 8); ctx.fill(); ctx.shadowBlur = 0; ctx.stroke();
        ctx.textAlign = "left"; ctx.textBaseline = "middle";
        lines.forEach((l, i) => { ctx.fillStyle = i ? text : l.c; ctx.font = i ? `600 ${font}` : font;
          if (i) { ctx.fillStyle = l.c; } ctx.fillText(l.t, bx + 8, by + 13 + i * 16); });
      }
    }
  }
}
