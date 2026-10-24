// Games: what is on the NAS, what is on the Steam Deck, one tap to install, and a way to find more
// (hub/plugins/games/). Route: #/games.
// Three tabs: Library (browse, detail sheet, install), Discover (rails of popular / new / on-sale games,
// no API key needed; hub/plugins/games/discover.py) and Find (torrent search that downloads into the
// games folder, plus the downloads in progress). The gear opens the settings sheet.
// Polling: 2 s while something installs, 3 s while a download is active on the Find tab, else 30 s.
// A failed poll never toasts: it leaves a small inline note that goes away by itself.
import { api, h, icon, toast, ensureCss, act, fmt, t, sheet } from "../app.js";

const P = (path, opts) => api(`p/games/${path}`, opts);
const KEY = "casa.games.view";
const KIND = () => ({ portable: t("PC"), installer: t("PC installer"), native: t("Linux"), rom: t("Emulation") });
const SORTS = () => [["az", t("A–Z")], ["new", t("Recently added")], ["size", t("Size")], ["system", t("System")]];

let data = { games: [], problems: [], deck: {}, systems: [] };
let loaded = false;
let libError = "";      // last library refresh failure (inline note), "" when fine
// filter: "all" | "installed" | "update" | "p:<platform>"
let view = { tab: "library", filter: "all", sort: "az", fplat: "pc" };
let query = "";
let root = null;
let timer = null;
let openId = null;      // game whose detail sheet is open
let detail = null;      // {el, close, sig}
const els = {};
// discover: rails come from two independent requests (steam: no key, igdb: only with keys)
const dx = { rails: { steam: [], igdb: [] }, errors: { steam: [], igdb: [] }, done: { steam: false, igdb: false },
  igdb: false, at: 0, seq: 0 };
const fx = { platforms: [], q: "", results: [], providers: [], page: 1, more: false, error: "", off: false,
  loading: false, searched: false, downloads: [], dlError: "", seen: new Map() };

try { view = { ...view, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* private mode */ }
const saveView = () => { try { localStorage.setItem(KEY, JSON.stringify(view)); } catch { /* ignore */ } };

const busyNow = () => data.games.some((g) => g.state === "installing");
const activeDl = () => fx.downloads.filter((d) => d.state === "downloading" || d.state === "extracting");
const platName = (k) => ({ pc: t("PC"), linux: t("Linux") }[k] || String(k || "").toUpperCase());
const gtitle = (g) => g.meta?.title || g.title;
const year = (g) => (g.meta?.released || "").slice(0, 4);

// ---- data ---------------------------------------------------------------------------------

async function loadLibrary(path = "library", opts, quiet = false) {
  try { data = await P(path, opts); libError = ""; }
  catch (e) {
    libError = e.message || t("Something went wrong");
    // polling never toasts (it would repeat every few seconds): the inline note is enough
    if (!quiet && root?.isConnected && loaded) toast(libError, true);
  }
  loaded = true;
  drawLibrary();
}

async function loadDownloads() {
  try {
    const list = await P("find/downloads");
    fx.downloads = Array.isArray(list) ? list : (list?.downloads || []);
    fx.dlError = "";
  } catch (e) { fx.dlError = e.message || t("Something went wrong"); }   // keep the list we had: no flicker
  // a download that just finished adds a game to the library: rescan comes from the hub, we reload
  let finished = false;
  for (const d of fx.downloads) {
    if (fx.seen.get(d.hash) && fx.seen.get(d.hash) !== "done" && d.state === "done") finished = true;
    fx.seen.set(d.hash, d.state);
  }
  drawDownloads();
  drawTabs();
  return finished;
}

function schedule() {
  clearTimeout(timer);
  if (!root?.isConnected) return;
  const ms = busyNow() ? 2000 : (view.tab === "find" && activeDl().length) ? 3000 : 30000;
  timer = setTimeout(tick, ms);
}

async function tick() {
  if (!root?.isConnected) return;
  const [, finished] = await Promise.all([loadLibrary("library", undefined, true), loadDownloads()]);
  if (finished) await loadLibrary("library", undefined, true);
  schedule();
}

const refresh = async (path, opts) => { await loadLibrary(path, opts); schedule(); };

// ---- small pieces -------------------------------------------------------------------------

const svg = (vb, inner, cls) => { const s = h("svg", { viewBox: vb, class: cls }); s.innerHTML = inner; return s; };
const sortIcon = () => svg("0 0 24 24", '<path d="M4 7h10M4 12h7M4 17h4M17 5v14M17 19l-3-3M17 19l3-3"/>', "i");

const hue = (s) => { let n = 0; for (const c of String(s)) n = (n * 31 + c.charCodeAt(0)) % 360; return n; };
const initials = (title) => {
  const w = title.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2))).toUpperCase();
};

function cover(g, cls = "") {
  const ph = () => h(`div.gm-cover.none${cls}`, { style: `--gh:${hue(g.platform || g.system || g.kind)}` },
    h("span.gm-init", initials(gtitle(g))), h("span.gm-sys", platName(g.platform || "pc")));
  if (!g.cover) return ph();
  const img = h(`img.gm-cover${cls}`, { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, loading: "lazy", alt: "" });
  img.addEventListener("error", () => img.replaceWith(ph()), { once: true });   // no broken-image icon
  return img;
}

function ring(p) {
  const c = 2 * Math.PI * 15;
  return svg("0 0 36 36", `<circle cx="18" cy="18" r="15" class="bg"/><circle cx="18" cy="18" r="15" class="fg" stroke-dasharray="${(c * Math.min(1, p || 0)).toFixed(1)} ${c.toFixed(1)}"/>`, "gm-ring");
}

const pct = (g) => Math.round((g.progress || 0) * 100);

/** Corner badge over a cover. */
function cornerBadge(g) {
  if (g.state === "installing") return h("span.gm-corner.busy", ring(g.progress), h("b", `${pct(g)}`));
  if (g.state === "installed") return h("span.gm-corner.ok", icon("check"));
  if (g.state === "update") return h("span.gm-corner.upd", icon("download"), t("Update"));
  if (g.state === "failed") return h("span.gm-corner.bad", icon("x"));
  return null;
}

function stateChip(g) {
  const m = {
    installed: [t("Installed"), "ok"], update: [t("Update available"), "warn"], installing: [t("Installing…"), "accent"],
    failed: [t("Failed"), "bad"], needs_manual: [t("Needs a manual step"), "accent"],
  }[g.state];
  return m ? h(`span.chip.${m[1]}`, m[0]) : null;
}

// ---- actions ------------------------------------------------------------------------------

const install = (g, e) => act(async () => {
  await P("install", { body: { id: g.id } }); toast(t("Sent to the Steam Deck")); await refresh();
}, e.currentTarget);

const remove = (g, e) => confirm(t("Remove {title} from the Steam Deck?", { title: gtitle(g) })) && act(async () => {
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
  const label = {
    not_installed: t("Install"), installed: t("Reinstall"), update: t("Update to {v}", { v: g.version }),
    failed: t("Try again"), needs_manual: t("Download again"),
  }[g.state] || t("Installing…");
  const primary = g.state === "not_installed" || g.state === "update";
  const rm = (g.state === "installed" || g.state === "update")
    ? h("button.danger", { disabled: !online, onclick: (e) => remove(g, e) }, icon("trash"), t("Remove")) : null;
  const steam = online && data.deck.pending && g.state === "installed" && g.kind !== "installer"
    ? h("button", { onclick: applySteam }, t("Apply to Steam")) : null;
  return h("div.gm-actions",
    h("button" + (primary ? ".primary" : ""), {
      disabled: !online || g.state === "installing", title: online ? "" : t("The Steam Deck is not reachable"),
      onclick: (e) => install(g, e) }, icon("download"), label),
    steam, rm);
}

// ---- library: cards -----------------------------------------------------------------------

function card(g) {
  const meta = [platName(g.platform), year(g)].filter(Boolean).join(" · ") || fmt.bytes(g.size);
  return h("button.gm-tile", { onclick: () => openDetail(g.id), "aria-label": gtitle(g) },
    h("div.gm-art", cover(g), cornerBadge(g),
      g.state === "installing" ? h("div.progress", h("div", { style: { width: `${pct(g)}%` } })) : null),
    h("div.gm-title", gtitle(g)),
    h("div.gm-meta", meta));
}

// ---- detail sheet -------------------------------------------------------------------------

const row = (k, v) => (v || v === 0 ? h("div.gm-kv", h("span", k), h("b", v)) : null);

function viewShot(url) {
  const close = () => { ov.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => e.key === "Escape" && close();
  const ov = h("div.gm-shot", { onclick: close }, h("img", { src: url, alt: "" }),
    h("button.icon", { "aria-label": t("Close"), onclick: close }, icon("x")));
  document.addEventListener("keydown", esc);
  document.body.append(ov);
}

function summary(text) {
  const p = h("p.gm-sum.clamp", text);
  if (text.length < 220) { p.classList.remove("clamp"); return p; }
  const btn = h("button.ghost.gm-more", { onclick: () => {
    const c = p.classList.toggle("clamp"); btn.textContent = c ? t("More") : t("Less");
  } }, t("More"));
  return h("div", p, btn);
}

function hero(g) {
  const m = g.meta;
  const none = () => h("div.gm-hero-img.none", { style: `--gh:${hue(g.platform || g.system || g.kind)}` });
  const bg = m?.has_hero ? h("img.gm-hero-img", { src: `/api/p/games/hero/${encodeURIComponent(g.id)}`, alt: "" })
    : g.cover ? h("img.gm-hero-img.blur", { src: `/api/p/games/cover/${encodeURIComponent(g.id)}`, alt: "" }) : none();
  if (bg.tagName === "IMG") bg.addEventListener("error", () => bg.replaceWith(none()), { once: true });
  return h("div.gm-hero", bg);
}

function detailBody(g) {
  const m = g.meta;
  const sub = [year(g), m?.developer].filter(Boolean).join(" · ");
  const sub2 = [...new Set([KIND()[g.kind], g.system && g.system.toUpperCase()].filter(Boolean))].join(" · ");
  const shots = (m?.screenshots || []).filter(Boolean);
  return [
    hero(g),
    h("div.gm-dhead", cover(g, ".big"),
      h("div.gm-dinfo", h("div.gm-dtitle", gtitle(g)),
        sub ? h("div.gm-sub", sub) : null, h("div.gm-meta", sub2),
        h("div.gm-chips-in", stateChip(g),
          m?.rating != null ? h("span.chip.gm-rate", icon("star"), `${fmt.num(m.rating / 10, 1)}/10`) : null))),
    m?.genres?.length ? h("div.gm-genres", m.genres.map((x) => h("span.chip", x))) : null,
    g.state === "installing" ? h("div.stack", { style: { gap: "6px" } },
      h("div.progress", h("div", { style: { width: `${pct(g)}%` } })),
      h("div.gm-note", `${pct(g)}%${g.message ? " · " + g.message : ""}`)) : null,
    g.message && g.state !== "installing" ? h("div.gm-banner" + (g.state === "failed" ? ".bad" : ""), g.message) : null,
    g.state === "needs_manual" ? h("div.gm-banner", t("This is an installer: it was downloaded to the Deck but you must run it there (Desktop Mode, with Proton).")) : null,
    g.note && g.note !== g.message ? h("div.gm-note", g.note) : null,
    m?.summary ? summary(m.summary) : null,
    shots.length ? shotStrip(shots.map((u) => ({ thumb: u, full: u }))) : null,
    actions(g),
    h("div.gm-kvs",
      row(t("Version"), g.version ? `v${g.version}` : ""),
      row(t("Installed on the Deck"), g.installed_version ? `v${g.installed_version}` : (g.state === "installed" || g.state === "update" ? t("Yes") : "")),
      row(t("Size"), fmt.bytes(g.size)),
      row(t("Files"), g.files != null ? fmt.num(g.files) : ""),
      row(t("Added"), g.added ? fmt.date(g.added, { weekday: false }) : ""),
      row(t("Folder on the NAS"), g.path)),
    h("button.ghost.gm-fix", { onclick: () => fixMatch(g) }, icon("edit"), t("Wrong game? Fix match")),
  ].filter(Boolean);
}

const sig = (g) => JSON.stringify([g.state, g.progress != null ? pct(g) : null, g.message, g.installed_version, g.meta?.igdb_id,
  g.cover, data.deck?.online, data.deck?.pending]);

function openDetail(id) {
  const g = data.games.find((x) => x.id === id);
  if (!g) return;
  openId = id;
  detail?.close();
  detail = sheet("", detailBody(g), { onClose: () => { openId = null; detail = null; } });
  detail.el.parentElement.classList.add("gm-dsheet");
  detail.sig = sig(g);
}

function redrawDetail() {
  if (!openId || !detail) return;
  const g = data.games.find((x) => x.id === openId);
  if (!g) { detail.close(); return; }
  if (sig(g) === detail.sig) return;
  detail.sig = sig(g);
  detail.el.replaceChildren(...detailBody(g));
}

function fixMatch(g) {
  const list = h("div.gm-list");
  const msg = h("div.gm-note");
  const input = h("input", { type: "search", value: gtitle(g), autocomplete: "off", enterkeyhint: "search" });
  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    list.replaceChildren(h("div.empty", h("span.spin")));
    msg.textContent = ""; msg.className = "gm-note";
    try {
      const d = await P(`meta/search?q=${encodeURIComponent(q)}&platform=${encodeURIComponent(g.platform || "")}`);
      const rs = d.results || [];
      msg.textContent = rs.length ? "" : t("No results.");
      list.replaceChildren(...rs.map((r) => h("button.gm-pick", { onclick: (e) => act(async () => {
        await P("meta/match", { body: { id: g.id, igdb_id: r.igdb_id } });
        toast(t("Match updated")); s.close(); await refresh();
      }, e.currentTarget) },
      r.cover_url ? h("img", { src: r.cover_url, loading: "lazy", alt: "" }) : h("div.gm-pick-ph", initials(r.title)),
      h("span.gm-pick-t", h("b", r.title), h("span.gm-meta", [r.year, (r.platforms || []).slice(0, 3).join(", ")].filter(Boolean).join(" · "))))));
    } catch (e) { list.replaceChildren(); msg.className = "gm-note err"; msg.textContent = e.message; }
  };
  const s = sheet(t("Fix match"), [
    h("div.gm-note", t("Search the game as it is called in the database, then pick the right one.")),
    h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); search(); } }, input,
      h("button.primary", { type: "submit" }, icon("search"), t("Search"))),
    msg, list]);
  search();
}

// ---- discover tab -------------------------------------------------------------------------

const RAIL_ORDER = ["popular", "top_sellers", "most_played", "new_releases", "specials", "top_rated", "coming_soon"];
const RAIL_TITLE = () => ({
  popular: t("Popular right now"), top_sellers: t("Top sellers on Steam"), most_played: t("Most played now"),
  new_releases: t("New releases"), specials: t("On sale"), top_rated: t("Top rated"), coming_soon: t("Coming soon"),
});
const railTitle = (r) => r.key.startsWith("platform:") ? t("Best on {platform}", { platform: r.label || r.platform })
  : (RAIL_TITLE()[r.key] || r.key);
const railRank = (r) => {
  const i = RAIL_ORDER.indexOf(r.key.replace(/^platform:.*/, "~"));
  return r.key.startsWith("platform:") ? 50 : (i < 0 ? 99 : i) + (r.source === "igdb" && r.key === "coming_soon" ? 0.5 : 0);
};
const errTitle = (key) => key.startsWith("platform:") ? t("Best on {platform}", { platform: key.slice(9).toUpperCase() })
  : (RAIL_TITLE()[key] || key);

/** Cover art that never shows a broken-image icon: gradient + initials underneath, the portrait fades in
 *  over it, a failed portrait falls back to the landscape header (blurred backdrop), then to the gradient. */
function artImg(it, cls = "") {
  const box = h(`div.gm-cv${cls}`, { style: `--gh:${hue(it.title)}` }, h("span.gm-init", initials(it.title || "?")));
  const stages = [it.cover, it.image].filter(Boolean);
  const load = () => {
    const src = stages.shift();
    if (!src) return;
    const wide = src === it.image && it.cover !== it.image;
    const img = h("img", { src, alt: "", loading: "lazy", decoding: "async", referrerpolicy: "no-referrer" });
    img.addEventListener("load", () => { img.classList.add("in"); if (wide) box.classList.add("land"); }, { once: true });
    img.addEventListener("error", () => { img.remove(); load(); }, { once: true });
    if (wide) box.append(h("img.bg", { src, alt: "", "aria-hidden": "true", referrerpolicy: "no-referrer" }));
    box.append(img);
  };
  load();
  return box;
}

const score10 = (it) => it.rating != null ? fmt.num(it.rating / 10, 1) : "";

function metaLine(it) {
  if (it.discount && it.price) return h("div.gm-meta", h("b.gm-price", it.price), it.was ? h("s", it.was) : null);
  if (it.free) return h("div.gm-meta", h("b.gm-price", t("Free to play")), it.year ? ` · ${it.year}` : "");
  if (it.price) return h("div.gm-meta", h("b.gm-price", it.price), it.year ? ` · ${it.year}` : "");
  return h("div.gm-meta", it.year || (it.platforms || []).slice(0, 2).join(", ") || " ");
}

function dcard(it) {
  return h("button.gm-tile.gm-dtile", { onclick: () => openDiscover(it), "aria-label": it.title },
    h("div.gm-art", artImg(it),
      h("div.gm-badges",
        it.in_library ? h("span.gm-b.lib", { title: t("In library") }, icon("check")) : h("span"),
        it.discount ? h("span.gm-b.disc", `−${it.discount}%`) : null),
      it.rating != null ? h("span.gm-score", icon("star"), score10(it)) : null),
    h("div.gm-title", it.title),
    metaLine(it));
}

const skelCards = (n = 5) => Array.from({ length: n }, () =>
  h("div.gm-tile.gm-skel", h("div.gm-art"), h("div.gm-sl"), h("div.gm-sl.s")));
const skelRail = () => h("section.gm-railsec", h("h2.gm-rh.gm-skel", h("span.gm-sl.h")), h("div.gm-rail", skelCards()));

function railErr(title, msg, retry) {
  return h("section.gm-railsec",
    h("h2.gm-rh", title),
    h("div.gm-rerr", icon("wifi"), h("span", msg || t("Couldn't load this list.")),
      retry ? h("button.ghost", { onclick: retry }, t("Retry")) : null));
}

function drawDiscover() {
  if (!els.dis) return;
  const all = [...dx.rails.steam, ...dx.rails.igdb].sort((a, b) => railRank(a) - railRank(b));
  const out = [];
  for (const r of all) {
    out.push(h("section.gm-railsec", h("h2.gm-rh", railTitle(r)), h("div.gm-rail", r.items.map(dcard))));
  }
  // rails that failed: grouped when many (all of Steam down = one note, not five)
  const steamErrs = dx.errors.steam, igdbErrs = dx.errors.igdb;
  if (steamErrs.length > 1 && !dx.rails.steam.length) {
    out.push(railErr(t("Steam"), steamErrs[0].error, () => loadDiscover(true, "steam")));
  } else for (const e of steamErrs) out.push(railErr(errTitle(e.rail), e.error, () => loadDiscover(true, "steam")));
  if (igdbErrs.length > 1 && !dx.rails.igdb.length) {
    out.push(railErr(t("IGDB"), igdbErrs[0].error, () => loadDiscover(true, "igdb")));
  } else for (const e of igdbErrs) out.push(railErr(errTitle(e.rail), e.error, () => loadDiscover(true, "igdb")));
  // skeletons only where nothing is on screen yet (a refresh keeps the old rails: no jump)
  if (!dx.done.steam && !dx.rails.steam.length) out.unshift(skelRail(), skelRail());
  if (!dx.done.igdb && !dx.rails.igdb.length && (data.metadata?.igdb || dx.igdb)) out.push(skelRail());
  if (dx.done.steam && dx.done.igdb && !all.length && !steamErrs.length && !igdbErrs.length) {
    out.push(h("div.gm-card.gm-center", h("div.gm-ico", icon("search")), h("b", t("Nothing to show yet")),
      h("p.muted", t("Couldn't load the game lists. Check the internet connection and try again.")),
      h("button.primary", { onclick: () => loadDiscover(true) }, t("Retry"))));
  }
  if (dx.done.igdb && !dx.igdb && data.metadata && !data.metadata.igdb)
    out.push(h("button.gm-slim", { onclick: openSettings }, icon("settings"),
      h("span", t("Add the free IGDB keys to see more lists: popular, top rated and the best games per console.")), icon("right")));
  els.disBody.replaceChildren(...out);
}

let discoverSeq = 0;
async function loadDiscover(refresh = false, only = "") {
  const my = ++discoverSeq;
  const sets = only ? [only] : ["steam", "igdb"];
  for (const k of sets) { dx.done[k] = false; dx.errors[k] = []; }
  drawDiscover();
  await Promise.all(sets.map(async (k) => {
    try {
      const d = await P(`discover?set=${k}${refresh ? "&refresh=1" : ""}`);
      if (my !== discoverSeq) return;
      dx.rails[k] = d.rails || []; dx.errors[k] = d.errors || [];
      if (k === "igdb") dx.igdb = !!d.igdb;
    } catch (e) {
      if (my !== discoverSeq) return;
      dx.errors[k] = [{ rail: k === "steam" ? "top_sellers" : "popular", error: e.message }];
      dx.rails[k] = [];
    }
    dx.done[k] = true;
    drawDiscover();
  }));
  if (my === discoverSeq) dx.at = Date.now();
}

const ensureDiscover = () => {
  if (!dx.at || Date.now() - dx.at > 30 * 60 * 1000) loadDiscover();
};

// ---- discover: detail sheet --------------------------------------------------------------

const clean = (s) => String(s || "").replace(/[™®©]/g, "").replace(/\s+/g, " ").trim();

function trackerSearch(it, platform) {
  fx.q = clean(it.title);
  view.fplat = platform || "pc";
  saveView();
  if (els.fq) els.fq.value = fx.q;
  setTab("find");
  drawPlat();
  search(true);
}

function shotStrip(shots) {
  return h("div.gm-shots", shots.map((s) => h("button.gm-shotbtn", { onclick: () => viewShot(s.full || s.thumb), "aria-label": t("Screenshot") },
    (() => {
      const img = h("img", { src: s.thumb, loading: "lazy", alt: "" });
      img.addEventListener("error", () => img.closest(".gm-shotbtn")?.remove(), { once: true });
      return img;
    })())));
}

function trailer(tr, link) {
  if (!tr) return null;
  const v = document.createElement("video");
  if (tr.mp4 || tr.webm) {
    v.controls = true; v.preload = "none"; v.playsInline = true; v.className = "gm-video";
    if (tr.thumb) v.poster = tr.thumb;
    for (const [u, type] of [[tr.mp4, "video/mp4"], [tr.webm, "video/webm"]]) if (u) v.append(h("source", { src: u, type }));
    return h("div.gm-trailer", v);
  }
  if (tr.hls && v.canPlayType("application/vnd.apple.mpegurl")) {
    v.controls = true; v.preload = "none"; v.playsInline = true; v.className = "gm-video"; v.src = tr.hls;
    if (tr.thumb) v.poster = tr.thumb;
    return h("div.gm-trailer", v);
  }
  const href = tr.youtube || link;
  return href ? h("a.btn.gm-trailer-link", { href, target: "_blank", rel: "noopener noreferrer" }, icon("play"), t("Watch the trailer")) : null;
}

function discoverBody(it, d, ctx) {
  const plats = (d.search_platforms || []).length ? d.search_platforms : ["pc"];
  const hero = d.hero || d.image;
  const heroEl = hero ? h("img.gm-hero-img", { src: hero, alt: "", referrerpolicy: "no-referrer" })
    : h("div.gm-hero-img.none", { style: `--gh:${hue(d.title)}` });
  if (heroEl.tagName === "IMG") heroEl.addEventListener("error", () => heroEl.replaceWith(h("div.gm-hero-img.none", { style: `--gh:${hue(d.title)}` })), { once: true });
  const sub = [d.year, (d.developers || [])[0]].filter(Boolean).join(" · ");
  const platLabel = (k) => platName(k === "pc" ? "pc" : (fx.platforms.find((p) => p.key === k)?.label || k));
  const platBox = h("div.gm-genres");
  const paintPlats = () => platBox.replaceChildren(...plats.map((k) => chip(platLabel(k), null, ctx.plat === k, () => { ctx.plat = k; paintPlats(); })));
  if (plats.length > 1) paintPlats();
  const price = d.free ? t("Free to play") : d.price;
  const link = d.url ? h("a.btn.gm-ext", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, icon("link"),
    d.source === "steam" ? t("Open on Steam") : t("Open on IGDB")) : null;
  return [
    h("div.gm-hero", heroEl),
    h("div.gm-dhead", artImg(d, ".big"),
      h("div.gm-dinfo", h("div.gm-dtitle", d.title),
        sub ? h("div.gm-sub", sub) : null,
        h("div.gm-chips-in",
          d.in_library ? h("span.chip.ok", icon("check"), t("In library")) : null,
          d.coming_soon ? h("span.chip.accent", t("Coming soon")) : null,
          d.metacritic != null ? h("span.chip.gm-rate", "Metacritic", ` ${d.metacritic}`) : null,
          d.rating != null ? h("span.chip.gm-rate", icon("star"), `${score10(d)}/10`) : null))),
    (d.genres || []).length ? h("div.gm-genres", d.genres.map((x) => h("span.chip", x))) : null,
    price || d.discount ? h("div.gm-pricebox",
      d.discount ? h("span.gm-b.disc", `−${d.discount}%`) : null,
      price ? h("b", price) : null, d.was ? h("s", d.was) : null) : null,
    d.summary ? summary(d.summary) : null,
    trailer(d.trailer, d.url),
    (d.screenshots || []).length ? shotStrip(d.screenshots) : null,
    plats.length > 1 ? h("div.gm-note", t("Search the trackers for:")) : null,
    plats.length > 1 ? platBox : null,
    h("div.gm-actions",
      h("button.primary", { onclick: () => { ctx.close(); trackerSearch(d, ctx.plat); } }, icon("search"), t("Search the trackers")),
      link),
    h("div.gm-kvs",
      row(t("Released"), d.released),
      row(t("Developer"), (d.developers || []).join(", ")),
      row(t("Publisher"), (d.publishers || []).filter((p) => !(d.developers || []).includes(p)).join(", ")),
      row(t("Platforms"), d.source === "igdb" ? (it.platforms || []).join(", ") : "")),
  ].filter(Boolean);
}

function openDiscover(it) {
  const ctx = { plat: it.platform || "pc", close: () => s.close() };
  const body = h("div.stack");
  const s = sheet("", [body], { onClose: () => { ctx.gone = true; } });
  s.el.parentElement.classList.add("gm-dsheet");
  const skeleton = () => body.replaceChildren(
    h("div.gm-hero", h("div.gm-hero-img.none", { style: `--gh:${hue(it.title)}` })),
    h("div.gm-dhead", artImg(it, ".big"), h("div.gm-dinfo", h("div.gm-dtitle", it.title))),
    h("div.gm-skel", h("div.gm-sl"), h("div.gm-sl"), h("div.gm-sl.s")));
  const go = async () => {
    skeleton();
    try {
      const d = await P(`discover/item/${encodeURIComponent(it.source)}/${encodeURIComponent(it.id)}?platform=${encodeURIComponent(it.platform || "")}`);
      if (ctx.gone) return;
      ctx.plat = d.platform || ctx.plat;
      body.replaceChildren(...discoverBody(it, d, ctx));
    } catch (e) {
      if (ctx.gone) return;
      // still useful without the details: the tracker search only needs the title
      body.replaceChildren(
        h("div.gm-hero", h("div.gm-hero-img.none", { style: `--gh:${hue(it.title)}` })),
        h("div.gm-dhead", artImg(it, ".big"), h("div.gm-dinfo", h("div.gm-dtitle", it.title))),
        h("div.gm-rerr", icon("wifi"), h("span", e.message || t("Couldn't load this list.")),
          h("button.ghost", { onclick: go }, t("Retry"))),
        h("div.gm-actions", h("button.primary", { onclick: () => { s.close(); trackerSearch(it, it.platform); } },
          icon("search"), t("Search the trackers")),
        it.url ? h("a.btn.gm-ext", { href: it.url, target: "_blank", rel: "noopener noreferrer" }, icon("link"),
          it.source === "steam" ? t("Open on Steam") : t("Open on IGDB")) : null));
    }
  };
  go();
}

// ---- find tab -----------------------------------------------------------------------------

function platRow() {
  els.fplat.replaceChildren(...fx.platforms.map((p) => chip(p.label, null, view.fplat === p.key, () => {
    view.fplat = p.key; saveView(); drawPlat();
    if (fx.q.trim().length >= 2) search(true);
  })));
}
const drawPlat = platRow;

async function loadPlatforms() {
  try { fx.platforms = await P("find/platforms"); } catch { fx.platforms = []; }
  if (!Array.isArray(fx.platforms) || !fx.platforms.length) fx.platforms = [{ key: "pc", label: t("PC") }];
  if (!fx.platforms.some((p) => p.key === view.fplat)) view.fplat = fx.platforms[0].key;
  drawPlat();
}

let findSeq = 0;   // ignore answers of superseded searches (trackers can take 10 s)
async function search(fresh = true) {
  const q = fx.q.trim();
  if (q.length < 2) { fx.error = t("Type at least 2 characters"); drawResults(); return; }
  const my = ++findSeq, plat = view.fplat;
  if (fresh) { fx.page = 1; fx.results = []; } else fx.page += 1;
  fx.loading = true; fx.error = ""; fx.off = false; fx.searched = true;
  drawResults();
  try {
    const d = await P(`find?q=${encodeURIComponent(q)}&platform=${encodeURIComponent(plat)}&page=${fx.page}`);
    if (my !== findSeq) return;
    fx.off = !!d.disabled;
    fx.plat = plat;   // the platform these results were searched for: downloads go there
    fx.results = fresh ? (d.results || []) : fx.results.concat(d.results || []);
    fx.providers = d.providers || [];
    fx.more = !!d.has_more;
    fx.error = d.error || "";
  } catch (e) {
    if (my !== findSeq) return;
    fx.error = e.message;
  }
  fx.loading = false;
  drawResults();
}

function confirmDownload(r) {
  const plat = fx.plat || view.fplat;
  const dest = plat === "pc" ? "games/pc" : `games/emu/${plat}`;
  const s = sheet(t("Download"), [
    h("div.gm-dtitle", { style: { fontSize: "17px" } }, r.title),
    h("div.gm-meta", [r.size ? fmt.bytes(r.size) : "", r.seeders != null ? t("{n} seeders", { n: r.seeders }) : ""].filter(Boolean).join(" · ")),
    h("div.gm-banner.info", icon("nas"), h("span", t("Downloads to {dest} on the NAS", { dest }))),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      await P("find/download", { body: { title: r.title, platform: plat, magnet: r.magnet, url: r.url } });
      toast(t("Download started")); s.close(); await loadDownloads(); schedule();
    }, e.currentTarget) }, icon("download"), t("Download")),
  ]);
}

function resultRow(r) {
  const srcs = r.sources?.length ? r.sources : (r.source ? [r.source] : []);
  return h("div.gm-res",
    h("div.gm-res-main",
      h("div.gm-res-title", r.title),
      h("div.gm-res-meta",
        r.size ? h("span", fmt.bytes(r.size)) : null,
        h("span.gm-seed", icon("up"), fmt.num(r.seeders || 0)),
        h("span.gm-leech", icon("down"), fmt.num(r.leechers || 0)),
        srcs.slice(0, 3).map((s) => h("span.chip", s)))),
    h("button.primary.gm-dl", { onclick: () => confirmDownload(r), "aria-label": t("Download") }, icon("download"), t("Download")));
}

function drawResults() {
  const out = [];
  const bad = fx.providers.filter((p) => p.ok === false && p.error);
  if (fx.off) {
    out.push(h("div.gm-card.gm-center", h("div.gm-ico", icon("search")),
      h("b", t("Search is off")),
      h("p.muted", t("Game search uses the Torrents page. Turn on the Torrents plugin and a tracker first.")),
      h("button.primary", { onclick: () => { location.hash = "#/torrents"; } }, t("Open Torrents"))));
  } else {
    if (bad.length) out.push(h("div.gm-warns", bad.map((p) => h("div.gm-warn", icon("wifi"), h("span", `${p.name}: ${p.error}`)))));
    if (fx.error && !fx.results.length) out.push(h("div.gm-warn.err", h("span", fx.error)));
    else if (fx.error) out.push(h("div.gm-warn", h("span", fx.error)));
    if (fx.results.length) out.push(h("div.gm-resl", fx.results.map(resultRow)));
    else if (fx.searched && !fx.loading && !fx.error) out.push(h("div.empty", t("No results.")));
    if (fx.loading) out.push(h("div.empty", h("span.spin")));
    else if (fx.more) out.push(h("div.gm-moreRow", h("button", { onclick: () => search(false) }, t("Load more"))));
    if (!fx.searched && !fx.loading) out.push(h("div.gm-hintcard", icon("search"),
      h("span", t("Search a game by name. Files are saved in your games folder and appear in the library once ready."))));
  }
  els.results.replaceChildren(...out);
}

function dlState(d) {
  if (d.state === "extracting") return t("Extracting…");
  if (d.state === "done") return t("Done");
  if (d.state === "needs_extraction") return d.error || t("Needs extracting on the NAS");
  if (d.state === "error") return d.error || t("Failed");
  const bits = [d.rate ? `${fmt.bytes(d.rate)}/s` : "", d.eta ? fmt.duration(d.eta) : ""].filter(Boolean);
  return bits.join(" · ") || t("Starting…");
}

function dlRow(d) {
  const active = d.state === "downloading" || d.state === "extracting";
  const p = Math.round((d.progress || 0) * 100);
  const bad = d.state === "error";
  return h("div.gm-dl-row",
    h("div.gm-dl-main",
      h("div.gm-res-title", d.title),
      active || d.state === "done" ? h("div.progress" + (d.state === "extracting" ? ".ind" : ""), h("div", { style: { width: d.state === "extracting" ? "100%" : `${p}%` } })) : null,
      h("div.gm-note" + (bad ? ".err" : ""), active && d.state === "downloading" ? `${p}% · ${dlState(d)}` : dlState(d))),
    h("button.icon.ghost", { "aria-label": active ? t("Cancel") : t("Remove"), onclick: (e) => act(async () => {
      await P(`find/downloads/${encodeURIComponent(d.hash)}`, { method: "DELETE" });
      await loadDownloads(); schedule();
    }, e.currentTarget) }, icon(active ? "x" : "trash")));
}

function drawDownloads() {
  if (!els.dl) return;
  els.dl.replaceChildren(...(fx.downloads.length ? [
    h("h3.gm-section", t("In progress"), h("span.gm-count", fmt.num(fx.downloads.length))),
    fx.dlError ? h("div.gm-warn.sm", icon("wifi"), h("span", t("Couldn't refresh the downloads: {error}", { error: fx.dlError }))) : null,
    h("div.gm-card.gm-dls", fx.downloads.map(dlRow))].filter(Boolean)
    : fx.dlError ? [h("div.gm-warn.sm", icon("wifi"), h("span", t("Couldn't refresh the downloads: {error}", { error: fx.dlError })))] : []));
}

// ---- settings sheet -----------------------------------------------------------------------

async function openSettings() {
  ensureCss(`/static/pages/settings.css?v=${window.CASA_V}`);
  const { renderForm } = await import(`./settings.js?v=${window.CASA_V}`);
  let all;
  try { all = await api("settings"); } catch (e) { toast(e.message, true); return; }
  const p = all.plugins.find((x) => x.name === "games");
  if (!p) return;
  const schema = p.schema || [], values = p.values || {};
  const meta = (f) => /^(igdb|steamgriddb)\b/.test(f.key);
  const deck = (f) => f.key.startsWith("deck");
  const forms = [
    renderForm(schema.filter((f) => !deck(f) && !meta(f)), values),
    renderForm(schema.filter(deck), values),
    renderForm(schema.filter(meta), values),
  ];
  const status = h("div.stack", { style: { gap: "8px" } });
  const paint = (st) => {
    const d = st || data.deck || {};
    status.replaceChildren(...[
      h("div.row.wrap",
        h("span.chip" + (d.online ? ".ok" : ".bad"), d.online ? t("Online") : t("Offline")),
        d.online ? h("span.chip" + (d.steam_running ? ".accent" : ""), d.steam_running ? t("Steam running") : t("Steam closed")) : null,
        d.online ? h("span.chip" + (d.pending ? ".warn" : ""), t("{n} waiting for Steam", { n: d.pending || 0 })) : null),
      !d.online && (d.error || !d.configured) ? h("div.gm-note", d.error || t("Set the Steam Deck address and token in Settings → Games.")) : null].filter(Boolean));
  };
  paint();
  const md = data.metadata || {};
  const skipped = (data.problems || []).length ? h("details.gm-problems",
    h("summary", t("{n} items skipped", { n: data.problems.length })),
    ...data.problems.map((x) => h("div.gm-note", h("b", x.path), ": ", x.reason))) : null;
  const btn = (ico, label, fn, extra = {}) => h("button.gm-wide", { ...extra, onclick: (e) => act(() => fn(e), e.currentTarget) }, icon(ico), label);
  const sec = (title, ...kids) => h("section.gm-sec", h("h3", title), ...kids);

  const s = sheet(t("Games settings"), [
    sec(t("Library"), forms[0].el,
      h("div.gm-btns",
        btn("refresh", t("Rescan library"), async () => { await refresh("rescan", { method: "POST", body: {} }); toast(t("Library rescanned")); }))),
    sec(t("Steam Deck"), status, forms[1].el,
      h("div.gm-btns",
        btn("wifi", t("Test connection"), async () => {
          const r = await P("test", { body: {} });
          paint({ ...data.deck, online: r.ok, error: r.error, pending: r.pending, steam_running: r.steam_running });
          toast(r.ok ? t("Connected to the Steam Deck") : (r.error || t("The Steam Deck is not reachable")), !r.ok);
        }),
        btn("play", t("Apply to Steam"), async (e) => applySteam({ currentTarget: e.currentTarget }), { disabled: !data.deck?.online || !data.deck?.pending }))),
    sec(t("Metadata"),
      md.configured === false ? h("div.gm-note", t("Covers and descriptions come from IGDB. Add the free keys below to get them.")) : null,
      forms[2].el,
      h("div.gm-btns",
        btn("refresh", t("Refresh metadata"), async () => {
          await P("meta/refresh", { body: {} }); toast(t("Metadata refresh started")); await refresh();
        }))),
    h("button.primary.lg", { onclick: (e) => act(async () => {
      const values = Object.assign({}, ...forms.map((f) => f.values()));
      for (const f of schema) if (f.type === "password" && !values[f.key]) delete values[f.key];   // empty = keep
      const r = await api("settings/plugins/games", { method: "PUT", body: { values } });
      if (r.error) { toast(r.error, true); return; }
      toast(t("Saved")); s.close(); await refresh();
    }, e.currentTarget) }, t("Save")),
    skipped,
  ].filter(Boolean), { wide: true });
  s.el.parentElement.classList.add("gm-ssheet");
}

// ---- library page -------------------------------------------------------------------------

function banner() {
  const d = data.deck || {};
  const out = [];
  if (libError && data.games.length)
    out.push(h("div.gm-slim.err", icon("wifi"), h("span", t("Couldn't refresh the library: {error}", { error: libError }))));
  if (loaded && !d.configured && !d.online)
    out.push(h("button.gm-slim", { onclick: openSettings }, icon("settings"), h("span", t("Set up your Steam Deck")), icon("right")));
  else if (loaded && !d.online)
    out.push(h("div.gm-slim", { title: d.error || "" }, icon("wifi"), h("span", t("Steam Deck offline. You can still browse the library."))));
  if (d.online && d.pending) {
    out.push(h("div.gm-banner",
      h("span", t("{n} waiting to be added to Steam. This closes and reopens Steam; use Desktop Mode.", { n: d.pending })),
      h("button.primary", { onclick: applySteam }, t("Apply to Steam"))));
  }
  return out;
}

function matches(g) {
  const q = query.trim().toLowerCase();
  if (q && !(g.title.toLowerCase().includes(q) || gtitle(g).toLowerCase().includes(q))) return false;
  const f = view.filter;
  if (f === "installed") return g.state === "installed" || g.state === "update";
  if (f === "update") return g.state === "update";
  if (f.startsWith("p:")) return g.platform === f.slice(2);
  return true;
}

const cmp = {
  az: (a, b) => gtitle(a).localeCompare(gtitle(b), undefined, { sensitivity: "base", numeric: true }),
  new: (a, b) => (b.added || 0) - (a.added || 0) || cmp.az(a, b),
  size: (a, b) => (b.size || 0) - (a.size || 0) || cmp.az(a, b),
  system: (a, b) => (a.platform || "").localeCompare(b.platform || "") || cmp.az(a, b),
};

function chip(label, count, on, fn) {
  return h("button.chip.gm-fchip" + (on ? ".on" : ""), { onclick: fn, "aria-pressed": on },
    label, count != null ? h("span.gm-count", fmt.num(count)) : null);
}

function drawChips() {
  const gs = data.games, sys = data.systems || [];
  const upd = gs.filter((g) => g.state === "update").length;
  const inst = gs.filter((g) => g.state === "installed" || g.state === "update").length;
  const set = (f) => () => { view.filter = f; saveView(); drawLibrary(); };
  // a saved platform filter that no longer exists would show an empty page
  if (view.filter.startsWith("p:") && !sys.some((s) => `p:${s.key}` === view.filter)) view.filter = "all";
  if (view.filter === "update" && !upd) view.filter = "all";
  els.chips.replaceChildren(
    chip(t("All"), gs.length, view.filter === "all", set("all")),
    chip(t("Installed"), inst, view.filter === "installed", set("installed")),
    upd ? chip(t("Updates"), upd, view.filter === "update", set("update")) : null,
    ...(sys.length > 1 ? sys.map((s) => chip(platName(s.key), s.count, view.filter === `p:${s.key}`, set(`p:${s.key}`))) : []));
  els.chips.parentElement.hidden = !gs.length;
}

function openSort() {
  const s = sheet(t("Sort"), SORTS().map(([k, l]) =>
    h("button.gm-opt" + (view.sort === k ? ".on" : ""), { onclick: () => { view.sort = k; saveView(); s.close(); drawLibrary(); } },
      h("span", l), view.sort === k ? icon("check") : null)));
}

function emptyState() {
  if (!loaded) return h("div.empty", h("span.spin"));
  if (!data.games.length && libError) {
    return h("div.gm-card.gm-center", h("div.gm-ico.bad", icon("wifi")), h("b", t("The library didn't load")),
      h("p.muted.gm-wrap", libError),
      h("button.primary", { onclick: () => refresh() }, icon("refresh"), t("Retry")));
  }
  if (!data.games.length) {
    const hint = h("div.gm-how", { hidden: true },
      h("p.muted", t("In the games folder, one folder is one game. Add cover.jpg to show a cover.")),
      h("div.gm-how-row", h("code", "pc/"), h("span", t("PC games: one folder each, with the .exe, .AppImage or installer"))),
      h("div.gm-how-row", h("code", "emu/snes/"), h("span", t("ROMs, one folder per system (snes, psx, …)"))),
      data.root ? h("div.gm-how-path", icon("folder"), h("span", data.root)) : null);
    const how = h("button.ghost", { onclick: () => { hint.hidden = !hint.hidden; how.firstChild.nodeValue = hint.hidden ? t("How to add games") : t("Hide"); } },
      t("How to add games"));
    return h("div.gm-card.gm-center.gm-emptycard", h("div.gm-ico", icon("play")),
      h("b", data.exists === false ? t("No games folder yet") : t("No games yet")),
      h("p.muted", t("Find a game and it lands here, ready to send to the Steam Deck.")),
      h("button.primary.lg", { onclick: () => setTab("find") }, icon("search"), t("Find games")),
      how, hint);
  }
  return h("div.empty", t("No matches."), " ",
    h("button.ghost", { onclick: () => { query = ""; els.q.value = ""; view.filter = "all"; saveView(); drawLibrary(); } }, t("Clear filters")));
}

function drawLibrary() {
  if (!root?.isConnected) return;
  els.banner.replaceChildren(...banner());
  drawChips();
  const list = data.games.filter(matches).sort(cmp[view.sort] || cmp.az);
  els.tools.hidden = !data.games.length;
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
  redrawDetail();
}

// ---- page ---------------------------------------------------------------------------------

function drawTabs() {
  if (!els.seg) return;
  const n = activeDl().length;
  els.seg.replaceChildren(
    h("button" + (view.tab === "library" ? ".active" : ""), { onclick: () => setTab("library") }, t("Library")),
    h("button" + (view.tab === "discover" ? ".active" : ""), { onclick: () => setTab("discover") }, t("Discover")),
    h("button" + (view.tab === "find" ? ".active" : ""), { onclick: () => setTab("find") }, t("Find"),
      n ? h("span.gm-nbadge", fmt.num(n)) : null));
  els.libPane.hidden = view.tab !== "library";
  els.dis.hidden = view.tab !== "discover";
  els.findPane.hidden = view.tab !== "find";
}

function setTab(tab) {
  view.tab = tab; saveView(); drawTabs(); schedule();
  if (tab === "find" && !fx.platforms.length) loadPlatforms();
  if (tab === "discover") ensureDiscover();
}

export default {
  async render(el) {
    ensureCss(`/static/pages/games.css?v=${window.CASA_V}`);
    loaded = false;
    root = h("div.gm");
    els.banner = h("div");
    els.chips = h("div.gm-chips");
    els.q = h("input", { type: "search", placeholder: t("Search…"), value: query, autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), oninput: (e) => { query = e.target.value; drawLibrary(); } });
    els.grid = h("div");
    els.tools = h("div.gm-tools",
      h("div.gm-searchrow",
        h("div.gm-search", icon("search"), els.q),
        h("button.icon.gm-sortbtn", { "aria-label": t("Sort"), onclick: openSort }, sortIcon())),
      h("div.gm-chipwrap", els.chips));
    els.libPane = h("div", els.banner, els.tools, els.grid);

    els.fq = h("input", { type: "search", value: fx.q, placeholder: t("Search a game…"), autocomplete: "off", enterkeyhint: "search",
      "aria-label": t("Search"), oninput: (e) => { fx.q = e.target.value; } });
    els.fplat = h("div.gm-chips");
    els.results = h("div");
    els.dl = h("div");
    els.findPane = h("div",
      h("form.gm-find", { onsubmit: (e) => { e.preventDefault(); els.fq.blur(); search(true); } },
        h("div.gm-search", icon("search"), els.fq),
        h("button.primary", { type: "submit" }, t("Search"))),
      h("div.gm-chipwrap", els.fplat),
      els.dl, els.results);

    els.disBody = h("div.gm-disc");
    els.dis = h("div", els.disBody);
    els.seg = h("div.seg.gm-seg");
    root.append(
      h("div.gm-head", h("h1", t("Games")), h("div.spacer"),
        h("button.icon.ghost", { "aria-label": t("Games settings"), onclick: openSettings }, icon("settings"))),
      els.seg, els.libPane, els.dis, els.findPane);
    el.replaceChildren(root);
    drawTabs(); drawLibrary(); drawResults(); drawPlat(); drawDownloads();
    loadPlatforms();
    if (view.tab === "discover") ensureDiscover(); else drawDiscover();
    await Promise.all([loadLibrary(), loadDownloads()]);
    schedule();
    return () => { clearTimeout(timer); discoverSeq++; root = null; };
  },
};
