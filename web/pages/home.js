// Home ("Casa"): two views, chosen per person (✎ → Vista, or Altro):
//   simple - the house at a glance: temperature + status, heating rows, light chips, what's
//            playing, thin TV/speaker rows, shortcuts. The hub's own sensors stay out of sight.
//   full   - every entity as a tile, grouped by room; the hub's own sensors folded in "Sistema".
// Sections of both views can be reordered/hidden with ✎, saved per person (store.prefs.home).
import { attentionBanner } from "./attention.js";
import { api, h, icon, sheet, bus, store, callEntity, act, debounce, fmt, toast, t, savePrefs, locale,
         ensureCss, go, hasPlugin } from "../app.js";

const KIND_ORDER = ["climate", "water_heater", "media_player", "tv", "speaker", "light", "switch", "sensor"];
const SYS = "__system";  // full view: the hub's own sensors (system plugin)
const SIMPLE = ["glance", "meal", "notes", "climate", "lights", "playing", "devices", "shortcuts"];
const SIMPLE_TITLE = () => ({ glance: t("At a glance"), meal: t("Menu"), notes: t("Notes"), climate: t("Heating"), lights: t("Lights"),
  playing: t("Now playing"), devices: t("TV and speakers"), shortcuts: t("Shortcuts") });
const HEADED = new Set(["notes", "lights", "playing", "devices"]);  // simple blocks with a visible title

export default {
  async render(root) {
    ensureCss(`/static/pages/home.css?v=${window.CASA_V}`);
    const tiles = new Map(); // full view: entity id -> tile element
    const body = h("div");
    const summary = h("div.row.wrap");
    let editing = false;
    const hp = () => store.prefs?.home || {};
    const view = () => (hp().view === "full" ? "full" : "simple");

    /** Ordered, hideable sections; `key` keeps each view's own order ("simple" / "full"). */
    const sections = (ids, key) => {
      const p = hp()[key] || {}, mine = p.order || [], hidden = new Set(p.hidden || []);
      const rank = (id) => (mine.includes(id) ? mine.indexOf(id) : 1000 + ids.indexOf(id));
      const sorted = [...ids].sort((a, b) => rank(a) - rank(b));
      const save = (o, hid) => savePrefs({ home: { ...hp(), [key]: { order: o, hidden: [...hid] } } },
                                         { rerender: false }).then(draw);
      const move = (id, d) => { const o = [...sorted], i = o.indexOf(id); [o[i], o[i + d]] = [o[i + d], o[i]]; save(o, hidden); };
      const head = (id, title, extra) => h(`div.section${hidden.has(id) ? ".off" : ""}`, h("h2", title), extra,
        editing && [h("div.spacer"),
          h("button.icon.ghost", { title: t("Move up"), disabled: sorted[0] === id, onclick: () => move(id, -1) }, icon("up")),
          h("button.icon.ghost", { title: t("Move down"), disabled: sorted.at(-1) === id, onclick: () => move(id, 1) }, icon("down")),
          h(`button.icon${hidden.has(id) ? "" : ".primary"}`, { title: hidden.has(id) ? t("Show") : t("Hide"),
            onclick: () => { hidden.has(id) ? hidden.delete(id) : hidden.add(id); save(sorted, hidden); } },
            icon(hidden.has(id) ? "plus" : "check"))]);
      return { list: sorted.filter((id) => editing || !hidden.has(id)), hidden, head };
    };

    // tiles: order and size per person (prefs.home.tiles), hiding for everybody (entity "hidden")
    const tp = () => hp().tiles || {};
    const saveTiles = (patch) => savePrefs({ home: { ...hp(), tiles: { ...tp(), ...patch } } },
                                           { rerender: false }).then(draw);
    const hide = (e, hidden) => act(() => api(`entities/${encodeURIComponent(e.id)}`, { method: "PATCH", body: { hidden } }))
      .then(() => hidden && toast(t("Hidden for everybody: {name}", { name: e.name })));
    /** Edit-mode overlay on a tile/chip/row: move (◀ ▶), wide/narrow, hide for everybody. */
    const decorate = (el, e, { earlier, later, wide } = {}) => {
      el.classList.add("editing");
      const stop = (ev) => ev.stopPropagation();
      el.append(h("div.t-edit", { onclick: stop, onpointerdown: stop, onpointerup: stop },
        h("b.t-name.ellipsis", e.name),
        (earlier !== undefined || later !== undefined) && [
          h("button.icon", { title: t("Move earlier"), disabled: !earlier, onclick: earlier }, icon("left")),
          h("button.icon", { title: t("Move later"), disabled: !later, onclick: later }, icon("right"))],
        wide !== undefined && h("button.icon", { title: wide ? t("Make narrow") : t("Make wide"),
          onclick: () => saveTiles({ size: { ...(tp().size || {}), [e.id]: wide ? "normal" : "wide" } }) },
          icon(wide ? "shrink" : "expand")),
        h("button.icon.danger", { title: t("Hide for everybody"), onclick: () => hide(e, true) }, icon("eyeoff"))));
      return el;
    };
    /** Edit mode: what's hidden for everybody, one tap to bring it back. */
    const hiddenList = () => {
      const hid = [...store.entities.values()].filter((e) => e.attrs.hidden).sort(byRoom);
      return hid.length ? [h("div.section", h("h2", t("Hidden for everybody")), h("span.muted.small", `${hid.length}`)),
        h("div.list", hid.map((e) => h("div.item", h("span.spacer.ellipsis", e.name), e.room && h("span.muted.small", e.room),
          h("button.small", { onclick: () => hide(e, false) }, icon("eye"), t("Show")))))] : null;
    };

    const viewSwitch = () => h("div.card.row", { style: { marginBottom: "6px" } },
      h("b", t("Home view")), h("div.spacer"),
      h("div.seg", [["simple", t("Simple")], ["full", t("Complete")]].map(([v, l]) =>
        h(`button${view() === v ? ".active" : ""}`, { onclick: () =>
          savePrefs({ home: { ...hp(), view: v } }, { rerender: false }).then(draw) }, l))));

    const drawFull = () => {
      const visible = [...store.entities.values()].filter((e) => !e.attrs.hidden);
      const rooms = new Map();
      for (const e of visible.sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind)
                                             || a.name.localeCompare(b.name))) {
        const r = e.plugin === "system" ? SYS : groupOf(e);
        if (!rooms.has(r)) rooms.set(r, []);
        rooms.get(r).push(e);
      }
      const order = [...store.rooms, "Other", SYS];
      const names = [...rooms.keys()].sort((a, b) =>
        ((order.indexOf(a) + 1) || 99) - ((order.indexOf(b) + 1) || 99) || a.localeCompare(b));
      const { list, hidden, head } = sections(names, "full");
      const mine = tp().order || [], size = tp().size || {};
      const rankT = (e) => { const i = mine.indexOf(e.id); return i < 0 ? 1e6 : i; };
      for (const r of rooms.values()) r.sort((a, b) => rankT(a) - rankT(b));  // stable: kind/name otherwise
      const tileOrder = list.flatMap((r) => rooms.get(r).map((e) => e.id));
      const grid = (room) => h("div.grid", rooms.get(room).map((e, i, arr) => {
        const tl = tile(e);
        if (size[e.id] === "wide") tl.classList.add("span2");
        if (size[e.id] === "normal") tl.classList.remove("span2");
        if (editing) {  // move only inside its room
          const swap = (other) => () => {
            const o = [...tileOrder], x = o.indexOf(e.id), y = o.indexOf(other.id);
            [o[x], o[y]] = [o[y], o[x]];
            saveTiles({ order: o });
          };
          decorate(tl, e, { earlier: i > 0 && swap(arr[i - 1]), later: i < arr.length - 1 && swap(arr[i + 1]),
                            wide: tl.classList.contains("span2") });
        }
        tiles.set(e.id, tl);
        return tl;
      }));
      body.replaceChildren(...[
        editing && viewSwitch(),
        ...list.map((room) => {
          if (room === SYS && !editing) {  // folded: one line unless opened
            return h("details.sys", h("summary", h("h2", t("System")), h("span.muted.small", houseStatus().text)), grid(room));
          }
          return [head(room, room === SYS ? t("System") : t(room), h("span.muted.small", `${rooms.get(room).length}`)),
                  !(editing && hidden.has(room)) && grid(room)];
        }).flat(),
        !visible.length && h("div.empty", t("No devices yet. Enable plugins in config.toml — see Settings.")),
        editing && hiddenList(),
      ].flat().filter(Boolean));
      summary.replaceChildren(...summaryChips());
    };

    // household notes (notes plugin): the latest few + quick add; live through bus "notes"
    let notes = [];
    const loadNotes = () => (hasPlugin("notes")
      ? api("p/notes/notes").then((n) => { notes = n; if (view() === "simple") redraw(); }).catch(() => {})
      : Promise.resolve());
    const onNotes = (ev) => { notes = ev.detail?.items || notes; if (view() === "simple") redraw(); };
    // today's planned meals (recipes planner)
    let today = null;
    const loadToday = () => (hasPlugin("recipes")
      ? api("p/recipes/plan/today").then((d) => { today = d; if (view() === "simple") redraw(); }).catch(() => {})
      : Promise.resolve());

    const drawSimple = () => {
      const ents = [...store.entities.values()].filter((e) => !e.attrs.hidden);
      const mine = tp().order || [];
      const rankT = (e) => { const i = mine.indexOf(e.id); return i < 0 ? 1e6 : i; };
      const sorted = (a, b) => rankT(a) - rankT(b) || byRoom(a, b);
      const all = [...ents].sort(sorted).map((e) => e.id);
      const by = (...kinds) => ents.filter((e) => kinds.includes(e.kind)).sort(sorted);
      // edit mode: each chip/row can move inside its block or be hidden for everybody
      const ed = (arr, mk) => arr.map((e, i) => {
        const el = mk(e);
        if (!editing) return el;
        const swap = (other) => () => {
          const o = [...all], x = o.indexOf(e.id), y = o.indexOf(other.id);
          [o[x], o[y]] = [o[y], o[x]];
          saveTiles({ order: o });
        };
        return decorate(el, e, { earlier: i > 0 && swap(arr[i - 1]), later: i < arr.length - 1 && swap(arr[i + 1]) });
      });
      const lights = by("light"), on = lights.filter((l) => l.state.on);
      const blocks = {
        glance: [glanceCard(ents), startHint(ents)],
        notes: hasPlugin("notes") ? notesBlock(notes) : null,
        meal: hasPlugin("recipes") ? mealCard(today) : null,
        climate: rowsCard(ed(by("climate", "water_heater"), climateRow)),
        lights: lights.length ? h("div.lchips", ed(lights, lightChip)) : null,
        playing: playingCard(ents),
        devices: rowsCard(ed(by("tv", "media_player", "speaker", "switch"), deviceRow)),
        shortcuts: shortcuts(),
      };
      const extra = {
        notes: [h("div.spacer"), h("a.small", { href: "#/notes" }, t("All notes"))],
        lights: [h("span.muted.small", on.length ? t("{n} on", { n: on.length }) : t("all off")),
                 on.length > 0 && [h("div.spacer"), h("button.small", { onclick: (ev) => act(() =>
                   Promise.all(on.map((l) => callEntity(l.id, "turn_off"))), ev.currentTarget) }, icon("power"), t("Turn all off"))]],
      };
      const { list, hidden, head } = sections(SIMPLE.filter((id) => blocks[id]), "simple");
      body.replaceChildren(...[
        editing && viewSwitch(),
        ...list.map((id) => [
          (editing || HEADED.has(id)) && head(id, SIMPLE_TITLE()[id], editing ? null : extra[id]),
          !(editing && hidden.has(id)) && blocks[id],
        ]).flat(),
        editing && hiddenList(),
      ].flat().filter(Boolean));
      summary.replaceChildren();
    };

    const draw = () => { tiles.clear(); view() === "full" ? drawFull() : drawSimple(); };

    // simple view redraws as a whole (small); never while the user is touching it
    let touchedUntil = 0, pending = null;
    body.addEventListener("pointerdown", () => { touchedUntil = Date.now() + 2500; });
    const redraw = () => {
      clearTimeout(pending);
      const wait = touchedUntil - Date.now();
      pending = setTimeout(wait > 0 ? redraw : draw, wait > 0 ? wait : 250);
    };

    const editBtn = h("button.icon.ghost", { title: t("Customize"), "aria-label": t("Customize"), onclick: () => {
      editing = !editing;
      editBtn.classList.toggle("primary", editing);
      draw();
    } }, icon("edit"));
    root.append(
      h("header.page", h("div.stack", { style: { gap: "6px" } }, h("h1", greeting()), summary), h("div.spacer"), editBtn),
      attentionBanner(),
      body,
    );
    draw();
    loadNotes();
    loadToday();

    const onEntity = (ev) => {
      if (view() === "simple") return redraw();
      const e = ev.detail, old = tiles.get(e.id);
      if (!old || old.dataset.busy) return;
      const r = e.plugin === "system" ? SYS : groupOf(e);
      if (old.dataset.room !== r || !!e.attrs.hidden) return draw();
      const fresh = tile(e);
      old.replaceWith(fresh);
      tiles.set(e.id, fresh);
      summary.replaceChildren(...summaryChips());
    };
    const onState = () => draw();
    bus.addEventListener("entity", onEntity);
    bus.addEventListener("state", onState);
    bus.addEventListener("entity_removed", onState);
    bus.addEventListener("attentionchange", redraw);
    bus.addEventListener("notes", onNotes);
    bus.addEventListener("recipes_plan", loadToday);
    return () => {
      bus.removeEventListener("recipes_plan", loadToday);
      bus.removeEventListener("notes", onNotes);
      clearTimeout(pending);
      bus.removeEventListener("entity", onEntity);
      bus.removeEventListener("state", onState);
      bus.removeEventListener("entity_removed", onState);
      bus.removeEventListener("attentionchange", redraw);
    };
  },
};

function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? t("Good night") : hr < 13 ? t("Good morning") : hr < 18 ? t("Good afternoon") : t("Good evening");
}

const modeName = (m) => ({ off: t("Off"), heat: t("Heat"), auto: t("Auto"), cool: t("Cool") }[m] || m);
const stateName = (s) => ({ playing: t("playing"), paused: t("paused"), idle: t("Idle"), stopped: t("stopped"),
                            standby: t("Standby"), buffering: t("buffering") }[s] || s);

function summaryChips() {
  const ents = [...store.entities.values()];
  const room = (e) => e.kind === "sensor" && !e.attrs.group && !e.attrs.hidden;
  const temps = ents.filter((e) => room(e) && typeof e.state.temperature === "number");
  const hums = ents.filter((e) => room(e) && typeof e.state.humidity === "number");
  const lightsOn = ents.filter((e) => e.kind === "light" && e.state.on).length;
  const heating = ents.find((e) => e.kind === "climate");
  const avg = (a, k) => a.reduce((s, e) => s + e.state[k], 0) / a.length;
  return [
    temps.length && h("span.chip", icon("thermo"), `${fmt.num(avg(temps, "temperature"), 1)}°`),
    hums.length && h("span.chip", icon("drop"), `${Math.round(avg(hums, "humidity"))}%`),
    heating && h(`span.chip${heating.state.heating ? ".warn" : ""}`, icon("flame"),
                 heating.state.mode === "off" ? t("Heating off") : `${fmt.temp(heating.state.target_temperature)}° ${modeName(heating.state.mode)}`),
    h("span.chip", icon("bulb"), lightsOn ? t("{n} on", { n: lightsOn }) : t("Lights off")),
  ].filter(Boolean);
}

// ------------------------------------------------------------------ simple view

const byRoom = (a, b) => {
  const r = (e) => { const i = store.rooms.indexOf(e.room); return i < 0 ? 99 : i; };
  return r(a) - r(b) || a.name.localeCompare(b.name);
};
const roomSensors = (ents) => ents.filter((e) => e.kind === "sensor" && e.plugin !== "system" && !e.attrs.hidden);
const avg = (a, k) => a.reduce((s, e) => s + e.state[k], 0) / a.length;

/** One line for the whole house: what needs fixing, internet, disk - or "all good". */
function houseStatus() {
  const ents = [...store.entities.values()];
  const sys = (pred) => ents.find((e) => e.plugin === "system" && pred(e));
  if (store.attention) return { level: "warn", text: t("{n} to fix", { n: store.attention }), go: "#/settings" };
  if (sys((e) => e.state.online === false)) return { level: "bad", text: t("No internet"), go: "#/history" };
  if (sys((e) => e.state.total && e.state.free !== undefined && e.state.free / e.state.total < 0.05))
    return { level: "warn", text: t("Disk almost full"), go: "#/history" };
  return { level: "ok", text: t("All good"), go: null };
}

function glanceCard(ents) {
  const temps = roomSensors(ents).filter((e) => typeof e.state.temperature === "number");
  const hums = roomSensors(ents).filter((e) => typeof e.state.humidity === "number");
  const st = houseStatus();
  return h("div.card.glance",
    temps.length > 0 && h("button.g-temp", { onclick: () => thermoSheet(temps) },
      icon("thermo"), h("b", `${fmt.num(avg(temps, "temperature"), 1)}°`),
      hums.length > 0 && h("span.muted", icon("drop"), `${Math.round(avg(hums, "humidity"))}%`)),
    temps.length > 0 && h("div.spacer"),
    h(`button.g-status.${st.level}`, { onclick: () => st.go && go(st.go) }, h("span.dot"), st.text));
}

/** A house without any controllable device yet (a friend's fresh install): say where to start. */
function startHint(ents) {
  const controllable = ents.some((e) => e.plugin !== "system" && e.kind !== "sensor") || roomSensors(ents).length;
  return controllable ? null : h("div.card.start-hint",
    h("span.sc-ico", icon("plug")),
    h("div.stack", { style: { gap: "2px", flex: 1, minWidth: 0 } },
      h("b", t("Connect your devices")),
      h("span.muted.small", t("Lights, heating, TV and speakers appear here once you add them."))),
    h("a.btn", { href: "#/settings" }, t("Add")));
}

function thermoSheet(temps) {
  sheet(t("Temperatures"), h("div.list", temps.map((e) => h("div.item", { style: { cursor: "pointer" }, onclick: () => sensorDetail(e) },
    icon("thermo"), h("div.stack.spacer", { style: { gap: "2px" } }, h("span", e.name), e.room && h("span.muted.small", e.room)),
    h("b", `${fmt.num(e.state.temperature, 1)}°`),
    e.state.humidity !== undefined && h("span.muted", `${Math.round(e.state.humidity)}%`)))));
}

const rowsCard = (rows) => (rows.length ? h("div.card.rows", rows) : null);

/** iOS-style on/off switch that doesn't trigger the row it sits in. */
function toggle(on, onchange, disabled = false, label = "") {
  const input = h("input", { type: "checkbox", role: "switch", checked: !!on, disabled, "aria-label": label,
                             onchange: (ev) => onchange(ev.target.checked) });
  return h("label.sw", { onclick: (ev) => ev.stopPropagation() }, input, h("span"));
}

/** Heating / hot water in two lines - name, plain status, on/off / "Ora 20,8°", −/+ target.
 *  Tap the name for every option (modes, schedule). */
function climateRow(e) {
  const st = e.state, a = e.attrs, water = e.kind === "water_heater", modes = a.modes || [];
  let target = st.target_temperature;
  const val = h("b.c-target", `${fmt.temp(target)}°`);
  const send = debounce(() => act(() => callEntity(e.id, "set_temperature", { value: target })), 900);
  const step = (d) => {
    target = Math.min(a.max, Math.max(a.min, Math.round(((target ?? a.min) + d * a.step) * 10) / 10));
    val.textContent = `${fmt.temp(target)}°`;
    send();
  };
  const onMode = modes.includes("auto") ? "auto" : modes.find((m) => m !== "off");
  const status = !e.available ? t("Unavailable") : st.mode === "off" ? t("Off")
    : st.heating ? t("Heating now") : water ? t("Ready") : st.mode === "auto" ? t("Schedule") : t("Paused");
  return h(`div.hrow.climate${st.heating ? ".hot" : ""}${e.available ? "" : ".unavailable"}`,
    h("div.c-line",
      h("button.h-main", { onclick: () => sheet(e.name, climateTile(e)) },
        h("div.ico", icon(water ? "shower" : "flame")),
        h("div.h-txt", h("div.name.ellipsis", e.name), h("div.sub", status))),
      // no "off" mode (e.g. hot water): keep the switch's place so rows line up
      modes.includes("off") && onMode ? toggle(st.mode !== "off",
        (on) => act(() => callEntity(e.id, "set_mode", { mode: on ? onMode : "off" })), !e.available, e.name)
        : h("span.sw-slot")),
    h("div.c-line.c-set",
      h("span.muted.small", t("Now {temp}°", { temp: fmt.temp(st.current_temperature) })),
      h("div.spacer"),
      h("span.muted.small", t("Target")),
      h("div.stepper.sm",
        h("button.icon", { onclick: () => step(-1), "aria-label": t("Lower") }, icon("minus")), val,
        h("button.icon", { onclick: () => step(1), "aria-label": t("Raise") }, icon("plus")))));
}

/** Tap = on/off, hold = brightness and colour. */
function lightChip(e) {
  const st = e.state, color = st.on && lampColor(st);
  const el = h(`button.lchip${st.on ? ".lit" : ""}${e.available ? "" : ".unavailable"}`,
    h("span.ldot", { style: color ? { background: color } : null }),
    h("span.l-txt", h("span.ellipsis", e.name),
      h("small.ellipsis", [st.on ? (st.brightness !== undefined ? `${st.brightness}%` : t("On")) : t("Off"),
                           e.room !== e.name && e.room].filter(Boolean).join(" · "))));
  el.setAttribute("aria-pressed", st.on ? "true" : "false");
  el.setAttribute("aria-label", `${e.name}: ${st.on ? t("On") : t("Off")}`);
  longPress(el, () => lightSheet(e), () => act(() => callEntity(e.id, st.on ? "turn_off" : "turn_on")));
  return el;
}

function longPress(el, onLong, onTap, ms = 450) {
  let timer = null, long = false, moved = false, x = 0, y = 0;
  el.addEventListener("pointerdown", (ev) => {
    long = moved = false; x = ev.clientX; y = ev.clientY;
    timer = setTimeout(() => { long = true; navigator.vibrate?.(10); onLong(); }, ms);
  });
  el.addEventListener("pointermove", (ev) => {
    if (Math.abs(ev.clientX - x) + Math.abs(ev.clientY - y) > 10) { moved = true; clearTimeout(timer); }
  });
  el.addEventListener("pointerup", () => { clearTimeout(timer); if (!long && !moved) onTap(); });
  el.addEventListener("pointercancel", () => clearTimeout(timer));
  el.addEventListener("contextmenu", (ev) => ev.preventDefault());
  el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onTap(); } });
}

/** What's playing right now (Apple TV with a title, or a speaker that plays) - else nothing. */
function playingCard(ents) {
  const e = ents.find((x) => x.kind === "media_player" && x.state.on && x.state.title && ["playing", "paused"].includes(x.state.state))
         || ents.find((x) => x.kind === "speaker" && x.state.on && x.state.playing);
  if (!e) return null;
  const st = e.state, playing = st.state === "playing" || st.playing;
  const pos = st.duration ? Math.min(100, (st.position || 0) / st.duration * 100) : null;
  const can = (a) => e.kind === "media_player" || e.actions.includes(a);
  return h("div.card.playing",
    st.artwork ? h("img.art", { src: st.artwork, alt: "" }) : h("div.art", icon(e.kind === "speaker" ? "speaker" : "appletv")),
    h("div.p-txt", h("b.ellipsis", st.title || st.source || e.name),
      h("div.small.muted.ellipsis", [st.artist || st.app, e.name].filter(Boolean).join(" · ")),
      pos !== null && h("div.progress", h("div", { style: { width: `${pos}%` } }))),
    h("div.row", { style: { gap: "2px" } },
      can("previous") && h("button.icon.ghost", { "aria-label": t("Previous"), onclick: () => act(() => callEntity(e.id, "previous")) }, icon("prev")),
      can("play_pause") && h("button.icon", { "aria-label": t("Play/pause"), onclick: () => act(() => callEntity(e.id, "play_pause")) }, icon(playing ? "pause" : "play")),
      can("next") && h("button.icon.ghost", { "aria-label": t("Next"), onclick: () => act(() => callEntity(e.id, "next")) }, icon("next"))));
}

const DEVICE_ICON = { tv: "tv", media_player: "appletv", speaker: "speaker", switch: "plug" };

// speaker inputs as people say them
const SOURCE = () => ({ wifi: t("Wi-Fi"), bluetooth: t("Bluetooth"), tv: t("TV"), optical: t("Optical"), aux: t("Aux"),
                        usb: t("USB"), analog: t("Aux") });
const sourceName = (s) => (s ? SOURCE()[String(s).toLowerCase()] || s : "");

function deviceSub(e) {
  const st = e.state;
  if (!e.available) return t("Unavailable");
  if (e.kind === "speaker") return st.on ? [t("On"), sourceName(st.source), st.playing && t("playing")].filter(Boolean).join(" · ") : t("Off");
  if (e.kind === "media_player") {
    if (!st.on) return t("Off");
    if (st.title && ["playing", "paused"].includes(st.state)) return `${stateName(st.state)} · ${st.title}`;
    return st.app ? t("On · {app}", { app: st.app }) : t("On");
  }
  if (e.kind === "tv") return st.on ? (st.source || t("On")) : t("Off");
  return st.on ? t("On") : t("Off");
}

/** Thin row: name + state, (volume), on/off. Tap the name for remote / all controls. */
function deviceRow(e) {
  const st = e.state;
  return h(`div.hrow${st.on ? ".on" : ""}${e.available ? "" : ".unavailable"}`,
    h("button.h-main", { onclick: () => deviceSheet(e) },
      h("div.ico", icon(DEVICE_ICON[e.kind] || "sensor")),
      h("div.h-txt", h("div.name.ellipsis", e.name), h("div.sub.ellipsis", deviceSub(e)))),
    toggle(st.on, (on) => act(() => callEntity(e.id, on ? "turn_on" : "turn_off")), !e.available, e.name),
    e.kind === "speaker" && st.on && h("div.h-vol", volume(e)));  // own line under the name
}

/** Volume slider with its number (updates while dragging, sends on release). */
function volume(e) {
  const num = h("span.vol-n", `${e.state.volume ?? 0}`);
  return h("span.vol-wrap", icon("volume"),
    h("input.vol", { type: "range", min: 0, max: e.attrs.max_volume || 100, value: e.state.volume ?? 0,
      "aria-label": t("Volume"), oninput: (ev) => { num.textContent = ev.target.value; },
      onchange: (ev) => act(() => callEntity(e.id, "set_volume", { value: +ev.target.value })) }),
    num);
}

function deviceSheet(e) {
  if (e.kind === "tv" && e.actions.includes("remote")) return remoteSheet(e);
  const full = { speaker: speakerFull, media_player: mediaFull }[e.kind];
  sheet(e.name, full ? full(e) : tile(e));
}

/** "Stasera: Risotto" (lunch too before 15:00); nothing planned -> a quiet link to plan. */
function mealCard(today) {
  if (!today) return null;
  const hr = new Date().getHours();
  const slots = [hr < 15 && today.lunch && [t("Lunch"), today.lunch], today.dinner && [t("Tonight"), today.dinner]].filter(Boolean);
  if (!slots.length) {
    return h("a.card.meal.empty", { href: "#/recipes/week" }, h("span.sc-ico", icon("utensils")),
      h("span.spacer", t("What's for dinner? Plan the week")), icon("right"));
  }
  return h("div.card.meal", slots.map(([when, sl]) =>
    h("a.meal-row", { href: sl.recipe_id ? `#/recipes/recipe/${sl.recipe_id}` : "#/recipes/week" },
      sl.image ? h("img.meal-img", { src: sl.image, alt: "" }) : h("span.sc-ico", icon("utensils")),
      h("div.stack.spacer", { style: { gap: "1px", minWidth: 0 } },
        h("span.muted.small", when), h("b.ellipsis", sl.title)),
      h("span.chip", t("{n} pers.", { n: sl.servings })))));
}

/** The latest notes (pinned first, max 3) and a field to leave a new one. */
function notesBlock(notes) {
  const input = h("input", { placeholder: t("Write a note…"), enterkeyhint: "send", "aria-label": t("New note") });
  const add = () => {
    const text = input.value.trim();
    if (!text) return;
    act(async () => { await api("p/notes/notes", { body: { text } }); input.value = ""; toast(t("Note saved")); });
  };
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); add(); } });
  const preview = (n) => n.text || (n.items || []).map((i) => (i.done ? "✓ " : "• ") + i.text).join("  ");
  return h("div.card.notes-block",
    notes.slice(0, 3).map((n) => h("a.n-line", { href: "#/notes" },
      n.pinned && h("span.n-pin", "📌"),
      h("span.n-text", preview(n)),
      h("span.muted.small.n-meta", [n.author, fmt.ago(n.updated)].filter(Boolean).join(" · ")))),
    h("div.n-add", input, h("button.icon.primary", { "aria-label": t("Add"), onclick: add }, icon("plus"))));
}

/** Quick links one level deeper than the tab bar (the tab bar already has Media/Cucina). */
function shortcuts() {
  const items = [
    hasPlugin("recipes") && ["cart", t("Shopping list"), "#/recipes/shopping"],
    hasPlugin("media") && ["heart", t("To watch"), "#/media/watchlist"],
    hasPlugin("recipes") && ["utensils", t("Recipe book"), "#/recipes/mine"],
    hasPlugin("media") && ["download", t("Coming in"), "#/media/downloads"],
  ].filter(Boolean);
  return items.length ? h("div.shortcuts", items.map(([ico, label, hash]) =>
    h("a.sc", { href: hash }, h("span.sc-ico", icon(ico)), h("span.sc-label", label)))) : null;
}

// ------------------------------------------------------------------ tiles

function tile(e) {
  const maker = { climate: climateTile, water_heater: climateTile, sensor: sensorTile, light: lightTile,
                  switch: switchTile, speaker: speakerTile, media_player: mediaTile, tv: tvTile }[e.kind];
  const el = maker ? maker(e) : genericTile(e);
  el.dataset.room = groupOf(e);
  if (!e.available) el.classList.add("unavailable");
  el.append(h("button.icon.ghost.more", { title: t("Edit"), onclick: (ev) => { ev.stopPropagation(); editEntity(e); } },
              icon("more")));
  // don't re-render a tile while the user drags one of its sliders
  el.addEventListener("pointerdown", (ev) => { if (ev.target.type === "range") el.dataset.busy = "1"; });
  el.addEventListener("pointerup", () => setTimeout(() => delete el.dataset.busy, 1500));
  return el;
}

const top = (e, ico, sub) => h("div.top", h("div.ico", icon(ico)),
  h("div", { style: { minWidth: 0, paddingRight: "22px" } }, h("div.name.ellipsis", e.name),
    h("div.sub.ellipsis", e.available ? sub : t("Unavailable"))));

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
        st.heating ? t("Heating now") : st.mode === "off" ? t("Off") : st.mode === "auto" ? t("Schedule") : t("Idle")),
    h("div.row",
      h("div.big", fmt.temp(st.current_temperature), h("small", "°C")),
      h("div.spacer"),
      h("div.stepper",
        h("button.icon", { onclick: () => step(-1), "aria-label": t("Lower") }, icon("minus")),
        val,
        h("button.icon", { onclick: () => step(1), "aria-label": t("Raise") }, icon("plus")))),
    modes.length > 1 && h("div.seg", { style: { alignSelf: "flex-start" } },
      modes.map((m) => h(`button${st.mode === m ? ".active" : ""}`,
        { onclick: (ev) => act(() => callEntity(e.id, "set_mode", { mode: m }), ev.currentTarget) },
        { off: t("Off"), heat: water ? t("On") : t("Manual"), auto: t("Auto") }[m] || m))));
}

// -- generic sensors: any numeric state, units from attrs.units --------------------

const groupOf = (e) => e.room || e.attrs.group || "Other";
const KEY_COLORS = { temperature: "#ff7a45", humidity: "#33a4d8", download: "#30b85a", upload: "#0a84ff",
                     latency: "#f2a516", used: "#a970ff", free: "#33a4d8", usage: "#ff7a45" };
const colorOf = (k) => KEY_COLORS[k] || "#ff7a45";
const labelOf = (k) => ({ temperature: t("Temperature"), humidity: t("Humidity"), battery: t("Battery"), usage: t("Usage"),
  load: t("Load"), used: t("Used"), free: t("Free"), download: t("Download"), upload: t("Upload"), signal: t("Signal"),
  quality: t("Link quality"), latency: t("Latency") }[k]
  || (k.charAt(0).toUpperCase() + k.slice(1)).replace(/_/g, " "));
const unitOf = (e, k) => (e.attrs.units || {})[k]
  ?? ({ temperature: "°", humidity: "%", battery: "%" }[k] || "");
const fmtNum = (v) => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10).toLocaleString(locale());

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
    const k = Math.max(0, Math.min(1, (st.color_temp - 2200) / 1800));
    const mix = (a, b) => Math.round(a + (b - a) * k);
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
    top(e, "bulb", st.on ? (st.brightness !== undefined ? `${st.brightness}%` : t("On")) : t("Off")));
  const color = st.on && lampColor(st);
  if (color) el.querySelector(".ico").style.background = color;
  if (st.brightness !== undefined || tunable) {
    el.append(h("div.row", { style: { gap: "8px" } },
      st.brightness !== undefined && h("input", { type: "range", min: 1, max: 100, value: st.brightness ?? 100,
        style: { flex: 1, minWidth: 0 },
        onclick: (ev) => ev.stopPropagation(),
        onchange: (ev) => act(() => callEntity(e.id, "turn_on", { brightness: +ev.target.value })) }),
      tunable && h("button.icon", { title: t("Colour"), "aria-label": t("Colour and white"),
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
        ev.currentTarget.textContent = st.on ? t("Turn off") : t("Turn on");
      } }, st.on ? t("Turn off") : t("Turn on"))),
    st.brightness !== undefined && slider(t("Brightness"), { min: 1, max: 100, value: st.brightness ?? 100 },
      (v) => { st.brightness = v; st.on = true; paint(); send({ brightness: v }); }),
    st.color_temp !== undefined && h("div.stack", { style: { gap: "6px" } },
      slider(t("White"), { min: lo, max: hi, step: 50, value: st.color_temp, class: "ct-range" },
        (v) => { st.color_temp = v; st.color_mode = "temperature"; st.on = true; paint(); send({ color_temp: v }); }),
      h("div.row.wrap", { style: { gap: "6px" } }, [[t("Warm"), lo], [t("Neutral"), Math.round((lo + hi) / 2 / 50) * 50], [t("Cool"), hi]]
        .map(([name, k]) => h("button", { onclick: () => {
          st.color_temp = k; st.color_mode = "temperature"; st.on = true; paint();
          body.querySelector(".ct-range").value = k; send({ color_temp: k });
        } }, name)))),
    e.attrs.color && h("div.stack", { style: { gap: "8px" } },
      h("div.small.muted", t("Colour")),
      h("div.swatches", SWATCHES.map((hue) => h("button.swatch", {
        style: { background: `hsl(${hue} 85% 55%)` }, "aria-label": t("Hue {n}", { n: hue }),
        onclick: () => {
          st.hue = hue; st.saturation = Math.max(st.saturation ?? 0, 60); st.color_mode = "color"; st.on = true;
          body.querySelector(".sat-range").value = st.saturation; paint();
          send({ hue, saturation: st.saturation });
        } }))),
      slider(t("Saturation"), { min: 0, max: 100, value: st.saturation ?? 100, class: "sat-range" },
        (v) => { st.saturation = v; st.color_mode = "color"; st.on = true; paint(); send({ hue: st.hue ?? 0, saturation: v }); })),
  );
  paint();
  return s;
}

function switchTile(e) {
  return h(`div.card.tile${e.state.on ? ".active" : ""}`,
    { style: { cursor: "pointer" }, onclick: () => act(() => callEntity(e.id, e.state.on ? "turn_off" : "turn_on")) },
    top(e, "plug", e.state.on ? t("On") : t("Off")));
}

function powerBtn(e) {
  return h(`button.icon${e.state.on ? ".on" : ""}`,
    { title: t("Power"), onclick: (ev) => act(() => callEntity(e.id, e.state.on ? "turn_off" : "turn_on"), ev.currentTarget) },
    icon("power"));
}

/** All speaker controls (the tile's sheet). */
function speakerFull(e) {
  const st = e.state, sources = e.attrs.sources || [];
  return h(`div.card.tile.span2${st.on ? ".active" : ""}`,
    top(e, "speaker", st.on ? `${st.source || ""}${st.playing ? ` · ${t("playing")}` : ""}` : t("Standby")),
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

/** Apple TV with cover, progress and transport (while playing, or in its sheet). */
function mediaFull(e) {
  const st = e.state, playing = st.state === "playing";
  const pos = st.duration ? Math.min(100, (st.position || 0) / st.duration * 100) : null;
  return h(`div.card.tile.span2${st.on ? ".active" : ""}`,
    top(e, "appletv", !st.on ? t("Off") : st.app || stateName(st.state || "idle")),
    st.title ? h("div.media-tile",
      st.artwork ? h("img.art", { src: st.artwork, alt: "" }) : h("div.art"),
      h("div.stack", { style: { gap: "4px", minWidth: 0, flex: 1 } },
        h("b.ellipsis", st.title), st.artist && h("div.small.muted.ellipsis", st.artist),
        pos !== null && h("div.progress", h("div", { style: { width: `${pos}%` } })),
        pos !== null && h("div.tiny.muted", `${fmt.duration(st.position)} / ${fmt.duration(st.duration)}`)))
      : null,
    h("div.row",
      powerBtn(e),
      h("button.icon", { title: t("Remote"), onclick: () => remoteSheet(e) }, icon("remote")),
      e.actions.includes("launch_app") && h("button", { onclick: (ev) => act(() => callEntity(e.id, "launch_app", { app: "Infuse" }), ev.currentTarget) }, "Infuse"),
      h("div.spacer"),
      h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "previous")) }, icon("prev")),
      h("button.icon", { onclick: () => act(() => callEntity(e.id, "play_pause")) }, icon(playing ? "pause" : "play")),
      h("button.icon.ghost", { onclick: () => act(() => callEntity(e.id, "next")) }, icon("next"))));
}

// TV, Apple TV (idle), speaker: one compact line - name, state, on/off; tap for remote or
// every control. The Apple TV grows into the cover card only while something plays.
function compactTile(e, extra) {
  return h(`div.card.tile.compact.span2${e.state.on ? ".active" : ""}`,
    h("div.c-main",
      h("button.h-main", { onclick: () => deviceSheet(e) },
        h("div.ico", icon(DEVICE_ICON[e.kind] || "sensor")),
        h("div.h-txt", h("div.name.ellipsis", e.name), h("div.sub.ellipsis", deviceSub(e)))),
      toggle(e.state.on, (on) => act(() => callEntity(e.id, on ? "turn_on" : "turn_off")), !e.available, e.name)),
    extra);
}

const tvTile = (e) => compactTile(e, e.attrs.experimental && h("span.chip.warn.tiny", t("beta")));

function mediaTile(e) {
  const st = e.state;
  return st.on && st.title && ["playing", "paused"].includes(st.state) ? mediaFull(e) : compactTile(e);
}

const speakerTile = (e) => compactTile(e, e.state.on && volume(e));

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
  const room = h("input", { value: e.room, list: "rooms-list", placeholder: t("Room") });
  const rooms = h("datalist#rooms-list", [...new Set([...store.rooms,
    ...[...store.entities.values()].map((x) => x.room).filter(Boolean)])].map((r) => h("option", { value: r })));
  const hidden = h("input", { type: "checkbox", checked: !!e.attrs.hidden });
  const s = sheet(t("Edit {name}", { name: e.name }), [
    h("label.field", t("Name"), name),
    h("label.field", t("Room"), room, rooms),
    h("label.row.small", hidden, t("Hide from Home")),
    h("div.small.muted", `${e.id} · ${e.plugin} · ${e.kind}`),
    h("button.primary.lg", { onclick: async (ev) => {
      await act(() => api(`entities/${encodeURIComponent(e.id)}`,
        { method: "PATCH", body: { name: name.value.trim(), room: room.value.trim(), hidden: hidden.checked } }), ev.currentTarget);
      s.close(); toast(t("Saved"));
    } }, t("Save")),
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
        h("a.small", { href: `#/history/${encodeURIComponent(e.id)}:${k}`, onclick: () => s.close() }, t("Open in History"))), c);
    }));
    body.replaceChildren(
      h("div.seg", [24, 168, 720].map((hr) => h(`button${hr === hours ? ".active" : ""}`,
        { onclick: () => { hours = hr; draw(); } }, hr === 24 ? t("24 h") : hr === 168 ? t("7 days") : t("30 days")))),
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
    const tx = x0 + span * i / 4, d = new Date(tx * 1000);
    const label = span > 2 * 86400 ? fmt.date(d, { weekday: false }) : fmt.time(d);
    ctx.fillText(label, Math.min(X(tx), w - 40), hgt - 3);
  }
}
