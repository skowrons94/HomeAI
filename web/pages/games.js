// Games: what is on the NAS, what is on the Steam Deck, one tap to install (hub/plugins/games/).
// Route: #/games. While something installs the page refreshes every 2 s, otherwise every 30 s.
// Browse: search, platform chips, status filter, sort (remembered per device). Tap a card for the
// detail sheet; the gear opens the settings sheet (plugin config, Deck status, rescan, skipped items).
import { api, h, icon, toast, ensureCss, act, fmt, t, sheet } from "../app.js";

const P = (path, opts) => api(`p/games/${path}`, opts);
const KIND = () => ({ portable: t("PC"), installer: t("PC installer"), native: t("Linux"), rom: t("Emulation") });
const KEY = "casa.games.view";
const SORTS = () => [["az", t("A–Z")], ["new", t("Recently added")], ["size", t("Size")], ["system", t("System")]];
const STATUS = () => [["all", t("All")], ["installed", t("Installed")], ["not", t("Not installed")],
  ["update", t("Update available")], ["installing", t("Installing")]];

let data = { games: [], problems: [], deck: {}, systems: [] };
let loaded = false;
let view = { plat: "all", status: "all", sort: "az" };
let query = "";
let root = null;
let timer = null;
let openId = null;      // game whose detail sheet is open
let detail = null;      // {el, close}
const els = {};

try { view = { ...view, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* private mode */ }
const saveView = () => { try { localStorage.setItem(KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const busyNow = () => data.games.some((g) => g.state === "installing");
const platName = (k) => ({ pc: t("PC"), linux: t("Linux") }[k] || k.toUpperCase());

async function refresh(path = "library", opts) {
  try { data = await P(path, opts); loaded = true; }
  catch (e) { if (root?.isConnected) toast(e.message, true); }
  draw();
  clearTimeout(timer);
  if (root?.isConnected) timer = setTimeout(() => refresh(), busyNow() ? 2000 : 30000);
}

const label = (g) => ({
  not_installed: t("Install"), installed: t("Reinstall"), update: t("Update to {v}", { v: g.version }),
  failed: t("Try again"), needs_manual: t("Download again"),
}[g.state]);

function badge(g) {
  const text = {
    installed: g.installed_version ? t("Installed · {v}", { v: g.installed_version }) : t("Installed"),
    update: t("Update available ({v})", { v: g.installed_version || "?" }),
    installing: t("Installing…"), failed: t("Failed"), needs_manual: t("Needs a manual step"),
  }[g.state];
  return text ? h(`span.gm-badge.s-${g.state}`, text) : null;
}

// ---- covers -------------------------------------------------------------------------------

const hue = (s) => { let n = 0; for (const c of s) n = (n * 31 + c.charCodeAt(0)) % 360; return n; };
const initials = (title) => {
  const w = title.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2))).toUpperCase();
};

function cover(g, cls = "") {
  if (g.cover) return h(`img.gm-cover${cls}`, { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, loading: "lazy", alt: "" });
  const hu = hue(g.platform || g.system || g.kind);
  return h(`div.gm-cover.none${cls}`, { style: `--gh:${hu}` },
    h("span.gm-init", initials(g.title)), h("span.gm-sys", platName(g.platform || "pc")));
}

// ---- actions ------------------------------------------------------------------------------

const install = (g, e) => act(async () => {
  await P("install", { body: { id: g.id } }); toast(t("Sent to the Steam Deck")); await refresh();
}, e.currentTarget);

const remove = (g, e) => confirm(t("Remove {title} from the Steam Deck?", { title: g.title })) && act(async () => {
  const r = await P("uninstall", { body: { id: g.id } });
  if (r.note) toast(r.note);
  await refresh();
}, e.currentTarget);

const applySteam = (e) => confirm(t("Close and reopen Steam on the Deck now?")) && act(async () => {
  await P("apply", { body: { restart: !!data.deck?.steam_running } });
  toast(t("Added to Steam")); await refresh();
}, e.currentTarget);

function actions(g) {
  const online = data.deck?.online;
  const primary = g.state === "not_installed" || g.state === "update";
  const go = h("button" + (primary ? ".primary" : ""), {
    disabled: !online || g.state === "installing",
    title: online ? "" : t("The Steam Deck is not reachable"),
    onclick: (e) => install(g, e),
  }, label(g) || t("Installing…"));
  const rm = (g.state === "installed" || g.state === "update")
    ? h("button.ghost", { disabled: !online, onclick: (e) => remove(g, e) }, t("Remove")) : null;
  const steam = data.deck?.online && data.deck.pending && g.state === "installed" && g.kind !== "installer"
    ? h("button", { onclick: applySteam }, t("Apply to Steam")) : null;
  return h("div.gm-actions", go, rm, steam);
}

// ---- card ---------------------------------------------------------------------------------

function card(g) {
  return h("button.gm-tile", { onclick: () => openDetail(g.id), "aria-label": g.title },
    h("div.gm-art", cover(g), badge(g),
      g.state === "installing" ? h("div.progress", h("div", { style: { width: `${Math.round((g.progress || 0) * 100)}%` } })) : null),
    h("div.gm-title", g.title),
    h("div.gm-meta", [platName(g.platform), g.version && `v${g.version}`, fmt.bytes(g.size)].filter(Boolean).join(" · ")));
}

// ---- detail sheet -------------------------------------------------------------------------

function row(k, v) { return v || v === 0 ? h("div.gm-kv", h("span", k), h("b", v)) : null; }

function detailBody(g) {
  return [
    h("div.gm-dhead", cover(g, ".big"),
      h("div.gm-dinfo", h("div.gm-dtitle", g.title), badge(g),
        h("div.gm-meta", [KIND()[g.kind], g.system && g.system.toUpperCase()].filter(Boolean).join(" · ")))),
    g.state === "installing" ? h("div.stack",
      h("div.progress", h("div", { style: { width: `${Math.round((g.progress || 0) * 100)}%` } })),
      h("div.gm-note", `${Math.round((g.progress || 0) * 100)}%${g.message ? " · " + g.message : ""}`)) : null,
    g.message && g.state !== "installing" ? h("div.gm-banner" + (g.state === "failed" ? ".bad" : ""), g.message) : null,
    g.state === "needs_manual" ? h("div.gm-banner", t("This is an installer: it was downloaded to the Deck but you must run it there (Desktop Mode, with Proton).")) : null,
    g.note && g.note !== g.message ? h("div.gm-note", g.note) : null,
    h("div.gm-kvs",
      row(t("Version"), g.version ? `v${g.version}` : ""),
      row(t("Installed on the Deck"), g.installed_version ? `v${g.installed_version}` : (g.state === "installed" || g.state === "update" ? t("Yes") : "")),
      row(t("Size"), fmt.bytes(g.size)),
      row(t("Files"), fmt.num(g.files)),
      row(t("Added"), g.added ? fmt.date(g.added, { weekday: false }) : ""),
      row(t("Folder on the NAS"), g.path)),
    actions(g),
  ].filter(Boolean);
}

function openDetail(id) {
  const g = data.games.find((x) => x.id === id);
  if (!g) return;
  openId = id;
  detail?.close();
  detail = sheet(g.title, detailBody(g), { onClose: () => { openId = null; detail = null; } });
}

function redrawDetail() {
  if (!openId || !detail) return;
  const g = data.games.find((x) => x.id === openId);
  if (!g) { detail.close(); return; }
  detail.el.replaceChildren(...detailBody(g));
}

// ---- settings sheet -----------------------------------------------------------------------

async function openSettings() {
  const { renderForm } = await import(`./settings.js?v=${window.CASA_V}`);
  const all = await api("settings");
  const p = all.plugins.find((x) => x.name === "games");
  if (!p) return;
  const form = renderForm(p.schema || [], p.values || {});
  const status = h("div.stack");
  const skipped = () => (data.problems || []).length ? h("details.gm-problems", { open: true },
    h("summary", t("{n} items skipped", { n: data.problems.length })),
    ...data.problems.map((x) => h("div.gm-note", h("b", x.path), ": ", x.reason))) : null;
  const paint = (st) => {
    const d = st || data.deck || {};
    status.replaceChildren(
      h("div.row.wrap",
        h("span.chip" + (d.online ? ".ok" : ".bad"), d.online ? t("Online") : t("Offline")),
        d.online ? h("span.chip" + (d.steam_running ? ".accent" : ""), d.steam_running ? t("Steam running") : t("Steam closed")) : null,
        d.online ? h("span.chip" + (d.pending ? ".warn" : ""), t("{n} waiting for Steam", { n: d.pending || 0 })) : null),
      !d.online && (d.error || !d.configured) ? h("div.gm-note", d.error || t("Set the Steam Deck address and token in Settings → Games.")) : null);
  };
  paint();
  const s = sheet(t("Games settings"), [
    h("h3", t("Steam Deck")), status,
    h("div.row.wrap",
      h("button", { onclick: (e) => act(async () => {
        const r = await P("test", { body: {} });
        paint({ ...data.deck, online: r.ok, error: r.error, pending: r.pending, steam_running: r.steam_running });
        toast(r.ok ? t("Connected to the Steam Deck") : (r.error || t("The Steam Deck is not reachable")), !r.ok);
      }, e.currentTarget) }, icon("wifi"), t("Test connection")),
      h("button", { onclick: (e) => act(async () => { await refresh("rescan", { method: "POST", body: {} }); toast(t("Library rescanned")); s.close(); },
        e.currentTarget) }, icon("refresh"), t("Rescan library")),
      h("button", { disabled: !data.deck?.online || !data.deck?.pending, onclick: applySteam }, t("Apply to Steam"))),
    h("h3", t("Settings")), form.el,
    h("button.primary.lg", { onclick: (e) => act(async () => {
      const values = form.values();
      if (!values["deck.token"]) delete values["deck.token"];     // empty = keep the current token
      const r = await api("settings/plugins/games", { method: "PUT", body: { values } });
      if (r.error) { toast(r.error, true); return; }
      toast(t("Saved")); s.close(); await refresh();
    }, e.currentTarget) }, t("Save")),
    skipped(),
  ].filter(Boolean), { wide: true });
}

// ---- page ---------------------------------------------------------------------------------

function banner() {
  const d = data.deck || {};
  const out = [];
  if (loaded && !d.online) out.push(h("div.gm-banner.bad", icon("wifi"),
    h("span", d.error || t("The Steam Deck is not reachable"), " ", t("You can still browse the library."))));
  if (d.online && d.pending) {
    out.push(h("div.gm-banner",
      h("span", t("{n} waiting to be added to Steam. This closes and reopens Steam; use Desktop Mode.", { n: d.pending })),
      h("button.primary", { onclick: applySteam }, t("Apply to Steam"))));
  }
  return out;
}

function matches(g) {
  const q = query.trim().toLowerCase();
  if (q && !g.title.toLowerCase().includes(q)) return false;
  if (view.plat !== "all" && g.platform !== view.plat) return false;
  const s = view.status;
  if (s === "installed") return g.state === "installed" || g.state === "update";
  if (s === "not") return g.state === "not_installed" || g.state === "failed" || g.state === "needs_manual";
  if (s === "update" || s === "installing") return g.state === s;
  return true;
}

const cmp = {
  az: (a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base", numeric: true }),
  new: (a, b) => (b.added || 0) - (a.added || 0) || cmp.az(a, b),
  size: (a, b) => (b.size || 0) - (a.size || 0) || cmp.az(a, b),
  system: (a, b) => a.platform.localeCompare(b.platform) || cmp.az(a, b),
};

function chip(label, count, on, fn) {
  return h("button.chip" + (on ? ".accent.on" : ""), { onclick: fn, "aria-pressed": on },
    label, count != null ? h("span.gm-count", fmt.num(count)) : null);
}

function drawChips() {
  const sys = data.systems || [];
  els.chips.replaceChildren(
    chip(t("All"), data.games.length, view.plat === "all", () => { view.plat = "all"; saveView(); draw(); }),
    ...sys.map((s) => chip(platName(s.key), s.count, view.plat === s.key, () => { view.plat = s.key; saveView(); draw(); })));
  els.status.replaceChildren(...STATUS().map(([k, l]) => h("option", { value: k, selected: view.status === k }, l)));
  els.sort.replaceChildren(...SORTS().map(([k, l]) => h("option", { value: k, selected: view.sort === k }, l)));
}

function emptyState() {
  if (!loaded) return h("div.empty", t("Loading…"));
  if (!data.games.length) {
    return h("div.empty.gm-empty",
      h("b", data.exists === false ? t("The games folder does not exist yet: {path}", { path: data.root || "" })
                                   : t("No games found in {path}", { path: data.root || "" })),
      h("p.muted", t("Put games in pc/ and ROMs in emu/<system>/. One folder is one game; add cover.jpg to show a cover.")),
      h("pre.gm-tree", "games/\n  pc/\n    Celeste v1.4/Celeste.exe\n    Penguin/Penguin.AppImage\n  emu/\n    snes/Super Game.sfc\n    psx/Disc Game/Disc.cue"),
      h("div.row", { style: { justifyContent: "center" } },
        h("button", { onclick: openSettings }, icon("settings"), t("Games settings")),
        h("button", { onclick: (e) => act(() => refresh("rescan", { method: "POST", body: {} }), e.currentTarget) }, icon("refresh"), t("Rescan library"))));
  }
  return h("div.empty", t("No matches."), " ",
    h("button.ghost", { onclick: () => { query = ""; els.q.value = ""; view.plat = "all"; view.status = "all"; saveView(); draw(); } }, t("Clear filters")));
}

function draw() {
  if (!root?.isConnected) return;
  els.banner.replaceChildren(...banner());
  drawChips();
  const list = data.games.filter(matches).sort(cmp[view.sort] || cmp.az);
  if (!list.length) els.grid.replaceChildren(emptyState());
  else if (view.sort === "system") {
    const out = [];
    let cur = null;
    for (const g of list) {
      if (g.platform !== cur) {
        cur = g.platform;
        out.push(h("h3.gm-section", platName(cur), h("span.gm-count", fmt.num(list.filter((x) => x.platform === cur).length))));
        out.push(h("div.gm-grid"));
      }
      out[out.length - 1].append(card(g));
    }
    els.grid.replaceChildren(...out);
  } else els.grid.replaceChildren(h("div.gm-grid", list.map(card)));
  els.count.textContent = data.games.length && list.length !== data.games.length
    ? t("{n} of {total}", { n: list.length, total: data.games.length }) : "";
  redrawDetail();
}

export default {
  async render(el) {
    ensureCss(`/static/pages/games.css?v=${window.CASA_V}`);
    loaded = false;
    root = h("div.gm");
    els.banner = h("div");
    els.chips = h("div.gm-chips");
    els.q = h("input", { type: "search", placeholder: t("Search…"), value: query, autocomplete: "off",
      oninput: (e) => { query = e.target.value; draw(); } });
    els.status = h("select", { "aria-label": t("Status"), onchange: (e) => { view.status = e.target.value; saveView(); draw(); } });
    els.sort = h("select", { "aria-label": t("Sort"), onchange: (e) => { view.sort = e.target.value; saveView(); draw(); } });
    els.count = h("span.muted.small");
    els.grid = h("div");
    root.append(
      h("div.gm-head", h("h1", t("Games")), els.count, h("div.spacer"),
        h("button.icon.ghost", { "aria-label": t("Rescan"), onclick: (e) => act(() => refresh("rescan", { method: "POST", body: {} }), e.currentTarget) }, icon("refresh")),
        h("button.icon.ghost", { "aria-label": t("Games settings"), onclick: openSettings }, icon("settings"))),
      els.banner,
      h("div.gm-tools", els.q, els.chips, h("div.gm-selects", els.status, els.sort)),
      els.grid);
    el.replaceChildren(root);
    draw();
    await refresh();
  },
};
