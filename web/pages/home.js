// Home: every entity grouped by room, rendered from its kind (see hub/core.py).
import { attentionBanner } from "./attention.js";
import { api, h, icon, sheet, bus, store, callEntity, act, debounce, fmt, toast } from "../app.js";

const KIND_ORDER = ["climate", "water_heater", "media_player", "tv", "speaker", "light", "switch", "sensor"];

export default {
  async render(root) {
    const tiles = new Map(); // entity id -> tile element
    const grids = h("div");

    const draw = () => {
      tiles.clear();
      const visible = [...store.entities.values()].filter((e) => !e.attrs.hidden);
      const rooms = new Map();
      for (const e of visible.sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind)
                                             || a.name.localeCompare(b.name))) {
        const r = groupOf(e);
        if (!rooms.has(r)) rooms.set(r, []);
        rooms.get(r).push(e);
      }
      const order = [...store.rooms, "Other", "This hub"];
      const names = [...rooms.keys()].sort((a, b) =>
        ((order.indexOf(a) + 1) || 99) - ((order.indexOf(b) + 1) || 99) || a.localeCompare(b));
      grids.replaceChildren(
        ...names.map((room) => [
          h("div.section", h("h2", room), h("span.muted.small", `${rooms.get(room).length}`)),
          h("div.grid", rooms.get(room).map((e) => { const t = tile(e); tiles.set(e.id, t); return t; })),
        ]).flat(),
        ...(visible.length ? [] : [h("div.empty", "No devices yet. Enable plugins in config.toml — see Settings.")]),
      );
      summary.replaceChildren(...summaryChips());
    };

    const summary = h("div.row.wrap");
    root.append(
      h("header.page", h("div.stack", { style: { gap: "6px" } }, h("h1", greeting()), summary)),
      attentionBanner(),
      grids,
    );
    draw();

    const onEntity = (ev) => {
      const e = ev.detail, old = tiles.get(e.id);
      if (!old || old.dataset.busy) return;
      if (old.dataset.room !== groupOf(e) || !!e.attrs.hidden) return draw();
      const fresh = tile(e);
      old.replaceWith(fresh);
      tiles.set(e.id, fresh);
      summary.replaceChildren(...summaryChips());
    };
    const onState = () => draw();
    const onRemoved = () => draw();
    bus.addEventListener("entity", onEntity);
    bus.addEventListener("state", onState);
    bus.addEventListener("entity_removed", onRemoved);
    return () => {
      bus.removeEventListener("entity", onEntity);
      bus.removeEventListener("state", onState);
      bus.removeEventListener("entity_removed", onRemoved);
    };
  },
};

function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? "Buonanotte" : hr < 13 ? "Buongiorno" : hr < 18 ? "Buon pomeriggio" : "Buonasera";
}

function summaryChips() {
  const ents = [...store.entities.values()];
  const room = (e) => e.kind === "sensor" && !e.attrs.group && !e.attrs.hidden;
  const temps = ents.filter((e) => room(e) && typeof e.state.temperature === "number");
  const hums = ents.filter((e) => room(e) && typeof e.state.humidity === "number");
  const lightsOn = ents.filter((e) => e.kind === "light" && e.state.on).length;
  const heating = ents.find((e) => e.kind === "climate");
  const avg = (a, k) => a.reduce((s, e) => s + e.state[k], 0) / a.length;
  return [
    temps.length && h("span.chip", icon("thermo"), `${avg(temps, "temperature").toFixed(1)}°`),
    hums.length && h("span.chip", icon("drop"), `${Math.round(avg(hums, "humidity"))}%`),
    heating && h(`span.chip${heating.state.heating ? ".warn" : ""}`, icon("flame"),
                 heating.state.mode === "off" ? "Heating off" : `${fmt.temp(heating.state.target_temperature)}° ${heating.state.mode}`),
    h("span.chip", icon("bulb"), lightsOn ? `${lightsOn} on` : "Lights off"),
  ].filter(Boolean);
}

// ------------------------------------------------------------------ tiles

function tile(e) {
  const maker = { climate: climateTile, water_heater: climateTile, sensor: sensorTile, light: lightTile,
                  switch: switchTile, speaker: speakerTile, media_player: mediaTile, tv: tvTile }[e.kind];
  const el = maker ? maker(e) : genericTile(e);
  el.dataset.room = groupOf(e);
  if (!e.available) el.classList.add("unavailable");
  el.append(h("button.icon.ghost.more", { title: "Edit", onclick: (ev) => { ev.stopPropagation(); editEntity(e); } },
              icon("more")));
  // don't re-render a tile while the user drags one of its sliders
  el.addEventListener("pointerdown", (ev) => { if (ev.target.type === "range") el.dataset.busy = "1"; });
  el.addEventListener("pointerup", () => setTimeout(() => delete el.dataset.busy, 1500));
  return el;
}

const top = (e, ico, sub) => h("div.top", h("div.ico", icon(ico)),
  h("div", { style: { minWidth: 0, paddingRight: "22px" } }, h("div.name.ellipsis", e.name),
    h("div.sub.ellipsis", e.available ? sub : "Unavailable")));

function climateTile(e) {
  const st = e.state, a = e.attrs, water = e.kind === "water_heater";
  let target = st.target_temperature;
  const val = h("div.val", `${fmt.temp(target)}°`);
  const send = debounce(() => act(() => callEntity(e.id, "set_temperature", { value: target })), 900);
  const step = (d) => {
    target = Math.min(a.max, Math.max(a.min, Math.round(((target ?? a.min) + d * a.step) * 10) / 10));
    val.textContent = `${fmt.temp(target)}°`;
    send();
  };
  const modes = a.modes || [];
  return h(`div.card.tile.heat.span2${st.heating ? ".active" : ""}`,
    top(e, water ? "shower" : "flame",
        st.heating ? "Heating now" : st.mode === "off" ? "Off" : st.mode === "auto" ? "Schedule" : "Idle"),
    h("div.row",
      h("div.big", fmt.temp(st.current_temperature), h("small", "°C")),
      h("div.spacer"),
      h("div.stepper",
        h("button.icon", { onclick: () => step(-1), "aria-label": "Lower" }, icon("minus")),
        val,
        h("button.icon", { onclick: () => step(1), "aria-label": "Raise" }, icon("plus")))),
    modes.length > 1 && h("div.seg", { style: { alignSelf: "flex-start" } },
      modes.map((m) => h(`button${st.mode === m ? ".active" : ""}`,
        { onclick: (ev) => act(() => callEntity(e.id, "set_mode", { mode: m }), ev.currentTarget) },
        { off: "Off", heat: water ? "On" : "Manual", auto: "Auto" }[m] || m))));
}

// -- generic sensors: any numeric state, units from attrs.units --------------------

const groupOf = (e) => e.room || e.attrs.group || "Other";
const KEY_COLORS = { temperature: "#ff7a45", humidity: "#33a4d8", download: "#30b85a", upload: "#0a84ff",
                     latency: "#f2a516", used: "#a970ff", free: "#33a4d8", usage: "#ff7a45" };
const colorOf = (k) => KEY_COLORS[k] || "#ff7a45";
const labelOf = (k) => (k.charAt(0).toUpperCase() + k.slice(1)).replace(/_/g, " ");
const unitOf = (e, k) => (e.attrs.units || {})[k]
  ?? ({ temperature: "°", humidity: "%", battery: "%" }[k] || "");
const fmtNum = (v) => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10).toLocaleString();

function sensorKeys(e) {  // values worth showing/charting, most important first
  const keys = e.attrs.history
    || Object.keys(e.state).filter((k) => typeof e.state[k] === "number" && k !== "battery");
  const p = primaryKey(e);
  return [p, ...keys.filter((k) => k !== p && e.state[k] !== undefined && e.state[k] !== null)].filter(Boolean);
}

function primaryKey(e) {
  if (e.attrs.primary && e.state[e.attrs.primary] !== undefined) return e.attrs.primary;
  if (e.state.temperature !== undefined) return "temperature";
  return (e.attrs.history || Object.keys(e.state))
    .find((k) => typeof e.state[k] === "number" && k !== "battery");
}

function sensorTile(e) {
  const st = e.state;
  const keys = sensorKeys(e);
  const [main, ...rest] = keys;
  const canvas = h("canvas.spark");
  const value = (k, big) => {
    const u = unitOf(e, k);
    return big ? h("div.big", fmtNum(st[k]), u && h("small", u === "°C" ? "°" : u))
               : h("span", fmtNum(st[k]), h("small.muted", ` ${u}`));
  };
  const climate = main === "temperature" && st.humidity !== undefined;  // thermo-hygrometer layout
  const sub = [st.battery !== undefined ? `🔋 ${st.battery}%` : "", e.attrs.model || ""].filter(Boolean).join(" · ");
  const el = h("div.card.tile", { onclick: () => sensorDetail(e), style: { cursor: "pointer" } },
    top(e, e.attrs.icon || "thermo", sub),
    main ? h("div.row", { style: { alignItems: "baseline", gap: "10px" } },
      value(main, true),
      h("div.spacer"),
      climate ? h("div", { style: { fontSize: "20px", fontWeight: 700 } }, `${Math.round(st.humidity)}`, h("small.muted", "%"))
              : rest[0] !== undefined && h("div.small.muted", { style: { textAlign: "right" } },
                  labelOf(rest[0]), h("br"), value(rest[0], false))) : null,
    canvas);
  if (main) loadSpark(e.id, main, canvas, colorOf(main));
  return el;
}

// -- lights ----------------------------------------------------------------------

/** CSS colour a lamp is shining with (for the tile icon and the sheet). */
function lampColor(st) {
  if (st.hue !== undefined && st.color_mode !== "temperature" && (st.saturation ?? 0) > 3) {
    return `hsl(${st.hue} ${Math.max(35, st.saturation)}% 60%)`;
  }
  if (st.color_temp) {  // 2200 K amber .. 4000+ K cool white
    const t = Math.max(0, Math.min(1, (st.color_temp - 2200) / 1800));
    const mix = (a, b) => Math.round(a + (b - a) * t);
    return `rgb(${mix(255, 236)} ${mix(176, 240)} ${mix(92, 255)})`;
  }
  return "";
}

function lightTile(e) {
  const st = e.state;
  const tunable = st.color_temp !== undefined || e.attrs.color;
  const el = h(`div.card.tile${st.on ? ".active" : ""}`,
    { style: { cursor: "pointer" },
      onclick: (ev) => { if (ev.target.type !== "range") act(() => callEntity(e.id, st.on ? "turn_off" : "turn_on")); } },
    top(e, "bulb", st.on ? (st.brightness !== undefined ? `${st.brightness}%` : "On") : "Off"));
  const color = st.on && lampColor(st);
  if (color) el.querySelector(".ico").style.background = color;
  if (st.brightness !== undefined || tunable) {
    el.append(h("div.row", { style: { gap: "8px" } },
      st.brightness !== undefined && h("input", { type: "range", min: 1, max: 100, value: st.brightness ?? 100,
        style: { flex: 1, minWidth: 0 },
        onclick: (ev) => ev.stopPropagation(),
        onchange: (ev) => act(() => callEntity(e.id, "turn_on", { brightness: +ev.target.value })) }),
      tunable && h("button.icon", { title: "Colour", "aria-label": "Colour and white",
        onclick: (ev) => { ev.stopPropagation(); lightSheet(e); } }, icon("bulb"))));
  }
  return el;
}

const SWATCHES = [0, 25, 45, 60, 100, 140, 175, 200, 225, 255, 285, 315];

function lightSheet(e) {
  const st = { ...e.state };
  const body = h("div.stack.light-sheet");
  const s = sheet(e.name, body);
  const send = debounce((params) => callEntity(e.id, "turn_on", params).catch((err) => toast(err.message, true)), 250);
  const preview = h("div.lamp-preview");
  const paint = () => {
    preview.style.background = lampColor({ ...st, on: true }) || "var(--on)";
    preview.style.opacity = st.on ? 0.35 + 0.65 * ((st.brightness ?? 100) / 100) : 0.15;
  };
  const slider = (label, attrs, onInput) => h("label.field", label,
    h("input", { type: "range", ...attrs, oninput: (ev) => onInput(+ev.target.value) }));
  const [lo, hi] = e.attrs.color_temp_range || [2200, 4000];
  body.append(
    h("div.row", preview, h("div.spacer"),
      h("button", { onclick: async (ev) => {
        st.on = !st.on; paint();
        await act(() => callEntity(e.id, st.on ? "turn_on" : "turn_off"), ev.currentTarget);
        ev.currentTarget.textContent = st.on ? "Turn off" : "Turn on";
      } }, st.on ? "Turn off" : "Turn on")),
    st.brightness !== undefined && slider("Brightness", { min: 1, max: 100, value: st.brightness ?? 100 },
      (v) => { st.brightness = v; st.on = true; paint(); send({ brightness: v }); }),
    st.color_temp !== undefined && h("div.stack", { style: { gap: "6px" } },
      slider("White", { min: lo, max: hi, step: 50, value: st.color_temp, class: "ct-range" },
        (v) => { st.color_temp = v; st.color_mode = "temperature"; st.on = true; paint(); send({ color_temp: v }); }),
      h("div.row.wrap", { style: { gap: "6px" } }, [["Warm", lo], ["Neutral", Math.round((lo + hi) / 2 / 50) * 50], ["Cool", hi]]
        .map(([name, k]) => h("button", { onclick: () => {
          st.color_temp = k; st.color_mode = "temperature"; st.on = true; paint();
          body.querySelector(".ct-range").value = k; send({ color_temp: k });
        } }, name)))),
    e.attrs.color && h("div.stack", { style: { gap: "8px" } },
      h("div.small.muted", "Colour"),
      h("div.swatches", SWATCHES.map((hue) => h("button.swatch", {
        style: { background: `hsl(${hue} 85% 55%)` }, "aria-label": `Hue ${hue}`,
        onclick: () => {
          st.hue = hue; st.saturation = Math.max(st.saturation ?? 0, 60); st.color_mode = "color"; st.on = true;
          body.querySelector(".sat-range").value = st.saturation; paint();
          send({ hue, saturation: st.saturation });
        } }))),
      slider("Saturation", { min: 0, max: 100, value: st.saturation ?? 100, class: "sat-range" },
        (v) => { st.saturation = v; st.color_mode = "color"; st.on = true; paint(); send({ hue: st.hue ?? 0, saturation: v }); })),
  );
  paint();
  return s;
}

function switchTile(e) {
  return h(`div.card.tile${e.state.on ? ".active" : ""}`,
    { style: { cursor: "pointer" }, onclick: () => act(() => callEntity(e.id, e.state.on ? "turn_off" : "turn_on")) },
    top(e, "plug", e.state.on ? "On" : "Off"));
}

function powerBtn(e) {
  return h(`button.icon${e.state.on ? ".on" : ""}`,
    { title: "Power", onclick: (ev) => act(() => callEntity(e.id, e.state.on ? "turn_off" : "turn_on"), ev.currentTarget) },
    icon("power"));
}

function speakerTile(e) {
  const st = e.state, sources = e.attrs.sources || [];
  return h(`div.card.tile.span2${st.on ? ".active" : ""}`,
    top(e, "speaker", st.on ? `${st.source || ""}${st.playing ? " · playing" : ""}` : "Standby"),
    h("div.row",
      powerBtn(e),
      h("button.icon", { onclick: () => act(() => callEntity(e.id, "mute", { value: !st.muted })) },
        icon(st.muted ? "mute" : "volume")),
      h("input", { type: "range", min: 0, max: e.attrs.max_volume || 100, value: st.volume ?? 0, disabled: !st.on,
        onchange: (ev) => act(() => callEntity(e.id, "set_volume", { value: +ev.target.value })) }),
      h("span.small.muted", { style: { width: "30px", textAlign: "right" } }, st.volume ?? "")),
    h("div.row",
      sources.length > 0 && h("select", { style: { width: "auto", padding: "6px 10px" },
        onchange: (ev) => act(() => callEntity(e.id, "set_source", { source: ev.target.value })) },
        sources.map((s) => h("option", { value: s, selected: s === st.source }, s))),
      h("div.spacer"),
      e.actions.includes("previous") && h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "previous")) }, icon("prev")),
      e.actions.includes("play_pause") && h("button.icon", { onclick: () => act(() => callEntity(e.id, "play_pause")) },
        icon(st.playing ? "pause" : "play")),
      e.actions.includes("next") && h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "next")) }, icon("next"))));
}

function mediaTile(e) {
  const st = e.state, playing = st.state === "playing";
  const pos = st.duration ? Math.min(100, (st.position || 0) / st.duration * 100) : null;
  return h(`div.card.tile.span2${st.on ? ".active" : ""}`,
    top(e, "appletv", !st.on ? "Off" : st.app || (st.state || "Idle")),
    st.title ? h("div.media-tile",
      st.artwork ? h("img.art", { src: st.artwork, alt: "" }) : h("div.art"),
      h("div.stack", { style: { gap: "4px", minWidth: 0, flex: 1 } },
        h("b.ellipsis", st.title), st.artist && h("div.small.muted.ellipsis", st.artist),
        pos !== null && h("div.progress", h("div", { style: { width: `${pos}%` } })),
        pos !== null && h("div.tiny.muted", `${fmt.duration(st.position)} / ${fmt.duration(st.duration)}`)))
      : null,
    h("div.row",
      powerBtn(e),
      h("button.icon", { title: "Remote", onclick: () => remoteSheet(e) }, icon("remote")),
      e.actions.includes("launch_app") && h("button", { onclick: (ev) => act(() => callEntity(e.id, "launch_app", { app: "Infuse" }), ev.currentTarget) }, "Infuse"),
      h("div.spacer"),
      h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "previous")) }, icon("prev")),
      h("button.icon", { onclick: () => act(() => callEntity(e.id, "play_pause")) }, icon(playing ? "pause" : "play")),
      h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "next")) }, icon("next"))));
}

function tvTile(e) {
  const st = e.state;
  return h(`div.card.tile${st.on ? ".active" : ""}`,
    top(e, "tv", st.on ? (st.source || "On") : "Off"),
    h("div.row", powerBtn(e),
      e.actions.includes("remote") && h("button.icon", { title: "Remote", onclick: () => remoteSheet(e) }, icon("remote")),
      e.attrs.experimental && h("span.chip.warn.tiny", "beta")));
}

function genericTile(e) {
  return h("div.card.tile", top(e, "sensor", e.kind),
    h("div.small.muted", Object.entries(e.state).map(([k, v]) => `${k}: ${v}`).join(" · ")));
}

// ------------------------------------------------------------------ sheets

function remoteSheet(e) {
  const key = (k) => () => act(() => callEntity(e.id, "remote", { key: k }));
  const b = (k, content, cls = "") => h(`button${cls}`, { onclick: key(k) }, content);
  sheet(e.name, [
    h("div.remote",
      h("div"), b("up", icon("up")), h("div"),
      b("left", icon("left")), b("select", "OK", ".ok"), b("right", icon("right")),
      b("menu", icon("back")), b("down", icon("down")), b("home", icon("home")),
      b("volume_down", icon("minus")), b("play_pause", icon("play")), b("volume_up", icon("plus"))),
    e.attrs.apps?.length ? h("div.row.wrap", { style: { justifyContent: "center" } },
      e.attrs.apps.map((app) => h("button", { onclick: () => act(() => callEntity(e.id, "launch_app", { app })) }, app))) : null,
  ]);
}

function editEntity(e) {
  const name = h("input", { value: e.name });
  const room = h("input", { value: e.room, list: "rooms-list", placeholder: "Room" });
  const rooms = h("datalist#rooms-list", [...new Set([...store.rooms,
    ...[...store.entities.values()].map((x) => x.room).filter(Boolean)])].map((r) => h("option", { value: r })));
  const hidden = h("input", { type: "checkbox", checked: !!e.attrs.hidden });
  const s = sheet(`Edit ${e.name}`, [
    h("label.field", "Name", name),
    h("label.field", "Room", room, rooms),
    h("label.row.small", hidden, "Hide from Home"),
    h("div.small.muted", `${e.id} · ${e.plugin} · ${e.kind}`),
    h("button.primary.lg", { onclick: async (ev) => {
      await act(() => api(`entities/${encodeURIComponent(e.id)}`,
        { method: "PATCH", body: { name: name.value.trim(), room: room.value.trim(), hidden: hidden.checked } }), ev.currentTarget);
      s.close(); toast("Saved");
    } }, "Save"),
  ]);
}

async function sensorDetail(e) {
  const body = h("div.stack");
  const s = sheet(e.name, body, { wide: true });
  let hours = 24;
  const draw = async () => {
    const keys = sensorKeys(e);
    const charts = await Promise.all(keys.map(async (k) => {
      const pts = await api(`history/${encodeURIComponent(e.id)}?key=${k}&hours=${hours}`);
      const c = h("canvas", { style: { width: "100%", height: "160px" } });
      requestAnimationFrame(() => drawChart(c, pts, colorOf(k), unitOf(e, k)));
      return h("div.card", h("div.row", h("h3", labelOf(k)),
        h("span.muted.small", `${fmtNum(e.state[k])} ${unitOf(e, k)}`), h("div.spacer"),
        h("a.small", { href: `#/history/${encodeURIComponent(e.id)}:${k}`, onclick: () => s.close() }, "Open in History")), c);
    }));
    body.replaceChildren(
      h("div.seg", [24, 168, 720].map((hr) => h(`button${hr === hours ? ".active" : ""}`,
        { onclick: () => { hours = hr; draw(); } }, hr === 24 ? "24 h" : hr === 168 ? "7 days" : "30 days"))),
      ...charts,
      h("div.small.muted", Object.entries(e.attrs)
        .filter(([k, v]) => !["hidden", "measures", "units", "history", "primary", "group", "icon"].includes(k)
                            && typeof v !== "object")
        .map(([k, v]) => `${k}: ${v}`).join(" · ")));
  };
  draw();
  return s;
}

// ------------------------------------------------------------------ charts

async function loadSpark(id, key, canvas, color = "#ff7a45") {
  try {
    const pts = await api(`history/${encodeURIComponent(id)}?key=${key}&hours=24`);
    requestAnimationFrame(() => drawChart(canvas, pts, color, "", true));
  } catch {}
}

function drawChart(canvas, pts, color, unit, spark = false) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (!w || pts.length < 2) return;
  canvas.width = w * dpr; canvas.height = hgt * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1) { y0 -= 0.5; y1 += 0.5; }
  const padL = spark ? 0 : 34, padB = spark ? 2 : 18, padT = 4;
  const X = (x) => padL + (x - x0) / (x1 - x0 || 1) * (w - padL - 2);
  const Y = (y) => padT + (1 - (y - y0) / (y1 - y0)) * (hgt - padT - padB);
  const grad = ctx.createLinearGradient(0, 0, 0, hgt);
  grad.addColorStop(0, color + "55"); grad.addColorStop(1, color + "00");
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
  ctx.lineTo(X(x1), hgt - padB); ctx.lineTo(X(x0), hgt - padB); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  if (spark) return;
  const muted = getComputedStyle(document.body).getPropertyValue("--muted");
  ctx.fillStyle = muted; ctx.font = "11px -apple-system, sans-serif";
  ctx.fillText(`${y1.toFixed(1)}${unit}`, 0, padT + 9);
  ctx.fillText(`${y0.toFixed(1)}${unit}`, 0, hgt - padB);
  const span = x1 - x0;
  for (let i = 0; i <= 4; i++) {
    const t = x0 + span * i / 4, d = new Date(t * 1000);
    const label = span > 2 * 86400 ? d.toLocaleDateString([], { day: "numeric", month: "short" })
                                   : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    ctx.fillText(label, Math.min(X(t), w - 40), hgt - 3);
  }
}
